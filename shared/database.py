import os
import logging
import socket
import json
import asyncio
import asyncpg
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List, Set, Tuple
from uuid import UUID, uuid4
from datetime import datetime

logger = logging.getLogger(__name__)

TASK_STATUS_ALIASES = {
    "pending": "queued",
    "processing": "running",
    "success": "completed",
}

TASK_TERMINAL_STATUSES: Set[str] = {"completed", "failed", "cancelled", "timed_out", "stale"}
PLAN_TERMINAL_STATUSES: Set[str] = {"completed", "completed_with_failures", "failed", "cancelled"}

TASK_VALID_TRANSITIONS: Dict[str, Set[str]] = {
    "created": {"queued", "dispatched", "running", "failed", "cancelled", "timed_out", "stale"},
    "queued": {"dispatched", "running", "completed", "failed", "cancelled", "timed_out", "stale"},
    "dispatched": {"running", "completed", "failed", "cancelled", "timed_out", "stale"},
    "running": {"completed", "failed", "cancelled", "timed_out", "stale"},
    "completed": {"completed"},
    "failed": {"failed"},
    "cancelled": {"cancelled"},
    "timed_out": {"timed_out"},
    "stale": {"stale"},
}


class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self.database_url = os.getenv("DATABASE_URL")
        
    async def connect(self) -> None:
        """
        Initialize connection pool with IPv4 resolution fix for Docker/WSL2.
        """
        if not self.database_url:
            raise ValueError("DATABASE_URL environment variable is not set")

        # 1. Parse the original URL
        parsed = urlparse(self.database_url)
        hostname = parsed.hostname
        port = parsed.port or 5432
        
        # Initialize defaults
        host_ip = hostname 
        connection_url = self.database_url

        # 2. FORCE IPv4 RESOLUTION (The Fix)
        try:
            logger.info("Resolving IP for %s...", hostname)
            host_ip = socket.gethostbyname(hostname)
            logger.info("Resolved to IPv4: %s", host_ip)
            
            new_netloc = parsed.netloc.replace(hostname, host_ip)
            connection_url = parsed._replace(netloc=new_netloc).geturl()
            
        except Exception as e:
            logger.warning("DNS Resolution failed (%s). Falling back to original URL.", e)

        logger.info("Connecting to Database at %s:%s...", host_ip, port)

        # 3. Create/Recreate the Connection Pool with retry for transient DNS/network errors.
        if self.pool and getattr(self.pool, "_closed", False):
            self.pool = None

        if not self.pool:
            max_attempts = int(os.getenv("DB_CONNECT_MAX_ATTEMPTS", "10"))
            last_error = None

            for attempt in range(1, max_attempts + 1):
                try:
                    self.pool = await asyncpg.create_pool(
                        connection_url,
                        min_size=2,
                        max_size=10,
                        command_timeout=60,
                        statement_cache_size=0,  # Fixed for Supabase PgBouncer
                        ssl="require"
                    )
                    logger.info("Database connected successfully on attempt %d", attempt)
                    break
                except Exception as e:
                    last_error = e
                    self.pool = None
                    wait_seconds = min(2 * attempt, 15)
                    logger.warning(
                        "Database connect attempt %d/%d failed: %s. Retrying in %ss...",
                        attempt,
                        max_attempts,
                        e,
                        wait_seconds,
                    )
                    await asyncio.sleep(wait_seconds)

                    # Retry DNS resolution each attempt in case network comes up late.
                    try:
                        refreshed_ip = socket.gethostbyname(hostname)
                        if refreshed_ip and refreshed_ip != host_ip:
                            logger.info("Resolved %s to %s for retry", hostname, refreshed_ip)
                            host_ip = refreshed_ip
                            new_netloc = parsed.netloc.replace(hostname, host_ip)
                            connection_url = parsed._replace(netloc=new_netloc).geturl()
                    except Exception:
                        # Keep retrying with current connection_url.
                        pass

            if not self.pool:
                logger.error(
                    "Database connection failed after %d attempts. Last error: %s",
                    max_attempts,
                    last_error,
                )
                raise last_error
            
    async def disconnect(self) -> None:
        """Close connection pool."""
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("Database disconnected")

    async def ping(self) -> bool:
        """Run a tiny query to keep the DB connection path warm."""
        if not self.pool or getattr(self.pool, "_closed", False):
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    def _normalize_task_status(self, status: str) -> str:
        raw = (status or "").strip().lower()
        return TASK_STATUS_ALIASES.get(raw, raw)

    def _can_transition_task_status(self, current: str, new: str) -> bool:
        if current == new:
            return True
        return new in TASK_VALID_TRANSITIONS.get(current, set())

    async def _transition_task_status(
        self,
        conn: asyncpg.Connection,
        *,
        task_id: UUID,
        status: str,
        result: Any = None,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
        callback_received: bool = False,
        callback_idempotency_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        row = await conn.fetchrow(
            """
            SELECT task_id, status
            FROM agent_tasks
            WHERE task_id = $1
            FOR UPDATE
            """,
            task_id,
        )
        if not row:
            return None

        normalized_current = self._normalize_task_status(row["status"])
        normalized_new = self._normalize_task_status(status)
        if not self._can_transition_task_status(normalized_current, normalized_new):
            logger.warning(
                "Ignoring invalid task status transition for task %s: %s -> %s",
                task_id,
                normalized_current,
                normalized_new,
            )
            existing = await conn.fetchrow("SELECT * FROM agent_tasks WHERE task_id = $1", task_id)
            return dict(existing) if existing else None

        output_json = None
        if result is not None:
            output_json = json.dumps(result, default=str)

        merged_error_detail = error_detail if error_detail is not None else error
        updates = await conn.fetchrow(
            """
            UPDATE agent_tasks
            SET status = $1,
                output_result = COALESCE($2::jsonb, output_result),
                error_message = $3,
                error_code = COALESCE($4, error_code),
                error_detail = COALESCE($5, error_detail),
                callback_received_at = CASE WHEN $6 THEN NOW() ELSE callback_received_at END,
                callback_idempotency_key = COALESCE($7, callback_idempotency_key),
                queued_at = CASE WHEN $1 = 'queued' THEN COALESCE(queued_at, NOW()) ELSE queued_at END,
                dispatched_at = CASE WHEN $1 = 'dispatched' THEN COALESCE(dispatched_at, NOW()) ELSE dispatched_at END,
                started_at = CASE WHEN $1 = 'running' THEN COALESCE(started_at, NOW()) ELSE started_at END,
                last_heartbeat_at = CASE WHEN $1 = 'running' THEN NOW() ELSE last_heartbeat_at END,
                completed_at = CASE WHEN $1 = ANY($8::text[]) THEN COALESCE(completed_at, NOW()) ELSE completed_at END,
                updated_at = NOW()
            WHERE task_id = $9
            RETURNING *
            """,
            normalized_new,
            output_json,
            error,
            error_code,
            merged_error_detail,
            callback_received,
            callback_idempotency_key,
            list(TASK_TERMINAL_STATUSES),
            task_id,
        )
        return dict(updates) if updates else None
             
    # --- Configuration & Users ---

    async def get_domain_config(self, domain_key: str) -> Optional[Dict[str, Any]]:
        """Fetch domain configuration"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM domain_configs WHERE domain_key = $1",
                domain_key
            )
            if row:
                return {
                    "domain_key": row["domain_key"],
                    "display_name": row["display_name"],
                    "system_prompt": row["system_prompt"],
                    "allowed_agent_queues": json.loads(row["allowed_agent_queues"]) if isinstance(row["allowed_agent_queues"], str) else row["allowed_agent_queues"]
                }
            return None
            
    async def get_user_profile(self, user_id: UUID) -> Optional[Dict[str, Any]]:
        """Fetch user profile"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM profiles WHERE id = $1", user_id)
            if row: return dict(row)
            return None

    async def get_integration(self, user_id: UUID, provider: str) -> Optional[Dict[str, Any]]:
        """Fetch a single integration row for a user/provider pair."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, provider, access_token, refresh_token, expires_at,
                       created_at, updated_at
                FROM integrations
                WHERE user_id = $1 AND provider = $2
                """,
                user_id,
                provider.lower(),
            )
            return dict(row) if row else None

    async def upsert_integration_tokens(
        self,
        user_id: UUID,
        provider: str,
        access_token: str,
        refresh_token: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> None:
        """Insert/update integration credentials for a provider."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO integrations (user_id, provider, access_token, refresh_token, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, provider)
                DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = COALESCE(EXCLUDED.refresh_token, integrations.refresh_token),
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                """,
                user_id,
                provider.lower(),
                access_token,
                refresh_token,
                expires_at,
            )

    async def create_email_draft(
        self,
        user_id: UUID,
        domain_key: str,
        session_id: str,
        draft_payload: Dict[str, Any],
    ) -> UUID:
        """Create a new pending email draft using EAV entities model."""
        async with self.pool.acquire() as conn:
            draft_id = await conn.fetchval(
                """
                INSERT INTO entities (user_id, entity_type, display_name, status, data)
                VALUES (
                    $1,
                    'email_draft',
                    'Email Draft',
                    'pending_approval',
                    $2::jsonb
                )
                RETURNING id
                """,
                user_id,
                json.dumps(
                    {
                        "domain_key": domain_key,
                        "session_id": session_id,
                        "draft_payload": draft_payload,
                    }
                ),
            )
            return draft_id

    async def get_pending_email_draft(self, user_id: UUID, session_id: str) -> Optional[Dict[str, Any]]:
        """Fetch latest pending email draft entity for a user/session."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, status, data, created_at, updated_at
                FROM entities
                WHERE user_id = $1
                  AND entity_type = 'email_draft'
                  AND status = 'pending_approval'
                  AND data->>'session_id' = $2
                ORDER BY created_at DESC
                LIMIT 1
                """,
                user_id,
                session_id,
            )
            if not row:
                return None
            out = dict(row)
            data = out.get("data") or {}
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    data = {}

            out["domain_key"] = data.get("domain_key")
            out["session_id"] = data.get("session_id")
            out["draft_payload"] = data.get("draft_payload") if isinstance(data.get("draft_payload"), dict) else {}
            return out

    async def update_email_draft(
        self,
        draft_id: UUID,
        status: str,
        draft_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update status and optionally payload for an email draft entity."""
        async with self.pool.acquire() as conn:
            if draft_payload is None:
                await conn.execute(
                    """
                    UPDATE entities
                    SET status = $1,
                        updated_at = NOW(),
                        version = version + 1
                    WHERE id = $2
                      AND entity_type = 'email_draft'
                    """,
                    status,
                    draft_id,
                )
            else:
                await conn.execute(
                    """
                    UPDATE entities
                    SET status = $1,
                        data = jsonb_set(data, '{draft_payload}', $2::jsonb, true),
                        updated_at = NOW(),
                        version = version + 1
                    WHERE id = $3
                      AND entity_type = 'email_draft'
                    """,
                    status,
                    json.dumps(draft_payload),
                    draft_id,
                )
            
    # --- Tasks (Main & Agent) ---

    async def create_task(
        self,
        user_id: UUID,
        domain_key: str,
        agent_queue: str,
        input_payload: Dict[str, Any],
        *,
        plan_id: Optional[str] = None,
        plan_node_id: Optional[str] = None,
    ) -> UUID:
        """Create a new agent task in 'created' state."""
        async with self.pool.acquire() as conn:
            task_id = await conn.fetchval(
                """INSERT INTO agent_tasks 
                   (user_id, domain_key, agent_queue, status, input_payload, correlation_id, plan_id, plan_node_id)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   RETURNING task_id""",
                user_id,
                domain_key,
                agent_queue,
                "created",
                json.dumps(input_payload),
                uuid4(),
                UUID(str(plan_id)) if plan_id else None,
                UUID(str(plan_node_id)) if plan_node_id else None,
            )
            return task_id

    async def mark_task_queued(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Transition a task to 'queued' once orchestrator accepts delegation."""
        async with self.pool.acquire() as conn:
            return await self._transition_task_status(
                conn,
                task_id=UUID(str(task_id)),
                status="queued",
            )

    async def mark_task_dispatched(self, task_id: str, attempt_increment: int = 1) -> Optional[Dict[str, Any]]:
        """Transition a task to 'dispatched' after transport acceptance."""
        async with self.pool.acquire() as conn:
            uuid_task_id = UUID(str(task_id))
            async with conn.transaction():
                current_status = await conn.fetchval(
                    "SELECT status FROM agent_tasks WHERE task_id = $1 FOR UPDATE",
                    uuid_task_id,
                )
                if current_status is None:
                    return None

                normalized_current = self._normalize_task_status(current_status)
                if normalized_current == "running" or normalized_current in TASK_TERMINAL_STATUSES:
                    latest = await conn.fetchrow("SELECT * FROM agent_tasks WHERE task_id = $1", uuid_task_id)
                    return dict(latest) if latest else None

                updated = await self._transition_task_status(
                    conn,
                    task_id=uuid_task_id,
                    status="dispatched",
                )
                if not updated:
                    return None
                await conn.execute(
                    """
                    UPDATE agent_tasks
                    SET attempt_count = COALESCE(attempt_count, 0) + $1,
                        updated_at = NOW()
                    WHERE task_id = $2
                    """,
                    max(0, int(attempt_increment)),
                    uuid_task_id,
                )
                latest = await conn.fetchrow("SELECT * FROM agent_tasks WHERE task_id = $1", uuid_task_id)
                return dict(latest) if latest else None

    async def mark_task_running(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Transition a task to 'running' when the worker actually begins execution."""
        async with self.pool.acquire() as conn:
            updated = await self._transition_task_status(
                conn,
                task_id=UUID(str(task_id)),
                status="running",
            )
            if updated and updated.get("plan_node_id"):
                await conn.execute(
                    """
                    UPDATE plan_nodes
                    SET status = 'running',
                        started_at = COALESCE(started_at, NOW()),
                        updated_at = NOW()
                    WHERE node_id = $1
                      AND status IN ('queued', 'dispatched', 'ready', 'pending')
                    """,
                    UUID(str(updated["plan_node_id"])),
                )
            return updated

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        result: Any,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
        callback_received: bool = False,
        callback_idempotency_key: Optional[str] = None,
    ):
        """
        Update task status and return the full task record.
        Used by the Callback Handler to retrieve session context.
        """
        uuid_task_id = UUID(str(task_id))
        async with self.pool.acquire() as conn:
            return await self._transition_task_status(
                conn,
                task_id=uuid_task_id,
                status=status,
                result=result,
                error=error,
                error_code=error_code,
                error_detail=error_detail,
                callback_received=callback_received,
                callback_idempotency_key=callback_idempotency_key,
            )

    async def register_callback_idempotency(
        self,
        *,
        task_id: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> Dict[str, Any]:
        """
        Persist callback idempotency key.
        Returns:
          {"accepted": True} for new key
          {"accepted": False, "reason": "..."} for duplicate/conflict
        """
        uuid_task_id = UUID(str(task_id))
        async with self.pool.acquire() as conn:
            task_exists = await conn.fetchval(
                "SELECT 1 FROM agent_tasks WHERE task_id = $1",
                uuid_task_id,
            )
            if not task_exists:
                return {"accepted": False, "reason": "unknown_task"}
            inserted = await conn.fetchrow(
                """
                INSERT INTO agent_task_callbacks (task_id, idempotency_key, payload_hash)
                VALUES ($1, $2, $3)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING task_id, payload_hash
                """,
                uuid_task_id,
                idempotency_key,
                payload_hash,
            )
            if inserted:
                return {"accepted": True}

            existing = await conn.fetchrow(
                """
                SELECT task_id, payload_hash
                FROM agent_task_callbacks
                WHERE idempotency_key = $1
                """,
                idempotency_key,
            )
            if not existing:
                # Defensive fallback: conflict observed but row not visible yet.
                return {"accepted": False, "reason": "duplicate"}
            if str(existing["task_id"]) != str(uuid_task_id):
                return {"accepted": False, "reason": "key_task_mismatch"}
            if existing["payload_hash"] != payload_hash:
                return {"accepted": False, "reason": "payload_hash_mismatch"}
            return {"accepted": False, "reason": "duplicate"}

    # --- Conversation History ---

    async def save_message(self, user_id: str, role: str, message: str, session_id: Optional[str] = None, domain: str = "HR_RECRUITER"):
        """
        Appends a message to the conversation history.
        If session_id (conversation_id) is provided, it updates that specific row.
        """
        msg_obj = {"role": role, "content": message, "timestamp": datetime.now().isoformat()}
        
        async with self.pool.acquire() as conn:
            if session_id:
                try:
                    # Append to existing history using Postgres JSONB operator '||'
                    res = await conn.execute(
                        """
                        UPDATE conversations
                        SET conversation_history = conversation_history || $1::jsonb,
                            updated_at = NOW()
                        WHERE id = $2
                        """,
                        json.dumps([msg_obj]), UUID(str(session_id))
                    )
                    
                    if res == "UPDATE 0":
                        logger.info("Initializing new conversation row for session %s", session_id)
                        await conn.execute(
                            "INSERT INTO conversations (id, user_id, domain_key, conversation_history) VALUES ($1, $2, $3, $4)",
                            UUID(str(session_id)), UUID(str(user_id)), domain, json.dumps([msg_obj])
                        )
                    else:
                        logger.debug("Appended message to existing session %s", session_id)
                except Exception as e:
                    logger.error("Failed to save message to DB: %s", e, exc_info=True)
            else:
                logger.warning("save_message called without session_id for user %s", user_id)


    async def save_conversation(self, user_id: UUID, domain_key: str,
                               conversation_history: List[Dict[str, str]],
                               current_plan: Optional[str] = None,
                               conversation_id: Optional[UUID] = None) -> UUID:
        """Create or Replace conversation state (Full Overwrite)"""
        async with self.pool.acquire() as conn:
            if conversation_id:
                await conn.execute(
                    """UPDATE conversations 
                       SET conversation_history = $1, current_plan = $2, updated_at = NOW()
                       WHERE id = $3 AND user_id = $4""",
                    json.dumps(conversation_history), current_plan, conversation_id, user_id
                )
                return conversation_id
            else:
                conv_id = await conn.fetchval(
                    """INSERT INTO conversations 
                       (user_id, domain_key, conversation_history, current_plan)
                       VALUES ($1, $2, $3, $4)
                       RETURNING id""",
                    user_id, domain_key, json.dumps(conversation_history), current_plan
                )
                return conv_id

    async def get_user_conversations(self, user_id: UUID, domain_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch all conversation summaries for a user"""
        async with self.pool.acquire() as conn:
            query = "SELECT id, domain_key, updated_at FROM conversations WHERE user_id = $1"
            params = [user_id]
            if domain_key:
                query += " AND domain_key = $2"
                params.append(domain_key)
            query += " ORDER BY updated_at DESC"
            
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def get_conversation(self, conversation_id: UUID, user_id: UUID) -> Optional[Dict[str, Any]]:
        """Fetch conversation"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM conversations 
                   WHERE id = $1 AND user_id = $2""",
                conversation_id, user_id
            )
            if row:
                return dict(row)
            return None

    # --- Batch Operations ---

    async def create_batch_job(self, user_id: str, domain_key: str, batch_type: str, 
                               entity_ids: List[str], instruction: str = "") -> str:
        """
        Creates a batch job record in the batch_jobs table.
        """
        async with self.pool.acquire() as conn:
            # Convert string IDs to UUIDs
            uuid_entity_ids = [UUID(eid) for eid in entity_ids]
            
            batch_id = await conn.fetchval(
                """
                INSERT INTO batch_jobs 
                    (user_id, domain_key, batch_type, entity_ids, instruction, status)
                VALUES ($1, $2, $3, $4, $5, 'pending')
                RETURNING id
                """,
                UUID(user_id), domain_key, batch_type, uuid_entity_ids, instruction
            )
            return str(batch_id)

    async def process_pending_batches(self, user_id: str, batch_ids: List[str]) -> int:
        """
        Marks batch jobs as 'processing' and returns count.
        """
        async with self.pool.acquire() as conn:
            uuid_batch_ids = [UUID(bid) for bid in batch_ids]
            result = await conn.execute(
                """
                UPDATE batch_jobs 
                SET status = 'processing', processed_at = NOW()
                WHERE id = ANY($1) AND user_id = $2 AND status = 'pending'
                """,
                uuid_batch_ids, UUID(user_id)
            )
            # Extract count from "UPDATE X"
            return int(result.split()[-1]) if result else 0

    # --- Agent Registry ---

    async def get_agents_for_domain(self, domain_key: str) -> List[Dict[str, Any]]:
        """
        Fetch all active agents whose supported_domains contain this domain_key.
        Uses the JSONB `?` operator (passed as $1) to check if the key exists.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT agent_key, display_name, queue_or_url, dispatch_method,
                       capabilities, supported_domains, version
                FROM agent_registry
                WHERE enabled = true AND supported_domains ? $1
                ORDER BY display_name
                """,
                domain_key
            )
            agents = []
            for r in rows:
                caps = r["capabilities"]
                if isinstance(caps, str):
                    caps = json.loads(caps)
                domains = r["supported_domains"]
                if isinstance(domains, str):
                    domains = json.loads(domains)
                agents.append({
                    "agent_key": r["agent_key"],
                    "display_name": r["display_name"],
                    "queue_or_url": r["queue_or_url"],
                    "dispatch_method": r["dispatch_method"],
                    "capabilities": caps,
                    "supported_domains": domains,
                    "version": r["version"]
                })
            return agents

    async def get_agent(self, agent_key: str) -> Optional[Dict[str, Any]]:
        """Fetch a single agent by its key."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT agent_key, display_name, queue_or_url, dispatch_method,
                       capabilities, supported_domains, version, enabled
                FROM agent_registry
                WHERE agent_key = $1
                """,
                agent_key
            )
            if not row:
                return None
            caps = row["capabilities"]
            if isinstance(caps, str):
                caps = json.loads(caps)
            domains = row["supported_domains"]
            if isinstance(domains, str):
                domains = json.loads(domains)
            return {
                "agent_key": row["agent_key"],
                "display_name": row["display_name"],
                "queue_or_url": row["queue_or_url"],
                "dispatch_method": row["dispatch_method"],
                "capabilities": caps,
                "supported_domains": domains,
                "version": row["version"],
                "enabled": row["enabled"]
            }

    async def get_entity_schema(self, entity_type: str) -> Optional[Dict[str, Any]]:
        """Fetch the validation schema for an entity type from entity_definitions."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT entity_type, display_name, validation_schema FROM entity_definitions WHERE entity_type = $1",
                entity_type
            )
            if not row:
                return None
            schema = row["validation_schema"]
            if isinstance(schema, str):
                schema = json.loads(schema)
            return {
                "entity_type": row["entity_type"],
                "display_name": row["display_name"],
                "validation_schema": schema
            }

    # --- Plan Orchestration (Phase 1) ---

    async def create_plan(
        self,
        *,
        user_id: str,
        domain_key: str,
        session_id: Optional[str],
        source_message: str,
        nodes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Persist a plan with DAG nodes.
        Node shape:
          {
            "node_key": "...",
            "node_type": "agent_task",
            "agent_queue": "...",
            "instruction": "...",
            "task_action": "...",
            "parameters": {...},
            "priority": 1,
            "dependencies": ["node_a", ...]
          }
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                session_uuid = None
                if session_id:
                    try:
                        session_uuid = UUID(str(session_id))
                    except Exception:
                        session_uuid = None
                plan_row = await conn.fetchrow(
                    """
                    INSERT INTO plans (user_id, domain_key, session_id, source_message, status)
                    VALUES ($1, $2, $3, $4, 'created')
                    RETURNING *
                    """,
                    UUID(str(user_id)),
                    domain_key,
                    session_uuid,
                    source_message,
                )
                plan_id = plan_row["plan_id"]
                for node in nodes:
                    await conn.execute(
                        """
                        INSERT INTO plan_nodes (
                            plan_id, node_key, node_type, status, agent_queue, instruction,
                            task_action, parameters, priority, dependencies
                        )
                        VALUES ($1, $2, $3, 'pending', $4, $5, $6, $7::jsonb, $8, $9::jsonb)
                        """,
                        plan_id,
                        node["node_key"],
                        node.get("node_type", "agent_task"),
                        node["agent_queue"],
                        node["instruction"],
                        node.get("task_action"),
                        json.dumps(node.get("parameters", {})),
                        int(node.get("priority", 1)),
                        json.dumps(node.get("dependencies", [])),
                    )
                return dict(plan_row)

    async def get_plan(self, plan_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            if user_id:
                row = await conn.fetchrow(
                    "SELECT * FROM plans WHERE plan_id = $1 AND user_id = $2",
                    UUID(str(plan_id)),
                    UUID(str(user_id)),
                )
            else:
                row = await conn.fetchrow("SELECT * FROM plans WHERE plan_id = $1", UUID(str(plan_id)))
            return dict(row) if row else None

    async def get_plan_nodes(self, plan_id: str) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM plan_nodes
                WHERE plan_id = $1
                ORDER BY created_at, node_key
                """,
                UUID(str(plan_id)),
            )
            out = []
            for r in rows:
                obj = dict(r)
                deps = obj.get("dependencies")
                if isinstance(deps, str):
                    try:
                        deps = json.loads(deps)
                    except Exception:
                        deps = []
                obj["dependencies"] = deps or []
                out.append(obj)
            return out

    async def get_plan_events(self, plan_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_id, plan_id, node_id, event_type, event_payload, created_at
                FROM plan_events
                WHERE plan_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                UUID(str(plan_id)),
                max(1, min(int(limit), 1000)),
            )
            return [dict(r) for r in rows]

    async def add_plan_event(
        self,
        *,
        plan_id: str,
        event_type: str,
        event_payload: Optional[Dict[str, Any]] = None,
        node_id: Optional[str] = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO plan_events (plan_id, node_id, event_type, event_payload)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                UUID(str(plan_id)),
                UUID(str(node_id)) if node_id else None,
                event_type,
                json.dumps(event_payload or {}),
            )

    async def update_plan_status(
        self,
        *,
        plan_id: str,
        status: str,
        summary: Optional[str] = None,
        error_detail: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE plans
                SET status = $1,
                    summary = COALESCE($2, summary),
                    error_detail = COALESCE($3, error_detail),
                    completed_at = CASE WHEN $1 = ANY($4::text[]) THEN COALESCE(completed_at, NOW()) ELSE completed_at END,
                    updated_at = NOW()
                WHERE plan_id = $5
                RETURNING *
                """,
                status,
                summary,
                error_detail,
                list(PLAN_TERMINAL_STATUSES),
                UUID(str(plan_id)),
            )
            return dict(row) if row else None

    async def update_plan_node_status(
        self,
        *,
        node_id: str,
        status: str,
        result: Any = None,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        result_json = json.dumps(result, default=str) if result is not None else None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE plan_nodes
                SET status = $1,
                    result = COALESCE($2::jsonb, result),
                    error_code = COALESCE($3, error_code),
                    error_detail = COALESCE($4, error_detail),
                    task_id = COALESCE($5, task_id),
                    started_at = CASE WHEN $1 IN ('queued','dispatched','running') THEN COALESCE(started_at, NOW()) ELSE started_at END,
                    completed_at = CASE WHEN $1 IN ('completed','failed','cancelled','blocked') THEN COALESCE(completed_at, NOW()) ELSE completed_at END,
                    updated_at = NOW()
                WHERE node_id = $6
                RETURNING *
                """,
                status,
                result_json,
                error_code,
                error_detail,
                UUID(str(task_id)) if task_id else None,
                UUID(str(node_id)),
            )
            if not row:
                return None
            obj = dict(row)
            deps = obj.get("dependencies")
            if isinstance(deps, str):
                try:
                    deps = json.loads(deps)
                except Exception:
                    deps = []
            obj["dependencies"] = deps or []
            return obj

    async def get_plan_node_by_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM plan_nodes
                WHERE task_id = $1
                LIMIT 1
                """,
                UUID(str(task_id)),
            )
            if not row:
                return None
            obj = dict(row)
            deps = obj.get("dependencies")
            if isinstance(deps, str):
                try:
                    deps = json.loads(deps)
                except Exception:
                    deps = []
            obj["dependencies"] = deps or []
            return obj

    async def summarize_plan_node_states(self, plan_id: str) -> Dict[str, int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT status, COUNT(*) AS count
                FROM plan_nodes
                WHERE plan_id = $1
                GROUP BY status
                """,
                UUID(str(plan_id)),
            )
            return {str(r["status"]): int(r["count"]) for r in rows}

    # --- Stale Task Detection (Task Watcher) ---

    async def get_stale_tasks(self, stale_minutes: int = 10) -> List[Dict[str, Any]]:
        """
        Find tasks stuck in active lifecycle states for longer
        than ``stale_minutes``.  Used by the orchestrator's task watcher
        to resume forgotten tasks.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT task_id, user_id, domain_key, agent_queue, status,
                       input_payload, output_result, error_message,
                       created_at, completed_at
                FROM agent_tasks
                WHERE status IN ('created', 'queued', 'dispatched', 'running', 'pending', 'processing')
                  AND COALESCE(last_heartbeat_at, created_at) < NOW() - INTERVAL '1 minute' * $1
                ORDER BY created_at ASC
                LIMIT 50
                """,
                stale_minutes,
            )
            return [dict(r) for r in rows]

    async def mark_task_stale(self, task_id: UUID) -> None:
        """Mark a task as 'stale' so it doesn't get picked up again."""
        async with self.pool.acquire() as conn:
            await self._transition_task_status(
                conn,
                task_id=UUID(str(task_id)),
                status="stale",
                error="Marked stale by task watcher",
                error_code="stale_timeout",
                error_detail="No callback heartbeat received within watcher threshold",
            )

# Global database instance
db = Database()
