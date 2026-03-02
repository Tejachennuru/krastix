# 🏗️ Krastix System Architecture

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                    KRASTIX PLATFORM                                      │
│                     Universal Agentic Engine — Multi-Agent AI Orchestration               │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

## System Diagram

```mermaid
flowchart TB
    subgraph CLIENT["🖥️ Client Layer"]
        FE[React Frontend<br/>Vite + JSX]
    end

    subgraph BRAIN["🧠 Orchestrator Service :8000"]
        API[FastAPI Endpoints]
        GRAPH[LangGraph<br/>State Machine]
        PLANNER[Planner Node<br/>+ Agent Registry Injection]
        DISPATCHER[Dispatcher Node<br/>Registry-Driven Routing]
        MEMORY[Memory Service<br/>Namespace-Isolated RAG]
    end

    subgraph REGISTRY["📋 Agent Registry"]
        AR[(agent_registry table)]
        ED[(entity_definitions table)]
    end

    subgraph MQ["⚡ Message Queue Layer"]
        REDIS[(Redis<br/>Celery Broker)]
        CQ[crm_queue]
        FQ[form_queue]
    end

    subgraph AGENTS["🤖 Specialized Agents"]
        CRM[CRM Agent<br/>Universal Entity Worker<br/>OCC + Schema Validation]
        FORM[Form Agent<br/>Celery Worker<br/>Tally Integration]
        RESEARCH[Research Agent :8001<br/>FastAPI + LangGraph<br/>Firecrawl + LinkedIn]
    end

    subgraph DATA["💾 Data Layer"]
        SUPABASE[(PostgreSQL<br/>Supabase)]
        PGVECTOR[pgvector<br/>Semantic Memory]
        CHECKPOINTS[LangGraph<br/>Checkpoints]
    end

    subgraph LLM_LAYER["🤖 LLM Layer"]
        OLLAMA[Ollama<br/>qwen2.5:14b-instruct]
        EMBED[nomic-embed-text<br/>768d Embeddings]
    end

    subgraph EXTERNAL["🌐 External APIs"]
        FIRECRAWL[Firecrawl API]
        SCRAPE[ScrapeCreators<br/>LinkedIn API]
        TALLY[Tally.so API]
    end

    %% Client Flow
    FE -->|HTTP REST| API
    
    %% Brain Internal Flow
    API --> GRAPH
    GRAPH --> PLANNER
    PLANNER -->|Tool Call| DISPATCHER
    PLANNER <-->|Namespace-Scoped Query| MEMORY
    PLANNER <-->|Fetch Capabilities| AR
    
    %% Task Dispatch (Registry-Driven)
    DISPATCHER -->|Celery| REDIS
    DISPATCHER -->|HTTP| RESEARCH
    REDIS --> CQ & FQ
    
    %% Agent Consumption
    CQ --> CRM
    FQ --> FORM
    
    %% Agent Results
    CRM -->|Update Task + OCC| SUPABASE
    FORM -->|Update Task| SUPABASE
    RESEARCH -->|/memory/ingest| API
    
    %% Schema Validation
    CRM <-->|Validate against| ED
    
    %% Data Connections
    MEMORY --> PGVECTOR
    GRAPH --> CHECKPOINTS
    PGVECTOR --> SUPABASE
    CHECKPOINTS --> SUPABASE
    
    %% LLM Connections
    PLANNER --> OLLAMA
    MEMORY --> EMBED
    RESEARCH --> OLLAMA
    
    %% External API Connections
    RESEARCH --> FIRECRAWL
    RESEARCH --> SCRAPE
    FORM --> TALLY
```

---

## Core Architecture Patterns

### 1. Agent Registry Pattern
Agents self-register in the `agent_registry` table with their capabilities, dispatch method, and supported domains. The orchestrator queries this at planning time to dynamically inject available agents into the LLM's system prompt, enabling domain-scoped tool routing without hardcoded mappings.

### 2. Schema-on-Demand Validation
Entity types (candidate, lead, contact, etc.) have JSON Schema definitions stored in `entity_definitions`. The CRM agent validates all payloads against these schemas before insert/update, allowing new entity types to be added via SQL without code changes.

### 3. Optimistic Concurrency Control (OCC)
Every entity row has a `version` column. Updates use `WHERE version = $expected` — if another agent modified the entity concurrently, the update affects 0 rows and a `ConcurrencyConflictError` is raised. Celery auto-retries with exponential backoff (up to 3 times).

### 4. Namespace-Isolated Memory
The `search_memory()` method accepts a mandatory `domain_key` parameter. All RAG queries are scoped to the user's current domain, preventing cross-domain context leakage. A composite index on `(user_id, domain_key)` ensures efficient filtering.

---

## Component Breakdown

### 1. 🖥️ Client Layer
| Component | Tech | Port | Description |
|-----------|------|------|-------------|
| **Frontend** | React + Vite | `5173` | Chat UI, Domain selector, Task status polling |

