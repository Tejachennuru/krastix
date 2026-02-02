from pydantic import BaseModel
from typing import Dict, Literal

class ResearchTask(BaseModel):
    user_id: str
    task_type: Literal[
        "LINKEDIN_PROFILE", 
        "LINKEDIN_COMPANY", 
        "GENERAL_SEARCH", 
        "QUICK_SCRAPE", 
        "SITE_MAP", 
        "DEEP_CRAWL"
    ] = "QUICK_SCRAPE"
    query_or_url: str
    context_metadata: Dict[str, str]

class AgentResponse(BaseModel):
    status: str
    task_id: str
    message: str
