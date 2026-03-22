from typing import List, Literal, Dict, Any
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
    task_action: str = Field(
        default="",
        description="Optional explicit action for the target agent (e.g. create_form, list_forms, list_form_responses)"
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict, 
        description="Dynamic structural dependencies (e.g. for `form_queue`, output a `blocks` array of dicts with 'label' and 'type' like INPUT_TEXT, FILE_UPLOAD, TEXTAREA)"
    )

class QueueBatch(BaseModel):
    """
    Queue a batch operation for manager approval (e.g., Bulk Email, Payroll Run).
    """
    batch_type: Literal["email_campaign", "payroll", "compliance_audit"]
    entity_ids: List[str] = Field(..., description="List of IDs (e.g., candidate_ids) to process")
    reason: str = Field(..., description="Reason for this batch operation")


# --- Registry-Driven Helpers ---

def build_agent_capability_prompt(agents: List[Dict[str, Any]]) -> str:
    """
    Build a dynamic system prompt section from agent registry entries.
    Gives the LLM awareness of what each agent can do so it routes accurately.
    """
    if not agents:
        return "\n\nNo specialized agents are available for this domain."

    lines = ["\n\n--- Available Agents ---"]
    for agent in agents:
        caps = agent.get("capabilities", {})
        # Handle both list and dict formats in capabilities JSONB
        if isinstance(caps, list):
            caps = {"actions": caps}
        actions = caps.get("actions", [])
        desc = caps.get("description", agent.get("display_name", ""))
        queue = agent.get("queue_or_url", "unknown")

        lines.append(f"\nAgent: {agent['display_name']}")
        lines.append(f"  Queue/URL: {queue}")
        lines.append(f"  Description: {desc}")
        if actions:
            lines.append(f"  Supported actions: {', '.join(actions)}")

    lines.append(
        "\nUse DelegateTask to route work to these agents. "
        "Set agent_queue to the agent's Queue/URL value."
    )
    return "\n".join(lines)


def build_dispatch_map(agents: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """
    Build a dispatch lookup from agent registry entries.
    Returns: { queue_or_url: { "method": "celery"|"http", "task_name": "...",
                               "http_endpoint": "...", "agent_key": "..." } }
    """
    dispatch = {}
    for agent in agents:
        key = agent.get("queue_or_url", "")
        caps = agent.get("capabilities", {})
        # Handle both list and dict formats in capabilities JSONB
        if isinstance(caps, list):
            caps = {"actions": caps}
        dispatch[key] = {
            "method": agent.get("dispatch_method", "celery"),
            "task_name": caps.get("celery_task_name", "agents.perform_task"),
            "agent_key": agent.get("agent_key", ""),
            "http_endpoint": caps.get("http_endpoint", "/research/run"),
        }
    return dispatch
