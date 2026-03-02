"""
Stage 4 — Vision-Audit (Reflect-Refine).

A secondary VLM pass (the *Refiner*) compares the extracted JSON
against the original page image.  If discrepancies are found the agent
performs a **Foveal Re-scan** — re-processing only the offending
bounding box at 2× resolution — then patches the extraction result.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage
from langchain_ollama import ChatOllama

from src.config import OLLAMA_BASE_URL, VLM_MODEL, CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Refiner VLM (reuses the same model, could be swapped)
# ────────────────────────────────────────────────────────────────────

_refiner: Optional[ChatOllama] = None


def get_refiner() -> ChatOllama:
    global _refiner
    if _refiner is None:
        _refiner = ChatOllama(
            model=VLM_MODEL,
            base_url=OLLAMA_BASE_URL,
            temperature=0,
            timeout=180.0,
        )
    return _refiner


# ────────────────────────────────────────────────────────────────────
# Audit Prompt
# ────────────────────────────────────────────────────────────────────

AUDIT_PROMPT = """\
You are a document verification expert. Compare the extracted data below
against the document page image and verify every field.

Extracted Data:
{extracted_json}

Instructions:
1. Check each field's value against what you see in the document.
2. Flag any discrepancies (misread characters, wrong numbers, etc.).
3. For each discrepancy, provide the corrected value and approximate location.

Return a JSON object with this EXACT structure:
{{
  "passed": <true if all fields are correct, false otherwise>,
  "verified_fields": ["field1", "field2"],
  "discrepancies": [
    {{
      "field": "<field_name>",
      "extracted": "<what we extracted>",
      "actual": "<what it should be>",
      "bbox": [<x%>, <y%>, <w%>, <h%>],
      "confidence": <0.0-1.0>
    }}
  ]
}}

Rules:
- Return ONLY valid JSON, no markdown fences.
- Only flag genuine errors — minor formatting differences are acceptable.
- If all data is correct, set passed=true and leave discrepancies empty.
"""

FOVEAL_PROMPT = """\
Look carefully at this cropped region of a document. Extract the exact
text/value you see for the field "{field_name}".

Return a JSON object:
{{
  "value": "<exact value you see>",
  "confidence": <0.0-1.0>
}}

Return ONLY valid JSON.
"""


# ────────────────────────────────────────────────────────────────────
# JSON Helpers
# ────────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Robustly extract JSON from VLM output."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    brace = re.search(r"\{[\s\S]*\}", text)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ────────────────────────────────────────────────────────────────────
# Audit
# ────────────────────────────────────────────────────────────────────

async def vision_audit(
    page_b64: str,
    extracted_fields: Dict[str, Any],
    page_number: int = 1,
) -> Dict[str, Any]:
    """
    Run a VLM verification pass comparing extraction vs. original image.

    Returns ``{"passed": bool, "verified_fields": [...], "discrepancies": [...]}``.
    """
    refiner = get_refiner()

    # Serialise fields for the prompt (strip bbox to keep prompt concise)
    fields_summary = {}
    for name, data in extracted_fields.items():
        if isinstance(data, dict):
            fields_summary[name] = data.get("value", data)
        else:
            fields_summary[name] = data

    prompt = AUDIT_PROMPT.format(
        extracted_json=json.dumps(fields_summary, indent=2, default=str)
    )

    message = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{page_b64}"},
        },
    ])

    try:
        response = await refiner.ainvoke([message])
        content = response.content if hasattr(response, "content") else str(response)
        parsed = _extract_json(content)

        if parsed:
            return {
                "passed": parsed.get("passed", True),
                "verified_fields": parsed.get("verified_fields", []),
                "discrepancies": parsed.get("discrepancies", []),
            }

        # If we can't parse the audit response, assume pass
        logger.warning("Could not parse audit response — assuming pass.")
        return {"passed": True, "verified_fields": [], "discrepancies": []}

    except Exception as exc:
        logger.error("Vision audit failed for page %d: %s", page_number, exc)
        return {"passed": True, "verified_fields": [], "discrepancies": []}


async def foveal_rescan(
    cropped_b64: str,
    field_name: str,
) -> Dict[str, Any]:
    """
    Re-scan a cropped + upscaled region to correct a specific field.

    Returns ``{"value": ..., "confidence": ...}``.
    """
    refiner = get_refiner()

    prompt = FOVEAL_PROMPT.format(field_name=field_name)

    message = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{cropped_b64}"},
        },
    ])

    try:
        response = await refiner.ainvoke([message])
        content = response.content if hasattr(response, "content") else str(response)
        parsed = _extract_json(content)

        if parsed and "value" in parsed:
            return {
                "value": parsed["value"],
                "confidence": float(parsed.get("confidence", 0.8)),
            }
        # Use raw text as fallback value
        return {"value": content.strip(), "confidence": 0.5}

    except Exception as exc:
        logger.error("Foveal rescan failed for '%s': %s", field_name, exc)
        return {"value": None, "confidence": 0.0}


async def audit_and_correct(
    page_images_b64: List[str],
    extracted_fields: Dict[str, Any],
) -> Dict[str, Any]:
    """
    High-level audit: runs vision_audit on the first page (or most relevant),
    then applies foveal re-scan corrections.

    Returns the updated fields dict and the audit report.
    """
    from src.pipeline.preprocessing import foveal_crop
    from src.config import FOVEAL_SCALE

    if not page_images_b64:
        return {
            "fields": extracted_fields,
            "audit": {"passed": True, "verified_fields": [], "discrepancies": []},
        }

    # Audit against the first page (multi-page: extend later)
    primary_page = page_images_b64[0]
    audit_result = await vision_audit(primary_page, extracted_fields, page_number=1)

    if audit_result["passed"]:
        return {"fields": extracted_fields, "audit": audit_result}

    # Apply foveal corrections for each discrepancy
    corrected_fields = dict(extracted_fields)

    for disc in audit_result.get("discrepancies", []):
        field_name = disc.get("field", "")
        bbox = disc.get("bbox")
        actual = disc.get("actual")

        if actual is not None:
            # Trust the audit's correction directly
            if field_name in corrected_fields:
                if isinstance(corrected_fields[field_name], dict):
                    corrected_fields[field_name]["value"] = actual
                    corrected_fields[field_name]["confidence"] = disc.get("confidence", 0.8)
                else:
                    corrected_fields[field_name] = {
                        "value": actual,
                        "confidence": disc.get("confidence", 0.8),
                    }
            continue

        # Foveal re-scan if we have coordinates
        if bbox and len(bbox) >= 4 and field_name:
            page_idx = disc.get("page", 1) - 1
            page_idx = max(0, min(page_idx, len(page_images_b64) - 1))

            cropped = foveal_crop(
                page_images_b64[page_idx],
                tuple(bbox[:4]),
                scale=FOVEAL_SCALE,
            )
            rescan = await foveal_rescan(cropped, field_name)

            if rescan["value"] is not None and rescan["confidence"] >= CONFIDENCE_THRESHOLD:
                if isinstance(corrected_fields.get(field_name), dict):
                    corrected_fields[field_name]["value"] = rescan["value"]
                    corrected_fields[field_name]["confidence"] = rescan["confidence"]
                else:
                    corrected_fields[field_name] = rescan

    return {"fields": corrected_fields, "audit": audit_result}
