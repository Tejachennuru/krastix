# Krastix System Architecture

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                    KRASTIX PLATFORM                                      │
│                     Universal Agentic Engine — Multi-Agent AI Orchestration               │
│                                                                                          │
│  Dual-LLM Strategy:  Groq/Grok (cloud) + Ollama qwen2.5:14b (local/text/free)             │
│  Real-Time SSE Streaming  ·  Callback + Task Watcher Reliability  ·  Registry-Driven     │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

## System Diagram

```mermaid
flowchart TB
    subgraph CLIENT["Client Layer"]
        FE[React Frontend<br/>Vite + SSE Streaming]
    end

    subgraph BRAIN["Orchestrator Service :8000"]
        API[FastAPI Endpoints<br/>+ SSE /chat/stream]
        GRAPH[LangGraph<br/>State Machine]
        PLANNER[Planner Node<br/>+ Agent Registry Injection]
        DISPATCHER[Dispatcher Node<br/>Registry-Driven Routing]
        MEMORY[Memory Service<br/>Namespace-Isolated RAG]
        WATCHER[Task Watcher<br/>Stale Task Safety Net]
    end

    subgraph REGISTRY["Agent Registry"]
        AR[(agent_registry table)]
        ED[(entity_definitions table)]
    end

    subgraph MQ["Message Queue Layer"]
        REDIS[(Redis<br/>Celery Broker)]
        CQ[crm_queue]
        FQ[form_queue]
        COMQ[communication_queue]
    end

    subgraph AGENTS["Specialized Agents"]
        CRM[CRM Agent<br/>Universal Entity Worker<br/>OCC + Schema Validation]
        FORM[Form Agent<br/>Celery Worker<br/>Tally Integration]
        COM[Communication Agent<br/>Celery Worker<br/>Approved Email Send]
        RESEARCH[Research Agent :8001<br/>FastAPI + LangGraph<br/>Firecrawl + LinkedIn]
        DOC[Document Agent :8002<br/>VDU Pipeline<br/>Groq Vision + Ollama Text]
    end

    subgraph DATA["Data Layer"]
        SUPABASE[(PostgreSQL<br/>Supabase)]
        PGVECTOR[pgvector<br/>Semantic Memory]
        CHECKPOINTS[LangGraph<br/>Checkpoints]
    end

    subgraph LLM_LAYER["Dual-LLM Layer"]
        GROQ[Groq API<br/>llama-3.2-90b-vision<br/>Cloud · Fast · Multimodal]
        OLLAMA[Ollama<br/>qwen2.5:14b-instruct<br/>Local · Free · Text]
        EMBED[nomic-embed-text<br/>768d Embeddings]
    end

    subgraph EXTERNAL["External APIs"]
        FIRECRAWL[Firecrawl API]
        SCRAPE[ScrapeCreators<br/>LinkedIn API]
        TALLY[Tally.so API]
        GMAIL[Gmail API]
    end

    %% Client Flow — SSE streaming
    FE -->|SSE Stream| API
    
    %% Brain Internal Flow
    API --> GRAPH
    GRAPH --> PLANNER
    PLANNER -->|Tool Call| DISPATCHER
    PLANNER <-->|Namespace-Scoped Query| MEMORY
    PLANNER <-->|Fetch Capabilities| AR
    WATCHER -->|Check stale tasks| SUPABASE
    WATCHER -->|Notify session| GRAPH
    
    %% Task Dispatch (Registry-Driven)
    DISPATCHER -->|Celery| REDIS
    DISPATCHER -->|HTTP| RESEARCH
    DISPATCHER -->|HTTP| DOC
    REDIS --> CQ
    REDIS --> FQ
    REDIS --> COMQ
    
    %% Agent Consumption
    CQ --> CRM
    FQ --> FORM
    COMQ --> COM
    
    %% Agent Results — ALL agents now callback
    CRM -->|callback| API
    FORM -->|callback| API
    COM -->|callback| API
    RESEARCH -->|callback + /memory/ingest| API
    DOC -->|callback + /memory/ingest| API
    
    %% Schema Validation
    CRM <-->|Validate against| ED
    DOC <-->|Fetch schema| ED
    
    %% Data Connections
    MEMORY --> PGVECTOR
    GRAPH --> CHECKPOINTS
    PGVECTOR --> SUPABASE
    CHECKPOINTS --> SUPABASE
    
    %% LLM Connections — Dual strategy
    PLANNER --> OLLAMA
    MEMORY --> EMBED
    RESEARCH --> OLLAMA
    DOC -->|Vision extraction| GROQ
    DOC -->|Text audit/refine| OLLAMA
    API -->|Email summarize (on-demand)| GROQ
    API -->|Reply draft (primary)| OLLAMA
    
    %% External API Connections
    RESEARCH --> FIRECRAWL
    RESEARCH --> SCRAPE
    FORM --> TALLY
    API --> GMAIL
    COM --> GMAIL
```

