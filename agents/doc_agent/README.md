# 📑 Document Agent (VDU) — Krastix Platform

**Multi-stage Vision-Document-Understanding pipeline** that transforms
unstructured binary files (PDFs, images) into structured, schema-validated
entities with spatial grounding.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    VDU Pipeline (LangGraph)                  │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌────────┐  ┌──────────────┐ │
│  │Preprocess├──►Extract   ├──►Ground  ├──►Audit         │ │
│  │PDF→Image │  │Qwen VL   │  │Schema  │  │Reflect-Refine│ │
│  └──────────┘  └──────────┘  │Mapping │  └──────┬───────┘ │
│                               └────────┘         │         │
│                    ┌──────────────────────────────┘         │
│                    │ Foveal Re-scan (2× crop)               │
│                    └────────────────────────────────────────┘
└─────────────────────────────────────────────────────────────┘
```

### Pipeline Stages

| Stage | Description |
|-------|-------------|
| **1. Preprocess** | PDF→Image (300 DPI) via PyMuPDF; layout anchoring |
| **2. Extract** | Multimodal extraction via Qwen 2.5-VL / 3.0-VL (Ollama) |
| **3. Ground** | Map to `entity_definitions` schema; Pydantic + jsonschema validation |
| **4. Audit** | Vision-audit comparing JSON vs original image; foveal re-scan on errors |

## Integration Points

| Target | Method | Purpose |
|--------|--------|---------|
| **Orchestrator** | HTTP `POST /document/run` | Receives task dispatch |
| **Orchestrator** | HTTP `POST /memory/ingest` | Pushes semantic chunks for RAG |
| **Orchestrator** | HTTP `POST /callbacks/task-completed` | Reports task completion |
| **Supabase Storage** | REST API | Downloads source files, uploads artifacts |
| **Supabase DB** | asyncpg | Fetches entity schemas from `entity_definitions` |
| **Ollama** | LangChain ChatOllama | VLM inference (multimodal) |

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VLM_MODEL` | **Yes** | `qwen2.5-vl:7b` | Vision-Language model in Ollama |
| `OLLAMA_BASE_URL` | Yes | `http://100.115.107.20:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | No | `qwen2.5:14b-instruct-q5_K_M` | Text-only model (refiner) |
| `DATABASE_URL` | Yes | — | PostgreSQL connection string |
| `SUPABASE_URL` | Yes | — | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | — | Service role key for Storage + DB |
| `ORCHESTRATOR_URL` | Yes | `http://orchestrator:8000` | Orchestrator base URL |
| `STORAGE_BUCKET` | No | `documents` | Supabase Storage bucket name |
| `DPI` | No | `300` | Rendering resolution for PDF pages |
| `MAX_PAGES` | No | `50` | Max pages to process per document |
| `MAX_AUDIT_RETRIES` | No | `2` | Max Reflect-Refine loops |
| `CONFIDENCE_THRESHOLD` | No | `0.85` | Min confidence for foveal corrections |

## Quick Start

### 1. Pull the VLM model
```bash
# On your Ollama host machine:
ollama pull qwen2.5-vl:7b
# Or for higher quality:
ollama pull qwen2.5-vl:14b
```

### 2. Set environment variables
```bash
# .env file
VLM_MODEL=qwen2.5-vl:7b
```

### 3. Run the migration
```bash
# Execute in your Supabase SQL editor or via psql:
psql -f migrations/003_doc_agent.sql
```

### 4. Start with Docker Compose
```bash
docker compose up -d doc_agent
```

### 5. Verify
```bash
curl http://localhost:8002/health
```

## API

### `POST /document/run`

Dispatched by the orchestrator via the agent registry.

```json
{
  "user_id": "a0eebc99-...",
  "instruction": "Extract candidate data from file_id: uploads/resume.pdf. Entity type: candidate.",
  "context_metadata": {
    "task_id": "uuid-task-id",
    "session_id": "uuid-session-id",
    "domain_key": "HR_RECRUITER"
  }
}
```

### `GET /health`

Returns VLM model availability and database connectivity.

## Agent Registry Entry

```json
{
  "agent_key": "doc_vdu_v1",
  "display_name": "Document Agent (VDU)",
  "queue_or_url": "http://doc_agent:8002",
  "dispatch_method": "http",
  "capabilities": {
    "actions": ["extract", "ocr", "table_parsing", "visual_verification"],
    "description": "Extracts structured data from PDFs and images using VLM",
    "http_endpoint": "/document/run"
  },
  "supported_domains": ["HR_RECRUITER", "PERSONAL_ASSISTANT", "SALES_LEAD_GEN", "FINANCE", "LEGAL"]
}
```

## File Structure

```
agents/doc_agent/
├── Dockerfile
├── requirements.txt
├── README.md
└── src/
    ├── __init__.py
    ├── config.py            # Environment configuration
    ├── models.py            # Pydantic models + LangGraph state
    ├── main.py              # FastAPI service entry
    ├── graph.py             # LangGraph Reflect-Refine pipeline
    ├── storage.py           # Supabase Storage client
    └── pipeline/
        ├── __init__.py
        ├── preprocessing.py  # Stage 1: PDF→Image, foveal crop
        ├── extraction.py     # Stage 2: VLM extraction (Qwen VL)
        ├── grounding.py      # Stage 3: Schema mapping + validation
        └── audit.py          # Stage 4: Vision-audit + foveal re-scan
```
