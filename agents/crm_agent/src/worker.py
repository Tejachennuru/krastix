import os
import logging
import asyncio
from uuid import UUID
import json
from celery import Task
import jsonschema

from shared.mq import celery_app
from shared.database import Database
from shared.callbacks import notify_task_completed

logger = logging.getLogger(__name__)

# Initialize database
db = Database()


class ConcurrencyConflictError(Exception):
    """Raised when an optimistic concurrency check fails (version mismatch)."""
    pass


class CRMWorker:
    """
    Universal CRM Agent — manages any entity type (candidates, leads, contacts)
    using schema-on-demand validation and optimistic concurrency.
    """

    def __init__(self):
        self.name = "CRMAgent_Universal_v1"

    async def execute(self, task_id: str) -> dict:
        """Execute CRM task routed from orchestrator."""
        await db.connect()

        try:
            # Fetch task from database
            task = await db.pool.fetchrow(
                "SELECT * FROM agent_tasks WHERE task_id = $1",
                UUID(task_id)
            )

            if not task:
                return {"error": "Task not found"}

            await db.mark_task_running(str(task_id))

            input_payload = task["input_payload"]
            if isinstance(input_payload, str):
                input_payload = json.loads(input_payload)

            task_action = input_payload.get("task_action")
            parameters = input_payload.get("parameters", {})
            user_id = task["user_id"]

            # Route to universal handlers
            if task_action == "upsert_entity":
                result = await self.upsert_entity(user_id, parameters)
            elif task_action in ("create_candidate", "create_lead", "create_contact"):
                # Backwards-compatible: map old actions to upsert_entity
                entity_type = task_action.replace("create_", "")
                parameters["entity_type"] = entity_type
                result = await self.upsert_entity(user_id, parameters)
            elif task_action in ("update_candidate", "update_lead", "update_contact"):
                entity_type = task_action.replace("update_", "")
                parameters["entity_type"] = entity_type
                result = await self.upsert_entity(user_id, parameters)
            elif task_action == "get_entities":
                result = await self.get_entities(user_id, parameters)
            elif task_action == "get_candidates":
                # Backwards-compatible
                parameters["entity_type"] = "candidate"
                result = await self.get_entities(user_id, parameters)
            else:
                result = {
                    "error": f"Unknown task action: {task_action}",
                    "status": "failed"
                }

            # Update task in database
            final_status = "completed" if result.get("status") != "failed" else "failed"
            await db.update_task_status(
                task_id=str(task_id),
                status=final_status,
                result=result
            )

            # Notify orchestrator (replaces fire-and-forget with guaranteed delivery)
            await notify_task_completed(
                task_id=str(task_id),
                status=final_status,
                result=result,
                error=result.get("error"),
            )

            return result

        except ConcurrencyConflictError as e:
            logger.warning("Concurrency conflict for task %s: %s", task_id, e)
            await db.update_task_status(
                task_id=str(task_id),
                status="failed",
                result=None,
                error=f"Concurrency conflict: {e}"
            )
            await notify_task_completed(
                task_id=str(task_id),
                status="failed",
                error=f"Concurrency conflict: {e}",
            )
            # Re-raise to trigger Celery retry
            raise

        except Exception as e:
            logger.error("CRM Worker error for task %s: %s", task_id, e, exc_info=True)
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
            return {"error": str(e), "status": "failed"}

        finally:
            await db.disconnect()

    # ------------------------------------------------------------------
    # Core: Universal Entity Upsert with Schema Validation + OCC
    # ------------------------------------------------------------------

    async def upsert_entity(self, user_id: UUID, params: dict) -> dict:
        """
        Dynamic entity upsert with:
        1. Schema validation from entity_definitions
        2. Optimistic concurrency via version column
        3. Event sourcing via entity_events
        """
        entity_type = params.get("entity_type")
        if not entity_type:
            return {"status": "failed", "error": "entity_type is required"}

        entity_id = params.get("entity_id") or params.get("id")
        data_payload = params.get("data", {})
        display_name = params.get("display_name") or params.get("name", "")
        status = params.get("status", "new")

        # 1. Fetch & validate against schema from entity_definitions
        async with db.pool.acquire() as conn:
            schema_row = await conn.fetchrow(
                "SELECT validation_schema FROM entity_definitions WHERE entity_type = $1",
                entity_type
            )

        if not schema_row:
            return {
                "status": "failed",
                "error": f"Unknown entity_type: '{entity_type}'. No schema registered in entity_definitions."
            }

        validation_schema = schema_row["validation_schema"]
        if isinstance(validation_schema, str):
            validation_schema = json.loads(validation_schema)

        # Validate payload against JSON Schema
        try:
            jsonschema.validate(instance=data_payload, schema=validation_schema)
        except jsonschema.ValidationError as e:
            return {
                "status": "failed",
                "error": f"Schema validation failed for '{entity_type}': {e.message}",
                "path": list(e.absolute_path)
            }

        # 2. INSERT or UPDATE with optimistic concurrency
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                if entity_id:
                    # --- UPDATE (with OCC) ---
                    old_version = params.get("version")
                    if old_version is None:
                        # Fetch current version if caller didn't provide it
                        current = await conn.fetchrow(
                            "SELECT version, data FROM entities WHERE id = $1 AND user_id = $2",
                            UUID(str(entity_id)), user_id
                        )
                        if not current:
                            return {"status": "failed", "error": "Entity not found or access denied"}
                        old_version = current["version"]

                        # Merge data: keep existing fields, overwrite with new ones
                        existing_data = current["data"]
                        if isinstance(existing_data, str):
                            existing_data = json.loads(existing_data)
                        merged_data = {**existing_data, **data_payload}
                        data_payload = merged_data

                    # Atomic update with version check
                    result = await conn.execute(
                        """
                        UPDATE entities
                        SET data = $1,
                            display_name = COALESCE(NULLIF($2, ''), display_name),
                            status = COALESCE(NULLIF($3, ''), status),
                            version = version + 1,
                            updated_at = NOW()
                        WHERE id = $4 AND user_id = $5 AND version = $6
                        """,
                        json.dumps(data_payload),
                        display_name,
                        status,
                        UUID(str(entity_id)),
                        user_id,
                        old_version
                    )

                    rows_affected = int(result.split()[-1])
                    if rows_affected == 0:
                        raise ConcurrencyConflictError(
                            f"Entity {entity_id} was modified by another agent. "
                            f"Expected version {old_version}."
                        )

                    # Log event
                    await conn.execute(
                        """INSERT INTO entity_events (entity_id, event_type, payload)
                           VALUES ($1, 'updated', $2)""",
                        UUID(str(entity_id)),
                        json.dumps({
                            "source": self.name,
                            "action": "upsert_entity",
                            "entity_type": entity_type,
                            "changes": data_payload
                        })
                    )

                    return {
                        "status": "success",
                        "action": "update_entity",
                        "data": {
                            "entity_id": str(entity_id),
                            "entity_type": entity_type,
                            "display_name": display_name,
                            "message": f"{entity_type} updated successfully"
                        }
                    }

                else:
                    # --- INSERT ---
                    new_id = await conn.fetchval(
                        """INSERT INTO entities
                           (user_id, entity_type, display_name, status, data, version)
                           VALUES ($1, $2, $3, $4, $5, 1)
                           RETURNING id""",
                        user_id,
                        entity_type,
                        display_name,
                        status,
                        json.dumps(data_payload)
                    )

                    # Log event
                    await conn.execute(
                        """INSERT INTO entity_events (entity_id, event_type, payload)
                           VALUES ($1, 'created', $2)""",
                        new_id,
                        json.dumps({
                            "source": self.name,
                            "action": "upsert_entity",
                            "entity_type": entity_type
                        })
                    )

                    return {
                        "status": "success",
                        "action": "create_entity",
                        "data": {
                            "entity_id": str(new_id),
                            "entity_type": entity_type,
                            "display_name": display_name,
                            "message": f"{entity_type} created successfully"
                        }
                    }

    # ------------------------------------------------------------------
    # Query: Universal Entity Retrieval
    # ------------------------------------------------------------------

    async def get_entities(self, user_id: UUID, params: dict) -> dict:
        """Retrieve entities of any type with optional filtering."""
        entity_type = params.get("entity_type")
        if not entity_type:
            return {"status": "failed", "error": "entity_type is required"}

        async with db.pool.acquire() as conn:
            status_filter = params.get("status")

            query = """
                SELECT id, entity_type, display_name, status, data, version, created_at
                FROM entities
                WHERE user_id = $1 AND entity_type = $2
            """
            args = [user_id, entity_type]

            if status_filter:
                query += " AND status = $3"
                args.append(status_filter)

            query += " ORDER BY created_at DESC LIMIT 50"

            rows = await conn.fetch(query, *args)

            entities = []
            for r in rows:
                entity_data = r["data"]
                if isinstance(entity_data, str):
                    entity_data = json.loads(entity_data)
                entity_data["id"] = str(r["id"])
                entity_data["display_name"] = r["display_name"]
                entity_data["status"] = r["status"]
                entity_data["version"] = r["version"]
                entity_data["created_at"] = r["created_at"].isoformat()
                entities.append(entity_data)

            return {
                "status": "success",
                "action": "get_entities",
                "data": {
                    "entity_type": entity_type,
                    "entities": entities,
                    "count": len(entities)
                }
            }


# ------------------------------------------------------------------
# Celery Task (with retry on concurrency conflicts)
# ------------------------------------------------------------------

@celery_app.task(
    name="agents.crm_worker.execute_task",
    bind=True,
    autoretry_for=(ConcurrencyConflictError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3}
)
def execute_task(self: Task, task_id: str):
    """Celery task wrapper with automatic retry on OCC conflicts."""
    worker = CRMWorker()
    result = asyncio.run(worker.execute(task_id))
    return result
