"""
Stage 2 — Multimodal Extraction (Qwen 2.5-VL / Qwen 3.0-VL).

Sends each page image to the VLM via Ollama for full-page extraction
and optional schema-guided field extraction.

Prompting Strategy
------------------
* **Full-page extraction**: Asks the VLM to return all content as
  structured JSON with sections, tables, and bounding boxes.
* **Schema-guided extraction**: Injects the ``entity_definitions``
  validation schema so the VLM targets specific fields with
  confidence scores and spatial grounding.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from src.config import OLLAMA_BASE_URL, VLM_MODEL

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# VLM Client (lazy singleton)
# ────────────────────────────────────────────────────────────────────

_vlm: Optional[ChatOllama] = None


def get_vlm() -> ChatOllama:
    """Return a shared ChatOllama instance for the vision-language model."""
    global _vlm
    if _vlm is None:
        logger.info("Initialising VLM: %s @ %s", VLM_MODEL, OLLAMA_BASE_URL)
        _vlm = ChatOllama(
            model=VLM_MODEL,
            base_url=OLLAMA_BASE_URL,
            temperature=0,
            timeout=180.0,
            # format="json",  # Enable if model supports constrained JSON
        )
    return _vlm


# ────────────────────────────────────────────────────────────────────
# Prompt Templates
# ────────────────────────────────────────────────────────────────────

FULL_EXTRACTION_PROMPT = """\
You are an expert document analysis system. Analyse the provided document page image
and extract ALL text and structural information.

Return a JSON object with this EXACT structure:
{{
  "sections": [
    {{
      "type": "<header|subheader|body|table|list|signature|footer>",
      "title": "<section title or empty string>",
      "content": "<full text content of this section>",
      "bbox": [<x%>, <y%>, <w%>, <h%>]
    }}
  ],
  "tables": [
    {{
      "headers": ["col1", "col2"],
      "rows": [["val1", "val2"]],
      "bbox": [<x%>, <y%>, <w%>, <h%>]
    }}
  ],
  "raw_text": "<complete page text in reading order>"
}}

Rules:
- bbox values are percentages (0-100) of the page dimensions.
- Preserve exact text — do NOT paraphrase or summarise.
- For tables, reconstruct all rows and columns accurately.
- Return ONLY valid JSON, no markdown fences.
"""

SCHEMA_EXTRACTION_PROMPT = """\
You are an expert document extraction system. Extract entity data from this
document page according to the schema below.

Entity Type: {entity_type}
Required Fields: {required_fields}
Schema:
{schema_json}

For EACH field in the schema, extract:
- value: the extracted value (use null if not found)
- confidence: your confidence score from 0.0 to 1.0
- bbox: approximate location as [x%, y%, w%, h%] (percentages of page size)

Return a JSON object with this EXACT structure:
{{
  "fields": {{
    "<field_name>": {{
      "value": <extracted_value>,
      "confidence": <0.0-1.0>,
      "bbox": [<x%>, <y%>, <w%>, <h%>]
    }}
  }}
}}

Rules:
- Return ONLY valid JSON, no markdown fences.
- If a field is not present in the document, set value to null and confidence to 0.0.
- Be precise with values — do NOT guess or fabricate data.
"""

REGION_EXTRACTION_PROMPT = """\
Extract the content from the highlighted region in this document image.
The region is located at approximately [{x}%, {y}%, {w}%, {h}%] from the
top-left corner of the page.

Region type: {region_type}

Return the content as clean, structured text.
If it's a table, format it as a Markdown table.
"""


# ────────────────────────────────────────────────────────────────────
# JSON Parsing Helpers
# ────────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Robustly extract a JSON object from VLM output.

    Handles markdown code fences, leading/trailing text, etc.
    """
    if not text:
        return None

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Find first { ... } block
    brace_match = re.search(r"\{[\s\S]*\}", text)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to extract JSON from VLM output (len=%d)", len(text))
    return None


# ────────────────────────────────────────────────────────────────────
# Extraction Functions
# ────────────────────────────────────────────────────────────────────