---

## Core Architecture Patterns

### 1. Agent Registry Pattern
Agents self-register in the `agent_registry` table with their capabilities, dispatch method, and supported domains. The orchestrator queries this at planning time to dynamically inject available agents into the LLM's system prompt, enabling domain-scoped tool routing without hardcoded mappings.

### 2. Dual-LLM Routing Strategy
The system uses two LLM providers strategically for speed and cost:

| Task Type | Provider | Model | Rationale |
|-----------|----------|-------|-----------|
| **Vision extraction** (document pages) | Groq Cloud | llama-3.2-90b-vision-preview | Fast cloud inference, multimodal capability |
| **Text analysis** (audit, refinement) | Local Ollama | qwen2.5:14b-instruct | Free, no API cost, good for structured text tasks |
| **Email summarization** (communications panel) | xAI Grok or Groq-compatible | provider auto-detected from `GROK_API_KEY` | Keeps summaries high quality while supporting existing key formats |
| **Reply draft generation** (communications panel) | Local Ollama primary, Groq qwen fallback | qwen2.5 local -> qwen-family cloud fallback | Resilient drafting when local Qwen endpoint is unavailable |
| **Orchestrator planning** | Local Ollama | qwen2.5:14b-instruct | Reasoning + tool calling |
| **Embeddings** | Local Ollama | nomic-embed-text | 768d vectors, co-located |

Fallback chain: Groq Vision -> local Ollama VLM -> error. Communications summarize auto-selects xAI/Groq based on key format. Reply draft uses Ollama first, then Groq qwen fallback.

### 3. SSE Token Streaming
The orchestrator exposes `POST /api/v1/chat/stream` which uses LangGraph's `astream_events(v2)` to deliver LLM tokens to the frontend in real-time via Server-Sent Events. Event types: `token`, `tool_start`, `tool_result`, `done`, `error`. The frontend falls back to the non-streaming `/api/v1/chat` endpoint if SSE fails.

### 4. Reliable Task Delivery (Callback + Task Watcher)
The original fire-and-forget Celery pattern had a critical gap: if an agent crashed or the callback HTTP request failed, tasks were silently lost. The new system uses a **dual safety net**:

1. **Primary: Agent Callbacks** — Every agent (Celery workers + HTTP services) calls `POST /callbacks/task-completed` after completing or failing a task. A shared `callbacks.py` utility standardises this.
2. **Safety Net: Task Watcher** — A background coroutine in the orchestrator checks for tasks stuck in `pending`/`processing` for >10 minutes, marks them `stale`, and notifies the user's session.

```
Agent completes work
    │
    ├─→ db.update_task_status()     (always happens first)
    │
    ├─→ POST /callbacks/task-completed  (primary delivery)
    │       │
    │       └─→ Orchestrator wakes up session, notifies user
    │
    └─→ (if callback fails)
            │
            └─→ Task Watcher picks up stale task → notifies user
```

