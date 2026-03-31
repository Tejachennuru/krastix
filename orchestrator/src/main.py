import asyncio
import json
import logging
import os
import re
import uuid
import base64
from email.message import EmailMessage
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi import Header
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Any
from contextlib import asynccontextmanager

import jwt
from passlib.context import CryptContext
import httpx
from langchain_ollama import ChatOllama

from shared.database import db
from shared.mq import celery_app
from shared.integrations_crypto import encrypt_secret, decrypt_secret
from orchestrator.src.graph import OrchestratorGraph
from orchestrator.src.services.memory import MemoryService

logger = logging.getLogger(__name__)

# --- Auth Config ---
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    logger.warning(
        "JWT_SECRET is not set. Using an insecure default — set this env var before deploying."
    )
    JWT_SECRET = "krastix-insecure-default-secret-CHANGE-ME"
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 7

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "http://localhost:8000/api/v1/integrations/google/oauth/callback",
)
GOOGLE_OAUTH_SCOPES = [
    "https://mail.google.com/",
    "openid",
    "email",
    "profile",
]
KEEPALIVE_TOKEN = os.getenv("KEEPALIVE_TOKEN", "").strip()
GROK_API_KEY = os.getenv("GROK_API_KEY", "").strip()
GROK_BASE_URL = os.getenv("GROK_BASE_URL", "https://api.x.ai/v1").rstrip("/")
GROK_MODEL = os.getenv("GROK_MODEL", "grok-2-latest")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

import bcrypt

def _hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))

def _create_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

# Internal Services
brain: Optional[OrchestratorGraph] = None
memory_service: Optional[MemoryService] = None
_task_watcher_handle = None

# --- Models ---
class ChatRequest(BaseModel):
    user_id: str
    domain: str = "HR_RECRUITER"
    message: str
    session_id: str 

class TaskCallback(BaseModel):
    task_id: str
    status: str
    result: Any
    error: Optional[str] = None

class BatchTrigger(BaseModel):
    user_id: str
    batch_ids: List[str]


class EmailSummarizeRequest(BaseModel):
    user_id: str
    subject: Optional[str] = ""
    sender: Optional[str] = ""
    body: str
    snippet: Optional[str] = ""


class EmailReplyDraftRequest(BaseModel):
    user_id: str
    subject: Optional[str] = ""
    sender: Optional[str] = ""
    body: str
    thread_id: Optional[str] = ""
    message_id_header: Optional[str] = ""


class EmailReplySendRequest(BaseModel):
    user_id: str
    to: str
    subject: str
    body: str
    thread_id: Optional[str] = ""
    in_reply_to: Optional[str] = ""
    references: Optional[str] = ""


def _build_task_history_message(task_id: str, status: str, result: Any, error: Optional[str]) -> str:
    """
    Build a deterministic assistant message to persist task outcomes in chat history.
    This guarantees critical artifacts (like form URLs) survive page reloads.
    """
    if status != "success":
        return f"SYSTEM NOTIFICATION: Task {task_id} failed. Error: {error or 'Unknown error'}"

    if isinstance(result, dict):
        action = (result.get("action") or "").strip().lower()
        payload_data = result.get("data") if isinstance(result.get("data"), dict) else {}

        if action == "list_form_responses":
            form_url = payload_data.get("form_url") or payload_data.get("form_id") or "(unknown form)"
            applicants = payload_data.get("applicants") if isinstance(payload_data.get("applicants"), list) else []
            count = payload_data.get("applicants_count")
            if not isinstance(count, int):
                count = len(applicants)

            lines = [
                "✅ Agent Task Complete!",
                "",
                f"Retrieved {count} submission(s) for {form_url}.",
            ]

            # Add a compact preview so the user immediately sees response content.
            previews = []
            for item in applicants[:3]:
                if not isinstance(item, dict):
                    continue
                responses = item.get("responses") if isinstance(item.get("responses"), list) else []
                answers = []
                for resp in responses:
                    if not isinstance(resp, dict):
                        continue
                    ans = resp.get("answer")
                    if ans is None:
                        continue
                    answers.append(str(ans).strip())
                if answers:
                    previews.append(" | ".join(answers[:6]))

            if previews:
                lines.append("")
                lines.append("Preview:")
                for idx, preview in enumerate(previews, start=1):
                    lines.append(f"{idx}. {preview}")

            return "\n".join(lines)

        if action == "send_email":
            to_items = payload_data.get("to") if isinstance(payload_data.get("to"), list) else []
            subject = payload_data.get("subject") or "(no subject)"
            message_id = payload_data.get("gmail_message_id") or "(unknown id)"
            return (
                "✅ Agent Task Complete!\n\n"
                f"Email sent successfully to {', '.join(to_items) if to_items else '(recipient unknown)'}.\n"
                f"Subject: {subject}\n"
                f"Gmail Message ID: {message_id}"
            )

        form_url = payload_data.get("form_url")
        if form_url:
            form_title = payload_data.get("form_title") or "Application"
            edit_url = payload_data.get("edit_url")
            lines = [
                "✅ Agent Task Complete!",
                "",
                f'The form "{form_title}" has been successfully generated for you.',
                "",
                f"Public Link: {form_url}",
            ]
            if edit_url:
                lines.append(f"Edit Mode: {edit_url}")
            return "\n".join(lines)

    return f"SYSTEM NOTIFICATION: Task {task_id} is complete. Summary of result: {str(result)[:500]}..."


def _normalize_email_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    out = []
    seen = set()
    for item in values:
        text = str(item).strip().lower()
        if not text or "@" not in text:
            continue
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _parse_json_object(raw_text: str) -> dict:
    if not raw_text:
        return {}
    text = raw_text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
    return {}


def _extract_all_emails(text: str) -> List[str]:
    found = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")
    deduped = []
    seen = set()
    for email in found:
        k = email.lower()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(k)
    return deduped


def _is_email_intent(text: str) -> bool:
    t = (text or "").lower()
    has_keyword = any(k in t for k in ["email", "mail", "gmail", "send", "draft", "cc", "bcc"])
    has_recipient = "@" in t
    return has_keyword and has_recipient


def _review_action(text: str) -> str:
    t = (text or "").strip().lower()
    if re.search(r"\b(reject|cancel|discard|don'?t send|do not send|abort)\b", t):
        return "reject"
    if re.search(r"\b(accept|approve|confirm|send now|looks good|go ahead|yes send|send it)\b", t):
        return "accept"
    return "modify"


def _format_draft_message(draft: dict) -> str:
    to_items = ", ".join(draft.get("to", [])) or "(missing)"
    cc_items = ", ".join(draft.get("cc", [])) or "(none)"
    bcc_items = ", ".join(draft.get("bcc", [])) or "(none)"
    subject = draft.get("subject") or "(no subject)"
    body = draft.get("body") or ""
    return (
        "Draft ready for your review:\n\n"
        f"To: {to_items}\n"
        f"CC: {cc_items}\n"
        f"BCC: {bcc_items}\n"
        f"Subject: {subject}\n\n"
        "Body:\n"
        f"{body}\n\n"
        "Reply with one option: accept, modify <changes>, or reject."
    )


