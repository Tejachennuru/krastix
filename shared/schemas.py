from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import UUID

class TaskPayload(BaseModel):
    """Schema for tasks sent to agent workers"""
    task_id: UUID
    user_id: UUID
    domain_key: str
    agent_queue: str
    action: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    
class AgentResult(BaseModel):
    """Schema for results returned by agent workers"""
    task_id: UUID
    status: str  # success, failed, partial
    data: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class AgentState(BaseModel):
    """LangGraph state schema"""
    user_id: UUID
    domain: str
    conversation_history: List[Dict[str, str]] = Field(default_factory=list)
    current_plan: Optional[str] = None
    active_microservice: Optional[str] = None
    pending_tasks: List[UUID] = Field(default_factory=list)
    completed_tasks: List[UUID] = Field(default_factory=list)
    system_prompt: Optional[str] = None
    allowed_agents: List[str] = Field(default_factory=list)
    current_message: Optional[str] = None
    final_response: Optional[str] = None
    
class ChatRequest(BaseModel):
    """API request for chat endpoint"""
    user_id: UUID
    domain_key: str
    message: str
    conversation_id: Optional[UUID] = None
    
class ChatResponse(BaseModel):
    """API response for chat endpoint"""
    conversation_id: UUID
    response: str
    pending_tasks: List[UUID] = Field(default_factory=list)
    status: str  # processing, completed, error
    
class TaskStatusResponse(BaseModel):
    """API response for task status check"""
    task_id: UUID
    status: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    
class DomainConfig(BaseModel):
    """Domain configuration from database"""
    domain_key: str
    display_name: str
    system_prompt: str
    allowed_agent_queues: List[str]