async def extract_page_full(page_b64: str, page_number: int) -> Dict[str, Any]:
    """
    Full-page extraction: sections, tables, raw text, bounding boxes.
    """
    vlm = get_vlm()

    message = HumanMessage(content=[
        {"type": "text", "text": FULL_EXTRACTION_PROMPT},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{page_b64}"},
        },
    ])

    try:
        response = await vlm.ainvoke([message])
        content = response.content if hasattr(response, "content") else str(response)
        parsed = _extract_json(content)

        if parsed:
            parsed["page_number"] = page_number
            return parsed

        # Fallback: return raw text as a single section
        return {
            "page_number": page_number,
            "sections": [
                {
                    "type": "body",
                    "title": "",
                    "content": content,
                    "bbox": [0, 0, 100, 100],
                }
            ],
            "tables": [],
            "raw_text": content,
        }

    except Exception as exc:
        logger.error("VLM extraction failed for page %d: %s", page_number, exc)
        return {
            "page_number": page_number,
            "sections": [],
            "tables": [],
            "raw_text": "",
            "error": str(exc),
        }


async def extract_page_with_schema(
    page_b64: str,
    page_number: int,
    entity_type: str,
    schema: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Schema-guided extraction: returns fields with confidence + bbox.
    """
    vlm = get_vlm()

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    prompt = SCHEMA_EXTRACTION_PROMPT.format(
        entity_type=entity_type,
        required_fields=", ".join(required) if required else "(none)",
        schema_json=json.dumps(schema, indent=2),
    )

    message = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{page_b64}"},
        },
    ])

    try:
        response = await vlm.ainvoke([message])
        content = response.content if hasattr(response, "content") else str(response)
        parsed = _extract_json(content)

        if parsed and "fields" in parsed:
            parsed["page_number"] = page_number
            return parsed

        # Try to treat the whole response as fields
        if parsed:
            return {"page_number": page_number, "fields": parsed}

        return {"page_number": page_number, "fields": {}, "error": "No fields extracted"}

    except Exception as exc:
        logger.error("Schema extraction failed for page %d: %s", page_number, exc)
        return {"page_number": page_number, "fields": {}, "error": str(exc)}


async def extract_region(
    page_b64: str,
    region: Dict[str, Any],
) -> str:
    """
    Spatial Prompting — extract content from a specific ROI.
    """
    vlm = get_vlm()
    bbox = region.get("bbox", [0, 0, 100, 100])

    prompt = REGION_EXTRACTION_PROMPT.format(
        x=bbox[0] if len(bbox) > 0 else 0,
        y=bbox[1] if len(bbox) > 1 else 0,
        w=bbox[2] if len(bbox) > 2 else 100,
        h=bbox[3] if len(bbox) > 3 else 100,
        region_type=region.get("region_type", "unknown"),
    )

    message = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{page_b64}"},
        },
    ])

    try:
        response = await vlm.ainvoke([message])
        return response.content if hasattr(response, "content") else str(response)
    except Exception as exc:
        logger.error("Region extraction failed: %s", exc)
        return ""


async def extract_all_pages(
    pages_b64: List[str],
    entity_type: Optional[str] = None,
    entity_schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Process all pages — returns a unified extraction result.

    If ``entity_type`` and ``entity_schema`` are provided, runs
    schema-guided extraction; otherwise does full-page extraction.
    """
    all_pages: List[Dict[str, Any]] = []
    all_fields: Dict[str, Any] = {}
    raw_parts: List[str] = []

    for idx, page_b64 in enumerate(pages_b64):
        page_num = idx + 1
        logger.info("Extracting page %d/%d", page_num, len(pages_b64))

        # Full-page extraction (always)
        page_result = await extract_page_full(page_b64, page_num)
        all_pages.append(page_result)
        raw_parts.append(page_result.get("raw_text", ""))

        # Schema extraction (optional, overlay)
        if entity_type and entity_schema:
            schema_result = await extract_page_with_schema(
                page_b64, page_num, entity_type, entity_schema
            )
            # Merge fields (later pages can fill gaps)
            for field_name, field_data in schema_result.get("fields", {}).items():
                existing = all_fields.get(field_name)
                if existing is None or (
                    isinstance(field_data, dict)
                    and field_data.get("confidence", 0) > existing.get("confidence", 0)
                ):
                    all_fields[field_name] = field_data

    return {
        "pages": all_pages,
        "fields": all_fields,
        "raw_markdown": "\n\n---\n\n".join(raw_parts),
        "page_count": len(pages_b64),
    }
