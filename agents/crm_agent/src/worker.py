import os
import logging
import asyncio
from uuid import UUID
import json
from celery import Task

from shared.mq import celery_app
from shared.database import Database

logger = logging.getLogger(__name__)

# Initialize database
db = Database()

class CRMWorker:
    """CRM agent that manages candidates, leads, and data operations"""
    
    def __init__(self):
        self.name = "CRMAgent"
        
    async def execute(self, task_id: str) -> dict:
        """Execute CRM task"""
        await db.connect()
        
        try:
            # Fetch task from database
            task = await db.pool.fetchrow(
                "SELECT * FROM agent_tasks WHERE task_id = $1",
                UUID(task_id)
            )
            
            if not task:
                return {"error": "Task not found"}
                
            input_payload = task["input_payload"]
            task_action = input_payload.get("task_action")
            parameters = input_payload.get("parameters", {})
            user_id = task["user_id"]
            
            # Route to appropriate handler
            if task_action == "create_candidate":
                result = await self.create_candidate(user_id, parameters)
            elif task_action == "update_candidate":
                result = await self.update_candidate(user_id, parameters)
            elif task_action == "get_candidates":
                result = await self.get_candidates(user_id, parameters)
            elif task_action == "create_lead":
                 # Fallback for leads if not defined in schema or requested
                 # We will treat them as generic entities or warn if validation fails
                 return {"status": "failed", "error": "Lead management temporarily unavailable in this schema version"}
            else:
                result = {
                    "error": f"Unknown task action: {task_action}",
                    "status": "failed"
                }
                
            # Update task in database
            await db.update_task_status(
                task_id=str(task_id),
                status="completed" if result.get("status") != "failed" else "failed",
                result=result
            )
            
            return result
            
        except Exception as e:
            await db.update_task_status(
                task_id=str(task_id),
                status="failed",
                result=None,
                error=str(e)
            )
            return {"error": str(e), "status": "failed"}
            
        finally:
            await db.disconnect()

    async def create_candidate(self, user_id: UUID, params: dict) -> dict:
        """Create a new candidate in the universal entities table"""
        
        # Prepare data payload strictly matching the `validation_schema` for candidate
        # Schema: email, skills (required), phone, linkedin_url
        data_payload = {
            "email": params.get("email"),
            "skills": params.get("skills", []),
            "phone": params.get("phone"),
            "linkedin_url": params.get("linkedin_profile") or params.get("linkedin_url"),
            "resume_text": params.get("resume_text") # Extra fields allowed in JSONB even if not in schema (usually)
        }
        
        # Determine Display Name
        display_name = params.get("name", params.get("candidate_name", "Unknown Candidate"))
        
        async with db.pool.acquire() as conn:
            entity_id = await conn.fetchval(
                """INSERT INTO entities 
                   (user_id, entity_type, display_name, status, data)
                   VALUES ($1, 'candidate', $2, $3, $4)
                   RETURNING id""",
                user_id,
                display_name,
                params.get("status", "new"),
                json.dumps(data_payload)
            )
            
            # Log Event
            await conn.execute(
                """INSERT INTO entity_events (entity_id, event_type, payload)
                   VALUES ($1, 'created', $2)""",
                entity_id,
                json.dumps({"source": "crm_worker", "action": "create_candidate"})
            )
            
            return {
                "status": "success",
                "action": "create_candidate",
                "data": {
                    "candidate_id": str(entity_id),
                    "name": display_name,
                    "message": "Candidate created successfully"
                }
            }
            
    async def update_candidate(self, user_id: UUID, params: dict) -> dict:
        """Update an existing candidate"""
        candidate_id = params.get("candidate_id")
        if not candidate_id:
            return {"status": "failed", "error": "candidate_id required"}
            
        async with db.pool.acquire() as conn:
            # Check ownership
            current_entity = await conn.fetchrow(
                "SELECT id, data FROM entities WHERE id = $1 AND user_id = $2",
                UUID(candidate_id), user_id
            )
            
            if not current_entity:
                return {"status": "failed", "error": "Candidate not found or access denied"}
            
            # Merge Data
            current_data = json.loads(current_entity['data'])
            updates = {}
            
            # Update specific fields in data
            for field in ["email", "phone", "linkedin_url", "resume_text"]:
                if field in params:
                    current_data[field] = params[field]
                    updates[field] = params[field]
                    
            if "skills" in params:
                current_data["skills"] = params["skills"]
                updates["skills"] = params["skills"]

            # Update core columns if present
            core_updates = []
            values = []
            idx = 1
            
            if "status" in params:
                core_updates.append(f"status = ${idx}")
                values.append(params["status"])
                idx += 1
                updates["status"] = params["status"]
                
            if "name" in params:
                core_updates.append(f"display_name = ${idx}")
                values.append(params["name"])
                idx += 1
                updates["name"] = params["name"]
            
            # Perform Update
            # Update data JSONB always
            core_updates.append(f"data = ${idx}")
            values.append(json.dumps(current_data))
            idx += 1
            
            # Add updated_at
            core_updates.append("updated_at = NOW()")
            
            values.append(UUID(candidate_id))
            query = f"UPDATE entities SET {', '.join(core_updates)} WHERE id = ${idx}"
            
            await conn.execute(query, *values)
            
            # Log Event
            await conn.execute(
                """INSERT INTO entity_events (entity_id, event_type, payload)
                   VALUES ($1, 'updated', $2)""",
                UUID(candidate_id),
                json.dumps(updates)
            )
                
            return {
                "status": "success",
                "action": "update_candidate",
                "data": {
                    "candidate_id": candidate_id,
                    "updated_fields": list(updates.keys()),
                    "message": "Candidate updated successfully"
                }
            }
            
    async def get_candidates(self, user_id: UUID, params: dict) -> dict:
        """Get candidates from entities table"""
        async with db.pool.acquire() as conn:
            status_filter = params.get("status")
            
            query = """
                SELECT id, display_name, status, data, created_at 
                FROM entities 
                WHERE user_id = $1 AND entity_type = 'candidate'
            """
            args = [user_id]
            
            if status_filter:
                query += " AND status = $2"
                args.append(status_filter)
                
            query += " ORDER BY created_at DESC LIMIT 50"
            
            rows = await conn.fetch(query, *args)
            
            candidates = []
            for r in rows:
                c_data = json.loads(r['data'])
                c_data['id'] = str(r['id'])
                c_data['name'] = r['display_name']
                c_data['status'] = r['status']
                c_data['created_at'] = r['created_at'].isoformat()
                candidates.append(c_data)
            
            return {
                "status": "success",
                "action": "get_candidates",
                "data": {
                    "candidates": candidates,
                    "count": len(candidates)
                }
            }

# Celery task
@celery_app.task(name="agents.crm_worker.execute_task", bind=True)
def execute_task(self: Task, task_id: str):
    """Celery task wrapper"""
    worker = CRMWorker()
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(worker.execute(task_id))
    return result