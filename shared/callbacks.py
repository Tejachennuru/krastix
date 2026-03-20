"""
Shared — Agent Callback Utility.

Provides a reliable way for all agents (Celery workers, HTTP services)
to notify the orchestrator when a task completes or fails.

This replaces the fire-and-forget pattern with guaranteed delivery:
  1. Primary:  HTTP POST to /callbacks/task-completed
  2. Fallback: DB status is already updated, orchestrator's task watcher
               will pick up stale tasks as a safety net.
"""

import os
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000")


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
    The caller should NOT treat a False return as fatal — the
    orchestrator's task watcher will catch it as a safety net.
    """
    payload = {
        "task_id": task_id,
        "status": status,
        "result": result,
        "error": error,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/callbacks/task-completed",
                json=payload,
            )
            resp.raise_for_status()
            logger.info(
                "Callback delivered: task=%s status=%s", task_id, status
            )
            return True
    except Exception as exc:
        logger.warning(
            "Callback delivery failed for task %s (non-fatal, "
            "task watcher will catch): %s",
            task_id,
            exc,
        )
        return False
