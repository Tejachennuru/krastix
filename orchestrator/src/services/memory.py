import os
import json
from uuid import UUID
from google import genai
from google.genai import types
import asyncpg
from pydantic import BaseModel

class MemoryService:
    def __init__(self, db_pool):
        self.db_pool = db_pool
        # Initialize modern SDK client
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    async def get_embedding(self, text: str):
        """Generates a vector embedding using text-embedding-004 (768 dimensions)"""
        try:
            result = self.client.models.embed_content(
                model="text-embedding-004",
                contents=text,
                config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT")
            )
            return result.embeddings[0].values
        except Exception as e:
            print(f"❌ Embedding Error: {e}")
            return None

    async def save_memory(self, user_id: str, domain: str, content: str, metadata: dict):
        """Saves text + vector + metadata with strict UUID validation"""
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
            print(f"❌ Save Memory Error: {e}")
            raise e

    async def search_memory(self, user_id: str, query_text: str, filter_metadata: dict = None, limit: int = 5):
        """Context-Aware Semantic Search using metadata filtering"""
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

            if filter_metadata:
                for key, value in filter_metadata.items():
                    where_clauses.append(f"metadata->>'{key}' = ${arg_counter}")
                    args.append(str(value))
                    arg_counter += 1

            where_sql = " AND ".join(where_clauses)
            
            # $arg_counter will be the limit
            full_sql = f"""
                SELECT content, metadata, 1 - (embedding <=> $1::vector) as similarity
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
            print("❌ Search Error: Invalid UUID format provided")
            return []
        except Exception as e:
            print(f"❌ Search Error: {e}")
            return []