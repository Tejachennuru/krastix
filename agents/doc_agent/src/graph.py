"""
Document Agent — LangGraph VDU Pipeline.

Orchestrates the four-stage Reflect-Refine loop:

    preprocess → extract → ground → audit
                   ↑                   │
                   └── (retry) ←───────┘

Uses LangGraph ``StateGraph`` for clear graph structure and
conditional retry edges, consistent with the wider Krastix architecture.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from langgraph.graph import StateGraph, END

from src.config import MAX_AUDIT_RETRIES
from src.models import PipelineState
from src.pipeline.preprocessing import pdf_to_images, image_to_b64
from src.pipeline.extraction import extract_all_pages
from src.pipeline.grounding import (
    fetch_entity_schema,
    map_fields_to_schema,
    validate_against_schema,
    chunk_document,
)
from src.pipeline.audit import audit_and_correct
from src.storage import detect_file_type

logger = logging.getLogger(__name__)


class DocumentPipeline:
    """
    LangGraph-powered multi-stage VDU pipeline.

    Nodes
    -----
    preprocess : Convert file bytes → page images.
    extract    : VLM extraction (full-page + schema-guided).
    ground     : Map to entity schema, validate, chunk for RAG.
    audit      : Vision-audit, foveal re-scan on discrepancies.
    finalize   : Prepare final output.
    """

    def __init__(self) -> None:
        self.workflow = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(PipelineState)

        builder.add_node("preprocess", self.node_preprocess)
        builder.add_node("extract", self.node_extract)
        builder.add_node("ground", self.node_ground)
        builder.add_node("audit", self.node_audit)
        builder.add_node("finalize", self.node_finalize)

        builder.set_entry_point("preprocess")
        builder.add_edge("preprocess", "extract")
        builder.add_edge("extract", "ground")
        builder.add_edge("ground", "audit")

        # Conditional: retry or finish
        builder.add_conditional_edges(
            "audit",
            self._should_retry,
            {"retry": "extract", "done": "finalize"},
        )
        builder.add_edge("finalize", END)

        return builder.compile()

    # ────────────────────────────────────────────────────────────────
    # Conditional Edge
    # ────────────────────────────────────────────────────────────────

    def _should_retry(self, state: PipelineState) -> str:
        if state.get("error"):
            return "done"
        if state.get("audit_passed", True):
            return "done"
        if state.get("attempt_count", 0) >= MAX_AUDIT_RETRIES:
            return "done"
        return "retry"

    # ────────────────────────────────────────────────────────────────
    # Node: Preprocess
    # ────────────────────────────────────────────────────────────────

    async def node_preprocess(self, state: PipelineState) -> Dict[str, Any]:
        """Convert raw file bytes to base64 page images."""
        logs = [f"[preprocess] Starting — file_type={state.get('file_type', 'unknown')}"]

        page_images = state.get("page_images_b64", [])
        if page_images:
            logs.append(f"[preprocess] Images already provided ({len(page_images)} pages)")
            return {"logs": logs}

        # This shouldn't happen (main.py sets images before invoking),
        # but handle gracefully
        return {
            "status": "error",
            "error": "No page images available. File preprocessing failed.",
            "logs": logs + ["[preprocess] ERROR: No images"],
        }

    # ────────────────────────────────────────────────────────────────
    # Node: Extract
    # ────────────────────────────────────────────────────────────────

    async def node_extract(self, state: PipelineState) -> Dict[str, Any]:
        attempt = state.get("attempt_count", 0) + 1
        logs = [f"[extract] Attempt {attempt}"]

        entity_type = state.get("entity_type", "")
        entity_schema = state.get("entity_schema", {})

        result = await extract_all_pages(
            pages_b64=state["page_images_b64"],
            entity_type=entity_type if entity_type else None,
            entity_schema=entity_schema if entity_schema else None,
        )

        pages = result.get("pages", [])
        fields = result.get("fields", {})
        raw_md = result.get("raw_markdown", "")

        # Collect tables from all pages
        all_tables = []
        for p in pages:
            all_tables.extend(p.get("tables", []))

        logs.append(
            f"[extract] Done — {len(pages)} pages, "
            f"{len(fields)} fields, {len(all_tables)} tables"
        )

        return {
            "extracted_fields": [
                {"field_name": k, **(v if isinstance(v, dict) else {"value": v})}
                for k, v in fields.items()
            ],
            "raw_markdown": raw_md,
            "tables": all_tables,
            "regions": [
                region
                for p in pages
                for region in p.get("sections", [])
            ],
            "attempt_count": attempt,
            "logs": logs,
        }

    # ────────────────────────────────────────────────────────────────
    # Node: Ground
    # ────────────────────────────────────────────────────────────────

    async def node_ground(self, state: PipelineState) -> Dict[str, Any]:
        logs = ["[ground] Mapping to entity schema"]

        entity_schema = state.get("entity_schema", {})
        raw_fields_list = state.get("extracted_fields", [])

        # Convert list-of-dicts → flat dict for mapping
        raw_fields: Dict[str, Any] = {}
        for f in raw_fields_list:
            name = f.get("field_name", "")
            if name:
                raw_fields[name] = f

        if entity_schema and entity_schema.get("properties"):
            grounded, extracted = map_fields_to_schema(raw_fields, entity_schema)
            is_valid, errors = validate_against_schema(grounded, entity_schema)

            if not is_valid:
                logs.append(f"[ground] Validation errors: {errors}")
            else:
                logs.append("[ground] Schema validation passed")

            return {
                "grounded_data": grounded,
                "extracted_fields": [ef.model_dump() for ef in extracted],
                "logs": logs,
            }

        # No schema — return raw fields as grounded data
        grounded = {
            f.get("field_name", f"field_{i}"): (
                f.get("value") if isinstance(f, dict) else f
            )
            for i, f in enumerate(raw_fields_list)
        }
        logs.append("[ground] No entity schema — using raw fields")
        return {"grounded_data": grounded, "logs": logs}

    # ────────────────────────────────────────────────────────────────
    # Node: Audit
    # ────────────────────────────────────────────────────────────────

    async def node_audit(self, state: PipelineState) -> Dict[str, Any]:
        logs = ["[audit] Running vision audit"]

        extracted = state.get("extracted_fields", [])
        page_images = state.get("page_images_b64", [])

        # Convert extracted list → dict for audit
        fields_dict: Dict[str, Any] = {}
        for f in extracted:
            name = f.get("field_name", "") if isinstance(f, dict) else ""
            if name:
                fields_dict[name] = f

        if not fields_dict:
            logs.append("[audit] No fields to audit — skipping")
            return {"audit_passed": True, "discrepancies": [], "logs": logs}

        result = await audit_and_correct(page_images, fields_dict)
        audit = result.get("audit", {})
        corrected = result.get("fields", fields_dict)

        passed = audit.get("passed", True)
        discs = audit.get("discrepancies", [])

        if passed:
            logs.append("[audit] All fields verified ✓")
        else:
            logs.append(f"[audit] {len(discs)} discrepancies found")

        # Update extracted fields with corrections
        corrected_list = [
            {"field_name": k, **(v if isinstance(v, dict) else {"value": v})}
            for k, v in corrected.items()
        ]

        # Update grounded_data with corrected values
        grounded = dict(state.get("grounded_data", {}))
        for k, v in corrected.items():
            val = v.get("value") if isinstance(v, dict) else v
            if val is not None:
                grounded[k] = val

        return {
            "audit_passed": passed,
            "discrepancies": discs,
            "extracted_fields": corrected_list,
            "grounded_data": grounded,
            "logs": logs,
        }

    # ────────────────────────────────────────────────────────────────
    # Node: Finalize
    # ────────────────────────────────────────────────────────────────

    async def node_finalize(self, state: PipelineState) -> Dict[str, Any]:
        logs = ["[finalize] Preparing output"]

        status = "success" if not state.get("error") else "error"
        if not state.get("audit_passed", True) and state.get("attempt_count", 0) >= MAX_AUDIT_RETRIES:
            status = "partial"
            logs.append("[finalize] Max retries reached — returning partial results")

        logs.append(f"[finalize] Status: {status}")
        return {"status": status, "logs": logs}

    # ────────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────────

    async def run(
        self,
        file_bytes: bytes,
        user_id: str,
        task_id: str,
        instruction: str,
        file_id: str = "",
        entity_type: str = "",
        domain_key: str = "",
    ) -> Dict[str, Any]:
        """
        Execute the full VDU pipeline on a document.

        Returns the final pipeline state as a dict.
        """
        # 1. Detect file type and convert to images
        file_type = detect_file_type(file_bytes)

        if file_type == "pdf":
            page_images = pdf_to_images(file_bytes)
        else:
            page_images = [image_to_b64(file_bytes)]

        logger.info(
            "Pipeline start — %d pages, entity=%s, task=%s",
            len(page_images), entity_type or "(none)", task_id,
        )

        # 2. Fetch entity schema if entity_type is provided
        entity_schema: Dict[str, Any] = {}
        if entity_type:
            schema = await fetch_entity_schema(entity_type)
            if schema:
                entity_schema = schema
                logger.info("Loaded schema for entity_type=%s", entity_type)
            else:
                logger.warning("No schema found for entity_type=%s", entity_type)

        # 3. Build initial state
        initial_state: PipelineState = {
            "page_images_b64": page_images,
            "file_type": file_type,
            "entity_type": entity_type,
            "entity_schema": entity_schema,
            "user_id": user_id,
            "task_id": task_id,
            "instruction": instruction,
            "file_id": file_id,
            "domain_key": domain_key,
            "regions": [],
            "extracted_fields": [],
            "raw_markdown": "",
            "tables": [],
            "grounded_data": {},
            "audit_passed": False,
            "discrepancies": [],
            "attempt_count": 0,
            "status": "processing",
            "error": "",
            "logs": [f"[init] Pipeline started for task {task_id}"],
        }

        # 4. Invoke the graph
        try:
            final_state = await self.workflow.ainvoke(initial_state)
        except Exception as exc:
            logger.error("Pipeline execution failed: %s", exc, exc_info=True)
            return {
                "status": "error",
                "error": str(exc),
                "grounded_data": {},
                "extracted_fields": [],
                "raw_markdown": "",
                "tables": [],
                "logs": [f"[error] Pipeline failed: {exc}"],
            }

        return dict(final_state)
