from typing import TypedDict, List, Literal
from langgraph.graph import StateGraph, END
from src.tools import ResearchTools

# --- 1. State Definition ---
class AgentState(TypedDict):
    task_type: str
    query_or_url: str
    attempt_count: int
    raw_content: str
    status: str # 'success', 'failed'
    logs: List[str]

# --- 2. The Graph Logic ---
class ResearchGraph:
    def __init__(self):
        self.tools = ResearchTools()
        self.workflow = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(AgentState)

        # Nodes
        builder.add_node("router", self.node_router)
        builder.add_node("linkedin_tool", self.node_linkedin)
        builder.add_node("firecrawl_tool", self.node_firecrawl)
        builder.add_node("validator", self.node_validator)

        # Edges
        builder.set_entry_point("router")
        
        # Conditional Routing
        builder.add_conditional_edges(
            "router",
            self.edge_router_logic,
            {
                "linkedin": "linkedin_tool",
                "firecrawl": "firecrawl_tool"
            }
        )

        # Flow to Validator
        builder.add_edge("linkedin_tool", "validator")
        builder.add_edge("firecrawl_tool", "validator")
        
        # Validator -> End (Or add retry loop here later)
        builder.add_edge("validator", END)

        return builder.compile()

    # --- Node Implementations ---

    def node_router(self, state: AgentState):
        state["logs"].append(f"Routing: {state['task_type']}")
        return state

    def edge_router_logic(self, state: AgentState):
        if "LINKEDIN" in state["task_type"]:
            return "linkedin"
        return "firecrawl"

    async def node_linkedin(self, state: AgentState):
        try:
            subtype = "profile" if "PROFILE" in state["task_type"] else "company"
            data = await self.tools.scrape_linkedin(state["query_or_url"], subtype)
            state["raw_content"] = data
            state["status"] = "success"
        except Exception as e:
            state["raw_content"] = f"Error: {str(e)}"
            state["status"] = "failed"
        return state

    async def node_firecrawl(self, state: AgentState):
        try:
            tt = state["task_type"]
            q = state["query_or_url"]
            
            if tt == "GENERAL_SEARCH":
                res = self.tools.firecrawl_search(q)
            elif tt == "SITE_MAP":
                res = self.tools.firecrawl_map(q)
            else: # QUICK_SCRAPE / DEEP_CRAWL
                res = self.tools.firecrawl_scrape(q)
                
            state["raw_content"] = res
            state["status"] = "success"
        except Exception as e:
            state["raw_content"] = f"Error: {str(e)}"
            state["status"] = "failed"
        return state

    def node_validator(self, state: AgentState):
        # Simple Validation: Is content empty?
        if not state["raw_content"] or len(str(state["raw_content"])) < 20:
            state["status"] = "failed"
            state["logs"].append("Validation Failed: Empty Content")
        else:
            state["logs"].append("Validation Passed")
        return state

    # --- Run Interface ---
    async def run(self, task_type: str, query: str):
        initial = {
            "task_type": task_type,
            "query_or_url": query,
            "attempt_count": 0,
            "raw_content": "",
            "status": "pending",
            "logs": []
        }
        result = await self.workflow.ainvoke(initial)
        return result["raw_content"]
