"""
Shared - Agent Callback Utility.

Provides a reliable and secure way for all agents (Celery workers, HTTP
services) to notify the orchestrator when a task completes or fails.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000")
CALLBACK_SIGNING_SECRET = os.getenv("CALLBACK_SIGNING_SECRET", "").strip()
CALLBACK_MAX_SKEW_SECONDS = int(os.getenv("CALLBACK_MAX_SKEW_SECONDS", "300"))

CALLBACK_SIGNATURE_HEADER = "X-Callback-Signature"
CALLBACK_TIMESTAMP_HEADER = "X-Callback-Timestamp"
CALLBACK_IDEMPOTENCY_HEADER = "X-Callback-Idempotency-Key"


def is_callback_signing_enabled() -> bool:
    return bool(CALLBACK_SIGNING_SECRET)


def serialize_callback_payload(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def build_callback_idempotency_key(task_id: str, serialized_payload: str) -> str:
    digest = hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()[:32]
    return f"{task_id}:{digest}"


def build_callback_headers(
    task_id: str,
    serialized_payload: str,
    *,
    idempotency_key: Optional[str] = None,
    timestamp: Optional[int] = None,
) -> Dict[str, str]:
    ts = str(int(timestamp if timestamp is not None else time.time()))
    headers = {
        CALLBACK_IDEMPOTENCY_HEADER: idempotency_key
        or build_callback_idempotency_key(task_id, serialized_payload),
    }

    if CALLBACK_SIGNING_SECRET:
        signed = f"{ts}.{serialized_payload}".encode("utf-8")
        signature = hmac.new(
            CALLBACK_SIGNING_SECRET.encode("utf-8"),
            signed,
            hashlib.sha256,
        ).hexdigest()
        headers[CALLBACK_TIMESTAMP_HEADER] = ts
        headers[CALLBACK_SIGNATURE_HEADER] = signature

    return headers


def verify_callback_signature(
    *,
    raw_body: bytes,
    signature: Optional[str],
    timestamp: Optional[str],
    max_skew_seconds: Optional[int] = None,
) -> bool:
    """
    Verify callback HMAC signature and replay window.
    Returns True when signature is valid, False otherwise.
    If signing is disabled, returns True.
    """
    if not CALLBACK_SIGNING_SECRET:
        return True

    if not signature or not timestamp:
        return False

    try:
        ts = int(timestamp)
    except Exception:
        return False

    skew_budget = max_skew_seconds if max_skew_seconds is not None else CALLBACK_MAX_SKEW_SECONDS
    if abs(int(time.time()) - ts) > skew_budget:
        return False

    signed = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(
        CALLBACK_SIGNING_SECRET.encode("utf-8"),
        signed,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def notify_task_completed(
    task_id: str,
    status: str,
    result: Any = None,
    error: Optional[str] = None,
    timeout: float = 15.0,
) -> bool:
    """
    Notify the orchestrator that an agent task has completed.

    Returns True if the callback was delivered, False otherwise.
    The caller should NOT treat a False return as fatal - the
    orchestrator's task watcher will catch it as a safety net.
    """
    payload: Dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "result": result,
        "error": error,
    }
    serialized_payload = serialize_callback_payload(payload)
    headers = build_callback_headers(task_id, serialized_payload)
    headers["Content-Type"] = "application/json"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/callbacks/task-completed",
                content=serialized_payload,
                headers=headers,
            )
            resp.raise_for_status()
            logger.info("Callback delivered: task=%s status=%s", task_id, status)
            return True
    except Exception as exc:
        logger.warning(
            "Callback delivery failed for task %s (non-fatal, task watcher will catch): %s",
            task_id,
            exc,
        )
        return False
