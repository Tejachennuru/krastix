import os
from typing import TypedDict, List, Annotated, Optional
from uuid import UUID

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage, BaseMessage
from langgraph.graph.message import add_messages
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.memory import MemorySaver

from shared.database import db
from shared.mq import celery_app
from orchestrator.src.schemas import DelegateTask, QueueBatch


# --- 1. State Definition ---
class AgentState(TypedDict):
    user_id: str
    domain: str
    messages: Annotated[List[BaseMessage], add_messages]
    current_task_id: Optional[str]


# --- 2. The Logic Engine ---
class OrchestratorGraph:
    def __init__(self, memory_service=None, db_pool=None):
        self.memory_service = memory_service
        self.db_pool = db_pool
        
        # Initialize Gemini with Tools
        # Using gemini-2.0-flash (current stable model)
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",
            google_api_key=os.getenv("GEMINI_API_KEY"),
            temperature=0
        )
        self.llm_with_tools = llm.bind_tools([DelegateTask, QueueBatch])
        
        # Checkpointer will be initialized async
        self.checkpointer = None
        self.workflow = None

    async def initialize(self):
        """Async initialization for PostgresSaver with URL cleaning"""
        try:
            raw_url = os.getenv("DATABASE_URL")
            # FIX: Ensure URL is compatible with psycopg (remove +asyncpg if present)
            conn_string = raw_url.replace("postgresql+asyncpg://", "postgresql://")

            # Disable prepared statements for Supabase transaction pooler (port 6543)
            self.checkpointer = AsyncPostgresSaver.from_conn_string(
                conn_string,
                conn_kwargs={"prepare_threshold": None} 
            )
            
            # This creates the necessary 'checkpoints' tables if they don't exist
            await self.checkpointer.setup()
            print("✅ PostgresSaver Connected (Persistent Memory)")
            
        except Exception as e:
            print(f"⚠️ Failed to init PostgresSaver: {e}")
            # Fallback is CRITICAL so the app doesn't crash on 500
            self.checkpointer = MemorySaver()
            print("⚠️ Switched to MemorySaver (Non-Persistent)")
        
        # Compile graph with whatever checkpointer worked
        self.workflow = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("planner", self.node_planner)
        graph.add_node("dispatcher", self.node_dispatcher)

        graph.set_entry_point("planner")
        
        graph.add_conditional_edges(
            "planner",
            self.should_delegate,
            {
                "delegate": "dispatcher",
                "respond": END
            }
        )
        graph.add_edge("dispatcher", END)

        return graph.compile(checkpointer=self.checkpointer)

    # --- Node Implementations ---

    async def node_planner(self, state: AgentState):
        """The Brain: Plans response using History + RAG Memory."""
        print(f"🧠 Planner Activated: {state['user_id']}")
        
        try:
            # 1. Fetch Domain Config (Handle Defaults if DB fails)
            try:
                config = await db.get_domain_config(state["domain"])
            except Exception as e:
                print(f"⚠️ DB Config Error: {e}")
                config = None

            sys_prompt = config["system_prompt"] if config else "You are a helpful assistant."
            # Safe list access - parse JSON string if needed
            allowed_queues = config.get("allowed_agent_queues", []) if config else ["research_queue"]
            if isinstance(allowed_queues, str):
                import json
                try:
                    allowed_queues = json.loads(allowed_queues)
                except:
                    allowed_queues = ["research_queue"]
            
            # 2. RAG MEMORY INJECTION
            rag_context = ""
            last_human_msg = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
            
            if self.memory_service and last_human_msg:
                try:
                    results = await self.memory_service.search_memory(
                        user_id=state["user_id"], 
                        query_text=last_human_msg.content, 
                        limit=3
                    )
                    if results:
                        rag_context = "\n\nRelevant Past Research:\n" + "\n".join(
                            [f"- {r['content']}" for r in results]
                        )
                except Exception as e:
                    print(f"⚠️ Memory Search Failed (Non-Critical): {e}")

            # 3. Construct Prompt
            agent_info = f"\n\nAllowed Queues: {', '.join(allowed_queues)}"
            final_sys_prompt = sys_prompt + rag_context + agent_info
            
            # 4. Invoke LLM
            messages = [SystemMessage(content=final_sys_prompt)] + state["messages"]
            
            # Handle LLM errors gracefully
            try:
                response = await self.llm_with_tools.ainvoke(messages)
            except Exception as e:
                print(f"❌ LLM Error: {e}")
                return {"messages": [AIMessage(content="I'm having trouble connecting to my brain (LLM Error). Please try again.")]}
            
            return {"messages": [response]}

        except Exception as e:
            import traceback
            print(f"🔥 CRITICAL PLANNER ERROR: {traceback.format_exc()}")
            return {"messages": [AIMessage(content="An internal error occurred in the planning module.")]}

    def should_delegate(self, state: AgentState):
        """Check if the last message has tool calls."""
        last_message = state["messages"][-1]
        if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            return "delegate"
        return "respond"

    async def node_dispatcher(self, state: AgentState, config: RunnableConfig):
        """The Hands: Executes Tool Calls (DelegateTask, QueueBatch)."""
        last_message = state["messages"][-1]
        tool_outputs = []
        
        thread_id = config.get("configurable", {}).get("thread_id", "unknown_session")
        
        # Fetch allowed queues for validation
        domain_config = await db.get_domain_config(state["domain"])
        allowed_queues = domain_config.get("allowed_agent_queues", []) if domain_config else []

        for tool_call in last_message.tool_calls:
            tool_name = tool_call["name"]
            args = tool_call["args"]
            
            if tool_name == "DelegateTask":
                queue = args["agent_queue"]
                
                # VALIDATION: Check if queue is allowed for this domain
                if queue not in allowed_queues:
                    tool_outputs.append(
                        ToolMessage(
                            tool_call_id=tool_call["id"],
                            content=f"ERROR: Agent queue '{queue}' is not authorized for this domain. Allowed queues: {allowed_queues}"
                        )
                    )
                    continue
                
                # Create Task in DB with session_id for callback wake-up
                task_id = await db.create_task(
                    user_id=UUID(state["user_id"]),
                    domain_key=state["domain"],
                    agent_queue=queue,
                    input_payload={
                        "instruction": args["instruction"],
                        "priority": args.get("priority", 1),
                        "session_id": thread_id  # Critical for callback routing
                    }
                )
                
                # Dispatch to Celery
                celery_app.send_task(
                    "agents.perform_task",
                    args=[args["instruction"], state["user_id"], str(task_id)],
                    queue=queue
                )
                
                tool_outputs.append(
                    ToolMessage(
                        tool_call_id=tool_call["id"],
                        content=f"✅ Task {task_id} dispatched to {queue}. I will notify you when complete."
                    )
                )
                state["current_task_id"] = str(task_id)

            elif tool_name == "QueueBatch":
                # Create Batch Job with full schema compliance
                batch_id = await db.create_batch_job(
                    user_id=UUID(state["user_id"]),
                    domain_key=state["domain"],
                    batch_type=args["batch_type"],
                    entity_ids=args["entity_ids"],
                    instruction=args.get("reason", "")
                )
                tool_outputs.append(
                    ToolMessage(
                        tool_call_id=tool_call["id"],
                        content=f"📋 Batch {batch_id} queued for manager approval."
                    )
                )

        return {"messages": tool_outputs}

    # --- Public Interface ---
    async def process_message(self, user_id: str, domain: str, message: str, thread_id: str, role: str = "user"):
        """
        Runs the graph. Supports both User messages and System notifications (Wake-up).
        """
        if not self.workflow:
            raise RuntimeError("Graph not initialized. Call await graph.initialize() first.")
        
        config = {"configurable": {"thread_id": thread_id}}
        
        if role == "system":
            input_message = SystemMessage(content=message)
        else:
            input_message = HumanMessage(content=message)
        
        initial_state = {
            "user_id": user_id,
            "domain": domain,
            "messages": [input_message]
        }
        
        final_state = await self.workflow.ainvoke(initial_state, config=config)
        
        # Extract response
        last_msg = final_state["messages"][-1]
        response_text = last_msg.content if hasattr(last_msg, "content") else "Processing..."
        
        return {
            "response": response_text,
            "task_id": final_state.get("current_task_id")
        }
