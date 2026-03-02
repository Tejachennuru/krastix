"""
Document Agent — Centralised Configuration.

All tunables are read from environment variables with sensible defaults.
The VLM_MODEL var controls which vision-language model Ollama serves
(e.g. qwen2.5-vl:7b, qwen2.5-vl:14b, qwen3-vl:7b).
"""

import os

# ── Ollama VLM ──────────────────────────────────────────────────────
OLLAMA_BASE_URL: str = os.getenv(
    "OLLAMA_BASE_URL", "http://100.115.107.20:11434"
).rstrip("/")

# Vision-Language Model (multimodal)
VLM_MODEL: str = os.getenv("VLM_MODEL", "qwen2.5-vl:7b")

# Text-only model (used as Refiner in the audit stage)
OLLAMA_MODEL: str = os.getenv(
    "OLLAMA_MODEL", "qwen2.5:14b-instruct-q5_K_M"
)

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