### 2. 🧠 Orchestrator (The Brain)
| Component | Tech | Description |
|-----------|------|-------------|
| **API Layer** | FastAPI | REST endpoints for `/chat`, `/health`, `/memory/ingest`, `/callbacks/task-completed` |
| **Graph Engine** | LangGraph | Stateful workflow: Planner → Dispatcher → END |
| **Planner Node** | Ollama (qwen2.5) | Intent recognition, RAG context + agent registry injection, tool binding |
| **Dispatcher Node** | Registry-Driven | Routes via Celery or HTTP based on `agent_registry.dispatch_method` |
| **Memory Service** | pgvector | Namespace-isolated semantic search with `nomic-embed-text` (768d) |

### 3. ⚡ Message Queue
| Component | Tech | Description |
|-----------|------|-------------|
| **Redis** | Redis 7 Alpine | Celery broker + result backend |
| **Queues** | Celery | `crm_queue`, `form_queue` (research uses HTTP) |

### 4. 🤖 Specialized Agents
| Agent | Type | Port | Capabilities |
|-------|------|------|--------------|
| **CRM Agent** | Celery Worker | - | Universal entity CRUD with OCC + JSON Schema validation |
| **Form Agent** | Celery Worker | - | Tally.so form creation/management |
| **Research Agent** | FastAPI Service | `8001` | Web scraping, LinkedIn profiles, Site mapping |

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
   │ POST /chat      │                  │                │              │
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
   │                 │ 3. LLM: Ollama Call               │              │
   │                 │ (context + agent caps)            │              │
   │                 │                  │                │              │
   │                 │ 4. Tool: DelegateTask             │              │
   │                 │──(registry route)>│               │              │
   │                 │                  │ task.apply()   │              │
   │                 │                  │───────────────>│              │
   │  Response       │                  │                │              │
   │<────────────────│                  │                │ 5. Validate  │
   │                 │                  │                │ schema + OCC │
   │                 │                  │                │─────────────>│
   │                 │                  │                │<─────────────│
   │                 │                  │                │              │
   │                 │ 6. /callbacks/task-completed      │              │
   │                 │<──────────────────────────────────│              │
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
│  ┌──────────────┐  ┌──────────────┐                            │
│  │  crm_agent   │  │  form_agent  │   (Celery Workers)         │
│  │  crm_queue   │  │  form_queue  │                            │
│  └──────────────┘  └──────────────┘                            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │    Supabase (External)        │
              │    PostgreSQL + pgvector      │
              └───────────────────────────────┘
              
                              │
                              ▼
              ┌───────────────────────────────┐
              │    Ollama (Tailscale LAN)     │
              │    100.115.107.20:11434       │
              └───────────────────────────────┘
```

---

## Tech Stack Summary

| Layer | Technology |
|-------|------------|
| **LLM** | Ollama — qwen2.5:14b-instruct-q5_K_M |
| **Embeddings** | nomic-embed-text (768d) via Ollama |
| **Orchestration** | LangGraph + LangChain |
| **API** | FastAPI (async) |
| **Queue** | Celery + Redis |
| **Database** | PostgreSQL (Supabase) with RLS |
| **Vector Store** | pgvector |
| **Frontend** | React + Vite |
| **Containers** | Docker Compose |
| **Schema Validation** | JSON Schema (jsonschema lib) |
| **Concurrency** | Optimistic Concurrency Control (version column) |

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
│       ├── main.py          # FastAPI app (lifespan, /chat, /callbacks, /memory)
│       ├── graph.py         # LangGraph workflow (Planner + Registry Dispatcher)
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
│   └── research_agent/      # 🔍 Web Research
│       ├── Dockerfile
│       ├── requirements.txt
│       ├── README.md
│       └── src/
│           ├── main.py      # FastAPI app
│           ├── graph.py     # LangGraph workflow
│           ├── tools.py     # Firecrawl + LinkedIn
│           └── models.py    # Pydantic schemas
│
├── shared/                  # 📦 Common Utilities
│   ├── database.py          # Async PostgreSQL pool + agent registry queries
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

# LLM (Ollama via Tailscale)
OLLAMA_BASE_URL=http://100.115.107.20:11434
OLLAMA_MODEL=qwen2.5:14b-instruct-q5_K_M

# Message Queue
REDIS_URL=redis://localhost:6379/0

# Research Agent
RESEARCH_AGENT_URL=http://research_agent:8001

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
```

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **LangGraph over raw LangChain** | Stateful, resumable workflows with built-in checkpointing |
| **Ollama over cloud LLM** | Privacy, cost, control — runs on LAN via Tailscale |
| **Agent Registry (DB-driven)** | Add/remove agents without code changes; domain-scoped routing |
| **JSON Schema validation** | Entity types defined in DB; new types via SQL, not code deploys |
| **Optimistic Concurrency** | Lock-free concurrent agent writes; Celery auto-retry on conflict |
| **Namespace-isolated memory** | Prevents cross-domain RAG leakage; mandatory domain_key filter |
| **Celery for CRM/Form** | Fire-and-forget tasks that don't need real-time responses |
| **FastAPI for Research** | Needs HTTP interface for direct invocation + streaming results |
| **pgvector over Pinecone** | Cost-effective, co-located with relational data in Supabase |

---

> **Krastix** is a **Universal Agentic Engine** — an event-driven, multi-agent AI system where the Orchestrator acts as a central cognitive hub that reasons, remembers (per-domain), and delegates work to dynamically registered specialized agents.
