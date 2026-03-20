"""
Document Agent — Dual-LLM Router.

Routes requests to the optimal LLM based on task type:

    ┌─────────────┐     ┌──────────────────────────────────┐
    │ VISION tasks │────▶│ Groq API (llama-3.2-90b-vision)  │
    │ (images)     │     │ Fast cloud inference, multimodal  │
    └─────────────┘     └──────────────────────────────────┘
    ┌─────────────┐     ┌──────────────────────────────────┐
    │ TEXT tasks   │────▶│ Local Ollama (qwen2.5:14b)       │
    │ (refine/aud) │     │ Free, on-prem, low latency       │
    └─────────────┘     └──────────────────────────────────┘

Fallback chain: Groq → local Ollama VLM → error.
"""

import logging
from typing import Optional

from langchain_core.language_models import BaseChatModel
from langchain_ollama import ChatOllama

from src.config import (
    GROQ_API_KEY,
    GROQ_VISION_MODEL,
    GROQ_TEXT_MODEL,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    VLM_MODEL,
)

logger = logging.getLogger(__name__)

# ── Singletons ──────────────────────────────────────────────────────
_groq_vision: Optional[BaseChatModel] = None
_groq_text: Optional[BaseChatModel] = None
_ollama_text: Optional[ChatOllama] = None
_ollama_vlm: Optional[ChatOllama] = None


def get_vision_llm() -> BaseChatModel:
    """
    Return the best available vision-capable LLM.

    Priority: Groq (cloud, fast) → local Ollama VLM (fallback).
    """
    global _groq_vision

    if GROQ_API_KEY:
        if _groq_vision is None:
            try:
                from langchain_groq import ChatGroq

                _groq_vision = ChatGroq(
                    model=GROQ_VISION_MODEL,
                    api_key=GROQ_API_KEY,
                    temperature=0,
                    max_tokens=4096,
                    timeout=120.0,
                )
                logger.info(
                    "Vision LLM: Groq %s (cloud)", GROQ_VISION_MODEL
                )
            except ImportError:
                logger.warning(
                    "langchain-groq not installed — falling back to Ollama VLM"
                )
            except Exception as exc:
                logger.warning("Groq init failed: %s — falling back", exc)

        if _groq_vision is not None:
            return _groq_vision

    # Fallback: local Ollama VLM
    return _get_ollama_vlm()


def get_text_llm() -> BaseChatModel:
    """
    Return the best available text-only LLM.

    Priority: Local Ollama qwen2.5:14b (free) → Groq text model.
    """
    global _ollama_text, _groq_text

    # Primary: local Ollama (free, no API cost)
    if _ollama_text is None:
        _ollama_text = ChatOllama(
            model=OLLAMA_MODEL,
            base_url=OLLAMA_BASE_URL,
            temperature=0,
            timeout=120.0,
        )
        logger.info("Text LLM: Ollama %s @ %s", OLLAMA_MODEL, OLLAMA_BASE_URL)

    return _ollama_text


def get_groq_text_llm() -> Optional[BaseChatModel]:
    """
    Return a fast Groq text model (for speed-critical text tasks).
    """
    global _groq_text

    if not GROQ_API_KEY:
        return None

    if _groq_text is None:
        try:
            from langchain_groq import ChatGroq

            _groq_text = ChatGroq(
                model=GROQ_TEXT_MODEL,
                api_key=GROQ_API_KEY,
                temperature=0,
                max_tokens=4096,
                timeout=60.0,
            )
            logger.info("Groq text LLM: %s", GROQ_TEXT_MODEL)
        except (ImportError, Exception) as exc:
            logger.warning("Groq text init failed: %s", exc)
            return None

    return _groq_text


def _get_ollama_vlm() -> ChatOllama:
    """Fallback: local Ollama VLM (may not be loaded)."""
    global _ollama_vlm
    if _ollama_vlm is None:
        _ollama_vlm = ChatOllama(
            model=VLM_MODEL,
            base_url=OLLAMA_BASE_URL,
            temperature=0,
            timeout=180.0,
        )
        logger.warning(
            "Using local Ollama VLM %s as fallback — may fail if not loaded",
            VLM_MODEL,
        )
    return _ollama_vlm


def get_llm_info() -> dict:
    """Return info about which LLMs are configured (for health checks)."""
    return {
        "vision_provider": "groq" if GROQ_API_KEY else "ollama",
        "vision_model": GROQ_VISION_MODEL if GROQ_API_KEY else VLM_MODEL,
        "text_provider": "ollama",
        "text_model": OLLAMA_MODEL,
        "groq_available": bool(GROQ_API_KEY),
    }
