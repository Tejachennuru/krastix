"""
Document Agent — Centralised Configuration.

All tunables are read from environment variables with sensible defaults.

LLM Routing Strategy:
  - VISION tasks (extraction, foveal re-scan) → Groq API (cloud, fast, multimodal)
  - TEXT tasks  (audit refinement, grounding)  → Local Ollama qwen2.5:14b (free)
"""

import os

# ── Groq Cloud API (Vision-capable) ────────────────────────────────
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", os.getenv("GROK_API_KEY", ""))
GROQ_VISION_MODEL: str = os.getenv(
    "GROQ_VISION_MODEL", "llama-3.2-90b-vision-preview"
)
GROQ_TEXT_MODEL: str = os.getenv(
    "GROQ_TEXT_MODEL", "llama-3.3-70b-versatile"
)

# ── Local Ollama (Text-only — audit / refinement) ──────────────────
OLLAMA_BASE_URL: str = os.getenv(
    "OLLAMA_BASE_URL", "http://100.115.107.20:11434"
).rstrip("/")

OLLAMA_MODEL: str = os.getenv(
    "OLLAMA_MODEL", "qwen2.5:14b-instruct-q5_K_M"
)

# Legacy: still accept VLM_MODEL env var for Ollama vision (if available)
VLM_MODEL: str = os.getenv("VLM_MODEL", "qwen2.5-vl:7b")

# ── Supabase ────────────────────────────────────────────────────────
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
STORAGE_BUCKET: str = os.getenv("STORAGE_BUCKET", "documents")

# ── Orchestrator ────────────────────────────────────────────────────
ORCHESTRATOR_URL: str = os.getenv(
    "ORCHESTRATOR_URL", "http://orchestrator:8000"
)

# ── Database ────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv("DATABASE_URL", "")

# ── Processing Limits ───────────────────────────────────────────────
MAX_PAGES: int = int(os.getenv("MAX_PAGES", "50"))
DPI: int = int(os.getenv("DPI", "300"))
MAX_AUDIT_RETRIES: int = int(os.getenv("MAX_AUDIT_RETRIES", "2"))
CONFIDENCE_THRESHOLD: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.85"))
FOVEAL_SCALE: float = float(os.getenv("FOVEAL_SCALE", "2.0"))
