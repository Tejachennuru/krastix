"""
Document Agent (VDU) — FastAPI Service.

Standalone microservice that transforms unstructured binary files
(PDFs, images) into structured, schema-validated entities using a
multi-stage Vision-Document-Understanding pipeline.

Endpoints
---------
POST /document/run   — Accept a processing task (dispatched by orchestrator).
GET  /health         — Liveness + VLM readiness probe.
"""

import json
import logging
import re
import traceback
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware

from src.config import (
    ORCHESTRATOR_URL,
    VLM_MODEL,
    OLLAMA_BASE_URL,
    STORAGE_BUCKET,
)
from src.models import AgentResponse, DocumentTask
from src.graph import DocumentPipeline
from src.storage import download_file, upload_artifact, detect_file_type
from src.pipeline.grounding import chunk_document

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)

# ────────────────────────────────────────────────────────────────────
# Globals
# ────────────────────────────────────────────────────────────────────

pipeline: Optional[DocumentPipeline] = None


# ────────────────────────────────────────────────────────────────────
# Lifespan
# ────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pipeline

    logger.info("Document Agent starting...")

    # 1. Connect shared database (for entity schema lookups)
    try:
        from shared.database import db
        await db.connect()
        logger.info("Database connected")
    except Exception as exc:
        logger.warning("Database connection failed (non-critical): %s", exc)

    # 2. Initialise the VDU pipeline
    pipeline = DocumentPipeline()
    logger.info("VDU Pipeline initialised (VLM: %s @ %s)", VLM_MODEL, OLLAMA_BASE_URL)

    yield

    logger.info("Document Agent shutting down...")
    try:
        from shared.database import db
        await db.disconnect()
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
# App
# ────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Krastix Document Agent (VDU)",
    description="Multi-stage Vision-Document-Understanding pipeline",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

def _parse_instruction(task: DocumentTask) -> dict:
    """
    Extract structured parameters from the task instruction.

    The orchestrator's DelegateTask sends free-text instructions.
    We parse out ``file_id`` and ``entity_type`` if embedded.
    """
    file_id = task.file_id
    entity_type = task.entity_type
    domain_key = task.domain_key or task.context_metadata.get("domain_key", "")
    instruction = task.instruction or task.query_or_url or ""

    # Try to extract file_id from instruction text
    if not file_id:
        # Patterns: "file_id: abc-123", "file: abc-123", "File ID abc-123"
        match = re.search(
            r"(?:file[_\s]?id|file)[:\s]+([a-zA-Z0-9_\-/.]+)",
            instruction,
            re.IGNORECASE,
        )
        if match:
            file_id = match.group(1).strip()

    # Try to extract entity_type from instruction text
    if not entity_type:
        match = re.search(
            r"(?:entity[_\s]?type|entity)[:\s]+([a-zA-Z_]+)",
            instruction,
            re.IGNORECASE,
        )
        if match:
            entity_type = match.group(1).strip().lower()

    # Common entity type keywords
    if not entity_type:
        lower = instruction.lower()
        for etype in ("candidate", "lead", "contact", "invoice", "receipt", "contract"):
            if etype in lower:
                entity_type = etype
                break

    return {
        "file_id": file_id or "",
        "entity_type": entity_type or "",
        "domain_key": domain_key,
        "instruction": instruction,
    }


# ────────────────────────────────────────────────────────────────────
# Background Task Runner
# ────────────────────────────────────────────────────────────────────