### 5. Schema-on-Demand Validation
Entity types (candidate, lead, contact, etc.) have JSON Schema definitions stored in `entity_definitions`. The CRM agent validates all payloads against these schemas before insert/update, allowing new entity types to be added via SQL without code changes.

### 6. Optimistic Concurrency Control (OCC)
Every entity row has a `version` column. Updates use `WHERE version = $expected` — if another agent modified the entity concurrently, the update affects 0 rows and a `ConcurrencyConflictError` is raised. Celery auto-retries with exponential backoff (up to 3 times).

### 7. Namespace-Isolated Memory
The `search_memory()` method accepts a mandatory `domain_key` parameter. All RAG queries are scoped to the user's current domain, preventing cross-domain context leakage. A composite index on `(user_id, domain_key)` ensures efficient filtering.

---

## Component Breakdown

### 1. 🖥️ Client Layer
| Component | Tech | Port | Description |
|-----------|------|------|-------------|
| **Frontend** | React + Vite | `5173` | Chat + Communications UI (inbox list, full-email summarize, reply draft approval) |

### 2. 🧠 Orchestrator (The Brain)
| Component | Tech | Description |
|-----------|------|-------------|
| **API Layer** | FastAPI | `/chat`, `/chat/stream` (SSE), `/health`, `/memory/ingest`, `/callbacks/task-completed`, integrations OAuth/connect/disconnect, communications Gmail endpoints |
| **Graph Engine** | LangGraph | Stateful workflow: Planner → Dispatcher → END |
| **Planner Node** | Ollama (qwen2.5) | Intent recognition, RAG context + agent registry injection, tool binding |
| **Dispatcher Node** | Registry-Driven | Routes via Celery or HTTP based on `agent_registry.dispatch_method` |
| **Memory Service** | pgvector | Namespace-isolated semantic search with `nomic-embed-text` (768d) |
| **Task Watcher** | asyncio | Background coroutine (60s poll) — catches stale tasks >10 min |

### 3. ⚡ Message Queue
| Component | Tech | Description |
|-----------|------|-------------|
| **Redis** | Redis 7 Alpine | Celery broker + result backend |
| **Queues** | Celery | `crm_queue`, `form_queue`, `communication_queue` (research/doc use HTTP) |

### 4. 🤖 Specialized Agents
| Agent | Type | Port | Capabilities |
|-------|------|------|--------------|
| **CRM Agent** | Celery Worker | - | Universal entity CRUD with OCC + JSON Schema validation |
| **Form Agent** | Celery Worker | - | Tally.so form creation/management |
| **Communication Agent** | Celery Worker | - | Sends approved Gmail emails via delegated tasks |
| **Research Agent** | FastAPI Service | `8001` | Web scraping, LinkedIn profiles, Site mapping |
| **Doc Agent** | FastAPI Service | `8002` | PDF/image extraction via LangGraph VDU pipeline (Groq Vision + Ollama) |

### 5. 💾 Data Layer
| Component | Tech | Description |
|-----------|------|-------------|
| **PostgreSQL** | Supabase | Primary data store with RLS |
| **pgvector** | Extension | Vector similarity search (768d) |
| **Checkpoints** | `AsyncPostgresSaver` | LangGraph conversation persistence |

---

## Data Flow Sequence