def _build_draft_model() -> ChatOllama:
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://100.115.107.20:11434").rstrip("/")
    model_name = os.getenv("OLLAMA_MODEL", "qwen2.5:14b-instruct-q5_K_M")
    return ChatOllama(model=model_name, base_url=ollama_base_url, temperature=0, timeout=120.0)


async def _llm_generate_email_draft(message: str, sender_email: str) -> dict:
    model = _build_draft_model()
    prompt = (
        "You are an email drafting assistant. Return strict JSON with keys: "
        "to (array), cc (array), bcc (array), subject (string), body (string). "
        "Do not include markdown. Infer recipients from prompt. "
        "If cc/bcc are explicitly requested, include them. "
        f"Sender email is {sender_email}.\n\n"
        f"User instruction:\n{message}"
    )
    response = await model.ainvoke(prompt)
    parsed = _parse_json_object(response.content if hasattr(response, "content") else str(response))

    recipients = _extract_all_emails(message)
    to_items = _normalize_email_list(parsed.get("to", [])) or recipients[:1]
    cc_items = _normalize_email_list(parsed.get("cc", []))
    bcc_items = _normalize_email_list(parsed.get("bcc", []))

    # If the prompt says CC/BCC explicitly and model omitted them, heuristically fill from extra emails.
    remaining = [e for e in recipients if e not in to_items and e not in cc_items and e not in bcc_items]
    lower_msg = (message or "").lower()
    if "cc" in lower_msg and not cc_items and remaining:
        cc_items.append(remaining.pop(0))
    if "bcc" in lower_msg and not bcc_items and remaining:
        bcc_items.append(remaining.pop(0))

    subject = str(parsed.get("subject") or "Regarding your request").strip()
    body = str(parsed.get("body") or "").strip()

    return {
        "to": to_items,
        "cc": cc_items,
        "bcc": bcc_items,
        "subject": subject,
        "body": body,
    }


async def _llm_modify_email_draft(existing: dict, instruction: str, sender_email: str) -> dict:
    model = _build_draft_model()
    prompt = (
        "You are editing an existing email draft. Return strict JSON with keys: "
        "to (array), cc (array), bcc (array), subject (string), body (string). "
        "Apply user modification request precisely. Do not return markdown.\n\n"
        f"Sender email: {sender_email}\n"
        f"Current draft JSON: {json.dumps(existing)}\n"
        f"Modification request: {instruction}"
    )
    response = await model.ainvoke(prompt)
    parsed = _parse_json_object(response.content if hasattr(response, "content") else str(response))
    merged = {
        "to": _normalize_email_list(parsed.get("to", existing.get("to", []))),
        "cc": _normalize_email_list(parsed.get("cc", existing.get("cc", []))),
        "bcc": _normalize_email_list(parsed.get("bcc", existing.get("bcc", []))),
        "subject": str(parsed.get("subject", existing.get("subject", ""))).strip(),
        "body": str(parsed.get("body", existing.get("body", ""))).strip(),
    }
    if not merged["to"]:
        merged["to"] = existing.get("to", [])
    return merged


async def _get_google_integration(user_id: str) -> Optional[dict]:
    row = await db.get_integration(uuid.UUID(user_id), "google")
    if not row:
        return None
    token = decrypt_secret(row.get("access_token"))
    if not token:
        return None
    return row


async def _refresh_google_access_token(user_id: uuid.UUID, refresh_token: str) -> dict:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise RuntimeError("Google OAuth client credentials are not configured")

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Google token refresh failed: {resp.text[:300]}")
        payload = resp.json()

    access_token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not access_token:
        raise RuntimeError("Google token refresh returned no access_token")

    expires_at = None
    if isinstance(expires_in, int):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    await db.upsert_integration_tokens(
        user_id=user_id,
        provider="google",
        access_token=encrypt_secret(access_token),
        refresh_token=encrypt_secret(refresh_token),
        expires_at=expires_at,
    )

    return {
        "access_token": access_token,
        "expires_at": expires_at,
    }


async def _get_fresh_google_access_token(user_id: str) -> str:
    user_uuid = uuid.UUID(user_id)
    row = await db.get_integration(user_uuid, "google")
    if not row:
        raise RuntimeError("Google integration is missing")

    access_token = None
    if row.get("access_token"):
        try:
            access_token = decrypt_secret(row.get("access_token"))
        except Exception:
            logger.warning("Google access token could not be decrypted for user %s", user_id)

    refresh_token = None
    if row.get("refresh_token"):
        try:
            refresh_token = decrypt_secret(row.get("refresh_token"))
        except Exception:
            # Keep serving requests with current access token; require reconnect only on actual refresh need.
            logger.warning("Google refresh token could not be decrypted for user %s", user_id)
    expires_at = row.get("expires_at")

    if not access_token:
        if refresh_token:
            refreshed = await _refresh_google_access_token(user_uuid, refresh_token)
            return refreshed["access_token"]
        raise RuntimeError(
            "Google credentials cannot be decrypted with the current encryption key. Reconnect Google from App Integrations."
        )

    if isinstance(expires_at, datetime):
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=60)
        normalized_expires = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        if normalized_expires <= cutoff:
            if not refresh_token:
                raise RuntimeError("Google token expired and no refresh token is available")
            refreshed = await _refresh_google_access_token(user_uuid, refresh_token)
            access_token = refreshed["access_token"]

    return access_token


