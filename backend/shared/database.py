import os
import socket
import json
import asyncpg
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List
from uuid import UUID

class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self.database_url = os.getenv("DATABASE_URL")
        
    async def connect(self):
        """
        Initialize connection pool with IPv4 resolution fix for Docker/WSL2.
        """
        if not self.database_url:
            raise ValueError("DATABASE_URL environment variable is not set")

        # 1. Parse the original URL
        parsed = urlparse(self.database_url)
        hostname = parsed.hostname
        port = parsed.port or 5432
        
        # Initialize defaults to prevent UnboundLocalError
        host_ip = hostname 
        connection_url = self.database_url

        # 2. FORCE IPv4 RESOLUTION (The Fix)
        # Docker on WSL2 sometimes fails to resolve domains to IPv4, causing "Network Unreachable"
        try:
            print(f"🔍 Resolving IP for {hostname}...")
            # This forces an IPv4 lookup (A Record), ignoring IPv6 (AAAA Record)
            host_ip = socket.gethostbyname(hostname)
            print(f"✅ Resolved to IPv4: {host_ip}")
            
            # Reconstruct URL with the raw IP address
            # This prevents asyncpg from doing its own DNS lookup which might pick IPv6
            new_netloc = parsed.netloc.replace(hostname, host_ip)
            connection_url = parsed._replace(netloc=new_netloc).geturl()
            
        except Exception as e:
            print(f"⚠️ DNS Resolution failed ({e}). Falling back to original URL.")
            # connection_url is already set to default above, so we are safe.

        print(f"🔌 Connecting to Database at {host_ip}:{port}...")

        # 3. Create the Connection Pool
        if not self.pool:
            try:
                self.pool = await asyncpg.create_pool(
                    connection_url,
                    min_size=2,
                    max_size=10,
                    command_timeout=60,
                    ssl="require" # Mandatory for Supabase
                )
                print("🚀 Database Connected Successfully!")
            except Exception as e:
                print(f"❌ Database Connection Failed: {e}")
                raise e
            
    async def disconnect(self):
        """Close connection pool"""
        if self.pool:
            await self.pool.close()
            print("Database Disconnected")
            
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
        """Fetch user profile with RLS simulation"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM profiles WHERE id = $1",
                user_id
            )
            if row:
                return dict(row)
            return None
            
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
            
    async def update_task(self, task_id: UUID, status: str, 
                          output_result: Optional[Dict[str, Any]] = None,
                          error_message: Optional[str] = None):
        """Update task status and result"""
        async with self.pool.acquire() as conn:
            if output_result:
                await conn.execute(
                    """UPDATE agent_tasks 
                       SET status = $1, output_result = $2, completed_at = NOW()
                       WHERE task_id = $3""",
                    status, json.dumps(output_result), task_id
                )
            elif error_message:
                await conn.execute(
                    """UPDATE agent_tasks 
                       SET status = $1, error_message = $2, completed_at = NOW()
                       WHERE task_id = $3""",
                    status, error_message, task_id
                )
            else:
                await conn.execute(
                    "UPDATE agent_tasks SET status = $1 WHERE task_id = $2",
                    status, task_id
                )
                
    async def get_task(self, task_id: UUID, user_id: UUID) -> Optional[Dict[str, Any]]:
        """Fetch task with RLS - only if owned by user"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM agent_tasks 
                   WHERE task_id = $1 AND user_id = $2""",
                task_id, user_id
            )
            if row:
                return dict(row)
            return None
            
    async def save_conversation(self, user_id: UUID, domain_key: str,
                               conversation_history: List[Dict[str, str]],
                               current_plan: Optional[str] = None,
                               conversation_id: Optional[UUID] = None) -> UUID:
        """Save or update conversation state"""
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
        """Fetch conversation with RLS"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM conversations 
                   WHERE id = $1 AND user_id = $2""",
                conversation_id, user_id
            )
            if row:
                return dict(row)
            return None

# Global database instance
db = Database()