```
┌──────┐      ┌─────────────┐      ┌──────────┐      ┌───────┐      ┌────────┐
│ User │      │ Orchestrator│      │  Redis   │      │ Agent │      │Supabase│
└──┬───┘      └──────┬──────┘      └────┬─────┘      └───┬───┘      └───┬────┘
   │                 │                  │                │              │
   │ POST /chat/stream (SSE)            │                │              │
   │────────────────>│                  │                │              │
   │                 │                  │                │              │
   │                 │ 1. Query agent_registry for domain                │
   │                 │─────────────────────────────────────────────────>│
   │                 │<─────────────────────────────────────────────────│
   │                 │                  │                │              │
   │                 │ 2. RAG: Namespace-scoped memory search           │
   │                 │─────────────────────────────────────────────────>│
   │                 │<─────────────────────────────────────────────────│
   │                 │                  │                │              │
   │  SSE: tokens    │ 3. LLM: Ollama Call (streamed)    │              │
   │<·····(streaming)│ (context + agent caps)            │              │
   │                 │                  │                │              │
   │  SSE: tool_start│ 4. Tool: DelegateTask             │              │
   │<················│──(registry route)>│               │              │
   │                 │                  │ task.apply()   │              │
   │  SSE: done      │                  │───────────────>│              │
   │<────────────────│                  │                │ 5. Validate  │
   │                 │                  │                │ schema + OCC │
   │                 │                  │                │─────────────>│
   │                 │                  │                │<─────────────│
   │                 │                  │                │              │
   │                 │ 6. POST /callbacks/task-completed │              │
   │                 │<──────────────────────────────────│              │
   │                 │                  │                │              │
   │                 │ 7. Task Watcher (safety net, 60s poll)           │
   │                 │──────────── stale task scan ────────────────────>│
```

### Communication Inbox and Reply Flow

```
1. Frontend calls GET /api/v1/communications/gmail/primary?user_id=...&after_ts=...
2. Orchestrator loads + refreshes Google tokens from integrations table
3. Orchestrator queries Gmail API and returns message list with snippet + full body
4. User opens one email and clicks Summarize -> POST /communications/gmail/summarize
5. User clicks Reply -> POST /communications/gmail/reply/draft (editable draft returned)
6. User approves and sends -> POST /communications/gmail/reply/send
```

---

## Database Schema (Key Tables)

```
┌─────────────────┐     ┌──────────────────┐     ┌────────────────────┐
│    profiles     │     │  domain_configs  │     │     entities       │
├─────────────────┤     ├──────────────────┤     ├────────────────────┤
│ id (UUID) PK    │     │ domain_key PK    │     │ id (UUID) PK       │
│ email           │     │ display_name     │     │ user_id FK         │
│ tier            │     │ system_prompt    │     │ entity_type FK     │
│ credits         │     │ allowed_agents[] │     │ data (JSONB)       │
└─────────────────┘     └──────────────────┘     │ version (INTEGER)  │
                                                 │ derived_skills[]   │
                                                 └────────────────────┘

┌──────────────────┐     ┌──────────────────┐     ┌────────────────────┐
│    memories      │     │   agent_tasks    │     │  agent_registry    │
├──────────────────┤     ├──────────────────┤     ├────────────────────┤
│ id (UUID) PK     │     │ task_id (UUID) PK│     │ agent_key PK       │
│ user_id FK       │     │ user_id FK       │     │ display_name       │
│ domain_key       │     │ agent_queue      │     │ queue_or_url       │
│ content          │     │ status           │     │ dispatch_method    │
│ embedding (vec)  │     │ input_payload    │     │ capabilities (JSON)│
│ metadata (JSON)  │     │ output_result    │     │ supported_domains[]│
└──────────────────┘     └──────────────────┘     │ enabled            │
                                                  └────────────────────┘
┌──────────────────────┐
│    integrations      │
├──────────────────────┤
│ user_id FK + provider│
│ access_token (enc)   │
│ refresh_token (enc)  │
│ expires_at           │
│ metadata (JSON)      │
└──────────────────────┘
┌──────────────────────┐
│  entity_definitions  │
├──────────────────────┤
│ entity_type PK       │
│ display_name         │
│ validation_schema    │
│ (JSON Schema)        │
└──────────────────────┘
```

---

## Container Network (Docker)

