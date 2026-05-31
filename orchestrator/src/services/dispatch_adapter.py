import os
from typing import Any, Dict, Optional

import httpx

from shared.mq import celery_app


class DispatchAdapter:
    """
    Transport adapter for agent dispatch.
    Supports celery/http/mcp with extensible fallback methods.
    """

    async def dispatch(
        self,
        *,
        route: Optional[Dict[str, Any]],
        queue: str,
        task_id: str,
        user_id: str,
        domain: str,
        thread_id: str,
        instruction: str,
        task_action: str,
        parameters: Dict[str, Any],
    ) -> Dict[str, Any]:
        method = (route or {}).get("method", "legacy")
        try:
            if method == "celery":
                task_name = (route or {}).get("task_name", "agents.perform_task")
                celery_app.send_task(task_name, args=[str(task_id)], queue=queue)
                return {"accepted": True, "method": "celery"}

            if method == "http":
                target_url = (route or {}).get("target_url", queue)
                endpoint = (route or {}).get("http_endpoint", "/research/run")
                if not str(target_url).startswith("http"):
                    return {
                        "accepted": False,
                        "error_code": "transport_invalid_target",
                        "error": f"HTTP target is invalid for queue '{queue}': {target_url}",
                    }
                payload = {
                    "user_id": user_id,
                    "task_type": "GENERAL_SEARCH",
                    "query_or_url": instruction,
                    "instruction": instruction,
                    "task_action": task_action,
                    "parameters": parameters,
                    "context_metadata": {
                        "task_id": str(task_id),
                        "session_id": thread_id,
                        "domain_key": domain,
                    },
                }
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post(f"{target_url}{endpoint}", json=payload)
                    resp.raise_for_status()
                return {"accepted": True, "method": "http", "target": f"{target_url}{endpoint}"}

            if method == "mcp":
                # Phase 1 bridge: call an MCP gateway endpoint if configured in route.
                target_url = (route or {}).get("target_url", "")
                endpoint = (route or {}).get("http_endpoint", "/mcp/invoke")
                if not str(target_url).startswith("http"):
                    return {
                        "accepted": False,
                        "error_code": "mcp_gateway_missing",
                        "error": f"MCP dispatch requires a gateway URL for queue '{queue}'",
                    }
                payload = {
                    "task_id": str(task_id),
                    "user_id": user_id,
                    "domain_key": domain,
                    "session_id": thread_id,
                    "agent_queue": queue,
                    "instruction": instruction,
                    "task_action": task_action,
                    "parameters": parameters,
                }
                async with httpx.AsyncClient(timeout=25.0) as client:
                    resp = await client.post(f"{target_url}{endpoint}", json=payload)
                    resp.raise_for_status()
                return {"accepted": True, "method": "mcp", "target": f"{target_url}{endpoint}"}

            return await self._legacy_dispatch(
                queue=queue,
                task_id=task_id,
                user_id=user_id,
                thread_id=thread_id,
                instruction=instruction,
            )
        except Exception as exc:
            return {
                "accepted": False,
                "error_code": "transport_failed",
                "error": str(exc),
                "method": method,
            }

    async def _legacy_dispatch(
        self,
        *,
        queue: str,
        task_id: str,
        user_id: str,
        thread_id: str,
        instruction: str,
    ) -> Dict[str, Any]:
        if queue == "research_queue":
            research_url = os.getenv("RESEARCH_AGENT_URL", "http://localhost:8001")
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    f"{research_url}/research/run",
                    json={
                        "user_id": user_id,
                        "task_type": "GENERAL_SEARCH",
                        "query_or_url": instruction,
                        "context_metadata": {
                            "task_id": str(task_id),
                            "session_id": thread_id,
                        },
                    },
                )
                resp.raise_for_status()
            return {"accepted": True, "method": "legacy_http", "target": f"{research_url}/research/run"}

        legacy_map = {
            "crm_queue": "agents.crm_worker.execute_task",
            "form_queue": "agents.form_worker.execute_task",
            "communication_queue": "agents.communication_worker.execute_task",
        }
        task_name = legacy_map.get(queue, "agents.perform_task")
        celery_app.send_task(task_name, args=[str(task_id)], queue=queue)
        return {"accepted": True, "method": "legacy_celery", "task_name": task_name}