def _decode_gmail_base64(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_gmail_text(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""

    mime_type = str(payload.get("mimeType") or "").lower()
    body_data = ((payload.get("body") or {}).get("data")) if isinstance(payload.get("body"), dict) else None

    if mime_type == "text/plain" and body_data:
        return _decode_gmail_base64(body_data).strip()

    parts = payload.get("parts")
    if isinstance(parts, list):
        plain_parts = []
        html_parts = []
        for part in parts:
            chunk = _extract_gmail_text(part)
            if not chunk:
                continue
            p_mime = str((part or {}).get("mimeType") or "").lower()
            if p_mime == "text/plain":
                plain_parts.append(chunk)
            else:
                html_parts.append(chunk)

        if plain_parts:
            return "\n\n".join(plain_parts).strip()
        if html_parts:
            html_joined = "\n\n".join(html_parts)
            stripped = re.sub(r"<[^>]+>", " ", html_joined)
            stripped = re.sub(r"\s+", " ", stripped)
            return stripped.strip()

    if body_data:
        decoded = _decode_gmail_base64(body_data)
        if "<" in decoded and ">" in decoded:
            decoded = re.sub(r"<[^>]+>", " ", decoded)
            decoded = re.sub(r"\s+", " ", decoded)
        return decoded.strip()

    return ""


async def _summarize_email_with_grok(subject: str, sender: str, body: str, snippet: str) -> str:
    """Summarize full email content with Grok. Summary can be short but must not exceed 600 chars."""

    def _trim_summary(text: str) -> str:
        clean = re.sub(r"\s+", " ", (text or "")).strip()
        if not clean:
            return "Summary unavailable."
        if len(clean) <= 600:
            return clean
        cut = clean[:600]
        # Prefer cutting at sentence boundary if available in the tail.
        tail_break = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        if tail_break >= 240:
            cut = cut[: tail_break + 1]
        return cut.strip()

    full_source = (body or snippet or "").strip()
    fallback = "Summary unavailable."
    if not full_source:
        return fallback

    async def _summarize_with_ollama() -> str:
        try:
            model = _build_draft_model()
            prompt = (
                "Summarize the following email in plain text. "
                "Keep it concise, do not copy lines verbatim, mention key intent and required action if any. "
                "Maximum 600 characters.\n\n"
                f"Subject: {subject or '(no subject)'}\n"
                f"From: {sender or 'unknown sender'}\n"
                f"Email body:\n{full_source[:24000]}"
            )
            response = await model.ainvoke(prompt)
            content_text = response.content if hasattr(response, "content") else str(response)
            if content_text:
                return _trim_summary(str(content_text))
        except Exception as exc:
            logger.warning("Ollama summarize exception: %s", exc)
        return fallback

    if not GROK_API_KEY:
        return await _summarize_with_ollama()

    content = full_source[:24000]
    if not content:
        return fallback

    system_prompt = (
        "You summarize full inbox emails. Return plain text summary, maximum 600 characters, "
        "shorter is allowed when appropriate. Capture core intent, key facts, and required action. "
        "Do not copy large portions verbatim. Do not use markdown, bullets, greetings, or signatures."
    )
    user_prompt = (
        f"Subject: {subject or '(no subject)'}\n"
        f"From: {sender or 'unknown sender'}\n"
        f"Email body:\n{content}"
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{GROK_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROK_MODEL,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            if resp.status_code != 200:
                logger.warning("Grok summarize failed: %s", resp.text[:300])
                return await _summarize_with_ollama()

            payload = resp.json()
            choices = payload.get("choices") if isinstance(payload, dict) else []
            if isinstance(choices, list) and choices:
                msg = choices[0].get("message") if isinstance(choices[0], dict) else {}
                summary = str((msg or {}).get("content") or "").strip()
                if summary:
                    return _trim_summary(summary)
    except Exception as exc:
        logger.warning("Grok summarize exception: %s", exc)
        return await _summarize_with_ollama()

    return await _summarize_with_ollama()


async def _summarize_email_with_grok_strict(subject: str, sender: str, body: str, snippet: str) -> str:
    """Summarize with Grok only (no snippet/ollama fallback)."""
    content = (body or snippet or "").strip()
    if not content:
        raise RuntimeError("Email body is empty")
    if not GROK_API_KEY:
        raise RuntimeError("GROK_API_KEY is not configured")

    def _trim_summary(text: str) -> str:
        clean = re.sub(r"\s+", " ", (text or "")).strip()
        if not clean:
            return "Summary unavailable."
        if len(clean) <= 600:
            return clean
        cut = clean[:600]
        tail_break = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        if tail_break >= 220:
            cut = cut[: tail_break + 1]
        return cut.strip()

    system_prompt = (
        "You summarize full inbox emails. Return plain text summary, maximum 600 characters, "
        "shorter is allowed when appropriate. Capture core intent, key facts, and required action. "
        "Do not copy large portions verbatim. Do not use markdown or bullets."
    )
    user_prompt = (
        f"Subject: {subject or '(no subject)'}\n"
        f"From: {sender or 'unknown sender'}\n"
        f"Email body:\n{content[:24000]}"
    )

    using_groq = GROK_API_KEY.lower().startswith("gsk_")
    api_base = GROQ_BASE_URL if using_groq else GROK_BASE_URL

    headers = {
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json",
    }

    async def _try_model(client: httpx.AsyncClient, model_name: str) -> tuple[Optional[str], str]:
        resp = await client.post(
            f"{api_base}/chat/completions",
            headers=headers,
            json={
                "model": model_name,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        if resp.status_code != 200:
            return None, resp.text[:300]

        payload = resp.json()
        choices = payload.get("choices") if isinstance(payload, dict) else []
        if not isinstance(choices, list) or not choices:
            return None, "no choices"

        msg = choices[0].get("message") if isinstance(choices[0], dict) else {}
        summary = str((msg or {}).get("content") or "").strip()
        if not summary:
            return None, "empty summary"
        return _trim_summary(summary), ""

    async with httpx.AsyncClient(timeout=25.0) as client:
        tried = []
        if using_groq:
            candidates = [
                GROQ_MODEL,
                "llama-3.3-70b-versatile",
                "qwen/qwen3-32b",
                "llama-3.1-8b-instant",
            ]
        else:
            candidates = [
                GROK_MODEL,
                "grok-3-mini",
                "grok-3-mini-beta",
                "grok-3-beta",
                "grok-2-1212",
                "grok-beta",
            ]

        # Initial attempt with configured/default model.
        for model_name in candidates:
            if model_name in tried:
                continue
            tried.append(model_name)
            summary, err = await _try_model(client, model_name)
            if summary:
                return summary

            # If model name is invalid, discover available Grok models and retry.
            if "model not found" in err.lower() or "invalid argument" in err.lower():
                try:
                    models_resp = await client.get(f"{api_base}/models", headers=headers)
                    if models_resp.status_code == 200:
                        models_payload = models_resp.json()
                        items = models_payload.get("data") if isinstance(models_payload, dict) else []
                        discovered = []
                        if isinstance(items, list):
                            for item in items:
                                if not isinstance(item, dict):
                                    continue
                                model_id = str(item.get("id") or "").strip()
                                if not model_id or model_id in tried:
                                    continue
                                if using_groq:
                                    if model_id not in discovered:
                                        discovered.append(model_id)
                                elif "grok" in model_id.lower() and model_id not in discovered:
                                    discovered.append(model_id)

                        for model_id in discovered:
                            tried.append(model_id)
                            summary2, err2 = await _try_model(client, model_id)
                            if summary2:
                                return summary2
                            err = err2
                except Exception as discover_exc:
                    logger.warning("Failed to discover Grok models: %s", discover_exc)

            # Continue trying known fallback candidates first.
            continue

        raise RuntimeError(f"Grok summarize failed: {err}")

    raise RuntimeError("Grok summarize failed: unknown error")


def _extract_email_address(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    angle = re.search(r"<([^>]+)>", text)
    if angle:
        candidate = angle.group(1).strip().lower()
        if "@" in candidate:
            return candidate

    direct = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if direct:
        return direct.group(0).lower()
    return ""


def _reply_subject(subject: str) -> str:
    s = str(subject or "").strip() or "(no subject)"
    return s if s.lower().startswith("re:") else f"Re: {s}"


async def _generate_reply_draft_with_groq_qwen(subject: str, sender: str, body: str) -> dict:
    """Fallback reply draft using Groq-compatible API when Ollama/Qwen is unreachable."""
    if not GROK_API_KEY:
        raise RuntimeError("GROK_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json",
    }
    system_prompt = (
        "You are an email assistant. Generate a professional reply email draft. "
        "Return strict JSON with keys: subject (string), body (string)."
    )
    user_prompt = (
        f"Original Subject: {subject or '(no subject)'}\n"
        f"Original Sender: {sender or 'unknown sender'}\n"
        f"Original Email Body:\n{(body or '').strip()[:24000]}"
    )

    candidates = [
        "qwen/qwen3-32b",
        GROQ_MODEL,
        "llama-3.3-70b-versatile",
    ]

    last_err = ""
    async with httpx.AsyncClient(timeout=25.0) as client:
        for model_name in candidates:
            if not model_name:
                continue
            resp = await client.post(
                f"{GROQ_BASE_URL}/chat/completions",
                headers=headers,
                json={
                    "model": model_name,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            if resp.status_code != 200:
                last_err = resp.text[:300]
                continue

            payload = resp.json()
            choices = payload.get("choices") if isinstance(payload, dict) else []
            if not isinstance(choices, list) or not choices:
                last_err = "no choices"
                continue

            msg = choices[0].get("message") if isinstance(choices[0], dict) else {}
            content_text = str((msg or {}).get("content") or "").strip()
            parsed = _parse_json_object(content_text)
            draft_subject = str(parsed.get("subject") or _reply_subject(subject)).strip()
            draft_body = str(parsed.get("body") or "").strip()
            if not draft_body:
                draft_body = (
                    "Thanks for your email. I reviewed your message and will share the requested details shortly."
                )
            return {
                "subject": draft_subject,
                "body": draft_body,
                "provider_model": model_name,
            }

    raise RuntimeError(f"Groq fallback reply generation failed: {last_err or 'unknown error'}")


async def _generate_reply_draft_with_qwen(subject: str, sender: str, body: str) -> dict:
    model = _build_draft_model()
    prompt = (
        "You are an email assistant. Generate a professional reply email draft. "
        "Return strict JSON with keys: subject (string), body (string). "
        "Keep tone concise and helpful.\n\n"
        f"Original Subject: {subject or '(no subject)'}\n"
        f"Original Sender: {sender or 'unknown sender'}\n"
        f"Original Email Body:\n{(body or '').strip()[:24000]}"
    )
    response = await model.ainvoke(prompt)
    parsed = _parse_json_object(response.content if hasattr(response, "content") else str(response))

    draft_subject = str(parsed.get("subject") or _reply_subject(subject)).strip()
    draft_body = str(parsed.get("body") or "").strip()
    if not draft_body:
        draft_body = (
            "Thanks for your email. I have reviewed your message and will get back with the requested details shortly."
        )

    return {
        "subject": draft_subject,
        "body": draft_body,
    }


async def _send_gmail_reply(
    access_token: str,
    to_email: str,
    subject: str,
    body: str,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> dict:
    msg = EmailMessage()
    msg["To"] = to_email
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    elif in_reply_to:
        msg["References"] = in_reply_to
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"Gmail send failed: {resp.text[:300]}")
        sent = resp.json()

    return {
        "gmail_message_id": sent.get("id"),
        "gmail_thread_id": sent.get("threadId"),
        "to": to_email,
        "subject": subject,
    }


async def _handle_email_draft_flow(user_id: str, domain: str, message: str, session_id: str) -> Optional[dict]:
    if domain not in {"HR_RECRUITER", "PERSONAL_ASSISTANT"}:
        return None

    user_uuid = uuid.UUID(user_id)
    pending = await db.get_pending_email_draft(user_uuid, session_id)

    if pending:
        action = _review_action(message)
        draft_id = pending["id"]
        draft_payload = pending.get("draft_payload") or {}
        profile = await db.get_user_profile(user_uuid)
        sender_email = (profile or {}).get("email", "")

        if action == "reject":
            await db.update_email_draft(draft_id, "rejected")
            return {"response": "Draft rejected. No email was sent.", "task_id": None}

        if action == "accept":
            integration = await _get_google_integration(user_id)
            if not integration:
                return {
                    "response": "Google sign-in is required before sending email. Open App Integrations and connect Google.",
                    "task_id": None,
                }

            task_id = await db.create_task(
                user_id=user_uuid,
                domain_key=domain,
                agent_queue="communication_queue",
                input_payload={
                    "instruction": "Send approved email draft",
                    "task_action": "send_email",
                    "parameters": draft_payload,
                    "priority": 1,
                    "session_id": session_id,
                },
            )
            celery_app.send_task(
                "agents.communication_worker.execute_task",
                args=[str(task_id)],
                queue="communication_queue",
            )
            await db.update_email_draft(draft_id, "approved")
            return {
                "response": "Approved. I delegated this to the communication agent and it will send from your connected Google account.",
                "task_id": str(task_id),
            }

        updated = await _llm_modify_email_draft(draft_payload, message, sender_email)
        await db.update_email_draft(draft_id, "pending_approval", updated)
        return {"response": _format_draft_message(updated), "task_id": None}

    if not _is_email_intent(message):
        return None

    integration = await _get_google_integration(user_id)
    if not integration:
        return {
            "response": "To draft and send email, connect Google first from App Integrations. After sign-in, send your instruction again.",
            "task_id": None,
        }

    profile = await db.get_user_profile(user_uuid)
    sender_email = (profile or {}).get("email", "")
    draft = await _llm_generate_email_draft(message, sender_email)

    if not draft.get("to"):
        return {
            "response": "I could not find a recipient email address. Please include at least one recipient in your prompt.",
            "task_id": None,
        }

    await db.create_email_draft(
        user_id=user_uuid,
        domain_key=domain,
        session_id=session_id,
        draft_payload=draft,
    )
    return {"response": _format_draft_message(draft), "task_id": None}


# --- Task Watcher (safety net for fire-and-forget) ---
async def task_watcher_loop(interval_seconds: int = 60, stale_minutes: int = 10):
    """
    Background coroutine that periodically checks for stale tasks.
    
    Any task stuck in 'pending' or 'processing' for > stale_minutes
    gets flagged and the user's session is notified. This is the safety
    net for the callback-based pattern — if an agent crashes or the
    callback fails, this ensures no task is silently lost.
    """
    logger.info(
        "Task watcher started (interval=%ds, stale_threshold=%dm)",
        interval_seconds, stale_minutes,
    )
    while True:
        try:
            await asyncio.sleep(interval_seconds)

            if not db.pool or db.pool._closed:
                continue

            stale_tasks = await db.get_stale_tasks(stale_minutes)
            if not stale_tasks:
                continue

            logger.warning("Task watcher found %d stale tasks", len(stale_tasks))

            for task in stale_tasks:
                task_id = task["task_id"]
                input_payload = task.get("input_payload") or {}
                if isinstance(input_payload, str):
                    try:
                        input_payload = json.loads(input_payload)
                    except json.JSONDecodeError:
                        input_payload = {}

                session_id = input_payload.get("session_id")
                user_id = task.get("user_id")
                domain = task.get("domain_key")

                # Mark as stale to prevent re-processing
                await db.mark_task_stale(task_id)

                # Notify user's session
                if session_id and brain and user_id:
                    try:
                        stale_msg = (
                            f"SYSTEM NOTIFICATION: Task {task_id} assigned to "
                            f"{task.get('agent_queue', 'unknown')} appears to have "
                            f"stalled (status: {task['status']}, created: "
                            f"{task['created_at']}). It has been marked as stale. "
                            f"You may want to retry the request."
                        )
                        await brain.process_message(
                            user_id=str(user_id),
                            domain=domain or "HR_RECRUITER",
                            message=stale_msg,
                            thread_id=session_id,
                            role="system",
                        )
                        logger.info("Notified session %s about stale task %s", session_id, task_id)
                    except Exception as e:
                        logger.warning("Failed to notify about stale task %s: %s", task_id, e)

        except asyncio.CancelledError:
            logger.info("Task watcher stopped")
            break
        except Exception as e:
            logger.error("Task watcher error: %s", e, exc_info=True)


# --- Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global brain, memory_service, _task_watcher_handle
    logger.info("Orchestrator starting...")
    
    await db.connect()
    
    if db.pool:
        # 1. Init Memory Service
        memory_service = MemoryService(db.pool)
        logger.info("Memory service connected")
        
        # 2. Init Brain with Memory + DB Pool for PostgresSaver
        brain = OrchestratorGraph(memory_service=memory_service, db_pool=db.pool)
        await brain.initialize()  # Async init for PostgresSaver
        logger.info("Graph brain loaded with persistent checkpoints")

        # 3. Start Task Watcher (safety net)
        _task_watcher_handle = asyncio.create_task(task_watcher_loop())
    
    yield

    logger.info("Orchestrator shutting down...")
    if _task_watcher_handle:
        _task_watcher_handle.cancel()
        try:
            await _task_watcher_handle
        except asyncio.CancelledError:
            pass
    await db.disconnect()

app = FastAPI(title="Krastix Orchestrator", lifespan=lifespan)

# CORS for Frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Global Exception Handler ---
from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    error_details = traceback.format_exc()
    logger.error("Unhandled server error: %s", error_details)
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "details": str(exc)}
    )

# --- Health Check ---
@app.get("/health")
async def health_check():
    """Verifies API, Database, and Broker connectivity."""
    health_status = {
        "status": "online",
        "database": "unknown",
        "broker": "unknown",
        "brain": "unknown"
    }
    
    # Database
    if db.pool and not db.pool._closed:
        health_status["database"] = "connected"
    else:
        health_status["database"] = "disconnected"
        health_status["status"] = "degraded"

    # Celery Broker
    try:
        with celery_app.connection_or_acquire() as conn:
            conn.ensure_connection(max_retries=1)
            health_status["broker"] = "connected"
    except Exception:
        health_status["broker"] = "disconnected"
        health_status["status"] = "degraded"

    # Brain
    if brain and brain.workflow:
        health_status["brain"] = "ready"
    else:
        health_status["brain"] = "not_initialized"
        health_status["status"] = "degraded"

    return health_status


@app.get("/health/keepalive")
async def keepalive_ping(x_keepalive_token: Optional[str] = Header(default=None)):
    """
    Lightweight endpoint for external cron pings to keep DB path warm.
    If KEEPALIVE_TOKEN is configured, caller must send it in X-Keepalive-Token.
    """
    if KEEPALIVE_TOKEN:
        if not x_keepalive_token or x_keepalive_token != KEEPALIVE_TOKEN:
            raise HTTPException(status_code=401, detail="Unauthorized keepalive token")

    ok = await db.ping()
    if not ok:
        raise HTTPException(status_code=503, detail="Database ping failed")

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# --- Auth Models ---
class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: Optional[str] = None

class LoginRequest(BaseModel):
    email: str
    password: str

# --- Auth Endpoints ---
@app.post("/auth/register")
async def register(req: RegisterRequest):
    """Register a new user account."""
    if not db.pool:
        raise HTTPException(503, "Database not available")
    async with db.pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM profiles WHERE email = $1", req.email
        )
        if existing:
            raise HTTPException(400, "Email already registered")
        hashed = await run_in_threadpool(_hash_password, req.password)
        user_id = await conn.fetchval(
            "INSERT INTO profiles (email, full_name, password_hash) VALUES ($1, $2, $3) RETURNING id",
            req.email, req.full_name, hashed
        )
    token = _create_token(str(user_id), req.email)
    return {"token": token, "user_id": str(user_id), "email": req.email, "full_name": req.full_name}

@app.post("/auth/login")
async def login(req: LoginRequest):
    """Authenticate and return a JWT token."""
    if not db.pool:
        raise HTTPException(503, "Database not available")
    async with db.pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, full_name, password_hash FROM profiles WHERE email = $1",
            req.email
        )
    if not user or not user["password_hash"]:
        raise HTTPException(401, "Invalid email or password")
    if not await run_in_threadpool(_verify_password, req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    token = _create_token(str(user["id"]), user["email"])
    return {
        "token": token,
        "user_id": str(user["id"]),
        "email": user["email"],
        "full_name": user["full_name"],
    }

# --- Integrations Management ---
class IntegrationRequest(BaseModel):
    user_id: str
    provider: str
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None

@app.post("/api/v1/integrations")
async def save_integration(req: IntegrationRequest):
    """Save an access token securely for a third-party app (Tally, Jotform)"""
    if not db.pool:
        raise HTTPException(503, "Database not available")
    encrypted_access = encrypt_secret(req.access_token)
    encrypted_refresh = encrypt_secret(req.refresh_token) if req.refresh_token else None
    await db.upsert_integration_tokens(
        user_id=uuid.UUID(req.user_id),
        provider=req.provider.lower(),
        access_token=encrypted_access,
        refresh_token=encrypted_refresh,
        expires_at=req.expires_at,
    )
    return {"status": "success", "provider": req.provider.lower()}

@app.get("/api/v1/integrations/{user_id}")
async def list_integrations(user_id: str):
    """List connected integrations for the user"""
    if not db.pool:
        return []
    import uuid
    try:
        async with db.pool.acquire() as conn:
            rows = await conn.fetch("SELECT provider, created_at FROM integrations WHERE user_id = $1", uuid.UUID(user_id))
        return [{"provider": r["provider"], "connected_at": r["created_at"]} for r in rows]
    except Exception as e:
        logger.error(f"Error listing integrations: {e}")
        return []


@app.delete("/api/v1/integrations/{user_id}/{provider}")
async def delete_integration(user_id: str, provider: str):
    """Disconnect/remove a provider integration for a user."""
    if not db.pool:
        raise HTTPException(503, "Database not available")

    try:
        async with db.pool.acquire() as conn:
            deleted = await conn.fetchval(
                """
                DELETE FROM integrations
                WHERE user_id = $1 AND provider = $2
                RETURNING id
                """,
                uuid.UUID(user_id),
                provider.lower(),
            )

        return {
            "status": "success",
            "provider": provider.lower(),
            "deleted": bool(deleted),
        }
    except Exception as e:
        logger.error("Error deleting integration %s for %s: %s", provider, user_id, e)
        raise HTTPException(500, "Failed to delete integration")


@app.get("/api/v1/integrations/google/oauth/start")
async def google_oauth_start(user_id: str):
    """Start Google OAuth 2.0 authorization code flow."""
    missing = []
    if not GOOGLE_CLIENT_ID:
        missing.append("GOOGLE_CLIENT_ID")
    if not GOOGLE_CLIENT_SECRET:
        missing.append("GOOGLE_CLIENT_SECRET")
    if not GOOGLE_REDIRECT_URI:
        missing.append("GOOGLE_REDIRECT_URI")

    if missing:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Google OAuth is not configured",
                "missing": missing,
            },
        )

    state_token = jwt.encode(
        {
            "sub": user_id,
            "purpose": "google_oauth",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_OAUTH_SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state_token,
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return {"auth_url": auth_url}


@app.get("/api/v1/integrations/google/oauth/callback", response_class=HTMLResponse)
async def google_oauth_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    """Handle Google OAuth callback and persist encrypted tokens."""
    if error:
        html = (
            "<html><body><script>"
            "window.opener && window.opener.postMessage({type:'google-oauth-complete', success:false, error:'" + error + "'}, '*');"
            "window.close();"
            "</script>Authorization failed. You can close this window.</body></html>"
        )
        return HTMLResponse(content=html)

    if not code or not state:
        raise HTTPException(400, "Missing OAuth callback parameters")

    try:
        payload = jwt.decode(state, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("purpose") != "google_oauth":
            raise HTTPException(400, "Invalid OAuth state")
        user_id = payload.get("sub")
        uuid.UUID(str(user_id))
    except Exception as exc:
        raise HTTPException(400, f"Invalid OAuth state: {exc}")

    async with httpx.AsyncClient(timeout=20.0) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code != 200:
            logger.error("Google token exchange failed: %s", token_resp.text)
            raise HTTPException(400, "Google token exchange failed")
        token_json = token_resp.json()

    access_token = token_json.get("access_token")
    refresh_token = token_json.get("refresh_token")
    expires_in = token_json.get("expires_in")
    expires_at = None
    if isinstance(expires_in, int):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    if not access_token:
        raise HTTPException(400, "Google did not return an access token")

    await db.upsert_integration_tokens(
        user_id=uuid.UUID(str(user_id)),
        provider="google",
        access_token=encrypt_secret(access_token),
        refresh_token=encrypt_secret(refresh_token) if refresh_token else None,
        expires_at=expires_at,
    )

    html = (
        "<html><body><script>"
        "window.opener && window.opener.postMessage({type:'google-oauth-complete', success:true}, '*');"
        "window.close();"
        "</script>Google connected successfully. You can close this window.</body></html>"
    )
    return HTMLResponse(content=html)


@app.get("/api/v1/communications/gmail/primary")
async def get_primary_gmail_messages(user_id: str, limit: int = 25, after_ts: Optional[int] = None):
    """
    Fetch Gmail inbox messages received today.
    Uses `after_ts` (unix seconds) as an additional baseline for "new since" behavior.
    """
    if not db.pool:
        raise HTTPException(503, "Database not available")

    safe_limit = max(1, min(limit, 50))

    try:
        access_token = await _get_fresh_google_access_token(user_id)
    except Exception as exc:
        message = str(exc)
        if "cannot be decrypted" in message.lower():
            return {
                "status": "reauth_required",
                "items": [],
                "message": "Google token encryption key changed or tokens are corrupted. Reconnect Google from App Integrations.",
            }
        if "integration is missing" in message.lower():
            return {
                "status": "not_connected",
                "items": [],
                "message": "Google is not connected.",
            }
        raise HTTPException(400, f"Google integration is not ready: {exc}")

    start_of_today_utc = int(
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    effective_after = start_of_today_utc
    if isinstance(after_ts, int) and after_ts > 0:
        effective_after = max(effective_after, after_ts)

    q_terms = [
        "in:inbox",
        "-in:spam",
        "-in:trash",
        f"after:{effective_after}",
    ]

    query = " ".join(q_terms)
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=25.0) as client:
        list_resp = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            params={
                "maxResults": safe_limit,
                "q": query,
            },
            headers=headers,
        )

        if list_resp.status_code != 200:
            logger.error("Gmail primary list failed: %s", list_resp.text[:500])
            raise HTTPException(502, "Failed to fetch Gmail primary messages")

        list_payload = list_resp.json()
        raw_messages = list_payload.get("messages") or []
        if not isinstance(raw_messages, list) or not raw_messages:
            return {"status": "success", "items": [], "query": query}

        items = []
        for msg in raw_messages:
            msg_id = (msg or {}).get("id")
            if not msg_id:
                continue

            detail_resp = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
                params={"format": "full"},
                headers=headers,
            )
            if detail_resp.status_code != 200:
                continue

            message = detail_resp.json()
            payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
            headers_list = payload.get("headers") if isinstance(payload.get("headers"), list) else []

            header_map = {}
            for h in headers_list:
                if not isinstance(h, dict):
                    continue
                key = str(h.get("name") or "").lower()
                if key and key not in header_map:
                    header_map[key] = str(h.get("value") or "")

            full_body = _extract_gmail_text(payload)
            snippet = str(message.get("snippet") or "").strip()
            if not snippet and full_body:
                snippet = full_body[:220]

            internal_date_raw = message.get("internalDate")
            try:
                internal_date_ms = int(internal_date_raw)
            except Exception:
                internal_date_ms = 0

            items.append(
                {
                    "id": str(message.get("id") or ""),
                    "thread_id": str(message.get("threadId") or ""),
                    "subject": header_map.get("subject") or "(no subject)",
                    "from": header_map.get("from") or "",
                    "date": header_map.get("date") or "",
                    "message_id_header": header_map.get("message-id") or "",
                    "snippet": snippet,
                    "full_body": full_body,
                    "internal_date_ms": internal_date_ms,
                    "unread": "UNREAD" in (message.get("labelIds") or []),
                }
            )

    items.sort(key=lambda x: x.get("internal_date_ms", 0), reverse=True)
    return {"status": "success", "items": items, "query": query}


@app.post("/api/v1/communications/gmail/summarize")
async def summarize_email(req: EmailSummarizeRequest):
    try:
        summary = await _summarize_email_with_grok_strict(
            subject=req.subject or "",
            sender=req.sender or "",
            body=req.body,
            snippet=req.snippet or "",
        )
        return {"status": "success", "summary": summary, "provider": "grok"}
    except Exception as exc:
        raise HTTPException(400, f"Failed to summarize email: {exc}")


@app.post("/api/v1/communications/gmail/reply/draft")
async def draft_reply_email(req: EmailReplyDraftRequest):
    to_email = _extract_email_address(req.sender or "")
    if not to_email:
        raise HTTPException(400, "Could not determine sender email address for reply")

    try:
        draft = await _generate_reply_draft_with_qwen(
            subject=req.subject or "",
            sender=req.sender or "",
            body=req.body,
        )
        provider_name = "qwen2.5"
    except Exception as exc:
        message = str(exc)
        if "all connection attempts failed" in message.lower():
            # Fallback: try qwen-family on Groq if available.
            try:
                draft = await _generate_reply_draft_with_groq_qwen(
                    subject=req.subject or "",
                    sender=req.sender or "",
                    body=req.body,
                )
                provider_name = f"qwen-fallback ({draft.get('provider_model')})"
            except Exception:
                base_url = os.getenv("OLLAMA_BASE_URL", "(not set)")
                raise HTTPException(
                    503,
                    f"Failed to generate reply draft: Qwen (Ollama) is unreachable at {base_url}. "
                    "Start/connect the Qwen server and retry.",
                )
        else:
            raise HTTPException(500, f"Failed to generate reply draft: {exc}")

    if not draft.get("subject"):
        draft["subject"] = _reply_subject(req.subject or "")

    return {
        "status": "success",
        "provider": provider_name,
        "draft": {
            "to": to_email,
            "subject": draft["subject"],
            "body": draft["body"],
            "thread_id": req.thread_id or "",
            "in_reply_to": req.message_id_header or "",
            "references": req.message_id_header or "",
        },
    }


@app.post("/api/v1/communications/gmail/reply/send")
async def send_reply_email(req: EmailReplySendRequest):
    if not req.to.strip() or not req.subject.strip() or not req.body.strip():
        raise HTTPException(400, "to, subject, and body are required")

    try:
        access_token = await _get_fresh_google_access_token(req.user_id)
        sent = await _send_gmail_reply(
            access_token=access_token,
            to_email=req.to.strip(),
            subject=req.subject.strip(),
            body=req.body,
            thread_id=(req.thread_id or "").strip() or None,
            in_reply_to=(req.in_reply_to or "").strip() or None,
            references=(req.references or "").strip() or None,
        )
        return {"status": "success", "data": sent}
    except Exception as exc:
        raise HTTPException(400, f"Failed to send reply email: {exc}")


@app.get("/api/v1/forms/tally/{user_id}")
async def list_tally_forms(user_id: str):
    """Return active Tally forms for user selection in frontend."""
    if not db.pool:
        raise HTTPException(503, "Database not available")

    import uuid
    try:
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT access_token FROM integrations WHERE user_id = $1 AND provider = 'tally'",
                uuid.UUID(user_id)
            )
        if not row or not row["access_token"]:
            return {"status": "not_connected", "forms": []}

        access_token = decrypt_secret(row["access_token"])
        if not access_token:
            return {"status": "not_connected", "forms": []}

        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get("https://api.tally.so/forms", headers=headers)
            if resp.status_code != 200:
                logger.warning("Tally list forms failed: %s %s", resp.status_code, resp.text[:300])
                return {"status": "error", "forms": []}

            data = resp.json()
            forms = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
            normalized = []
            for f in forms:
                if not isinstance(f, dict):
                    continue
                form_id = f.get("id")
                title = f.get("title") or "Untitled Form"
                form_url = f.get("url") or (f"https://tally.so/r/{form_id}" if form_id else "")
                normalized.append(
                    {
                        "id": form_id,
                        "title": title,
                        "url": form_url,
                        "status": f.get("status"),
                    }
                )

            return {"status": "success", "forms": normalized}
    except Exception as e:
        logger.error("Error listing tally forms for user %s: %s", user_id, e, exc_info=True)
        return {"status": "error", "forms": []}


@app.get("/api/v1/applicants/stored")
async def list_stored_applicants(user_id: str, form_id: Optional[str] = None, limit: int = 100):
    """Return applicant_submission entities cached in universal entities table."""
    if not db.pool:
        raise HTTPException(503, "Database not available")

    import uuid
    try:
        safe_limit = max(1, min(limit, 300))

        async with db.pool.acquire() as conn:
            if form_id:
                rows = await conn.fetch(
                    """
                    SELECT id, display_name, status, data, created_at, updated_at
                    FROM entities
                    WHERE user_id = $1
                      AND entity_type = 'applicant_submission'
                      AND data->>'source_form_id' = $2
                    ORDER BY updated_at DESC
                    LIMIT $3
                    """,
                    uuid.UUID(user_id),
                    form_id,
                    safe_limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, display_name, status, data, created_at, updated_at
                    FROM entities
                    WHERE user_id = $1
                      AND entity_type = 'applicant_submission'
                    ORDER BY updated_at DESC
                    LIMIT $2
                    """,
                    uuid.UUID(user_id),
                    safe_limit,
                )

        items = []
        for row in rows:
            payload = row["data"] or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}

            items.append(
                {
                    "id": str(row["id"]),
                    "display_name": row["display_name"],
                    "status": row["status"],
                    "source_form_id": payload.get("source_form_id"),
                    "response_id": payload.get("response_id"),
                    "submitted_at": payload.get("submitted_at"),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )

        return {"status": "success", "items": items}
    except Exception as e:
        logger.error("Error listing stored applicants for %s: %s", user_id, e, exc_info=True)
        return {"status": "error", "items": []}

# --- Domain List ---
@app.get("/domains")
async def list_domains():
    """Return available domain configs for the frontend selector."""
    if not db.pool:
        return []
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT domain_key, display_name FROM domain_configs ORDER BY created_at"
        )
    return [dict(row) for row in rows]

# --- Endpoints ---

@app.post("/api/v1/chat")
async def chat_endpoint(req: ChatRequest):
    """
    User -> AI Chat (non-streaming).
    """
    if not brain: raise HTTPException(503, "Brain not initialized")

    email_flow = await _handle_email_draft_flow(
        user_id=req.user_id,
        domain=req.domain,
        message=req.message,
        session_id=req.session_id,
    )
    if email_flow is not None:
        try:
            await db.save_message(req.user_id, "user", req.message, req.session_id, req.domain)
            await db.save_message(req.user_id, "assistant", str(email_flow["response"]), req.session_id, req.domain)
        except Exception as e:
            logger.warning("Failed to save audit log for email flow: %s", e)
        return email_flow

    result = await brain.process_message(
        user_id=req.user_id,
        domain=req.domain,
        message=req.message,
        thread_id=req.session_id,
        role="user"
    )
    
    # Audit Log
    try:
        await db.save_message(req.user_id, "user", req.message, req.session_id, req.domain)
        await db.save_message(req.user_id, "assistant", str(result["response"]), req.session_id, req.domain)
    except Exception as e:
        logger.warning("Failed to save audit log: %s", e)

    return result

@app.get("/task/{task_id}")
async def get_task_status(task_id: str, user_id: str):
    """Fetch task status for the frontend UI polling"""
    if not db.pool:
        raise HTTPException(503, "Database not available")
    import uuid
    async with db.pool.acquire() as conn:
        task = await conn.fetchrow("SELECT * FROM agent_tasks WHERE task_id = $1 AND user_id = $2", uuid.UUID(task_id), uuid.UUID(user_id))
    
    if not task:
        raise HTTPException(404, "Task not found")
        
    res = dict(task)
    if isinstance(res.get("output_result"), str):
        try: res["output_result"] = json.loads(res["output_result"])
        except: pass
    
    return {
        "status": res["status"],
        "result": res.get("output_result"),
        "error": res.get("error_message")
    }

@app.get("/api/v1/chat/history")
async def get_chat_history(session_id: str, user_id: str):
    import uuid
    try:
        conv = await db.get_conversation(uuid.UUID(session_id), uuid.UUID(user_id))
        if conv and conv.get("conversation_history"):
            hist = conv["conversation_history"]
            return json.loads(hist) if isinstance(hist, str) else hist
    except Exception as e:
        logger.error("Error fetching chat history for %s: %s", session_id, e)
    return []

@app.get("/api/v1/conversations")
async def list_conversations(user_id: str, domain_key: Optional[str] = None):
    import uuid
    try:
        results = await db.get_user_conversations(uuid.UUID(user_id), domain_key)
        return results
    except Exception as e:
        logger.error("Error listing conversations for %s: %s", user_id, e)
    return []


@app.post("/api/v1/chat/stream")
async def chat_stream_endpoint(req: ChatRequest):
    """
    User -> AI Chat with Server-Sent Events (SSE) streaming.
    
    Streams the LLM's thought process token-by-token to the frontend.
    Compatible with CopilotKit/GEN UI via standard SSE format.
    
    SSE Event Types:
      - token:       Partial text token from the LLM
      - tool_start:  Agent delegation started (tool call)
      - tool_result: Agent task dispatched/completed
      - done:        Final response with full text + task_id
      - error:       Error occurred during processing
    """
    if not brain:
        raise HTTPException(503, "Brain not initialized")

    email_flow = await _handle_email_draft_flow(
        user_id=req.user_id,
        domain=req.domain,
        message=req.message,
        session_id=req.session_id,
    )
    if email_flow is not None:
        async def email_event_generator():
            try:
                await db.save_message(req.user_id, "user", req.message, req.session_id, domain=req.domain)
                await db.save_message(req.user_id, "assistant", str(email_flow["response"]), req.session_id, domain=req.domain)
            except Exception as e:
                logger.warning("Failed to save email-flow messages: %s", e)

            payload = {
                "response": email_flow.get("response", ""),
                "task_id": email_flow.get("task_id"),
            }
            yield f"event: done\ndata: {json.dumps(payload)}\n\n"

        return StreamingResponse(
            email_event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def event_generator():
        full_response = ""
        
        try:
            # Save user message
            try:
                await db.save_message(req.user_id, "user", req.message, req.session_id, domain=req.domain)
            except Exception as e:
                logger.warning("Failed to save user message: %s", e)

            async for event in brain.stream_message(
                user_id=req.user_id,
                domain=req.domain,
                message=req.message,
                thread_id=req.session_id,
                role="user",
            ):
                event_type = event.get("event", "token")
                data = event.get("data", "")
                
                if event_type == "token":
                    full_response += data
                
                # Format as SSE
                payload = json.dumps(data) if not isinstance(data, str) else json.dumps(data)
                yield f"event: {event_type}\ndata: {payload}\n\n"

                if event_type == "done":
                    # Save assistant response
                    try:
                        response_text = data.get("response", full_response) if isinstance(data, dict) else full_response
                        await db.save_message(
                            req.user_id, "assistant", response_text, req.session_id, domain=req.domain
                        )
                    except Exception as e:
                        logger.warning("Failed to save assistant message: %s", e)

        except Exception as e:
            logger.error("SSE stream error: %s", e, exc_info=True)
            yield f"event: error\ndata: {json.dumps(str(e))}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@app.post("/callbacks/task-completed")
async def agent_callback(payload: TaskCallback):
    """
    [The Return Path]
    1. Update DB task status.
    2. 'Wake Up' the Graph to notify the user.
    """
    logger.info("Task %s finished: %s", payload.task_id, payload.status)
    
    # 1. Update Task in DB
    # Ensure update_task_status returns the full task object (including session_id metadata)
    updated_task = await db.update_task_status(
        task_id=payload.task_id,
        status=payload.status,
        result=payload.result,
        error=payload.error
    )
    
    if not updated_task:
        logger.warning("Callback received for unknown task: %s", payload.task_id)
        return {"status": "ignored"}

    # 2. Extract Session ID to Resume Context
    # We assume 'input_payload' or a 'metadata' column in DB stored the session_id
    # Note: 'input_payload' is likely a json string or dict depending on db implementation
    # Let's handle dict access safely
    input_payload = updated_task.get("input_payload") or {}
    if isinstance(input_payload, str):
        import json
        try:
             input_payload = json.loads(input_payload)
        except json.JSONDecodeError:
             input_payload = {}

    session_id = input_payload.get("session_id")
    user_id = updated_task.get("user_id")
    domain = updated_task.get("domain_key")

    if session_id and user_id:
        try:
            persisted_msg = _build_task_history_message(
                task_id=payload.task_id,
                status=payload.status,
                result=payload.result,
                error=payload.error,
            )
            await db.save_message(
                user_id=str(user_id),
                role="assistant",
                message=persisted_msg,
                session_id=str(session_id),
                domain=domain or "HR_RECRUITER",
            )
        except Exception as e:
            logger.warning("Failed to persist callback message for task %s: %s", payload.task_id, e)

    if session_id and brain:
        logger.info("Scheduling session wake-up: %s", session_id)
        asyncio.create_task(
            _wake_session_after_callback(
                task_id=payload.task_id,
                status=payload.status,
                result=payload.result,
                error=payload.error,
                user_id=str(user_id),
                domain=domain or "HR_RECRUITER",
                session_id=str(session_id),
            )
        )
        
    return {"status": "processed"}

@app.post("/api/v1/batch/process")
async def trigger_batch(req: BatchTrigger, bg: BackgroundTasks):
    """Triggers HR Batch Jobs"""
    count = await db.process_pending_batches(req.user_id, req.batch_ids)
    return {"status": "processing", "items_count": count}


# --- Memory Ingest (Research Agent pushes chunks here) ---
class MemoryIngestRequest(BaseModel):
    user_id: str
    domain: str
    content: str
    metadata: dict = {}

@app.post("/memory/ingest")
async def memory_ingest(req: MemoryIngestRequest):
    """
    Receives research chunks from agents and stores them in semantic memory.
    """
    if not memory_service:
        raise HTTPException(503, "Memory service not initialized")

    try:
        memory_id = await memory_service.save_memory(
            user_id=req.user_id,
            domain=req.domain,
            content=req.content,
            metadata=req.metadata
        )
        return {"status": "stored", "memory_id": memory_id}
    except Exception as e:
        logger.error("Memory ingest failed: %s", e, exc_info=True)
        raise HTTPException(500, f"Failed to store memory: {e}")


async def _wake_session_after_callback(
    *,
    task_id: str,
    status: str,
    result: Any,
    error: Optional[str],
    user_id: str,
    domain: str,
    session_id: str,
):
    """Run callback wake-up in background so /callbacks returns quickly."""
    if not brain:
        return

    try:
        if status == "success":
            sys_msg = f"SYSTEM NOTIFICATION: The task {task_id} is complete. Summary of result: {str(result)[:500]}..."
        else:
            sys_msg = f"SYSTEM NOTIFICATION: The task {task_id} FAILED. Error: {error}"

        ai_response = await brain.process_message(
            user_id=user_id,
            domain=domain or "HR_RECRUITER",
            message=sys_msg,
            thread_id=session_id,
            role="system",
        )

        await db.save_message(
            user_id,
            "assistant",
            ai_response["response"],
            session_id,
            domain=domain or "HR_RECRUITER",
        )
    except Exception as e:
        logger.warning("Failed background wake-up for session %s task %s: %s", session_id, task_id, e)
