# 🏗️ Krastix System Architecture

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                    KRASTIX PLATFORM                                      │
│                        Multi-Agent AI Orchestration System                               │
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
        PLANNER[Planner Node]
        DISPATCHER[Dispatcher Node]
        MEMORY[Memory Service<br/>RAG + Embeddings]
    end

    subgraph MQ["⚡ Message Queue Layer"]
        REDIS[(Redis<br/>Celery Broker)]
        CQ[crm_queue]
        FQ[form_queue]
        RQ[research_queue]
    end

    subgraph AGENTS["🤖 Specialized Agents"]
        CRM[CRM Agent<br/>Celery Worker]
        FORM[Form Agent<br/>Celery Worker<br/>Tally Integration]
        RESEARCH[Research Agent :8001<br/>FastAPI + LangGraph<br/>Firecrawl + LinkedIn]
    end

    subgraph DATA["💾 Data Layer"]
        SUPABASE[(PostgreSQL<br/>Supabase)]
        PGVECTOR[pgvector<br/>Semantic Memory]
        CHECKPOINTS[LangGraph<br/>Checkpoints]
    end

    subgraph EXTERNAL["🌐 External APIs"]
        GEMINI[Google Gemini<br/>2.0-flash]
        EMBED[text-embedding-004]
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
    PLANNER <-->|RAG Query| MEMORY
    
    %% Task Dispatch
    DISPATCHER -->|Publish| REDIS
    REDIS --> CQ & FQ & RQ
    
    %% Agent Consumption
    CQ --> CRM
    FQ --> FORM
    RQ -.->|HTTP Trigger| RESEARCH
    
    %% Agent Results
    CRM -->|Update Task| SUPABASE
    FORM -->|Update Task| SUPABASE
    RESEARCH -->|/memory/ingest| API
    
    %% Data Connections
    MEMORY --> PGVECTOR
    GRAPH --> CHECKPOINTS
    PGVECTOR --> SUPABASE
    CHECKPOINTS --> SUPABASE
    CRM --> SUPABASE
    FORM --> SUPABASE
    
    %% External API Connections
    PLANNER --> GEMINI
    MEMORY --> EMBED
    RESEARCH --> FIRECRAWL
    RESEARCH --> SCRAPE
    FORM --> TALLY
```

---

## Component Breakdown

### 1. 🖥️ Client Layer
| Component | Tech | Port | Description |
|-----------|------|------|-------------|
| **Frontend** | React + Vite | `5173` | Chat UI, Domain selector, Task status polling |

### 2. 🧠 Orchestrator (The Brain)
| Component | Tech | Description |
|-----------|------|-------------|
| **API Layer** | FastAPI | REST endpoints for `/chat`, `/health`, `/memory/ingest` |
| **Graph Engine** | LangGraph | Stateful workflow: Planner → Dispatcher → END |
| **Planner Node** | Gemini 2.0 | Intent recognition, RAG context injection, tool binding |
| **Dispatcher Node** | Celery | Publishes tasks to Redis queues |
| **Memory Service** | pgvector | Semantic search with `text-embedding-004` (768d) |

### 3. ⚡ Message Queue
| Component | Tech | Description |
|-----------|------|-------------|
| **Redis** | Redis 7 Alpine | Celery broker + result backend |
| **Queues** | Celery | `crm_queue`, `form_queue`, `research_queue` |

### 4. 🤖 Specialized Agents
| Agent | Type | Port | Capabilities |
|-------|------|------|--------------|
| **CRM Agent** | Celery Worker | - | CRUD on Candidates, Leads, Entities |
| **Form Agent** | Celery Worker | - | Tally.so form creation/management |
| **Research Agent** | FastAPI Service | `8001` | Web scraping, LinkedIn profiles, Site mapping |

### 5. 💾 Data Layer
| Component | Tech | Description |
|-----------|------|-------------|
| **PostgreSQL** | Supabase | Primary data store |
| **pgvector** | Extension | Vector similarity search |
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
   │                 │ RAG: Search Memory                │              │
   │                 │─────────────────────────────────────────────────>│
   │                 │<─────────────────────────────────────────────────│
   │                 │                  │                │              │
   │                 │ LLM: Gemini Call │                │              │
   │                 │ (with context)   │                │              │
   │                 │                  │                │              │
   │                 │ Tool: DelegateTask               │              │
   │                 │─────────────────>│                │              │
   │                 │                  │ task.apply()   │              │
   │                 │                  │───────────────>│              │
   │  Response       │                  │                │              │
   │<────────────────│                  │                │ Execute      │
   │                 │                  │                │─────────────>│
   │                 │                  │                │<─────────────│
   │                 │                  │                │              │
   │                 │       /memory/ingest (Research)   │              │
   │                 │<──────────────────────────────────│              │
   │                 │─────────────────────────────────────────────────>│
   │                 │                  │                │              │
```

