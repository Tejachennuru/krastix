# Krastix: Technical Target Architecture & Vision Document

**Project Name:** Krastix
**Vision:** A Distributed AI Operating System for Autonomous Enterprise Workflows
**Core Philosophy:** Decoupled Orchestration, Framework-Agnostic Micro-Agents, and Human-Centric Automation.

---

## 1. Executive Summary
Krastix is an Orchestrated Multi-Agent System designed as AI-as-a-Service (AaaS). Unlike traditional rigid chatbots, Krastix uses a Dynamic Planning Orchestrator to decompose complex user tasks into sub-tasks, which are then dispatched to a pool of independent Agentic Microservices. By leveraging the Model Context Protocol (MCP) and a Unified Data Schema (EAV), Krastix enables enterprises to automate high-stakes workflows (HR, Business Dev, Personal Command Centres) with extreme modularity and security.

---

## 2. System Architecture & The "Brain" (Layered View)

### 2.1 The Dynamic Planning Orchestrator
The Orchestrator is not a fixed script but a Just-In-Time (JIT) Planner.
- **Discovery:** When a task is received, the Orchestrator performs a semantic lookup against the `agent_registry`. It matches the task requirements to agent capabilities and descriptions.
- **Plan Generation:** It generates a Graph (DAG) of tasks.
- **Parallel Execution:** Independent tasks (e.g., "Scrape LinkedIn" and "Scrape GitHub") are fired simultaneously via Celery.
- **Sequential Execution:** Dependent tasks wait for the `output_result` of the previous node.
- **The Capability Guardrail:** If a task exceeds the capabilities of the registered agents, the Orchestrator triggers a "Service Gap" event, prompting the user to "Upgrade" or "Contact Krastix for a New Agent Microservice."

### 2.2 Agent Microservices (The Workers)
Agents are completely decoupled. An HR Agent might be built in LangGraph, while a Research Agent uses CrewAI or AutoGen.
- **Standard Interface:** All agents must accept a JSON payload and return an `output_result` to the `agent_tasks` table.
- **Tooling (MCP):** Agents do not "own" tools. They connect to MCP Servers using the user's specific integrations tokens (Google Docs, Tally, Slack). This ensures data lives where the user wants it.

---

## 3. Pillar 1: Smart Human-in-the-Loop (HITL) Protocol
To solve "Approval Fatigue," Krastix implements a Conditional Breakpoint System.

| Task Type      | Strategy         | Execution Flow                                                                                     |
|----------------|------------------|----------------------------------------------------------------------------------------------------|
| Bulk/Low Risk  | Batch Approval   | "I found 100 leads. Approve sending all emails?" → 1 Click → 100 Actions.                           |
| High Sensitivity | Strategic Pause | Agent writes status: 'PENDING_HUMAN'. System waits for human entities update before resuming.       |
| Iterative      | Human Feedback   | A human developer reviews an AI-screened resume and provides a "Score." The Agent resumes based on that score. |

---

## 4. Pillar 2: Unified Data & Security Strategy
Krastix uses an Entity-Attribute-Value (EAV) model to allow infinite flexibility without database migrations for every new use case.

### 4.1 Multi-Tenancy & RBAC
- **Workspaces:** Every user belongs to a Workspace (Solo or Corporate).
- **Domain Scoping:** The `domain_configs` table controls access. A user can only see the Orchestrators they are permitted to use (e.g., an HR employee sees the "Recruiter" but not the "CEO Command Centre").
- **User-Centric Integrations:** API tokens are stored per `user_id`. If Agent A makes a Doc, it is created in the user's Google Drive, not a generic Krastix folder.

### 4.2 Semantic Memory
The `memories` table uses pgvector (Cosine Search). The Orchestrator "remembers" past user preferences across different tasks within the same domain, allowing for proactive assistance.

---

## 5. Pillar 3: Gen UI (Dynamic Dashboarding)
The frontend is built using Gen UI (AG-UI/CopilotKit) patterns to ensure the interface adapts to the agent's output.
- **UI Hinting:** Agent outputs include a `ui_component` key.
  - Example: If an HR agent finishes screening, it returns `component: 'CandidateComparisonTable'`.
- **Streaming Progress:** Users see a live "Thinking Trace" (Reasoning → Planning → Delegating → Executing) so they are never left wondering about the status of long-running tasks.

---

## 6. Technical Stack & Latency Optimization

| Component      | Technology               | Role                                                                                     |
|----------------|--------------------------|------------------------------------------------------------------------------------------|
| Orchestration  | Python / FastAPI / LangGraph | Dynamic planning and task delegation.                                                    |
| Task Queue     | Redis + Celery           | High-speed, parallel background execution.                                               |
| Database       | PostgreSQL + pgvector    | EAV data storage and RAG capabilities.                                                  |
| Tooling        | MCP (Model Context Protocol) | Standardized, secure tool integration.                                                 |
| Frontend       | React + CopilotKit + Shadcn | Dynamic Gen UI and agent-chat interface.                                                |
| Observability  | LangSmith                | Real-time tracing and debugging of agent logic.                                          |

---

## 7. Target Workflow Example: The HR Role
**Input:** User says: "Find me 5 Python developers and schedule interviews."
- **Plan:** Orchestrator creates 3 tasks: [Search], [Screen], [Coordinate].
- **Search (Parallel):** LinkedIn Agent and GitHub Agent run simultaneously.
- **Screen (Sequential):** Data is parsed into the `entities` table.
- **HITL:** The Orchestrator shows a Gen UI card: "Here are the top 5. Approve for scheduling?"
- **Action:** Upon approval, the Scheduling Agent uses the user's Calendar MCP to send invites.

---

## 8. Summary of Target Capabilities
- **Accuracy:** Guaranteed by using specialized micro-agents for specific sub-tasks.
- **Speed:** Achieved through parallel Celery workers and lightweight MCP tool calls.
- **Scalability:** New domains are added simply by inserting a new row in `domain_configs` and registering a new microservice.
- **Security:** Full RBAC and encrypted user-level OAuth tokens.

---
**Document Status:** Target Architecture Finalized
**Next Phase:** Implementation of Agent Registry Semantic Search & Gen UI Component Mapping.