async def execute_document_task(task: DocumentTask) -> None:
    """
    Background worker that runs the full VDU pipeline and pushes
    results back to the orchestrator.
    """
    task_id = task.context_metadata.get("task_id", "unknown")
    session_id = task.context_metadata.get("session_id", "")

    logger.info("Processing document task %s", task_id)

    try:
        # 1. Parse structured params from instruction
        params = _parse_instruction(task)
        file_id = params["file_id"]
        entity_type = params["entity_type"]
        domain_key = params["domain_key"]
        instruction = params["instruction"]

        # 2. Download file from Supabase Storage
        if not file_id:
            raise ValueError(
                "No file_id found in task. Please include file_id in the "
                "instruction or as a separate field."
            )

        file_bytes = await download_file(file_id)
        file_type = detect_file_type(file_bytes)
        logger.info("Downloaded %s file: %d bytes", file_type, len(file_bytes))

        # 3. Run the VDU pipeline
        result = await pipeline.run(
            file_bytes=file_bytes,
            user_id=task.user_id,
            task_id=task_id,
            instruction=instruction,
            file_id=file_id,
            entity_type=entity_type,
            domain_key=domain_key,
        )

        status = result.get("status", "error")
        grounded_data = result.get("grounded_data", {})
        raw_markdown = result.get("raw_markdown", "")
        extracted_fields = result.get("extracted_fields", [])
        tables = result.get("tables", [])
        pages_data = result.get("regions", [])
        logs = result.get("logs", [])

        logger.info(
            "Pipeline complete: status=%s, fields=%d, tables=%d",
            status, len(extracted_fields), len(tables),
        )

        # 4. Upload full artifact to Supabase Storage
        artifact = {
            "task_id": task_id,
            "entity_type": entity_type,
            "grounded_data": grounded_data,
            "extracted_fields": extracted_fields,
            "tables": tables,
            "raw_markdown": raw_markdown[:5000],  # Truncate for storage
            "logs": logs,
        }

        try:
            artifact_path = await upload_artifact(
                user_id=task.user_id,
                task_id=task_id,
                data=json.dumps(artifact, indent=2, default=str).encode(),
            )
            logger.info("Artifact saved: %s", artifact_path)
        except Exception as exc:
            logger.warning("Artifact upload failed (non-critical): %s", exc)
            artifact_path = ""

        # 5. Push semantic chunks to orchestrator memory
        # Build page-level data for chunking
        page_dicts = []
        current_page: dict = {"page_number": 1, "sections": [], "raw_text": ""}
        for region in pages_data:
            page_dicts.append({
                "page_number": region.get("page", 1) if isinstance(region, dict) else 1,
                "sections": [region] if isinstance(region, dict) else [],
                "raw_text": region.get("content", "") if isinstance(region, dict) else "",
            })

        if not page_dicts and raw_markdown:
            page_dicts = [{"page_number": 1, "sections": [], "raw_text": raw_markdown}]

        chunks = chunk_document(page_dicts)

        async with httpx.AsyncClient(timeout=30.0) as client:
            for chunk in chunks:
                try:
                    await client.post(
                        f"{ORCHESTRATOR_URL}/memory/ingest",
                        json={
                            "user_id": task.user_id,
                            "domain": domain_key or "DOCUMENT_AGENT",
                            "content": chunk["content"],
                            "metadata": {
                                **chunk.get("metadata", {}),
                                "agent": "doc_vdu_v1",
                                "task_id": task_id,
                                "file_id": file_id,
                                "entity_type": entity_type,
                            },
                        },
                    )
                except Exception as exc:
                    logger.warning("Memory ingest failed for chunk: %s", exc)

        # 6. Callback to orchestrator with final result
        callback_result = {
            "entity_type": entity_type,
            "extracted_data": grounded_data,
            "field_count": len(extracted_fields),
            "table_count": len(tables),
            "page_count": len(result.get("page_images_b64", [])),
            "artifact_path": artifact_path,
            "audit_passed": result.get("audit_passed", False),
            "summary": _build_summary(entity_type, grounded_data, extracted_fields),
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"{ORCHESTRATOR_URL}/callbacks/task-completed",
                json={
                    "task_id": task_id,
                    "status": "success" if status != "error" else "failed",
                    "result": callback_result,
                    "error": result.get("error"),
                },
            )

        logger.info("Task %s completed and callback sent", task_id)

    except Exception as exc:
        error_msg = f"Document processing failed: {exc}"
        logger.error(error_msg, exc_info=True)

        # Send failure callback
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    f"{ORCHESTRATOR_URL}/callbacks/task-completed",
                    json={
                        "task_id": task_id,
                        "status": "failed",
                        "result": None,
                        "error": error_msg,
                    },
                )
        except Exception as cb_exc:
            logger.error("Failure callback also failed: %s", cb_exc)


def _build_summary(
    entity_type: str,
    grounded_data: dict,
    extracted_fields: list,
) -> str:
    """Build a human-readable summary of what was extracted."""
    parts = []

    if entity_type:
        parts.append(f"Extracted **{entity_type}** entity")

    if grounded_data:
        fields_preview = []
        for k, v in list(grounded_data.items())[:5]:
            val = str(v)[:50] if v else "(empty)"
            fields_preview.append(f"  • {k}: {val}")
        parts.append("Key fields:\n" + "\n".join(fields_preview))

    if len(grounded_data) > 5:
        parts.append(f"  ... and {len(grounded_data) - 5} more fields")

    low_conf = [
        f.get("field_name", "?")
        for f in extracted_fields
        if isinstance(f, dict) and f.get("confidence", 1.0) < 0.7
    ]
    if low_conf:
        parts.append(f"⚠ Low-confidence fields: {', '.join(low_conf)}")

    return "\n".join(parts) if parts else "Document processed successfully."


# ────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────

@app.post("/document/run", response_model=AgentResponse)
async def run_document(task: DocumentTask, bg: BackgroundTasks):
    """
    Accept a document processing task from the orchestrator.

    The actual processing happens in the background so the HTTP
    response returns immediately (matching the research agent pattern).
    """
    task_id = task.context_metadata.get("task_id", "unknown")

    if not pipeline:
        return AgentResponse(
            status="error",
            task_id=task_id,
            message="Pipeline not initialised.",
        )

    bg.add_task(execute_document_task, task)

    return AgentResponse(
        status="accepted",
        task_id=task_id,
        message=f"Document pipeline started (VLM: {VLM_MODEL}).",
    )


@app.get("/health")
async def health():
    """Liveness probe — checks VLM availability."""
    vlm_status = "unknown"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                model_names = [m.get("name", "") for m in models]
                vlm_status = "available" if any(
                    VLM_MODEL.split(":")[0] in n for n in model_names
                ) else f"model_not_found (have: {model_names[:5]})"
            else:
                vlm_status = f"ollama_error ({resp.status_code})"
    except Exception as exc:
        vlm_status = f"unreachable ({exc})"

    db_status = "unknown"
    try:
        from shared.database import db
        db_status = "connected" if (db.pool and not db.pool._closed) else "disconnected"
    except Exception:
        db_status = "not_configured"

    return {
        "status": "online",
        "engine": "VDU Pipeline + LangGraph",
        "vlm_model": VLM_MODEL,
        "vlm_status": vlm_status,
        "database": db_status,
    }