```
┌─────────────────────────────────────────────────────────────────┐
│                     Docker Network: krastix_default              │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │    redis     │  │ orchestrator │  │   research_agent     │  │
│  │   :6379      │  │    :8000     │  │       :8001          │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │  crm_agent   │  │  form_agent  │  │    doc_agent         │  │
│  │  crm_queue   │  │  form_queue  │  │       :8002          │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
│                                                                 │
│  ┌──────────────────────┐                                       │
│  │ communication_agent  │                                       │
│  │ communication_queue  │                                       │
│  └──────────────────────┘                                       │
│  (Celery Workers)                    (FastAPI Services)         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
┌───────────────────┐ ┌─────────────┐ ┌──────────────────┐
│ Supabase          │ │ Ollama (LAN)│ │ Groq/xAI Cloud   │
│ PostgreSQL+pgvec  │ │ :11434      │ │ (Vision + Text)  │
└───────────────────┘ └─────────────┘ └──────────────────┘
```

---

## Tech Stack Summary

| Layer | Technology |
|-------|------------|
| **LLM (Text)** | Ollama — qwen2.5:14b-instruct-q5_K_M (local, free) |
| **LLM (Vision)** | Groq — llama-3.2-90b-vision-preview (cloud, fast) |
| **LLM (Text-fast)** | Groq — llama-3.3-70b-versatile (cloud fallback) |
| **Embeddings** | nomic-embed-text (768d) via Ollama |
| **Orchestration** | LangGraph + LangChain |
| **API** | FastAPI (async) + SSE streaming |
| **Queue** | Celery + Redis |
| **Database** | PostgreSQL (Supabase) with RLS |
| **Vector Store** | pgvector |
| **Frontend** | React + Vite (SSE consumer) |
| **Containers** | Docker Compose (7 services) |
| **Schema Validation** | JSON Schema (jsonschema lib) |
| **Concurrency** | Optimistic Concurrency Control (version column) |
| **Reliability** | Agent callbacks + Task Watcher background coroutine |

---

## Directory Structure

```
krastix/
├── ARCHITECTURE.md          # This file
├── README.md                # Project overview & setup guide
├── docker-compose.yml       # Container orchestration
├── init.sql                 # Full database schema
├── migrations/              # Incremental SQL migrations
│   └── 002_universal_engine.sql
├── .env                     # Environment variables
│
├── orchestrator/            # 🧠 The Brain
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
│       ├── main.py          # FastAPI app (/chat, /chat/stream SSE, /callbacks, Task Watcher)
│       ├── graph.py         # LangGraph workflow (Planner + Dispatcher + stream_message)
│       ├── schemas.py       # Pydantic tools + registry helpers
│       └── services/
│           └── memory.py    # Namespace-isolated RAG + Embeddings
│
├── agents/
│   ├── crm_agent/           # 📊 Universal CRM Operations
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── src/worker.py    # upsert_entity + OCC + JSON Schema validation
│   │
│   ├── form_agent/          # 📝 Form Management
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── src/worker.py    # Tally.so integration
│   │
│   ├── communication_agent/ # ✉️ Communications
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── src/worker.py    # approved Gmail send + callback
│   │
│   ├── research_agent/      # 🔍 Web Research
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── README.md
│   │   └── src/
│   │       ├── main.py      # FastAPI app + callback on completion
│   │       ├── graph.py     # LangGraph workflow
│   │       ├── tools.py     # Firecrawl + LinkedIn
│   │       └── models.py    # Pydantic schemas
│   │
│   └── doc_agent/           # 📄 Document Processing (VDU)
│       ├── Dockerfile
│       ├── requirements.txt
│       └── src/
│           ├── main.py      # FastAPI app
│           ├── config.py    # Dual-LLM config (Groq + Ollama)
│           ├── llm_router.py # LLM factory (vision → Groq, text → Ollama)
│           ├── graph.py     # LangGraph pipeline (preprocess→extract→ground→audit)
│           ├── models.py    # Pydantic schemas + PipelineState
│           ├── storage.py   # Supabase storage client
│           └── pipeline/
│               ├── preprocessing.py  # PDF→images, foveal crop
│               ├── extraction.py     # VLM page extraction (Groq Vision)
│               ├── grounding.py      # Schema mapping + chunking
│               └── audit.py          # Vision audit + text refinement
│
├── shared/                  # 📦 Common Utilities
│   ├── database.py          # Async PostgreSQL pool + stale task queries
│   ├── callbacks.py         # Agent → Orchestrator callback utility (httpx)
│   └── mq.py                # Celery configuration
│
└── frontend/                # 🖥️ React UI
    ├── package.json
    ├── vite.config.js
    └── src/
        └── App.jsx          # Chat interface
```

