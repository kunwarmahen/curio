"""Application settings, loaded from environment variables and `.env`."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ScoringWeights(BaseSettings):
    """Weights of the LLM rubric criteria. They are normalized, so they need not sum to 1."""

    model_config = SettingsConfigDict(env_prefix="CURIO_WEIGHT_", env_file=".env", extra="ignore")

    educational_value: float = 0.30
    novelty: float = 0.20
    practical: float = 0.20
    depth: float = 0.15
    relevance: float = 0.15

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CURIO_", env_file=".env", extra="ignore")

    # --- LLM -----------------------------------------------------------------
    llm_provider: Literal["auto", "ollama", "anthropic"] = "auto"
    # Default model for every task; empty means the provider's default.
    llm_model: str = ""
    # Optional per-task overrides.
    llm_model_outline: str = ""
    llm_model_evaluate: str = ""
    llm_model_notes: str = ""
    llm_model_ask: str = ""
    llm_model_study: str = ""
    llm_model_course: str = ""
    llm_max_retries: int = 2
    llm_temperature: float = 0.2

    ollama_host: str = "http://localhost:11434"
    ollama_default_model: str = "qwen3.8-64k"
    # Fixed context for every call. 32k keeps a 27B model fully on a 24 GB GPU.
    ollama_num_ctx: int = 32768
    ollama_timeout_s: float = 1800.0

    anthropic_default_model: str = "claude-opus-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"

    # Prompt budget for a single outline call, in approximate tokens. Longer
    # transcripts are processed in overlapping windows.
    outline_window_tokens: int = 22000
    # Move section starts from block level (20-45 s) to the sentence where the topic changes.
    refine_boundaries: bool = True
    # Find sponsor reads hidden inside content sections and cut them out (one LLM call).
    detect_ads: bool = True
    # Approximate tokens of section text per evaluation call.
    evaluate_batch_tokens: int = 12000

    # --- Ask the Video ---------------------------------------------------------
    # Transcript tokens Ask may show the model. 0 = as much as the context allows
    # (Ollama: num_ctx minus room for the outline, conversation and answer; Claude: 150k).
    # A longer transcript is searched instead.
    ask_context_tokens: int = 0
    # Embedding model (on Ollama) for searching long transcripts. Empty = keyword search only.
    embed_model: str = "nomic-embed-text"

    # --- Overnight queue -------------------------------------------------------
    # Local hours when queued videos are analyzed, e.g. "1-7" or "22:30-6".
    # Empty = any time the GPU is idle.
    queue_hours: str = "1-7"
    # The GPU counts as in use at or above this utilization (median of 5 nvidia-smi
    # readings). A desktop idles at 0-5% with brief spikes; generation and games run at 80-100%.
    queue_gpu_busy_percent: float = 20.0
    # How often the API server checks the queue, in seconds. 0 turns the queue worker off.
    queue_check_s: float = 60.0
    # After each queued analysis, also write playlist notes and a quiz with flashcards,
    # so they're ready in the morning (skipped if someone starts using the GPU).
    queue_prepare: bool = True

    # --- Transcripts -----------------------------------------------------------
    transcript_languages: str = "en,en-US,en-GB"

    # --- Playlist ------------------------------------------------------------
    threshold_essential: float = 70.0
    threshold_recommended: float = 55.0
    threshold_optional: float = 40.0
    budget_fraction: float = 0.30
    budget_min_minutes: float = 10.0
    budget_max_minutes: float = 35.0
    merge_gap_s: float = 15.0

    # --- Storage -------------------------------------------------------------
    database_url: str = Field(default=f"sqlite:///{PROJECT_ROOT / 'data' / 'curio.db'}")

    log_level: str = "INFO"

    weights: ScoringWeights = Field(default_factory=ScoringWeights)

    @property
    def languages(self) -> list[str]:
        return [lang.strip() for lang in self.transcript_languages.split(",") if lang.strip()]

    @property
    def resolved_provider(self) -> Literal["ollama", "anthropic"]:
        if self.llm_provider != "auto":
            return self.llm_provider
        return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "ollama"

    def model_for(self, task: str) -> str:
        override = {
            "outline": self.llm_model_outline,
            "evaluate": self.llm_model_evaluate,
            "notes": self.llm_model_notes,
            "ask": self.llm_model_ask,
            "study": self.llm_model_study,
            "course": self.llm_model_course,
        }.get(task, "")
        if override:
            return override
        if self.llm_model:
            return self.llm_model
        if self.resolved_provider == "anthropic":
            return self.anthropic_default_model
        return self.ollama_default_model


@lru_cache
def get_settings() -> Settings:
    # Make ANTHROPIC_API_KEY from .env visible to the SDK and to provider auto-detection.
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return Settings()
