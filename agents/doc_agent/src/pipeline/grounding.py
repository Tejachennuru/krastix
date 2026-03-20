"""
Stage 3 — Semantic Grounding & Pydantic / JSON-Schema Mapping.

Maps raw VLM extraction output onto the ``entity_definitions``
validation schema.  Every extracted field is returned with a
``bbox`` and ``confidence_score`` for visual grounding.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

from src.models import BoundingBox, ExtractedField

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Schema Fetching (direct DB query)
# ────────────────────────────────────────────────────────────────────

async def fetch_entity_schema(entity_type: str) -> Optional[Dict[str, Any]]:
    """
    Fetch ``validation_schema`` from the ``entity_definitions`` table.

    Uses the shared DB pool (initialised at FastAPI startup).
    """
    from shared.database import db  # Lazy import to avoid circular deps

    if not db.pool:
        logger.warning("Database pool not available — cannot fetch schema.")
        return None

    try:
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT validation_schema FROM entity_definitions "
                "WHERE entity_type = $1",
                entity_type,
            )
            if not row:
                return None
            schema = row["validation_schema"]
            if isinstance(schema, str):
                return json.loads(schema)
            return dict(schema) if schema else None
    except Exception as exc:
        logger.error("Failed to fetch entity schema for '%s': %s", entity_type, exc)
        return None


# ────────────────────────────────────────────────────────────────────
# Field Mapping
# ────────────────────────────────────────────────────────────────────

def map_fields_to_schema(
    raw_fields: Dict[str, Any],
    schema: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[ExtractedField]]:
    """
    Map raw VLM-extracted fields to the entity schema.

    Returns:
        grounded_data: A flat dict matching the schema's ``properties``
            (ready to be stored in ``entities.data``).
        extracted_fields: A list of ``ExtractedField`` with visual
            grounding (bbox + confidence).
    """
    properties = schema.get("properties", {})
    grounded: Dict[str, Any] = {}
    extracted_list: List[ExtractedField] = []

    for prop_name, prop_spec in properties.items():
        field_data = raw_fields.get(prop_name)

        if field_data is None:
            # Try case-insensitive / fuzzy match
            for k, v in raw_fields.items():
                if k.lower().replace(" ", "_") == prop_name.lower():
                    field_data = v
                    break

        if field_data is None:
            extracted_list.append(
                ExtractedField(
                    field_name=prop_name,
                    value=None,
                    confidence=0.0,
                    bbox=None,
                )
            )
            continue

        # Handle both raw values and structured {value, confidence, bbox}
        if isinstance(field_data, dict) and "value" in field_data:
            value = field_data["value"]
            confidence = float(field_data.get("confidence", 0.5))
            bbox_raw = field_data.get("bbox")
        else:
            value = field_data
            confidence = 0.7  # Default confidence for un-scored fields
            bbox_raw = None

        # Coerce to schema type
        value = _coerce_value(value, prop_spec)

        grounded[prop_name] = value

        # Build bbox
        bbox = None
        if bbox_raw and isinstance(bbox_raw, (list, tuple)) and len(bbox_raw) >= 4:
            bbox = BoundingBox(
                page=field_data.get("page", 0) if isinstance(field_data, dict) else 0,
                x=float(bbox_raw[0]),
                y=float(bbox_raw[1]),
                w=float(bbox_raw[2]),
                h=float(bbox_raw[3]),
            )

        extracted_list.append(
            ExtractedField(
                field_name=prop_name,
                value=value,
                confidence=confidence,
                bbox=bbox,
            )
        )

    return grounded, extracted_list


def _coerce_value(value: Any, prop_spec: Dict[str, Any]) -> Any:
    """Best-effort type coercion to match the JSON Schema type."""
    expected_type = prop_spec.get("type", "string")

    if value is None:
        return None

    try:
        if expected_type == "number":
            return float(value) if not isinstance(value, (int, float)) else value
        if expected_type == "integer":
            return int(value) if not isinstance(value, int) else value
        if expected_type == "boolean":
            if isinstance(value, str):
                return value.lower() in ("true", "yes", "1")
            return bool(value)
        if expected_type == "array":
            if isinstance(value, str):
                # Try JSON parse, then comma-split
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        return parsed
                except json.JSONDecodeError:
                    return [v.strip() for v in value.split(",") if v.strip()]
            return value if isinstance(value, list) else [value]
        # Default to string
        return str(value) if not isinstance(value, str) else value
    except (ValueError, TypeError):
        return value


# ────────────────────────────────────────────────────────────────────
# Validation
# ────────────────────────────────────────────────────────────────────

def validate_against_schema(
    data: Dict[str, Any],
    schema: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    Validate ``data`` against a JSON Schema.

    Returns ``(is_valid, list_of_error_messages)``.
    """
    errors: List[str] = []
    try:
        jsonschema.validate(instance=data, schema=schema)
        return True, []
    except jsonschema.ValidationError as exc:
        errors.append(str(exc.message))
    except jsonschema.SchemaError as exc:
        errors.append(f"Schema error: {exc.message}")
    return False, errors


# ────────────────────────────────────────────────────────────────────
# Hierarchical Chunking (for RAG)
# ────────────────────────────────────────────────────────────────────

def chunk_document(
    pages: List[Dict[str, Any]],
    chunk_size: int = 1500,
) -> List[Dict[str, Any]]:
    """
    Segment extracted pages into semantic chunks for memory storage.

    Each chunk carries metadata (page, section_type, bbox) for
    targeted RAG retrieval and "click-to-source" in the UI.
    """
    chunks: List[Dict[str, Any]] = []

    for page in pages:
        page_num = page.get("page_number", 0)
        sections = page.get("sections", [])

        if not sections:
            # Whole-page fallback
            raw = page.get("raw_text", "")
            if raw:
                for i in range(0, len(raw), chunk_size):
                    chunks.append({
                        "content": f"[Page {page_num}]\n{raw[i:i + chunk_size]}",
                        "metadata": {
                            "page": page_num,
                            "section_type": "full_page",
                            "chunk_index": len(chunks),
                        },
                    })
            continue

        for section in sections:
            sec_type = section.get("type", "body")
            title = section.get("title", "")
            content = section.get("content", "")
            bbox = section.get("bbox")

            header = f"[Page {page_num} | {sec_type}]"
            if title:
                header += f" {title}"

            text = f"{header}\n{content}"

            # Split long sections
            for i in range(0, max(len(text), 1), chunk_size):
                chunk_text = text[i : i + chunk_size]
                chunks.append({
                    "content": chunk_text,
                    "metadata": {
                        "page": page_num,
                        "section_type": sec_type,
                        "title": title,
                        "bbox": bbox,
                        "chunk_index": len(chunks),
                    },
                })

    return chunks
