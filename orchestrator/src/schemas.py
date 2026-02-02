from typing import List, Literal
from pydantic import BaseModel, Field

# --- Tools Definitions ---
class DelegateTask(BaseModel):
    """
    Delegate a specific sub-task to a specialized agent.
    
    The `agent_queue` value MUST be one of the queues allowed for your current domain.
    Examples: 'research_queue', 'crm_queue', 'form_queue', 'legal_queue', 
              'calendar_queue', 'payment_queue', 'notification_queue', etc.
    
    The Orchestrator will validate this against the domain's `allowed_agent_queues`.
    """
    instruction: str = Field(..., description="Precise instruction for the agent")
    agent_queue: str = Field(..., description="Target agent queue (must be in domain's allowed_agent_queues)")
    priority: int = Field(default=1, description="Task priority (1-5)")

class QueueBatch(BaseModel):
    """
    Queue a batch operation for manager approval (e.g., Bulk Email, Payroll Run).
    """
    batch_type: Literal["email_campaign", "payroll", "compliance_audit"]
    entity_ids: List[str] = Field(..., description="List of IDs (e.g., candidate_ids) to process")
    reason: str = Field(..., description="Reason for this batch operation")
