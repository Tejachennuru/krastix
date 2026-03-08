import os
import logging
import traceback
import httpx
from typing import TypedDict, List, Annotated, Optional, Dict, Any
from uuid import UUID

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage, BaseMessage
from langgraph.graph.message import add_messages
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from shared.database import db
from shared.mq import celery_app
from orchestrator.src.schemas import DelegateTask, QueueBatch, build_agent_capability_prompt, build_dispatch_map

logger = logging.getLogger(__name__)


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
        
        # Initialize Ollama (Local Model)
        # Using qwen2.5:32b (or configured model)# Connect to the LLM running on 'cogaan' via Tailscale
        ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://100.115.107.20:11434")
        # Ensure base URL is clean (no trailing slash) as the library handles paths
        ollama_base_url = ollama_base_url.rstrip("/")
        
        model_name = os.getenv("OLLAMA_MODEL", "qwen2.5:14b-instruct-q5_K_M")
        
        logger.info(f"Initializing Orchestrator with Ollama: {model_name} at {ollama_base_url}")
        
        llm = ChatOllama(
            model=model_name,
            base_url=ollama_base_url,
            temperature=0,
            # Increase timeout for tool calling with large local models
            timeout=120.0
        )
        self.llm_with_tools = llm.bind_tools([DelegateTask, QueueBatch])
        
        # Checkpointer will be initialized async
        self.checkpointer = None
        self.workflow = None
        self._pg_cm = None  # Holds async context manager for cleanup

    async def initialize(self) -> None:
        """Async initialization for PostgresSaver with URL cleaning."""
        try:
            raw_url = os.getenv("DATABASE_URL")
            if not raw_url:
                raise ValueError("DATABASE_URL environment variable is not set")
            # FIX: Ensure URL is compatible with psycopg (remove +asyncpg if present)
            conn_string = raw_url.replace("postgresql+asyncpg://", "postgresql://")

            # from_conn_string is an async context manager — enter it and keep ref for cleanup
            self._pg_cm = AsyncPostgresSaver.from_conn_string(conn_string)
            self.checkpointer = await self._pg_cm.__aenter__()
            
            # This creates the necessary 'checkpoints' tables if they don't exist
            await self.checkpointer.setup()
            logger.info("PostgresSaver connected (persistent memory)")
            
        except Exception as e:
            logger.warning("Failed to init PostgresSaver: %s. Switching to MemorySaver (non-persistent).", e)
            # Fallback is CRITICAL so the app doesn't crash on 500
            self.checkpointer = MemorySaver()
        
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

    async def node_planner(self, state: AgentState) -> Dict[str, Any]:
        """The Brain: Plans response using History + RAG Memory."""
        logger.info("Planner activated for user: %s", state['user_id'])
        
        try:
            # 1. Fetch Domain Config (Handle Defaults if DB fails)
            try:
                config = await db.get_domain_config(state["domain"])
            except Exception as e:
                logger.error("DB Config Error for domain '%s': %s", state["domain"], e, exc_info=True)
                config = None

            if config is None:
                logger.warning("No config found for domain '%s', using defaults.", state["domain"])

            sys_prompt = config["system_prompt"] if config else "You are a helpful assistant."
            # Safe list access - parse JSON string if needed
            allowed_queues = config.get("allowed_agent_queues", []) if config else ["research_queue"]
            if isinstance(allowed_queues, str):
                import json
                try:
                    allowed_queues = json.loads(allowed_queues)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse allowed_agent_queues JSON, using default.")
                    allowed_queues = ["research_queue"]
            
            # 2. RAG MEMORY INJECTION
            rag_context = ""
            last_human_msg = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
            
            if self.memory_service and last_human_msg:
                try:
                    results = await self.memory_service.search_memory(
                        user_id=state["user_id"], 
                        query_text=last_human_msg.content,
                        domain_key=state["domain"],
                        limit=3
                    )
                    if results:
                        rag_context = "\n\nRelevant Past Research:\n" + "\n".join(
                            [f"- {r['content']}" for r in results]
                        )
                except Exception as e:
                    logger.warning("Memory search failed (non-critical): %s", e)

            # 2b. AGENT REGISTRY INJECTION — domain-scoped capabilities
            agent_context = ""
            try:
                domain_agents = await db.get_agents_for_domain(state["domain"])
                if domain_agents:
                    agent_context = build_agent_capability_prompt(domain_agents)
            except Exception as e:
                logger.warning("Agent registry lookup failed (non-critical): %s", e)

            # 3. Construct Prompt
            agent_info = f"\n\nAllowed Queues: {', '.join(allowed_queues)}"
            final_sys_prompt = sys_prompt + rag_context + agent_context + agent_info
            
            # 4. Invoke LLM
            messages = [SystemMessage(content=final_sys_prompt)] + state["messages"]
            
            # Handle LLM errors gracefully
            try:
                response = await self.llm_with_tools.ainvoke(messages)
            except Exception as e:
                logger.error("LLM invocation failed: %s", e, exc_info=True)
                return {"messages": [AIMessage(content="I'm having trouble connecting to my brain (LLM Error). Please try again.")]}
            
            return {"messages": [response]}

        except Exception as e:
            logger.critical("CRITICAL PLANNER ERROR: %s", e, exc_info=True)
            return {"messages": [AIMessage(content="An internal error occurred in the planning module.")]}

    def should_delegate(self, state: AgentState) -> str:
        """Check if the last message has tool calls."""
        last_message = state["messages"][-1]
        if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            return "delegate"
        return "respond"

    async def node_dispatcher(self, state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
        """The Hands: Executes Tool Calls using registry-driven dispatch."""
        last_message = state["messages"][-1]
        tool_outputs = []
        
        thread_id = config.get("configurable", {}).get("thread_id", "unknown_session")
        
        # Fetch allowed queues for validation
        try:
            domain_config = await db.get_domain_config(state["domain"])
        except Exception as e:
            logger.error("Failed to fetch domain config in dispatcher: %s", e, exc_info=True)
            domain_config = None
        allowed_queues = domain_config.get("allowed_agent_queues", []) if domain_config else []

        # Build registry-driven dispatch map
        dispatch_map = {}
        try:
            domain_agents = await db.get_agents_for_domain(state["domain"])
            dispatch_map = build_dispatch_map(domain_agents)
        except Exception as e:
            logger.warning("Registry dispatch map failed, using legacy routing: %s", e)

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
                
                # Registry-driven dispatch
                route = dispatch_map.get(queue)
                
                if route and route["method"] == "http":
                    # Generic HTTP dispatch (research agent, doc agent, etc.)
                    target_url = queue  # queue_or_url is the HTTP base URL
                    endpoint = route.get("http_endpoint", "/research/run")
                    try:
                        async with httpx.AsyncClient(timeout=15.0) as client:
                            resp = await client.post(
                                f"{target_url}{endpoint}",
                                json={
                                    "user_id": state["user_id"],
                                    "task_type": "GENERAL_SEARCH",
                                    "query_or_url": args["instruction"],
                                    "instruction": args["instruction"],
                                    "context_metadata": {
                                        "task_id": str(task_id),
                                        "session_id": thread_id,
                                        "domain_key": state["domain"]
                                    }
                                }
                            )
                            resp.raise_for_status()
                    except Exception as e:
                        logger.error("HTTP dispatch to %s%s failed: %s", target_url, endpoint, e)
                elif route and route["method"] == "celery":
                    # Celery dispatch using registry task name
                    celery_app.send_task(
                        route["task_name"],
                        args=[str(task_id)],
                        queue=queue
                    )
                else:
                    # Legacy fallback: use hardcoded mapping
                    if queue == "research_queue":
                        research_url = os.getenv("RESEARCH_AGENT_URL", "http://localhost:8001")
                        try:
                            async with httpx.AsyncClient(timeout=15.0) as client:
                                resp = await client.post(
                                    f"{research_url}/research/run",
                                    json={
                                        "user_id": state["user_id"],
                                        "task_type": "GENERAL_SEARCH",
                                        "query_or_url": args["instruction"],
                                        "context_metadata": {
                                            "task_id": str(task_id),
                                            "session_id": thread_id
                                        }
                                    }
                                )
                                resp.raise_for_status()
                        except Exception as e:
                            logger.error("Legacy HTTP dispatch to research agent failed: %s", e)
                    else:
                        legacy_map = {
                            "crm_queue": "agents.crm_worker.execute_task",
                            "form_queue": "agents.form_worker.execute_task",
                        }
                        task_name = legacy_map.get(queue, "agents.perform_task")
                        celery_app.send_task(task_name, args=[str(task_id)], queue=queue)
                
                tool_outputs.append(
                    ToolMessage(
                        tool_call_id=tool_call["id"],
                        content=f"Task {task_id} dispatched to {queue}. I will notify you when complete."
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
                        content=f"Batch {batch_id} queued for manager approval."
                    )
                )

        return {"messages": tool_outputs}

    # --- Public Interface ---
    async def process_message(self, user_id: str, domain: str, message: str, thread_id: str, role: str = "user") -> Dict[str, Any]:
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

    async def stream_message(self, user_id: str, domain: str, message: str, thread_id: str, role: str = "user"):
        """
        Stream the graph execution, yielding SSE events as tokens arrive.
        
        Yields dicts with event types:
          {"event": "token",     "data": "partial text"}
          {"event": "tool_call", "data": {"tool": "DelegateTask", "args": {...}}}
          {"event": "tool_result", "data": "Task dispatched..."}
          {"event": "done",      "data": {"response": "full text", "task_id": "..."}}
          {"event": "error",     "data": "error message"}
        """
        if not self.workflow:
            yield {"event": "error", "data": "Graph not initialized"}
            return

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

        full_response = ""
        task_id = None

        try:
            async for event in self.workflow.astream_events(
                initial_state, config=config, version="v2"
            ):
                kind = event.get("event", "")
                
                # Stream LLM tokens as they arrive
                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        full_response += chunk.content
                        yield {"event": "token", "data": chunk.content}
                    
                    # Check for tool calls in streaming chunks
                    if chunk and hasattr(chunk, "tool_call_chunks") and chunk.tool_call_chunks:
                        for tc in chunk.tool_call_chunks:
                            if tc.get("name"):
                                yield {
                                    "event": "tool_start",
                                    "data": {"tool": tc["name"], "id": tc.get("id", "")},
                                }

                # Tool execution results
                elif kind == "on_chain_end":
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict):
                        messages = output.get("messages", [])
                        for msg in messages:
                            if hasattr(msg, "content") and isinstance(msg, ToolMessage):
                                yield {"event": "tool_result", "data": msg.content}

            # Get final state for task_id
            try:
                final_snapshot = await self.workflow.aget_state(config)
                if final_snapshot and final_snapshot.values:
                    task_id = final_snapshot.values.get("current_task_id")
                    # Get the final response from last message if streaming missed it
                    msgs = final_snapshot.values.get("messages", [])
                    if msgs:
                        last = msgs[-1]
                        if hasattr(last, "content") and last.content and not full_response:
                            full_response = last.content
            except Exception:
                pass

            yield {
                "event": "done",
                "data": {"response": full_response, "task_id": task_id},
            }

        except Exception as exc:
            logger.error("Stream error: %s", exc, exc_info=True)
            yield {"event": "error", "data": str(exc)}
