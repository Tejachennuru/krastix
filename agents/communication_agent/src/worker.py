import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx
from celery import Task

from shared.mq import celery_app
from shared.database import db
from shared.callbacks import notify_task_completed
from shared.integrations_crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")


class CommunicationWorker:
    def __init__(self):
        self.name = "CommunicationAgent"

    async def execute(self, task_id: str) -> dict:
        await db.connect()

        try:
            task = await db.pool.fetchrow(
                "SELECT * FROM agent_tasks WHERE task_id = $1",
                UUID(task_id),
            )
            if not task:
                return {"status": "failed", "error": "Task not found"}

            input_payload = task["input_payload"]
            if isinstance(input_payload, str):
                input_payload = json.loads(input_payload)

            task_action = input_payload.get("task_action", "")
            parameters = input_payload.get("parameters", {})
            user_id = task["user_id"]

            if task_action != "send_email":
                result = {
                    "status": "failed",
                    "error": f"Unsupported task_action: {task_action}",
                }
            else:
                result = await self._send_email(user_id=user_id, payload=parameters)

            final_status = "completed" if result.get("status") == "success" else "failed"
            await db.update_task_status(task_id=str(task_id), status=final_status, result=result, error=result.get("error"))
            await notify_task_completed(
                task_id=str(task_id),
                status="success" if final_status == "completed" else "failed",
                result=result,
                error=result.get("error"),
            )
            return result

        except Exception as exc:
            logger.exception("Communication worker failed for task %s", task_id)
            await db.update_task_status(task_id=str(task_id), status="failed", result=None, error=str(exc))
            await notify_task_completed(task_id=str(task_id), status="failed", error=str(exc))
            return {"status": "failed", "error": str(exc)}

        finally:
            await db.disconnect()

    def _normalize_list(self, value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        out = []
        seen = set()
        for item in value:
            text = str(item).strip().lower()
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out

    async def _load_google_tokens(self, user_id: UUID) -> Dict[str, Any]:
        row = await db.get_integration(user_id, "google")
        if not row:
            raise RuntimeError("Google integration is missing")

        access = None
        if row.get("access_token"):
            try:
                access = decrypt_secret(row.get("access_token"))
            except Exception:
                logger.warning("Access token decryption failed for user %s", user_id)

        refresh = None
        if row.get("refresh_token"):
            try:
                refresh = decrypt_secret(row.get("refresh_token"))
            except Exception:
                logger.warning("Refresh token decryption failed for user %s", user_id)
        expires_at = row.get("expires_at")

        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": expires_at,
        }

    async def _refresh_google_access_token(self, user_id: UUID, refresh_token: str) -> Dict[str, Any]:
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
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        }

    async def _ensure_fresh_access_token(self, user_id: UUID) -> str:
        token_info = await self._load_google_tokens(user_id)
        access_token = token_info["access_token"]
        refresh_token = token_info.get("refresh_token")
        expires_at = token_info.get("expires_at")

        if not access_token:
            if refresh_token:
                token_info = await self._refresh_google_access_token(user_id, refresh_token)
                access_token = token_info["access_token"]
            else:
                raise RuntimeError(
                    "Google credentials cannot be decrypted with the current encryption key. Reconnect Google from App Integrations."
                )

        if isinstance(expires_at, datetime):
            cutoff = datetime.now(timezone.utc) + timedelta(seconds=60)
            normalized_expires = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
            if normalized_expires <= cutoff:
                if not refresh_token:
                    raise RuntimeError("Google token expired and no refresh token is available")
                token_info = await self._refresh_google_access_token(user_id, refresh_token)
                access_token = token_info["access_token"]

        return access_token

    async def _send_gmail_message(self, access_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        to_items = self._normalize_list(payload.get("to"))
        cc_items = self._normalize_list(payload.get("cc"))
        bcc_items = self._normalize_list(payload.get("bcc"))
        subject = str(payload.get("subject") or "").strip()
        body = str(payload.get("body") or "").strip()

        if not to_items:
            raise RuntimeError("At least one recipient is required")

        msg = EmailMessage()
        msg["To"] = ", ".join(to_items)
        if cc_items:
            msg["Cc"] = ", ".join(cc_items)
        if bcc_items:
            msg["Bcc"] = ", ".join(bcc_items)
        msg["Subject"] = subject
        msg.set_content(body)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                json={"raw": raw},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code not in (200, 202):
                raise RuntimeError(f"Gmail send failed: {resp.text[:400]}")
            sent = resp.json()

        return {
            "gmail_message_id": sent.get("id"),
            "gmail_thread_id": sent.get("threadId"),
            "to": to_items,
            "cc": cc_items,
            "bcc": bcc_items,
            "subject": subject,
        }

    async def _send_email(self, user_id: UUID, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            access_token = await self._ensure_fresh_access_token(user_id)
            data = await self._send_gmail_message(access_token, payload)
            return {"status": "success", "action": "send_email", "data": data}
        except RuntimeError as first_error:
            # Retry once if token might be invalid but refresh token exists.
            token_info = await self._load_google_tokens(user_id)
            refresh = token_info.get("refresh_token")
            if not refresh:
                return {"status": "failed", "error": str(first_error)}
            try:
                refreshed = await self._refresh_google_access_token(user_id, refresh)
                data = await self._send_gmail_message(refreshed["access_token"], payload)
                return {"status": "success", "action": "send_email", "data": data}
            except Exception as retry_error:
                return {"status": "failed", "error": str(retry_error)}


@celery_app.task(name="agents.communication_worker.execute_task", bind=True)
def execute_task(self: Task, task_id: str):
    worker = CommunicationWorker()
    return asyncio.run(worker.execute(task_id))
