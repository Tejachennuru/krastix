import os
import httpx
from fastapi import FastAPI, BackgroundTasks
from src.models import ResearchTask, AgentResponse
from src.graph import ResearchGraph

app = FastAPI(title="Krastix Research Agent (LangGraph)")
graph_engine = ResearchGraph()
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000")

def chunk_text(text: str, chunk_size=1500):
    if not text: return []
    # Ensure text is string
    text = str(text)
    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

async def execute_task_bg(task: ResearchTask):
    print(f"🚀 Processing: {task.task_type} -> {task.query_or_url}")
    
    # 1. Run LangGraph Workflow
    content = await graph_engine.run(task.task_type, task.query_or_url)
    
    # 2. Chunk
    chunks = chunk_text(content)
    
    # 3. Push to Brain
    async with httpx.AsyncClient() as client:
        for i, chunk in enumerate(chunks):
            payload = {
                "user_id": task.user_id,
                "domain": "RESEARCH_AGENT",
                "content": f"**Source:** {task.query_or_url}\n**Type:** {task.task_type}\n\n{chunk}",
                "metadata": {
                    **task.context_metadata,
                    "agent": "langgraph-v1",
                    "chunk_index": i
                }
            }
            try:
                # Assuming orchestrator is reachable. 
                # Note: If this fails, we lose data (fire-and-forget). 
                # In production, this should push to Queue or retry.
                await client.post(f"{ORCHESTRATOR_URL}/memory/ingest", json=payload)
            except Exception as e:
                print(f"❌ Failed to push chunk: {e}")
    
    print(f"✅ Task Completed: {task.query_or_url}")

@app.post("/research/run", response_model=AgentResponse)
async def run_research(task: ResearchTask, bg: BackgroundTasks):
    bg.add_task(execute_task_bg, task)
    return AgentResponse(
        status="accepted", 
        task_id=task.context_metadata.get("task_id", "gen"), 
        message="Graph workflow started."
    )

@app.get("/health")
def health():
    return {"status": "online", "engine": "LangGraph + Firecrawl"}
