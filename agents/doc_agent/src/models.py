"""
Document Agent — Pydantic Models.

Covers the HTTP request/response contract, pipeline data structures,
and the visual-grounding primitives used throughout the VDU pipeline.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel, Field


# ────────────────────────────────────────────────────────────────────
# 1.  Visual Grounding Primitives
# ────────────────────────────────────────────────────────────────────

class BoundingBox(BaseModel):
    """Spatial anchor on a specific page (coordinates as % of page size)."""
    page: int = 0
    x: float = Field(ge=0.0, le=100.0, description="Left offset %")
    y: float = Field(ge=0.0, le=100.0, description="Top offset %")
    w: float = Field(ge=0.0, le=100.0, description="Width %")
    h: float = Field(ge=0.0, le=100.0, description="Height %")


class ExtractedField(BaseModel):
    """A single data point extracted from the document."""
    field_name: str
    value: Any = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    bbox: Optional[BoundingBox] = None


class DocumentRegion(BaseModel):
    """A region of interest detected during layout analysis."""
    region_type: str  # header, body, table, list, signature, image, footer
    bbox: BoundingBox
    summary: str = ""


class TableData(BaseModel):
    """A reconstructed table."""
    headers: List[str] = []
    rows: List[List[str]] = []
    bbox: Optional[BoundingBox] = None


class PageResult(BaseModel):
    """Extraction output for a single page."""
    page_number: int
    regions: List[DocumentRegion] = []
    raw_text: str = ""
    tables: List[TableData] = []
    sections: List[Dict[str, Any]] = []


# ────────────────────────────────────────────────────────────────────
# 2.  Pipeline Aggregates
# ────────────────────────────────────────────────────────────────────

class ExtractionResult(BaseModel):
    """Full extraction output across all pages."""
    fields: List[ExtractedField] = []
    pages: List[PageResult] = []
    chunks: List[str] = []
    raw_markdown: str = ""


class AuditDiscrepancy(BaseModel):
    field: str
    extracted_value: Any = None
    actual_value: Any = None
    bbox: Optional[BoundingBox] = None
    confidence: float = 0.0


class AuditResult(BaseModel):
    passed: bool = False
    verified_fields: List[str] = []
    discrepancies: List[AuditDiscrepancy] = []


# ────────────────────────────────────────────────────────────────────
# 3.  HTTP Contract
# ────────────────────────────────────────────────────────────────────

class DocumentTask(BaseModel):
    """Inbound task dispatched by the orchestrator."""
    user_id: str
    instruction: str
    file_id: Optional[str] = None
    entity_type: Optional[str] = None
    domain_key: Optional[str] = None
    context_metadata: Dict[str, str] = {}

    # Backward-compat fields (ignored but accepted so the unified
    # payload from the orchestrator doesn't cause validation errors).
    task_type: Optional[str] = None
    query_or_url: Optional[str] = None


class AgentResponse(BaseModel):
    status: str
    task_id: str
    message: str


# ────────────────────────────────────────────────────────────────────
# 4.  LangGraph Pipeline State
# ────────────────────────────────────────────────────────────────────

class PipelineState(TypedDict):
    # ── inputs (set once) ──
    page_images_b64: List[str]
    file_type: str                     # "pdf" | "image"
    entity_type: str
    entity_schema: Dict[str, Any]
    user_id: str
    task_id: str
    instruction: str
    file_id: str
    domain_key: str

    # ── accumulated outputs ──
    regions: List[Dict[str, Any]]
    extracted_fields: List[Dict[str, Any]]
    raw_markdown: str
    tables: List[Dict[str, Any]]
    grounded_data: Dict[str, Any]

    # ── audit ──
    audit_passed: bool
    discrepancies: List[Dict[str, Any]]

    # ── control ──
    attempt_count: int
    status: str
    error: str
    logs: Annotated[List[str], operator.add]
