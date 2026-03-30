# Krastix — Universal Agentic Engine

A multi-agent AI orchestration platform where a central LangGraph orchestrator reasons, remembers, and delegates work to dynamically registered specialized agents. Features **dual-LLM routing** (Groq Cloud + local Ollama), **SSE token streaming**, and **reliable task delivery** with agent callbacks + a background Task Watcher.

## What It Does

Krastix is a **domain-agnostic agentic engine** for building AI-powered workflows. The orchestrator:

1. **Reasons** — Plans responses using LLMs (Ollama local + Groq cloud) with RAG context injection
2. **Remembers** — Stores and retrieves semantic memories scoped per domain via pgvector
3. **Delegates** — Routes sub-tasks to specialized agents via a registry-driven dispatcher
4. **Streams** — Delivers LLM tokens in real-time to the frontend via Server-Sent Events
5. **Validates** — Enforces JSON Schema on all entity writes with optimistic concurrency
6. **Watches** — Background Task Watcher catches stale/lost tasks as a safety net

Current agents: **CRM** (universal entity management), **Form Builder** (Tally.so), **Research** (Firecrawl + LinkedIn scraping), **Doc Agent** (PDF/image extraction via Groq Vision).

---

## Quick Start

### Prerequisites

- Docker & Docker Compose
- Ollama running on a reachable host (default: Tailscale LAN node)
- Supabase project (PostgreSQL + pgvector)
- Groq API key (for vision + fast text models)
- API keys: Firecrawl, ScrapeCreators (for research agent)

### 1. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```ini
# Database (Supabase PostgreSQL)
DATABASE_URL=postgresql://postgres.xxx:password@host:5432/postgres

# LLM — Local (Ollama via Tailscale)
OLLAMA_BASE_URL=http://100.115.107.20:11434
OLLAMA_MODEL=qwen2.5:14b-instruct-q5_K_M

# LLM — Cloud (Groq)
GROQ_API_KEY=gsk_...              # Vision + fast text inference
GROQ_VISION_MODEL=llama-3.2-90b-vision-preview
GROQ_TEXT_MODEL=llama-3.3-70b-versatile

# Message Queue
REDIS_URL=redis://redis:6379/0

# External APIs
FIRECRAWL_API_KEY=fc_...
SCRAPECREATORS_API_KEY=...

# Google OAuth (Communication Agent)
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/integrations/google/oauth/callback

# Integration secret encryption (Fernet key, generate once and keep private)
INTEGRATIONS_ENCRYPTION_KEY=...

# Optional keepalive token for external cron pings
KEEPALIVE_TOKEN=...

# Supabase Auth (optional)
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
```

### 2. Initialize Database

Run the full schema against your Supabase PostgreSQL:

```bash
psql "$DATABASE_URL" -f init.sql
```

Or for existing databases, apply the incremental migration:

```bash
psql "$DATABASE_URL" -f migrations/002_universal_engine.sql
psql "$DATABASE_URL" -f migrations/003_doc_agent.sql
psql "$DATABASE_URL" -f migrations/004_communication_agent.sql
```

### 3. Start Services

```bash
docker compose up --build -d
```

This starts 6 containers:

| Service | Container | Port | Role |
|---------|-----------|------|------|
| Redis | krastix-redis | 6379 | Celery broker |
| Orchestrator | krastix-orchestrator | 8000 | LangGraph brain + SSE streaming + Task Watcher |
| Research Agent | krastix-research-agent | 8001 | Web research (FastAPI) |
| Doc Agent | krastix-doc-agent | 8002 | Document extraction (Groq Vision + LangGraph) |
| CRM Agent | krastix-crm-agent | — | Entity management (Celery) |
| Form Agent | krastix-form-agent | — | Tally.so forms (Celery) |
| Communication Agent | krastix-communication-agent | — | Gmail send after draft approval (Celery) |

### 4. Verify

```bash
# Health checks
curl http://localhost:8000/health
curl http://localhost:8001/health

# Send a chat message
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "YOUR_UUID",
    "domain": "recruitment",
    "message": "Hello, what can you do?",
    "session_id": "test-session-001"
  }'
```

---

## Architecture Overview

```
User → FastAPI Orchestrator → LangGraph (Planner → Dispatcher)
          │ (SSE stream)            ↕              ↓
          ▼                   Memory (pgvector)   Agent Registry
   Token-by-token                                  ↓
   to frontend              ┌───────────────────────┼─────────────┐
                            ↓          ↓          ↓             ↓
                        CRM Agent  Form Agent  Research Agent  Doc Agent
                        (Celery)   (Celery)    (HTTP)         (HTTP/Groq)
                            │          │          │             │
                            └────── POST /callbacks/task-completed ─────┘
                                       ↓
                              PostgreSQL (Supabase + pgvector)
