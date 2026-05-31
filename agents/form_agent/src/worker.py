import os
import logging
import asyncio
from uuid import UUID
import json
import re
from urllib.parse import urlparse
from typing import Optional
from celery import Task
import aiohttp

from shared.mq import celery_app
from shared.database import db
from shared.callbacks import notify_task_completed
from shared.integrations_crypto import decrypt_secret

logger = logging.getLogger(__name__)

class FormWorker:
    """Agent for creating and managing Tally forms"""
    
    def __init__(self):
        self.name = "FormAgent"
        self.tally_api_url = "https://api.tally.so"

    async def _ensure_entity_definitions(self):
        """Ensure entity definitions used by form/applicant sync exist."""
        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO entity_definitions (entity_type, description, validation_schema)
                VALUES (
                    'tally_form',
                    'A Tally form synced from integration',
                    $1::jsonb
                )
                ON CONFLICT (entity_type) DO NOTHING
                """,
                json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "provider": {"type": "string"},
                            "form_id": {"type": "string"},
                            "form_url": {"type": "string"},
                            "title": {"type": "string"},
                            "status": {"type": "string"},
                        },
                        "required": ["provider", "form_id"],
                    }
                ),
            )
            await conn.execute(
                """
                INSERT INTO entity_definitions (entity_type, description, validation_schema)
                VALUES (
                    'applicant_submission',
                    'A form submission synced from Tally',
                    $1::jsonb
                )
                ON CONFLICT (entity_type) DO NOTHING
                """,
                json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "provider": {"type": "string"},
                            "source_form_id": {"type": "string"},
                            "response_id": {"type": "string"},
                            "submitted_at": {"type": "string"},
                            "raw": {"type": "object"},
                        },
                        "required": ["provider", "source_form_id", "response_id"],
                    }
                ),
            )

    async def _upsert_form_entity(self, user_id: UUID, form_id: str, form_url: str, title: str = "", status: str = ""):
        async with db.pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT id FROM entities
                WHERE user_id = $1 AND entity_type = 'tally_form'
                  AND data->>'form_id' = $2
                LIMIT 1
                """,
                user_id,
                form_id,
            )

            payload = {
                "provider": "tally",
                "form_id": form_id,
                "form_url": form_url,
                "title": title,
                "status": status,
            }

            if existing:
                await conn.execute(
                    """
                    UPDATE entities
                    SET display_name = COALESCE(NULLIF($1, ''), display_name),
                        status = COALESCE(NULLIF($2, ''), status),
                        data = data || $3::jsonb,
                        updated_at = NOW(),
                        version = version + 1
                    WHERE id = $4
                    """,
                    title,
                    status,
                    json.dumps(payload),
                    existing["id"],
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO entities (user_id, entity_type, display_name, status, data)
                    VALUES ($1, 'tally_form', $2, $3, $4::jsonb)
                    """,
                    user_id,
                    title or f"Tally Form {form_id}",
                    status or "active",
                    json.dumps(payload),
                )

    async def _upsert_submission_entities(self, user_id: UUID, form_id: str, applicants: list):
        async with db.pool.acquire() as conn:
            for idx, item in enumerate(applicants):
                if not isinstance(item, dict):
                    continue
                response_id = str(
                    item.get("id")
                    or item.get("responseId")
                    or item.get("submissionId")
                    or f"{form_id}-{idx}"
                )
                submitted_at = item.get("submittedAt") or item.get("createdAt") or ""

                existing = await conn.fetchrow(
                    """
                    SELECT id FROM entities
                    WHERE user_id = $1
                      AND entity_type = 'applicant_submission'
                      AND data->>'source_form_id' = $2
                      AND data->>'response_id' = $3
                    LIMIT 1
                    """,
                    user_id,
                    form_id,
                    response_id,
                )

                payload = {
                    "provider": "tally",
                    "source_form_id": form_id,
                    "response_id": response_id,
                    "submitted_at": submitted_at,
                    "raw": item,
                }

                display_name = f"Applicant Submission {response_id[:8]}"

                if existing:
                    await conn.execute(
                        """
                        UPDATE entities
                        SET data = data || $1::jsonb,
                            updated_at = NOW(),
                            version = version + 1
                        WHERE id = $2
                        """,
                        json.dumps(payload),
                        existing["id"],
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO entities (user_id, entity_type, display_name, status, data)
                        VALUES ($1, 'applicant_submission', $2, 'new', $3::jsonb)
                        """,
                        user_id,
                        display_name,
                        json.dumps(payload),
                    )

    def _extract_form_id(self, form_url_or_id: str) -> str:
        """Extract a Tally form id/slug from a full URL or return the raw id."""
        if not form_url_or_id:
            return ""

        value = str(form_url_or_id).strip()
        if value.startswith("http://") or value.startswith("https://"):
            parsed = urlparse(value)
            path_parts = [p for p in parsed.path.split("/") if p]
            if not path_parts:
                return ""
            # Public share links are often /r/<id>, edit links may contain /forms/<id>/edit
            if len(path_parts) >= 2 and path_parts[0] in ("r", "forms"):
                return path_parts[1]
            return path_parts[-1]
        return value

    def _infer_form_title_from_instruction(self, instruction: str) -> str:
        """Infer a stable, intent-driven form title when planner omits form_name."""
        text = re.sub(r"\s+", " ", (instruction or "").strip())
        if not text:
            return "Generated Form"

        explicit_patterns = [
            r"(?:named|called|titled)\s+[\"']([^\"']+)[\"']",
            r"(?:form|survey|questionnaire|quiz|checklist)\s+(?:named|called|titled)\s+([^.,;]+)",
            r"title\s*(?:it|as)?\s*[\"']?([^\"'.,;]+)",
        ]
        for pattern in explicit_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m and m.group(1).strip():
                return self._humanize_title(m.group(1).strip())

        descriptor_patterns = [
            r"(?:create|build|generate|design|draft|make)\s+(?:a|an)?\s*([^.,;]+?)\s+(?:form|survey|questionnaire|quiz|checklist)",
            r"(?:form|survey|questionnaire|quiz|checklist)\s+for\s+([^.,;]+)",
        ]
        for pattern in descriptor_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m and m.group(1).strip():
                descriptor = m.group(1).strip(" .:-")
                return self._humanize_title(f"{descriptor} Form")

        return "Generated Form"

    def _humanize_title(self, value: str) -> str:
        """Normalize capitalization while preserving common all-caps acronyms."""
        words = re.split(r"\s+", value.strip())
        out = []
        for word in words:
            clean = word.strip()
            if not clean:
                continue
            if clean.isupper() and len(clean) <= 5:
                out.append(clean)
            else:
                out.append(clean[:1].upper() + clean[1:])
        title = " ".join(out).strip()
        if title and not re.search(r"\b(form|survey|questionnaire|quiz|checklist)\b", title, re.IGNORECASE):
            title = f"{title} Form"
        return title or "Generated Form"

    def _normalize_choice_options(self, field: dict) -> list:
        """Normalize choice options for dropdown/choice-like fields."""
        raw = field.get("options")
        if not isinstance(raw, list):
            return []

        out = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                label = item.get("label") or item.get("value") or item.get("text")
                if isinstance(label, str) and label.strip():
                    out.append(label.strip())
        return out

    def _infer_choice_options_from_instruction(self, instruction: str) -> list:
        """Extract list options from natural language like 'options: A, B, C' or 'options A B C'."""
        text = (instruction or "").strip()
        if not text:
            return []

        m = re.search(
            r"(?:options?|choices?|values?)\s*(?::|-)?\s*(.+?)(?:[.?!]|$)",
            text,
            re.IGNORECASE,
        )
        if not m:
            return []

        raw = m.group(1).strip()
        if not raw:
            return []

        if re.search(r",|/|\||\band\b", raw, re.IGNORECASE):
            parts = re.split(r",|/|\||\band\b", raw, flags=re.IGNORECASE)
        else:
            parts = raw.split()

        options = []
        seen = set()
        for part in parts:
            item = part.strip(" .;:-")
            if not item:
                continue
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            options.append(item)
        return options

    async def _resolve_form_reference(self, user_id: UUID, params: dict, instruction: str) -> str:
        """
        Resolve form reference from parameters, URL in instruction, or latest synced form.
        This supports prompts like 'get applicants for this form' after a recent form creation.
        """
        raw_form_ref = params.get("form_id") or params.get("form_url") or ""
        if not raw_form_ref and instruction:
            match = re.search(r"https?://tally\.so/r/[A-Za-z0-9]+", instruction)
            if match:
                raw_form_ref = match.group(0)

        form_id = self._extract_form_id(raw_form_ref)
        if form_id:
            return form_id

        text = (instruction or "").lower()
        deictic_ref = any(p in text for p in ["this form", "that form", "latest form", "recent form"])
        if not deictic_ref:
            return ""

        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT data->>'form_id' AS form_id
                FROM entities
                WHERE user_id = $1 AND entity_type = 'tally_form'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                user_id,
            )
        if row and row["form_id"]:
            return row["form_id"]
        return ""

    def _infer_action(self, instruction: str, parameters: dict, explicit_action: str) -> str:
        """
        Infer action when planner omits task_action.
        Keeps backwards compatibility and avoids accidental create_form for applicant queries.
        """
        text = (instruction or "").lower()
        params = parameters if isinstance(parameters, dict) else {}

        if explicit_action:
            # Safety guard: if planner asks for list_form_responses but provides no form reference,
            # and user intent is clearly creating a form, fallback to create_form.
            if explicit_action == "list_form_responses":
                has_form_ref = bool(params.get("form_id") or params.get("form_url")) or ("tally.so/r/" in text)
                create_intent = ("create" in text and "form" in text) or bool(params.get("blocks") or params.get("fields"))
                if not has_form_ref and create_intent:
                    return "create_form"
            return explicit_action

        if params.get("form_id") or params.get("form_url"):
            return "list_form_responses"

        if any(k in text for k in ["applicant", "applicants", "response", "responses", "submission", "submissions", "entries"]):
            if (
                "tally.so" in text
                or re.search(r"\br/[A-Za-z0-9]+", text)
                or any(p in text for p in ["this form", "that form", "latest form", "recent form"])
            ):
                return "list_form_responses"

        if "list forms" in text or "show forms" in text or "my forms" in text:
            return "list_forms"

        return "create_form"
        
    async def execute(self, task_id: str) -> dict:
        """Execute form task"""
        # Connect fresh for each task — each Celery task runs in a new event loop
        # via asyncio.run(), so we must never reuse a pool from a previous loop.
        await db.connect()
        
        try:
            task = await db.pool.fetchrow(
                "SELECT * FROM agent_tasks WHERE task_id = $1",
                UUID(task_id)
            )
            
            if not task:
                return {"error": "Task not found"}

            await db.mark_task_running(str(task_id))
                
            input_payload = task["input_payload"]
            if isinstance(input_payload, str):
                try:
                    input_payload = json.loads(input_payload)
                except json.JSONDecodeError:
                    pass
            
            raw_task_action = input_payload.get("task_action", "")
            instruction = input_payload.get("instruction", "")
            parameters = input_payload.get("parameters", [])

            # ── Normalize parameters ──────────────────────────────────────────
            # The orchestrator may send parameters in several shapes:
            #
            # Shape A — flat list (simple format):
            #   parameters = [{"type": "TEXT", "label": "Name", "value": null}, ...]
            #
            # Shape B — dict with "blocks" + optional "schema" + "form_name":
            #   parameters = {
            #     "blocks":    [{"type": "INPUT_TEXT", "label": "Why interested?"}, ...],
            #     "schema":    {"job_title": "...", "job_details": "...", "company_name": "..."},
            #     "form_name": "Software Engineer Application - The TechX"
            #   }
            #
            # Shape C — dict with "fields" key (legacy):
            #   parameters = {"fields": [...], "title": "..."}
            #
            # We unify all shapes into (form_title, field_list, schema_info).

            schema_info = {}

            if isinstance(parameters, list):
                # Shape A
                field_list = parameters
                form_title = self._infer_form_title_from_instruction(instruction)

            elif isinstance(parameters, dict):
                # Extract schema metadata (Shape B)
                schema_info = parameters.get("schema", {})

                # Try "blocks" first (Shape B), then "fields" (Shape C)
                field_list = parameters.get("blocks") or parameters.get("fields") or []

                # Title priority: "form_name" → "title" → schema job_title fallback
                form_title = (
                    parameters.get("form_name")
                    or parameters.get("title")
                    or (
                        f"{schema_info.get('job_title', 'Job')} Application"
                        if schema_info.get("job_title") else "Job Application Form"
                    )
                )
                if form_title == "Job Application Form":
                    form_title = self._infer_form_title_from_instruction(instruction)
            else:
                field_list = []
                form_title = self._infer_form_title_from_instruction(instruction)
            # ─────────────────────────────────────────────────────────────────

            task_action = self._infer_action(instruction, parameters if isinstance(parameters, dict) else {}, raw_task_action)

            logger.info(
                "Task %s: action=%s, form_title=%s, field_count=%d, schema=%s",
                task_id, task_action, form_title, len(field_list), schema_info
            )

            user_id = task["user_id"]
            
            token = await self.get_tally_token(user_id)
            if not token:
                result = {
                    "status": "failed", 
                    "error": "Integration Required: Please connect your Tally Forms account in the 'App Integrations' menu on the sidebar before creating forms."
                }
            else:
                if task_action == "create_form":
                    result = await self.create_form(
                        token, form_title, instruction, field_list, schema_info
                    )
                    if result.get("status") == "success":
                        try:
                            await self._ensure_entity_definitions()
                            data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
                            form_url = data.get("form_url", "")
                            form_id = self._extract_form_id(form_url)
                            await self._upsert_form_entity(
                                user_id=user_id,
                                form_id=form_id,
                                form_url=form_url,
                                title=data.get("form_title", form_title),
                                status="active",
                            )
                        except Exception as persist_error:
                            logger.warning("Form entity sync failed for task %s: %s", task_id, persist_error)
                elif task_action == "list_forms":
                    result = await self.list_forms(token)
                elif task_action == "list_form_responses":
                    result = await self.list_form_responses(
                        token,
                        user_id,
                        parameters if isinstance(parameters, dict) else {},
                        instruction,
                    )
                    if result.get("status") == "success":
                        try:
                            await self._ensure_entity_definitions()
                            data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
                            form_id = data.get("form_id", "")
                            form_url = data.get("form_url", "")
                            await self._upsert_form_entity(
                                user_id=user_id,
                                form_id=form_id,
                                form_url=form_url,
                                title=data.get("form_title", ""),
                                status="active",
                            )
                            await self._upsert_submission_entities(
                                user_id=user_id,
                                form_id=form_id,
                                applicants=data.get("applicants", []),
                            )
                        except Exception as persist_error:
                            logger.warning("Entity sync failed for task %s: %s", task_id, persist_error)
                else:
                    result = {
                        "error": f"Unknown task action: {task_action}",
                        "status": "failed"
                    }

            final_status = "completed" if result.get("status") != "failed" else "failed"
            await db.update_task_status(
                task_id=str(task_id),
                status=final_status,
                result=result
            )
            
            await notify_task_completed(
                task_id=str(task_id),
                status=final_status,
                result=result,
                error=result.get("error"),
            )
            
            return result
            
        except Exception as e:
            logger.exception("FormWorker.execute failed for task %s", task_id)
            try:
                await db.update_task_status(
                    task_id=str(task_id),
                    status="failed",
                    result=None,
                    error=str(e)
                )
                await notify_task_completed(
                    task_id=str(task_id),
                    status="failed",
                    error=str(e),
                )
            except Exception as inner_e:
                logger.error("Failed to update task status after error: %s", inner_e)
            return {"error": str(e), "status": "failed"}
        
        finally:
            # Always disconnect so the pool doesn't leak into the next task's event loop
            try:
                await db.disconnect()
            except Exception as e:
                logger.warning("Failed to disconnect db after task %s: %s", task_id, e)

    async def get_tally_token(self, user_id: UUID) -> str:
        """Retrieve Tally access token for the user"""
        row = await db.pool.fetchrow(
            "SELECT access_token FROM integrations WHERE user_id = $1 AND provider = 'tally'",
            user_id
        )
        return decrypt_secret(row['access_token']) if row else None

    # ── Tally block type map ──────────────────────────────────────────────────
    # Maps every type the orchestrator may send → valid Tally API block type.
    TALLY_TYPE_MAP = {
        # Text inputs
        "TEXT":               "INPUT_TEXT",
        "INPUT_TEXT":         "INPUT_TEXT",
        "SHORT_TEXT":         "INPUT_TEXT",
        "INPUT_SHORT_TEXT":   "INPUT_TEXT",

        # Long text
        "TEXTAREA":           "TEXTAREA",
        "LONG_TEXT":          "TEXTAREA",
        "INPUT_LONG_TEXT":    "TEXTAREA",

        # Email
        "EMAIL":              "INPUT_EMAIL",
        "INPUT_EMAIL":        "INPUT_EMAIL",

        # Phone
        "PHONE":              "INPUT_PHONE_NUMBER",
        "INPUT_PHONE_NUMBER": "INPUT_PHONE_NUMBER",

        # URL / Link
        "URL":                "INPUT_LINK",
        "LINK":               "INPUT_LINK",
        "URL_INPUT":          "INPUT_LINK",     # ← orchestrator sends this
        "INPUT_LINK":         "INPUT_LINK",

        # Number
        "NUMBER":             "INPUT_NUMBER",
        "INPUT_NUMBER":       "INPUT_NUMBER",

        # File upload
        "FILE":               "FILE_UPLOAD",
        "FILE_UPLOAD":        "FILE_UPLOAD",

        # Multiple choice / checkbox / dropdown
        "MULTIPLE_CHOICE":    "MULTIPLE_CHOICE",
        "CHECKBOXES":         "CHECKBOXES",
        "DROPDOWN":           "DROPDOWN",

        # Rating / scale
        "RATING":             "RATING",
        "LINEAR_SCALE":       "LINEAR_SCALE",

        # Date / time
        "DATE":               "INPUT_DATE",
        "INPUT_DATE":         "INPUT_DATE",
        "TIME":               "INPUT_TIME",
        "INPUT_TIME":         "INPUT_TIME",

        # Display-only / structural — NOT interactive questions
        # We use sentinel strings prefixed with "__" to handle these specially
        "HEADER":             "__DISPLAY_HEADING__",   # ← orchestrator sends this
        "HEADING":            "__DISPLAY_HEADING__",
        "PARAGRAPH":          "__DISPLAY_TEXT__",
        "DIVIDER":            "__DISPLAY_DIVIDER__",
        "STATEMENT":          "STATEMENT",
    }

    def _make_question_blocks(
        self, label: str, tally_type: str,
        is_required: bool = False, placeholder: str = "", extra_payload: Optional[dict] = None
    ):
        """
        Build a Tally question pair: TITLE block + input block sharing a groupUuid.
        This is what the Tally API actually validates to render a question.
        """
        import uuid as _uuid
        group_id = str(_uuid.uuid4())
        blocks = [
            {
                "uuid": str(_uuid.uuid4()),
                "groupUuid": group_id,
                "groupType": "QUESTION",
                "type": "TITLE",
                "payload": {"html": f"<p>{label}</p>"}
            },
            {
                "uuid": str(_uuid.uuid4()),
                "groupUuid": group_id,
                "groupType": tally_type,
                "type": tally_type,
                "payload": {"isRequired": is_required}
            }
        ]
        if placeholder:
            blocks[1]["payload"]["placeholder"] = placeholder
        if extra_payload and isinstance(extra_payload, dict):
            blocks[1]["payload"].update(extra_payload)
        return blocks

    def _make_display_block(self, html_content: str, block_type: str = "TEXT"):
        """Build a display-only (non-question) Tally block."""
        import uuid as _uuid
        group_id = str(_uuid.uuid4())
        return {
            "uuid": str(_uuid.uuid4()),
            "groupUuid": group_id,
            "groupType": block_type,
            "type": block_type,
            "payload": {"html": html_content}
        }

    def _build_tally_blocks(
        self, form_title: str, instruction: str,
        fields: list, schema_info: dict
    ) -> list:
        """
        Convert the orchestrator's planned field list + schema metadata
        into a valid Tally API blocks array.

        Block construction order:
          1. FORM_TITLE  — always first
          2. Schema info — job details banner (company, role, conditions)
          3. Static value fields — fields with a pre-filled "value" (display-only paragraphs)
          4. Question fields    — fields without a value → interactive questions
        """
        import uuid as _uuid
        blocks = []

        # 1. Form title block (required by Tally, always first)
        title_group = str(_uuid.uuid4())
        blocks.append({
            "uuid": str(_uuid.uuid4()),
            "groupUuid": title_group,
            "groupType": "FORM_TITLE",
            "type": "FORM_TITLE",
            "payload": {"html": f"<p>{form_title}</p>"}
        })

        # 2. Schema info banner — renders company/role/job-details as a styled paragraph
        if schema_info:
            parts = []
            if schema_info.get("company_name"):
                parts.append(f"<strong>Company:</strong> {schema_info['company_name']}")
            if schema_info.get("job_title"):
                parts.append(f"<strong>Role:</strong> {schema_info['job_title']}")
            if schema_info.get("job_details"):
                parts.append(f"<strong>Details:</strong> {schema_info['job_details']}")
            if parts:
                banner_html = "<p>" + " &nbsp;|&nbsp; ".join(parts) + "</p>"
                blocks.append(self._make_display_block(banner_html, "TEXT"))

        # 3. Static "value" fields — display-only info blocks
        for field in fields:
            if not isinstance(field, dict):
                continue
            value = field.get("value")
            label = field.get("label", "")
            if value:
                blocks.append(self._make_display_block(
                    f"<p><strong>{label}:</strong> {value}</p>", "TEXT"
                ))

        # 4. Question fields (no pre-filled value → interactive input)
        for field in fields:
            if not isinstance(field, dict):
                continue

            # Skip static display fields already handled above
            if field.get("value"):
                continue

            label       = field.get("label", "Question")
            raw_type    = field.get("type", "TEXT").upper()
            tally_type  = self.TALLY_TYPE_MAP.get(raw_type, "INPUT_TEXT")
            is_required = field.get("required", True)
            placeholder = field.get("placeholder", "")
            options     = self._normalize_choice_options(field)
            if not options and tally_type in {"DROPDOWN", "MULTIPLE_CHOICE", "CHECKBOXES", "MULTI_SELECT", "RANKING"}:
                options = self._infer_choice_options_from_instruction(instruction)
            extra_payload = {}
            if options and tally_type in {"DROPDOWN", "MULTIPLE_CHOICE", "CHECKBOXES", "MULTI_SELECT", "RANKING"}:
                extra_payload["options"] = [{"label": opt, "value": opt} for opt in options]

            # Handle display-only sentinel types
            if tally_type == "__DISPLAY_HEADING__":
                blocks.append(self._make_display_block(f"<h2>{label}</h2>", "TEXT"))
                continue

            if tally_type == "__DISPLAY_TEXT__":
                blocks.append(self._make_display_block(f"<p>{label}</p>", "TEXT"))
                continue

            if tally_type == "__DISPLAY_DIVIDER__":
                divider_group = str(_uuid.uuid4())
                blocks.append({
                    "uuid": str(_uuid.uuid4()),
                    "groupUuid": divider_group,
                    "groupType": "DIVIDER",
                    "type": "DIVIDER",
                    "payload": {}
                })
                continue

            # All interactive types → question block pair
            blocks.extend(
                self._make_question_blocks(label, tally_type, is_required, placeholder, extra_payload)
            )

        logger.info(
            "Built %d Tally blocks for form '%s' (from %d field defs, schema keys: %s)",
            len(blocks), form_title, len(fields), list(schema_info.keys())
        )
        return blocks

    async def create_form(
        self, token: str, form_title: str, instruction: str,
        fields: list, schema_info: dict = None
    ) -> dict:
        """Create a form on Tally via its public API."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        tally_blocks = self._build_tally_blocks(
            form_title, instruction, fields, schema_info or {}
        )

        payload = {
            "status": "PUBLISHED",
            "blocks": tally_blocks
        }

        logger.info(
            "Posting %d blocks to Tally API for form: '%s'", len(tally_blocks), form_title
        )
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f"{self.tally_api_url}/forms",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        form_id = data.get("id")
                        return {
                            "status": "success",
                            "action": "create_form",
                            "data": {
                                "message": "Form created successfully!",
                                "form_title": data.get("title", form_title),
                                "form_url": data.get("url", f"https://tally.so/r/{form_id}"),
                                "edit_url": f"https://tally.so/forms/{form_id}/edit"
                            }
                        }
                    else:
                        error_text = await resp.text()
                        logger.error("Tally API error %d: %s", resp.status, error_text)
                        return {
                            "status": "failed",
                            "error": f"Tally API Error ({resp.status}): {error_text}"
                        }
            except Exception as e:
                logger.exception("HTTP request to Tally failed")
                return {"status": "failed", "error": f"HTTP Request failed: {str(e)}"}
        
    async def list_forms(self, token: str) -> dict:
        """List user's Tally forms"""
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.tally_api_url}/forms", headers=headers
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {"status": "success", "data": data}
                else:
                    return {
                        "status": "failed",
                        "error": f"Tally API error: {resp.status}"
                    }

    async def list_form_responses(self, token: str, user_id: UUID, params: dict, instruction: str = "") -> dict:
        """Fetch applicant submissions for a specific Tally form id/url."""
        headers = {"Authorization": f"Bearer {token}"}

        form_id = await self._resolve_form_reference(user_id, params, instruction)
        if not form_id:
            return {
                "status": "failed",
                "error": "Missing form reference. Please provide a Tally form URL/form_id, or ask after creating/syncing a form."
            }

        candidate_paths = [
            f"/forms/{form_id}/responses",
            f"/forms/{form_id}/submissions",
        ]

        async with aiohttp.ClientSession() as session:
            last_error = None
            for path in candidate_paths:
                try:
                    async with session.get(
                        f"{self.tally_api_url}{path}",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        body_text = await resp.text()
                        if resp.status != 200:
                            last_error = f"{path} -> {resp.status}: {body_text[:300]}"
                            continue

                        try:
                            data = json.loads(body_text)
                        except json.JSONDecodeError:
                            return {
                                "status": "failed",
                                "error": f"Tally API returned non-JSON for {path}"
                            }

                        items = []
                        if isinstance(data, list):
                            items = data
                        elif isinstance(data, dict):
                            if isinstance(data.get("items"), list):
                                items = data["items"]
                            elif isinstance(data.get("responses"), list):
                                items = data["responses"]
                            elif isinstance(data.get("submissions"), list):
                                items = data["submissions"]

                        return {
                            "status": "success",
                            "action": "list_form_responses",
                            "data": {
                                "form_id": form_id,
                                "form_url": f"https://tally.so/r/{form_id}",
                                "form_title": data.get("title", ""),
                                "applicants_count": len(items),
                                "applicants": items,
                            }
                        }
                except Exception as e:
                    last_error = f"{path} -> {str(e)}"

            return {
                "status": "failed",
                "error": f"Unable to fetch applicants for form {form_id}. Last error: {last_error}"
            }


# ── Celery task wrapper ───────────────────────────────────────────────────────
@celery_app.task(name="agents.form_worker.execute_task", bind=True)
def execute_task(self: Task, task_id: str):
    """
    Celery task wrapper.

    asyncio.run() creates a fresh event loop per invocation.
    The DB pool is connected and disconnected inside execute() so it
    never bleeds across event loops (which causes 'Event loop is closed').
    """
    worker = FormWorker()
    result = asyncio.run(worker.execute(task_id))
    return result
