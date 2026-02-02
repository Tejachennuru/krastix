from pydantic import BaseModel, Field
from typing import Dict, Any, Optional

# The Universal Message Format
class AgentTask(BaseModel):
    task_id: str
    agent_type: str
    payload: Dict[str, Any]
    context_memory: Optional[list] = None

class AgentResult(BaseModel):
    task_id: str
    status: str
    data: Dict[str, Any]
    new_memory_chunks: Optional[list] = None
