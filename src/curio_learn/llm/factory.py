"""Construct the configured LLM provider."""

from __future__ import annotations

from curio_learn.config import Settings
from curio_learn.llm.base import LLMProvider


def build_provider(settings: Settings) -> LLMProvider:
    if settings.resolved_provider == "anthropic":
        from curio_learn.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(effort=settings.anthropic_effort)
    from curio_learn.llm.ollama_provider import OllamaProvider

    return OllamaProvider(
        settings.ollama_host,
        max_ctx=settings.ollama_num_ctx,
        temperature=settings.llm_temperature,
        timeout_s=settings.ollama_timeout_s,
    )
