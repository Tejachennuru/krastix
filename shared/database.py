import os
import logging
import socket
import json
import asyncpg
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4
from datetime import datetime

logger = logging.getLogger(__name__)


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

        # 3. Create the Connection Pool
        if not self.pool:
            try:
                self.pool = await asyncpg.create_pool(
                    connection_url,
                    min_size=2,
                    max_size=10,
                    command_timeout=60,
                    statement_cache_size=0, # Fixed for Supabase PgBouncer
                    ssl="require" 
                )
                logger.info("Database connected successfully")
            except Exception as e:
                logger.error("Database connection failed: %s", e, exc_info=True)
                raise
            
    async def disconnect(self) -> None:
        """Close connection pool."""
        if self.pool:
            await self.pool.close()
            logger.info("Database disconnected")
            
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
            
    # --- Tasks (Main & Agent) ---

    async def create_task(self, user_id: UUID, domain_key: str, agent_queue: str, 
                          input_payload: Dict[str, Any]) -> UUID:
        """Create a new agent task"""
        async with self.pool.acquire() as conn:
            task_id = await conn.fetchval(
                """INSERT INTO agent_tasks 
                   (user_id, domain_key, agent_queue, status, input_payload)
                   VALUES ($1, $2, $3, $4, $5)
                   RETURNING task_id""",
                user_id, domain_key, agent_queue, "pending", json.dumps(input_payload)
            )
            return task_id

    async def update_task_status(self, task_id: str, status: str, result: Any, error: Optional[str] = None):
        """
        Update task status and return the full task record.
        Used by the Callback Handler to retrieve session context.
        """
        # Ensure task_id is UUID
        uuid_task_id = UUID(str(task_id))
        
        async with self.pool.acquire() as conn:
            # Prepare optional fields
            output_json = json.dumps(result) if result else None
            
            row = await conn.fetchrow(
                """
                UPDATE agent_tasks 
                SET status = $1, 
                    output_result = $2, 
                    error_message = $3, 
                    completed_at = NOW()
                WHERE task_id = $4
                RETURNING *
                """,
                status, output_json, error, uuid_task_id
            )
            
            if row:
                return dict(row)
            return None

    # --- Conversation History ---

    async def save_message(self, user_id: str, role: str, message: str, session_id: Optional[str] = None):
        """
        Appends a message to the conversation history.
        If session_id (conversation_id) is provided, it updates that specific row.
        """
        msg_obj = {"role": role, "content": message, "timestamp": str(datetime.now())}
        
        async with self.pool.acquire() as conn:
            if session_id:
                # Append to existing history using Postgres JSONB operator '||'
                await conn.execute(
                    """
                    UPDATE conversations
                    SET conversation_history = conversation_history || $1::jsonb,
                        updated_at = NOW()
                    WHERE id = $2
                    """,
                    json.dumps([msg_obj]), UUID(str(session_id))
                )
            else:
                # Fallback: We need a session/conversation. If none provided, we can't easily save 
                # to a specific conversation without creating one. 
                # For now, we'll log a warning if this case happens in this architecture.
                print(f"⚠️ Warning: save_message called without session_id for user {user_id}")


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

# Global database instance
db = Database()
