import os
import sys
import asyncio
from uuid import UUID
import json
from celery import Task

# Add parent directories to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from shared.redis_client import celery_app
from shared.database import Database

# Initialize database
db = Database()

class ResearchWorker:
    """Research agent that performs web searches, PDF analysis, etc."""
    
    def __init__(self):
        self.name = "ResearchAgent"
        
    async def execute(self, task_id: str) -> dict:
        """Execute research task"""
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
            
            # Route to appropriate handler
            if task_action == "research_company":
                result = await self.research_company(parameters)
            elif task_action == "research_candidate":
                result = await self.research_candidate(parameters)
            elif task_action == "analyze_document":
                result = await self.analyze_document(parameters)
            elif task_action == "web_search":
                result = await self.web_search(parameters)
            else:
                result = {
                    "error": f"Unknown task action: {task_action}",
                    "status": "failed"
                }
                
            # Update task in database
            await db.update_task(
                UUID(task_id),
                "completed" if result.get("status") != "failed" else "failed",
                output_result=result
            )
            
            return result
            
        except Exception as e:
            await db.update_task(
                UUID(task_id),
                "failed",
                error_message=str(e)
            )
            return {"error": str(e), "status": "failed"}
            
        finally:
            await db.disconnect()
            
    async def research_company(self, params: dict) -> dict:
        """Research a company (mock implementation)"""
        company_name = params.get("company_name", "Unknown Company")
        
        # Mock research result
        return {
            "status": "success",
            "action": "research_company",
            "data": {
                "company_name": company_name,
                "industry": "Technology",
                "size": "500-1000 employees",
                "founded": "2015",
                "headquarters": "San Francisco, CA",
                "description": f"{company_name} is a rapidly growing technology company specializing in cloud infrastructure and AI solutions.",
                "recent_news": [
                    f"{company_name} raises $50M Series B",
                    f"{company_name} launches new AI product line",
                    f"{company_name} expands to European market"
                ],
                "key_contacts": [
                    {"name": "John Smith", "title": "CEO"},
                    {"name": "Jane Doe", "title": "VP of Sales"}
                ]
            },
            "tool_used": "web_search_api",
            "confidence": 0.85
        }
        
    async def research_candidate(self, params: dict) -> dict:
        """Research a candidate (mock implementation)"""
        candidate_name = params.get("candidate_name", "Unknown Candidate")
        
        return {
            "status": "success",
            "action": "research_candidate",
            "data": {
                "name": candidate_name,
                "current_role": "Senior Software Engineer at TechCorp",
                "experience_years": 8,
                "education": "BS Computer Science, Stanford University",
                "skills": ["Python", "React", "AWS", "Machine Learning"],
                "github_profile": f"github.com/{candidate_name.lower().replace(' ', '')}",
                "linkedin_summary": f"{candidate_name} is an experienced software engineer with expertise in full-stack development and cloud architecture.",
                "notable_projects": [
                    "Open source contributor to popular ML libraries",
                    "Built scalable microservices handling 1M+ requests/day"
                ]
            },
            "tool_used": "linkedin_api",
            "confidence": 0.80
        }
        
    async def analyze_document(self, params: dict) -> dict:
        """Analyze a document (mock implementation)"""
        doc_type = params.get("document_type", "resume")
        
        return {
            "status": "success",
            "action": "analyze_document",
            "data": {
                "document_type": doc_type,
                "key_findings": [
                    "5+ years of relevant experience",
                    "Strong technical skills in required areas",
                    "Previous experience at notable companies"
                ],
                "summary": "Candidate appears to be a strong fit for the position based on experience and skills.",
                "extracted_data": {
                    "skills": ["Python", "JavaScript", "AWS", "Docker"],
                    "experience_years": 5,
                    "education": "BS in Computer Science"
                }
            },
            "tool_used": "pdf_parser",
            "confidence": 0.90
        }
        
    async def web_search(self, params: dict) -> dict:
        """Perform web search (mock implementation)"""
        query = params.get("query", "")
        
        return {
            "status": "success",
            "action": "web_search",
            "data": {
                "query": query,
                "results": [
                    {
                        "title": f"Result 1 for {query}",
                        "url": "https://example.com/result1",
                        "snippet": "This is a relevant result for your search query..."
                    },
                    {
                        "title": f"Result 2 for {query}",
                        "url": "https://example.com/result2",
                        "snippet": "Another relevant finding related to your query..."
                    }
                ],
                "total_results": 2
            },
            "tool_used": "search_api",
            "confidence": 0.95
        }

# Celery task
@celery_app.task(name="agents.research_worker.execute_task", bind=True)
def execute_task(self: Task, task_id: str):
    """Celery task wrapper"""
    worker = ResearchWorker()
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(worker.execute(task_id))
    return result