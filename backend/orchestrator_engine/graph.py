from typing import TypedDict, Annotated, Sequence
from langgraph.graph import StateGraph, END
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.schema import HumanMessage, AIMessage, SystemMessage
import os
from uuid import UUID
from shared.database import db
from shared.schemas import AgentState, TaskPayload
from shared.redis_client import celery_app
import json

class OrchestratorGraph:
    def __init__(self):
        self.llm = ChatGoogleGenerativeAI(
            model="gemini-1.5-flash",
            google_api_key=os.getenv("GEMINI_API_KEY"),
            temperature=0.7
        )
        
    async def load_config_node(self, state: AgentState) -> AgentState:
        """Load domain configuration from database"""
        config = await db.get_domain_config(state.domain)
        if not config:
            state.final_response = f"Error: Domain '{state.domain}' not found"
            return state
            
        state.system_prompt = config["system_prompt"]
        state.allowed_agents = config["allowed_agent_queues"]
        return state
        
    async def reasoning_node(self, state: AgentState) -> AgentState:
        """Main reasoning node - decides whether to delegate or respond"""
        messages = [
            SystemMessage(content=state.system_prompt + """

You have access to the following agent queues for delegation:
""" + "\n".join([f"- {agent}" for agent in state.allowed_agents]) + """

When you need to perform a specific action (research, data operations), respond with a JSON object:
{
  "action": "delegate",
  "agent_queue": "research_queue" or "crm_queue",
  "task_action": "research_company" or "update_lead" etc,
  "parameters": {"key": "value"}
}

When you can respond directly to the user, respond with:
{
  "action": "respond",
  "message": "Your response to the user"
}

Always respond with valid JSON only, no other text.
""")
        ]
        
        # Add conversation history
        for msg in state.conversation_history[-6:]:  # Last 3 exchanges
            if msg["role"] == "user":
                messages.append(HumanMessage(content=msg["content"]))
            else:
                messages.append(AIMessage(content=msg["content"]))
                
        # Add current message
        messages.append(HumanMessage(content=state.current_message))
        
        # Get LLM response
        response = await self.llm.ainvoke(messages)
        response_text = response.content.strip()
        
        # Parse JSON response
        try:
            # Extract JSON if wrapped in markdown
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0].strip()
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0].strip()
                
            decision = json.loads(response_text)
            
            if decision.get("action") == "delegate":
                # Create task for agent
                agent_queue = decision.get("agent_queue")
                if agent_queue not in state.allowed_agents:
                    state.final_response = f"Error: Agent queue '{agent_queue}' not allowed for this domain"
                    return state
                    
                task_payload = {
                    "task_action": decision.get("task_action"),
                    "parameters": decision.get("parameters", {}),
                    "user_message": state.current_message
                }
                
                # Create task in database
                task_id = await db.create_task(
                    state.user_id,
                    state.domain,
                    agent_queue,
                    task_payload
                )
                
                # Send to Celery queue
                if agent_queue == "research_queue":
                    celery_app.send_task(
                        "agents.research_worker.execute_task",
                        args=[str(task_id)],
                        queue="research_queue"
                    )
                elif agent_queue == "crm_queue":
                    celery_app.send_task(
                        "agents.crm_worker.execute_task",
                        args=[str(task_id)],
                        queue="crm_queue"
                    )
                elif agent_queue == "form_queue":
                    celery_app.send_task(
                        "agents.form_worker.execute_task",
                        args=[str(task_id)],
                        queue="form_queue"
                    )
                
                state.pending_tasks.append(task_id)
                state.active_microservice = agent_queue
                state.current_plan = f"Delegated to {agent_queue}: {decision.get('task_action')}"
                state.final_response = f"Task delegated to {agent_queue}. Task ID: {task_id}"
                
            else:
                # Direct response
                state.final_response = decision.get("message", response_text)
                state.conversation_history.append({
                    "role": "user",
                    "content": state.current_message
                })
                state.conversation_history.append({
                    "role": "assistant",
                    "content": state.final_response
                })
                
        except json.JSONDecodeError:
            # Fallback if JSON parsing fails
            state.final_response = response_text
            state.conversation_history.append({
                "role": "user",
                "content": state.current_message
            })
            state.conversation_history.append({
                "role": "assistant",
                "content": state.final_response
            })
            
        return state
        
    def should_continue(self, state: AgentState) -> str:
        """Decide if we should end or continue"""
        if state.final_response:
            return "end"
        return "continue"
        
    def build_graph(self) -> StateGraph:
        """Build the LangGraph workflow"""
        workflow = StateGraph(AgentState)
        
        # Add nodes
        workflow.add_node("load_config", self.load_config_node)
        workflow.add_node("reasoning", self.reasoning_node)
        
        # Define edges
        workflow.set_entry_point("load_config")
        workflow.add_edge("load_config", "reasoning")
        workflow.add_conditional_edges(
            "reasoning",
            self.should_continue,
            {
                "end": END,
                "continue": END
            }
        )
        
        return workflow.compile()
        
    async def process_message(self, user_id: UUID, domain: str, message: str,
                             conversation_history: list = None) -> AgentState:
        """Process a user message through the graph"""
        initial_state = AgentState(
            user_id=user_id,
            domain=domain,
            current_message=message,
            conversation_history=conversation_history or []
        )
        
        graph = self.build_graph()
        result = await graph.ainvoke(initial_state)
        
        return result