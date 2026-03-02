# Krastix — Universal Agentic Engine

A multi-agent AI orchestration platform where a central LangGraph orchestrator reasons, remembers, and delegates work to dynamically registered specialized agents.

## What It Does

Krastix is a **domain-agnostic agentic engine** for building AI-powered workflows. The orchestrator:

1. **Reasons** — Plans responses using an LLM (Ollama qwen2.5) with RAG context injection
2. **Remembers** — Stores and retrieves semantic memories scoped per domain via pgvector
3. **Delegates** — Routes sub-tasks to specialized agents via a registry-driven dispatcher
4. **Validates** — Enforces JSON Schema on all entity writes with optimistic concurrency

Current agents: **CRM** (universal entity management), **Form Builder** (Tally.so), **Research** (Firecrawl + LinkedIn scraping).

---

## Quick Start

### Prerequisites

- Docker & Docker Compose
- Ollama running on a reachable host (default: Tailscale LAN node)
- Supabase project (PostgreSQL + pgvector)
- API keys: Firecrawl, ScrapeCreators (for research agent)

### 1. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```ini
# Database (Supabase PostgreSQL)
DATABASE_URL=postgresql://postgres.xxx:password@host:5432/postgres

# LLM (Ollama instance — Tailscale or local)
OLLAMA_BASE_URL=http://100.115.107.20:11434
OLLAMA_MODEL=qwen2.5:14b-instruct-q5_K_M

# Message Queue
REDIS_URL=redis://redis:6379/0

# External APIs
FIRECRAWL_API_KEY=fc_...
SCRAPECREATORS_API_KEY=...

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
```

### 3. Start Services

```bash
docker compose up --build -d
```

This starts 5 containers:

| Service | Container | Port | Role |
|---------|-----------|------|------|
| Redis | krastix-redis | 6379 | Celery broker |
| Orchestrator | krastix-orchestrator | 8000 | LangGraph brain |
| Research Agent | krastix-research-agent | 8001 | Web research (FastAPI) |
| CRM Agent | krastix-crm-agent | — | Entity management (Celery) |
| Form Agent | krastix-form-agent | — | Tally.so forms (Celery) |

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
                                  ↕              ↓
                           Memory (pgvector)   Agent Registry
                                               ↓
                                    ┌──────────┼──────────┐
                                    ↓          ↓          ↓
                                CRM Agent  Form Agent  Research Agent
                                (Celery)   (Celery)    (HTTP/FastAPI)
                                    ↓          ↓          ↓
                                PostgreSQL (Supabase + pgvector)
```

**Key patterns:**
- **Agent Registry** — Agents register in `agent_registry` table with capabilities and dispatch method. Orchestrator queries at planning time for domain-scoped routing.
- **Schema-on-Demand** — Entity types defined in `entity_definitions` with JSON Schema. New types added via SQL, not code.
- **Optimistic Concurrency** — `version` column on entities. Concurrent writes detected and retried automatically.
- **Namespace Isolation** — Memory searches scoped by `domain_key` to prevent cross-domain RAG leakage.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system diagram, database schema, and design decisions.

---

## API Reference

### Orchestrator (`:8000`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/chat` | Send a message to the orchestrator |
| `POST` | `/callbacks/task-completed` | Agent callback on task completion |
| `POST` | `/memory/ingest` | Ingest text into semantic memory |
| `POST` | `/api/v1/batch/process` | Process pending batch jobs |
| `GET` | `/health` | Health check |

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
├── docker-compose.yml          # 5 services: redis, orchestrator, crm, form, research
├── init.sql                    # Full PostgreSQL schema (entities, registry, pgvector)
├── migrations/                 # Incremental SQL migrations
├── ARCHITECTURE.md             # Detailed architecture docs
│
├── orchestrator/src/
│   ├── main.py                 # FastAPI app with lifespan
│   ├── graph.py                # LangGraph: Planner + Registry Dispatcher
│   ├── schemas.py              # DelegateTask/QueueBatch tools + registry helpers
│   └── services/memory.py      # Namespace-isolated RAG (pgvector + nomic-embed-text)
│
├── agents/
│   ├── crm_agent/src/worker.py       # Universal entity CRUD + OCC + JSON Schema
│   ├── form_agent/src/worker.py      # Tally.so form management
│   └── research_agent/src/           # Firecrawl + LinkedIn (FastAPI + LangGraph)
│
├── shared/
│   ├── database.py             # Async PostgreSQL pool + agent registry queries
│   └── mq.py                   # Celery configuration
│
└── frontend/src/               # React + Vite chat interface
```

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| LLM | Ollama (qwen2.5:14b-instruct-q5_K_M) |
| Embeddings | nomic-embed-text (768d) |
| Orchestration | LangGraph + LangChain |
| API | FastAPI (async) |
| Queue | Celery + Redis 7 |
| Database | PostgreSQL (Supabase) + pgvector |
| Validation | JSON Schema (jsonschema) |
| Concurrency | Optimistic Concurrency Control |
| Containers | Docker Compose |
| Frontend | React + Vite |

---

## Development

### Running Individual Services

```bash
# Orchestrator (with hot reload)
cd orchestrator && uvicorn src.main:app --reload --port 8000

# Research Agent
cd agents/research_agent && uvicorn src.main:app --reload --port 8001

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
