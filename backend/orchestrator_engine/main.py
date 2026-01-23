from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from uuid import UUID
from pydantic import BaseModel
from shared.database import db
from shared.mq import celery_app
# We import simple types if needed or just define the payload model here
from typing import Optional

# --------------------------------------------
# 1. Models for Test Endpoints
# --------------------------------------------
class TestTaskRequest(BaseModel):
    user_id: str
    instruction: str

# --------------------------------------------
# 2. Lifecycle Manager (Database Connection)
# --------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages startup and shutdown events.
    Connects to the database using the IPv4 fix if needed.
    """
    print("🚀 Starting Orchestrator...")
    await db.connect()
    yield
    print("🛑 Shutting down Orchestrator...")
    await db.disconnect()

# --------------------------------------------
# 3. FastAPI Application Setup
# --------------------------------------------
app = FastAPI(title="Krastix Orchestrator (Oracle Cloud Edition)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------
# 4. Critical Health Check (The "Pulse")
# --------------------------------------------
@app.get("/health")
async def health_check():
    """
    Verifies that the API can talk to:
    1. The Database (Supabase)
    2. The Broker (Redis)
    """
    health_status = {
        "status": "online",
        "database": "unknown",
        "broker": "unknown"
    }
    
    # 1. Check Database
    if db.pool and not db.pool._closed:
        health_status["database"] = "connected"
    else:
        health_status["database"] = "disconnected"
        health_status["status"] = "degraded"

    # 2. Check Celery Broker (Redis)
    try:
        with celery_app.connection_or_acquire() as conn:
            conn.ensure_connection(max_retries=1)
            health_status["broker"] = "connected"
    except Exception as e:
        print(f"Broker Error: {e}")
        health_status["broker"] = "disconnected"
        health_status["status"] = "degraded"

    return health_status

# --------------------------------------------
# 5. Task Dispatch Test (The "Nerves")
# --------------------------------------------
@app.post("/test-agent")
async def dispatch_test_task(payload: TestTaskRequest):
    """
    Dispatches a dummy task to the 'research_queue' to verify workers are listening.
    """
    try:
        # Import task signature here to avoid circular imports at top level if any
        # (Though we handled it in shared/mq.py, this is safe)
        from agents.tasks import perform_task
        
        # Send task to Celery
        # We explicitly route to 'research_queue' to test that specific worker container
        task = perform_task.apply_async(
            args=[payload.instruction, payload.user_id],
            queue="research_queue"
        )
        
        return {
            "status": "dispatched",
            "task_id": str(task.id),
            "queue": "research_queue",
            "message": "Check the 'krastix-research-worker' logs for output."
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to dispatch task: {str(e)}")

# --------------------------------------------
# (Keep existing endpoints if you wish, or we add basics)
# --------------------------------------------
@app.get("/")
async def root():
    return {"message": "Krastix Orchestrator is Live on Oracle Cloud!"}
