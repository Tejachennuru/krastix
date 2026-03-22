import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Any
from contextlib import asynccontextmanager

import jwt
from passlib.context import CryptContext
import httpx

from shared.database import db
from shared.mq import celery_app
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

@app.post("/api/v1/integrations")
async def save_integration(req: IntegrationRequest):
    """Save an access token securely for a third-party app (Tally, Jotform)"""
    if not db.pool:
        raise HTTPException(503, "Database not available")
    import uuid
    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO integrations (user_id, provider, access_token) 
               VALUES ($1, $2, $3) 
               ON CONFLICT (user_id, provider) 
               DO UPDATE SET access_token = EXCLUDED.access_token, updated_at = NOW()""",
            uuid.UUID(req.user_id), req.provider.lower(), req.access_token
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

        headers = {"Authorization": f"Bearer {row['access_token']}"}
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
