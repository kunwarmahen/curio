"""Anthropic Claude provider (structured outputs, streaming, refusal fallback)."""

from __future__ import annotations

import logging

from pydantic import BaseModel

from curio_learn.errors import LLMError
from curio_learn.llm.base import TransientLLMError

log = logging.getLogger(__name__)

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, effort: str = "high", client=None):
        import anthropic

        self._client = client or anthropic.Anthropic()
        self._effort = effort

    def schema_for(self, model_cls: type[BaseModel]) -> dict:
        import anthropic

        return anthropic.transform_schema(model_cls)

    def complete_json(
        self, *, model: str, system: str, user: str, schema: dict, max_tokens: int, shared_prefix: str = ""
    ) -> str:
        import anthropic

        content: list[dict] = []
        if shared_prefix:
            # Cached across the evaluation batches of one analysis.
            content.append({"type": "text", "text": shared_prefix, "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": user})

        kwargs: dict = dict(
            model=model,
            # Adaptive thinking tokens count against max_tokens; leave headroom.
            max_tokens=max_tokens + 32000,
            system=system,
            messages=[{"role": "user", "content": content}],
            thinking={"type": "adaptive"},
            output_config={"effort": self._effort, "format": {"type": "json_schema", "schema": schema}},
        )
        if model in _FALLBACK_MODELS:
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        try:
            with self._client.beta.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic rejected the API key (ANTHROPIC_API_KEY).") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"Anthropic model {model!r} not found.") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Anthropic rejected the request: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise TransientLLMError("Anthropic rate limit reached") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientLLMError(f"Anthropic server error {exc.status_code}") from exc
            raise LLMError(f"Anthropic error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise TransientLLMError("Could not connect to the Anthropic API") from exc

        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None) if message.stop_details else None
            raise LLMError(f"Claude declined this request (category: {category}).")
        if message.stop_reason == "max_tokens":
            log.warning("Claude output hit max_tokens=%d; JSON is probably truncated", max_tokens)
        usage = message.usage
        log.info(
            "Claude usage: in=%s cache_read=%s out=%s",
            usage.input_tokens,
            getattr(usage, "cache_read_input_tokens", None),
            usage.output_tokens,
        )
        return "".join(b.text for b in message.content if b.type == "text")