---

## Database Schema (Key Tables)

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│    profiles     │     │  domain_configs  │     │    entities     │
├─────────────────┤     ├──────────────────┤     ├─────────────────┤
│ id (UUID) PK    │     │ domain_key PK    │     │ id (UUID) PK    │
│ email           │     │ display_name     │     │ user_id FK      │
│ tier            │     │ system_prompt    │     │ entity_type FK  │
│ credits         │     │ allowed_agents[] │     │ data (JSONB)    │
└─────────────────┘     └──────────────────┘     │ derived_skills[]│
                                                 └─────────────────┘
┌─────────────────┐     ┌──────────────────┐
│    memories     │     │   agent_tasks    │
├─────────────────┤     ├──────────────────┤
│ id (UUID) PK    │     │ task_id (UUID) PK│
│ user_id FK      │     │ user_id FK       │
│ domain_key      │     │ agent_queue      │
│ content         │     │ status           │
│ embedding (vec) │     │ input_payload    │
│ metadata (JSON) │     │ output_result    │
└─────────────────┘     └──────────────────┘
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
```

---

## Tech Stack Summary

| Layer | Technology |
|-------|------------|
| **LLM** | Google Gemini 2.0-flash |
| **Embeddings** | text-embedding-004 (768d) |
| **Orchestration** | LangGraph + LangChain |
| **API** | FastAPI (async) |
| **Queue** | Celery + Redis |
| **Database** | PostgreSQL (Supabase) |
| **Vector Store** | pgvector |
| **Frontend** | React + Vite |
| **Containers** | Docker Compose |

---

## Directory Structure

```
krastix/
├── ARCHITECTURE.md          # This file
├── docker-compose.yml       # Container orchestration
├── init.sql                 # Database schema
├── .env                     # Environment variables
│
├── orchestrator/            # 🧠 The Brain
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
│       ├── main.py          # FastAPI app
│       ├── graph.py         # LangGraph workflow
│       ├── schemas.py       # Pydantic models + Tools
│       └── services/
│           └── memory.py    # RAG + Embeddings
│
├── agents/
│   ├── crm_agent/           # 📊 CRM Operations
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── src/worker.py    # Celery tasks
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
│   ├── database.py          # Async PostgreSQL pool
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

# LLM
GEMINI_API_KEY=AI...

# Message Queue
REDIS_URL=redis://localhost:6379/0

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
| **LangGraph over raw LangChain** | Provides stateful, resumable workflows with built-in checkpointing |
| **Celery for CRM/Form Agents** | Fire-and-forget tasks that don't need real-time responses |
| **FastAPI for Research Agent** | Needs HTTP interface for direct invocation + streaming results |
| **pgvector over Pinecone** | Cost-effective, co-located with relational data in Supabase |
| **Redis as Broker** | Lightweight, fast, supports both Celery and caching |
| **Gemini 2.0-flash** | Fast inference, good tool-calling, cost-effective |

---

> **Krastix** is an **Event-Driven, Multi-Agent AI System** where the Orchestrator acts as a central cognitive hub that reasons, remembers, and delegates work to specialized agents.
