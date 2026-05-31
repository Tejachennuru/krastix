import os
import httpx
from fastapi import FastAPI, BackgroundTasks
from src.models import ResearchTask, AgentResponse
from src.graph import ResearchGraph
from shared.callbacks import notify_task_completed

app = FastAPI(title="Krastix Research Agent (LangGraph)")
graph_engine = ResearchGraph()
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000")

def chunk_text(text: str, chunk_size=1500):
    if not text: return []
    # Ensure text is string
    text = str(text)
    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

async def execute_task_bg(task: ResearchTask):
    task_id = task.context_metadata.get("task_id", "unknown")
    print(f"Processing: {task.task_type} -> {task.query_or_url}")
    
    try:
        # 1. Run LangGraph Workflow
        content = await graph_engine.run(task.task_type, task.query_or_url)
        
        # 2. Chunk
        chunks = chunk_text(content)
        
        # 3. Push to Brain (memory)
        ingested_count = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i, chunk in enumerate(chunks):
                payload = {
                    "user_id": task.user_id,
                    "domain": task.context_metadata.get("domain_key", "RESEARCH_AGENT"),
                    "content": f"**Source:** {task.query_or_url}\n**Type:** {task.task_type}\n\n{chunk}",
                    "metadata": {
                        **task.context_metadata,
                        "agent": "langgraph-v1",
                        "chunk_index": i
                    }
                }
                try:
                    resp = await client.post(
                        f"{ORCHESTRATOR_URL}/memory/ingest", json=payload
                    )
                    resp.raise_for_status()
                    ingested_count += 1
                except Exception as e:
                    print(f"Failed to push chunk {i}: {e}")
        
        # 4. Notify orchestrator — task is DONE
        summary = content[:500] if content else "No content found."
        callback_result = {
            "query": task.query_or_url,
            "task_type": task.task_type,
            "chunks_ingested": ingested_count,
            "total_length": len(content) if content else 0,
            "summary": summary,
        }
        
        await notify_task_completed(
            task_id=task_id,
            status="completed",
            result=callback_result,
            error=None,
        )
        
        print(f"Task Completed: {task.query_or_url} ({ingested_count} chunks)")

    except Exception as exc:
        print(f"Task failed: {exc}")
        # Send failure callback
        await notify_task_completed(
            task_id=task_id,
            status="failed",
            result=None,
            error=str(exc),
        )

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
