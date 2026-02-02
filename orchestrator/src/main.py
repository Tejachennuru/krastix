from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Any
from contextlib import asynccontextmanager

from shared.database import db
from shared.mq import celery_app
from orchestrator.src.graph import OrchestratorGraph
from orchestrator.src.services.memory import MemoryService

# Internal Services
brain: Optional[OrchestratorGraph] = None
memory_service: Optional[MemoryService] = None

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

# --- Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global brain, memory_service
    print("🚀 Orchestrator (Enterprise Edition) Starting...")
    
    await db.connect()
    
    if db.pool:
        # 1. Init Memory Service
        memory_service = MemoryService(db.pool)
        print("🧠 Memory Service Connected")
        
        # 2. Init Brain with Memory + DB Pool for PostgresSaver
        brain = OrchestratorGraph(memory_service=memory_service, db_pool=db.pool)
        await brain.initialize()  # Async init for PostgresSaver
        print("🤖 Graph Brain Loaded with Persistent Checkpoints")
    
    yield
    print("🛑 Orchestrator Shutting Down...")
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
    print(f"🔥 SERVER ERROR: {error_details}")  # Print to console
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "details": str(exc), "trace": error_details.split("\n")}
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

# --- Endpoints ---

@app.post("/api/v1/chat")
async def chat_endpoint(req: ChatRequest):
    """
    User -> AI Chat.
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
        await db.save_message(req.user_id, "user", req.message, req.session_id)
        await db.save_message(req.user_id, "assistant", str(result["response"]), req.session_id)
    except Exception as e:
        print(f"⚠️ Failed to save audit log: {e}")

    return result

@app.post("/callbacks/task-completed")
async def agent_callback(payload: TaskCallback):
    """
    [The Return Path]
    1. Update DB task status.
    2. 'Wake Up' the Graph to notify the user.
    """
    print(f"📩 Task {payload.task_id} Finished: {payload.status}")
    
    # 1. Update Task in DB
    # Ensure update_task_status returns the full task object (including session_id metadata)
    updated_task = await db.update_task_status(
        task_id=payload.task_id,
        status=payload.status,
        result=payload.result,
        error=payload.error
    )
    
    if not updated_task:
        print("⚠️ Callback received for unknown task.")
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
        except:
             input_payload = {}

    session_id = input_payload.get("session_id")
    user_id = updated_task.get("user_id")
    domain = updated_task.get("domain_key")

    if session_id and brain:
        print(f"🔔 Waking up Session: {session_id}")
        
        # 3. Construct System Notification
        if payload.status == "success":
            sys_msg = f"SYSTEM NOTIFICATION: The task {payload.task_id} is complete. Summary of result: {str(payload.result)[:500]}..."
        else:
            sys_msg = f"SYSTEM NOTIFICATION: The task {payload.task_id} FAILED. Error: {payload.error}"
            
        # 4. Resume Graph (Role = system)
        # This will trigger the LLM to generate a user-friendly notification
        try:
            ai_response = await brain.process_message(
                user_id=str(user_id),
                domain=domain,
                message=sys_msg,
                thread_id=session_id,
                role="system"
            )
            
            # 5. Save the AI's notification to history
            await db.save_message(user_id, "assistant", ai_response["response"], session_id)
        except Exception as e:
            print(f"⚠️ Failed to wake up brain: {e}")
        
    return {"status": "processed"}

@app.post("/api/v1/batch/process")
async def trigger_batch(req: BatchTrigger, bg: BackgroundTasks):
    """Triggers HR Batch Jobs"""
    count = await db.process_pending_batches(req.user_id, req.batch_ids)
    return {"status": "processing", "items_count": count}
