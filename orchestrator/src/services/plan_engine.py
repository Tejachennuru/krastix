import asyncio
import logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from shared.database import db
from orchestrator.src.services.dispatch_adapter import DispatchAdapter

logger = logging.getLogger(__name__)


class PlanValidationError(Exception):
    pass


class PlanEngine:
    def __init__(self):
        self.dispatcher = DispatchAdapter()

    def validate_dag(self, nodes: List[Dict[str, Any]]) -> None:
        if not nodes:
            raise PlanValidationError("Plan contains no nodes")
        keys = [str(n.get("node_key", "")).strip() for n in nodes]
        if any(not k for k in keys):
            raise PlanValidationError("Each plan node must have a non-empty node_key")
        if len(set(keys)) != len(keys):
            raise PlanValidationError("Duplicate node_key detected in plan")

        key_set = set(keys)
        graph = defaultdict(list)
        indeg = {k: 0 for k in keys}

        for node in nodes:
            k = node["node_key"]
            deps = node.get("dependencies", []) or []
            if not isinstance(deps, list):
                raise PlanValidationError(f"Dependencies for node '{k}' must be a list")
            for dep in deps:
                dep_key = str(dep).strip()
                if dep_key not in key_set:
                    raise PlanValidationError(f"Node '{k}' depends on unknown node '{dep_key}'")
                graph[dep_key].append(k)
                indeg[k] += 1

        q = deque([k for k, v in indeg.items() if v == 0])
        visited = 0
        while q:
            cur = q.popleft()
            visited += 1
            for nxt in graph[cur]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    q.append(nxt)
        if visited != len(keys):
            raise PlanValidationError("Cycle detected in plan DAG")

    async def create_plan_from_tool_calls(
        self,
        *,
        user_id: str,
        domain: str,
        session_id: str,
        source_message: str,
        tool_calls: List[Dict[str, Any]],
        allowed_queues: List[str],
    ) -> Dict[str, Any]:
        delegate_calls = []
        for idx, tool_call in enumerate(tool_calls):
            if tool_call.get("name") != "DelegateTask":
                continue
            args = tool_call.get("args", {})
            queue = args.get("agent_queue", "")
            if queue not in (allowed_queues or []):
                raise PlanValidationError(
                    f"Queue '{queue}' is not authorized for this domain. Allowed: {allowed_queues}"
                )
            params = args.get("parameters", {}) if isinstance(args.get("parameters", {}), dict) else {}
            deps = args.get("depends_on", [])
            if not deps:
                deps = params.get("depends_on", [])
            if isinstance(deps, str):
                deps = [deps]
            if not isinstance(deps, list):
                deps = []
            node_key = str(args.get("node_key", "")).strip() or f"task_{idx+1}"
            delegate_calls.append(
                {
                    "node_key": node_key,
                    "node_type": "agent_task",
                    "agent_queue": queue,
                    "instruction": args.get("instruction", ""),
                    "task_action": args.get("task_action", ""),
                    "parameters": params,
                    "priority": int(args.get("priority", 1)),
                    "dependencies": [str(d) for d in deps if str(d).strip()],
                }
            )

        self.validate_dag(delegate_calls)
        plan = await db.create_plan(
            user_id=user_id,
            domain_key=domain,
            session_id=session_id,
            source_message=source_message,
            nodes=delegate_calls,
        )
        await db.add_plan_event(
            plan_id=str(plan["plan_id"]),
            event_type="plan_created",
            event_payload={"node_count": len(delegate_calls)},
        )
        return plan

    async def dispatch_ready_nodes(
        self,
        *,
        plan_id: str,
        user_id: str,
        domain: str,
        session_id: str,
        dispatch_map: Dict[str, Dict[str, str]],
    ) -> Dict[str, Any]:
        nodes = await db.get_plan_nodes(plan_id)
        if not nodes:
            return {"dispatched": 0, "failed": 0}

        node_by_key = {n["node_key"]: n for n in nodes}
        statuses = {n["node_key"]: n["status"] for n in nodes}

        ready: List[Dict[str, Any]] = []
        for node in nodes:
            if node["status"] not in ("pending", "ready"):
                continue
            deps = node.get("dependencies", []) or []
            if all(statuses.get(dep) == "completed" for dep in deps):
                ready.append(node)

        dispatched = 0
        failed = 0
        for node in ready:
            node_id = str(node["node_id"])
            queue = node["agent_queue"]
            await db.update_plan_node_status(node_id=node_id, status="ready")
            await db.add_plan_event(
                plan_id=plan_id,
                node_id=node_id,
                event_type="node_ready",
                event_payload={"node_key": node["node_key"], "queue": queue},
            )

            task_id = await db.create_task(
                user_id=UUID(str(user_id)),
                domain_key=domain,
                agent_queue=queue,
                input_payload={
                    "instruction": node.get("instruction", ""),
                    "task_action": node.get("task_action", "") or "",
                    "parameters": node.get("parameters", {}) or {},
                    "priority": int(node.get("priority", 1)),
                    "session_id": session_id,
                },
                plan_id=plan_id,
                plan_node_id=node_id,
            )
            await db.mark_task_queued(str(task_id))
            await db.update_plan_node_status(node_id=node_id, status="queued", task_id=str(task_id))

            route = dispatch_map.get(queue)
            dispatch_result = await self.dispatcher.dispatch(
                route=route,
                queue=queue,
                task_id=str(task_id),
                user_id=user_id,
                domain=domain,
                thread_id=session_id,
                instruction=node.get("instruction", ""),
                task_action=node.get("task_action", "") or "",
                parameters=node.get("parameters", {}) or {},
            )
            if dispatch_result.get("accepted"):
                await db.mark_task_dispatched(str(task_id))
                await db.update_plan_node_status(node_id=node_id, status="dispatched")
                await db.add_plan_event(
                    plan_id=plan_id,
                    node_id=node_id,
                    event_type="node_dispatched",
                    event_payload={"node_key": node["node_key"], "task_id": str(task_id), **dispatch_result},
                )
                dispatched += 1
            else:
                failed += 1
                error = dispatch_result.get("error", "Dispatch transport failure")
                error_code = dispatch_result.get("error_code", "transport_failed")
                await db.update_task_status(
                    task_id=str(task_id),
                    status="failed",
                    result={"status": "failed", "dispatch_target": queue, "error": error},
                    error=error,
                    error_code=error_code,
                    error_detail=error,
                )
                await db.update_plan_node_status(
                    node_id=node_id,
                    status="failed",
                    error_code=error_code,
                    error_detail=error,
                    task_id=str(task_id),
                )
                await db.add_plan_event(
                    plan_id=plan_id,
                    node_id=node_id,
                    event_type="node_dispatch_failed",
                    event_payload={"node_key": node["node_key"], "task_id": str(task_id), **dispatch_result},
                )

        await self.refresh_plan_status(plan_id)
        return {"dispatched": dispatched, "failed": failed}

    async def on_task_terminal(
        self,
        *,
        task_id: str,
        task_status: str,
        result: Any,
        error: Optional[str],
    ) -> None:
        node = await db.get_plan_node_by_task(task_id)
        if not node:
            return
        plan_id = str(node["plan_id"])
        node_id = str(node["node_id"])
        node_status = "completed" if task_status == "completed" else "failed"
        await db.update_plan_node_status(
            node_id=node_id,
            status=node_status,
            result=result,
            error_code="agent_failed" if node_status == "failed" else None,
            error_detail=error,
        )
        await db.add_plan_event(
            plan_id=plan_id,
            node_id=node_id,
            event_type="node_terminal",
            event_payload={"task_id": task_id, "task_status": task_status, "node_status": node_status},
        )

        plan = await db.get_plan(plan_id)
        if not plan:
            return
        await self.dispatch_ready_nodes(
            plan_id=plan_id,
            user_id=str(plan["user_id"]),
            domain=plan["domain_key"],
            session_id=str(plan["session_id"]) if plan.get("session_id") else "",
            dispatch_map=await self._build_dispatch_map(plan["domain_key"]),
        )
        await self.refresh_plan_status(plan_id)

    async def _build_dispatch_map(self, domain: str) -> Dict[str, Dict[str, str]]:
        from orchestrator.src.schemas import build_dispatch_map
        agents = await db.get_agents_for_domain(domain)
        return build_dispatch_map(agents)

    async def refresh_plan_status(self, plan_id: str) -> Optional[Dict[str, Any]]:
        counts = await db.summarize_plan_node_states(plan_id)
        total = sum(counts.values())
        if total == 0:
            return None
        completed = counts.get("completed", 0)
        failed = counts.get("failed", 0)
        blocked = counts.get("blocked", 0)
        active = sum(counts.get(s, 0) for s in ("pending", "ready", "queued", "dispatched", "running"))

        next_status = "running"
        summary = None
        if active > 0:
            next_status = "running"
        elif failed > 0 and completed > 0:
            next_status = "completed_with_failures"
            summary = f"Plan finished with partial failures ({completed} completed, {failed} failed, {blocked} blocked)."
        elif failed > 0 and completed == 0:
            next_status = "failed"
            summary = f"Plan failed ({failed} failed nodes)."
        elif completed == total:
            next_status = "completed"
            summary = f"Plan completed ({completed}/{total} nodes)."
        else:
            next_status = "completed_with_failures"
            summary = f"Plan ended with unresolved states: {counts}"

        updated = await db.update_plan_status(plan_id=plan_id, status=next_status, summary=summary)
        return updated

