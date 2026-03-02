import os
import re
import json
import logging
from uuid import UUID
from typing import Optional, List, Dict, Any
from langchain_ollama import OllamaEmbeddings
import asyncpg
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Whitelist pattern: only allow alphanumeric keys with underscores
_VALID_METADATA_KEY = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]{0,63}$')


class MemoryService:
    def __init__(self, db_pool):
        self.db_pool = db_pool
        # Initialize Ollama Embeddings
        ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://100.115.107.20:11434")
        self.embeddings = OllamaEmbeddings(
            model="nomic-embed-text",
            base_url=ollama_base_url
        )

    async def get_embedding(self, text: str) -> Optional[List[float]]:
        """Generates a vector embedding using nomic-embed-text (768 dimensions)."""
        try:
            # Langchain's embed_query is synchronous, but usually fast enough for local. 
            # If blocking becomes an issue, run in executor.
            return self.embeddings.embed_query(text)
        except Exception as e:
            logger.error("Embedding generation failed: %s", e, exc_info=True)
            return None

    async def save_memory(self, user_id: str, domain: str, content: str, metadata: dict) -> Optional[str]:
        """Saves text + vector + metadata with strict UUID validation."""
        try:
            valid_user_id = UUID(user_id)
            vector = await self.get_embedding(content)
            if not vector:
                raise ValueError("Failed to generate embedding")

            query = """
            INSERT INTO memories (user_id, domain_key, content, embedding, metadata)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id;
            """
            
            async with self.db_pool.acquire() as conn:
                # Ensure the vector is passed as a string representation of a list for pgvector
                row = await conn.fetchrow(query, valid_user_id, domain, content, str(vector), json.dumps(metadata))
                return str(row['id'])
        except Exception as e:
            logger.error("Save memory failed: %s", e, exc_info=True)
            raise

    async def search_memory(self, user_id: str, query_text: str, 
                           domain_key: Optional[str] = None,
                           filter_metadata: Optional[Dict[str, Any]] = None, 
                           limit: int = 5) -> List[Dict[str, Any]]:
        """
        Namespace-isolated semantic search.
        When domain_key is provided, results are scoped to that domain only.
        Metadata filters are applied on top of the domain scope.
        """
        try:
            valid_user_id = UUID(user_id)
            query_vector = await self.get_embedding(query_text)
            if not query_vector:
                return []

            # Build Dynamic SQL
            # $1 = query_vector, $2 = user_id
            where_clauses = ["user_id = $2"]
            args = [str(query_vector), valid_user_id]
            arg_counter = 3

            # Namespace isolation: filter by domain_key
            if domain_key:
                where_clauses.append(f"domain_key = ${arg_counter}")
                args.append(domain_key)
                arg_counter += 1

            if filter_metadata:
                for key, value in filter_metadata.items():
                    # Validate metadata key against whitelist to prevent SQL injection
                    if not _VALID_METADATA_KEY.match(key):
                        logger.warning("Rejected invalid metadata key: %r", key)
                        continue
                    where_clauses.append(f"metadata->>'{key}' = ${arg_counter}")
                    args.append(str(value))
                    arg_counter += 1

            where_sql = " AND ".join(where_clauses)
            
            # $arg_counter will be the limit
            full_sql = f"""
                SELECT content, metadata, domain_key, 1 - (embedding <=> $1::vector) as similarity
                FROM memories
                WHERE {where_sql}
                ORDER BY embedding <=> $1::vector 
                LIMIT ${arg_counter}
            """
            args.append(limit)

            async with self.db_pool.acquire() as conn:
                results = await conn.fetch(full_sql, *args)
                return [dict(r) for r in results]
                
        except ValueError:
            logger.error("Search error: Invalid UUID format provided")
            return []
        except Exception as e:
            logger.error("Search error: %s", e, exc_info=True)
            return []