```

**Key patterns:**
- **Dual-LLM Routing** — Groq (cloud) for vision/speed-critical tasks; Ollama (local) for text reasoning at zero API cost.
- **SSE Token Streaming** — `/chat/stream` delivers LLM tokens to the frontend in real-time via Server-Sent Events.
- **Agent Registry** — Agents register in `agent_registry` with capabilities + dispatch method. Orchestrator queries at planning time.
- **Callback + Task Watcher** — Agents call back on completion; background watcher catches anything missed after 10 minutes.
- **Schema-on-Demand** — Entity types defined in `entity_definitions` with JSON Schema. New types via SQL.
- **Optimistic Concurrency** — `version` column on entities. Concurrent writes detected and retried automatically.
- **Namespace Isolation** — Memory searches scoped by `domain_key` to prevent cross-domain RAG leakage.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system diagram, database schema, and design decisions.

---

## API Reference

### Orchestrator (`:8000`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/chat` | Send a message (non-streaming response) |
| `POST` | `/api/v1/chat/stream` | Send a message (SSE token streaming) |
| `POST` | `/callbacks/task-completed` | Agent callback on task completion |
| `POST` | `/memory/ingest` | Ingest text into semantic memory |
| `POST` | `/api/v1/batch/process` | Process pending batch jobs |
| `GET` | `/health` | Health check |
| `GET` | `/health/keepalive` | Lightweight DB keepalive ping (optional token via `X-Keepalive-Token`) |

#### POST `/api/v1/chat`

```json
{
  "user_id": "uuid",
  "domain": "recruitment",
  "message": "Find me senior React developers",
  "session_id": "optional-thread-id"
}
```

Response:
```json
{
  "response": "I'll research that for you...",
  "task_id": "uuid-of-delegated-task (if any)",
  "session_id": "thread-id"
}
```

#### POST `/api/v1/chat/stream` (SSE)

Same request body as `/api/v1/chat`. Returns `text/event-stream`:

```
data: {"event": "token", "data": "I'll"}
data: {"event": "token", "data": " research"}
data: {"event": "tool_start", "data": {"tool": "DelegateTask", "input": {...}}}
data: {"event": "tool_result", "data": {"tool": "DelegateTask", "output": "..."}}
data: {"event": "done", "data": {"response": "...", "task_id": "uuid"}}
```

The frontend connects via `fetch()` + `ReadableStream` and falls back to `/api/v1/chat` on failure.

#### POST `/memory/ingest`

```json
{
  "user_id": "uuid",
  "domain": "recruitment",
  "content": "Research results about React developers...",
  "metadata": { "source": "research_agent", "task_type": "GENERAL_SEARCH" }
}
```

### Research Agent (`:8001`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/research/run` | Execute a research task |
| `GET` | `/health` | Health check |

#### POST `/research/run`

```json
{
  "user_id": "uuid",
  "task_type": "GENERAL_SEARCH",
  "query_or_url": "latest AI recruitment tools 2025",
  "context_metadata": { "task_id": "uuid", "session_id": "thread-id" }
}
```

Supported `task_type` values: `GENERAL_SEARCH`, `QUICK_SCRAPE`, `SITE_MAP`, `LINKEDIN_PROFILE`

---

## Keeping Free-Tier DB Warm (GitHub Actions)

This repo includes a scheduler at [.github/workflows/db-keepalive.yml](.github/workflows/db-keepalive.yml) that pings your PostgreSQL database directly every 12 hours.

This does **not** require the orchestrator app to be running.

### 2. Set GitHub repository secrets

Go to GitHub -> Settings -> Secrets and variables -> Actions -> New repository secret:

- `DATABASE_URL`: full PostgreSQL connection string used by your deployment

### 3. Verify

Run the workflow manually once from Actions tab (`DB Keepalive`) and ensure it passes.

---

## Adding a New Agent

1. **Register in database:**

```sql
INSERT INTO agent_registry (agent_key, display_name, queue_or_url, dispatch_method, capabilities, supported_domains)
VALUES (
  'my_agent_v1',
  'My Custom Agent',
  'my_queue',
  'celery',
  '{"actions": ["do_thing"], "description": "Does the thing", "celery_task_name": "agents.my_worker.execute_task"}',
  '["recruitment", "sales"]'
);
```

2. **Add queue to domain config:**

```sql
UPDATE domain_configs
SET allowed_agent_queues = allowed_agent_queues || '"my_queue"'
WHERE domain_key = 'recruitment';
```