---

## Environment Variables

```ini
# Database
DATABASE_URL=postgresql://user:pass@host:5432/krastix_db

# LLM — Local (Ollama via Tailscale)
OLLAMA_BASE_URL=http://100.115.107.20:11434
OLLAMA_MODEL=qwen2.5:14b-instruct-q5_K_M

# LLM — Cloud (Groq)
GROQ_API_KEY=gsk_...           # Vision + fast text inference
GROQ_VISION_MODEL=llama-3.2-90b-vision-preview
GROQ_TEXT_MODEL=llama-3.3-70b-versatile

# Communications summarize key (xAI/Groq compatible)
GROK_API_KEY=gsk_or_xai_...

# Google OAuth + token encryption for integrations
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/integrations/google/oauth/callback
INTEGRATIONS_ENCRYPTION_KEY=...

# Message Queue
REDIS_URL=redis://localhost:6379/0

# Agent URLs
RESEARCH_AGENT_URL=http://research_agent:8001
DOC_AGENT_URL=http://doc_agent:8002
ORCHESTRATOR_URL=http://orchestrator:8000  # For agent callbacks

# Research Agent APIs
FIRECRAWL_API_KEY=fc_...
SCRAPECREATORS_API_KEY=...

# Supabase (Optional Auth)
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
```

---

## Running the System

### Development (Docker Compose)
```bash
# Start all services
docker-compose up --build

# Services will be available at:
# - Frontend:       http://localhost:5173
# - Orchestrator:   http://localhost:8000
# - Research Agent: http://localhost:8001
# - Doc Agent:      http://localhost:8002
# - Redis:          localhost:6379
```

### Individual Services
```bash
# Orchestrator
cd orchestrator && uvicorn src.main:app --reload --port 8000

# Research Agent
cd agents/research_agent && uvicorn src.main:app --reload --port 8001

# CRM Worker
celery -A shared.mq:celery_app worker -Q crm_queue --loglevel=info

# Form Worker
celery -A shared.mq:celery_app worker -Q form_queue --loglevel=info

# Communication Worker
celery -A shared.mq:celery_app worker -Q communication_queue --loglevel=info
```

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **LangGraph over raw LangChain** | Stateful, resumable workflows with built-in checkpointing |
| **Dual-LLM (Groq + Ollama)** | Groq for fast vision/cloud tasks; Ollama for free local text inference |
| **SSE over WebSocket** | Simpler, HTTP-native, works through proxies; no bidirectional needed |
| **Agent Registry (DB-driven)** | Add/remove agents without code changes; domain-scoped routing |
| **Callback + Task Watcher** | Primary HTTP callback for speed; background poll as safety net |
| **JSON Schema validation** | Entity types defined in DB; new types via SQL, not code deploys |
| **Optimistic Concurrency** | Lock-free concurrent agent writes; Celery auto-retry on conflict |
| **Namespace-isolated memory** | Prevents cross-domain RAG leakage; mandatory domain_key filter |
| **Celery for CRM/Form** | Background tasks with reliable callback on completion |
| **Human-in-the-loop email send** | Reply drafts are generated first, then sent only after explicit user approval |
| **FastAPI for Research/Doc** | Needs HTTP interface for direct invocation + streaming results |
| **pgvector over Pinecone** | Cost-effective, co-located with relational data in Supabase |

---

> **Krastix** is a **Universal Agentic Engine** — an event-driven, multi-agent AI system where the Orchestrator acts as a central cognitive hub that reasons, remembers (per-domain), and delegates work to dynamically registered specialized agents.