3. **Create the agent** in `agents/my_agent/`:
   - `Dockerfile` — copy from crm_agent
   - `requirements.txt` — celery, redis, asyncpg, your deps
   - `src/worker.py` — Celery task matching the `celery_task_name` above

4. **Add to `docker-compose.yml`** following the crm_agent pattern.

The orchestrator will automatically discover your agent via the registry and include its capabilities in LLM prompts for matching domains.

---

## Adding a New Entity Type

No code changes required. Just add a schema definition:

```sql
INSERT INTO entity_definitions (entity_type, display_name, validation_schema) VALUES (
  'invoice',
  'Invoice',
  '{
    "type": "object",
    "required": ["amount", "currency", "client_name"],
    "properties": {
      "amount": { "type": "number", "minimum": 0 },
      "currency": { "type": "string", "enum": ["USD", "EUR", "GBP"] },
      "client_name": { "type": "string" },
      "due_date": { "type": "string", "format": "date" }
    }
  }'
);
```

The CRM agent will validate all `upsert_entity` calls for type `invoice` against this schema automatically.

---

## Project Structure

```
krastix/
├── docker-compose.yml          # 6 services: redis, orchestrator, crm, form, research, doc
├── init.sql                    # Full PostgreSQL schema (entities, registry, pgvector)
├── migrations/                 # Incremental SQL migrations
├── ARCHITECTURE.md             # Detailed architecture docs
│
├── orchestrator/src/
│   ├── main.py                 # FastAPI app (/chat, /chat/stream SSE, Task Watcher)
│   ├── graph.py                # LangGraph: Planner + Dispatcher + stream_message()
│   ├── schemas.py              # DelegateTask/QueueBatch tools + registry helpers
│   └── services/memory.py      # Namespace-isolated RAG (pgvector + nomic-embed-text)
│
├── agents/
│   ├── crm_agent/src/worker.py       # Universal entity CRUD + OCC + callback
│   ├── form_agent/src/worker.py      # Tally.so form management + callback
│   ├── research_agent/src/           # Firecrawl + LinkedIn (FastAPI + LangGraph)
│   └── doc_agent/src/                # PDF/image VDU pipeline (Groq Vision + Ollama)
│       ├── llm_router.py             # Dual-LLM factory (vision→Groq, text→Ollama)
│       └── pipeline/                 # preprocess → extract → ground → audit
│
├── shared/
│   ├── database.py             # Async PostgreSQL pool + stale task queries
│   ├── callbacks.py            # Agent → Orchestrator callback (httpx)
│   └── mq.py                   # Celery configuration
│
└── frontend/src/               # React + Vite chat interface (SSE consumer)
```

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| LLM (Text) | Ollama (qwen2.5:14b-instruct-q5_K_M) — local, free |
| LLM (Vision) | Groq (llama-3.2-90b-vision-preview) — cloud, fast |
| Embeddings | nomic-embed-text (768d) |
| Orchestration | LangGraph + LangChain |
| API | FastAPI (async) + SSE streaming |
| Queue | Celery + Redis 7 |
| Database | PostgreSQL (Supabase) + pgvector |
| Validation | JSON Schema (jsonschema) |
| Concurrency | Optimistic Concurrency Control |
| Reliability | Agent callbacks + Task Watcher |
| Containers | Docker Compose (6 services) |
| Frontend | React + Vite (SSE consumer) |

---

## Development

### Running Individual Services

```bash
# Orchestrator (with hot reload)
cd orchestrator && uvicorn src.main:app --reload --port 8000

# Research Agent
cd agents/research_agent && uvicorn src.main:app --reload --port 8001

# Doc Agent
cd agents/doc_agent && uvicorn src.main:app --reload --port 8002

# CRM Worker
celery -A shared.mq:celery_app worker -Q crm_queue --loglevel=info

# Form Worker
celery -A shared.mq:celery_app worker -Q form_queue --loglevel=info
```

### Logs

```bash
# All containers
docker compose logs -f

# Specific service
docker compose logs -f orchestrator
docker compose logs -f crm_agent
```

### Testing from Inside Docker

```bash
# Send a chat message from inside the orchestrator container
docker exec krastix-orchestrator python -c "
import httpx, asyncio
async def test():
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post('http://localhost:8000/api/v1/chat', json={
            'user_id': 'YOUR_UUID',
            'domain': 'recruitment',
            'message': 'Research the latest AI tools',
            'session_id': 'test-001'
        })
        print(r.status_code, r.json())
asyncio.run(test())
"
```

---

## License

Private / Proprietary
