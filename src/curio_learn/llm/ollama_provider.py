"""Ollama provider for locally hosted models."""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel

from curio_learn.errors import LLMError
from curio_learn.llm.base import TransientLLMError
from curio_learn.llm.schema import strict_object_schema
from curio_learn.processing.normalize import approx_tokens

log = logging.getLogger(__name__)


class OllamaProvider:
    name = "ollama"

    def __init__(
        self, host: str, *, max_ctx: int = 65536, temperature: float = 0.2, timeout_s: float = 1800.0, client=None
    ):
        import ollama

        self._client = client or ollama.Client(host=host, timeout=timeout_s)
        self._max_ctx = max_ctx
        self._temperature = temperature
        self._think_supported: dict[str, bool] = {}

    def schema_for(self, model_cls: type[BaseModel]) -> dict:
        return strict_object_schema(model_cls)

    def _num_ctx(self, prompt_tokens: int, max_tokens: int) -> int:
        """Every call uses the same context size. A different num_ctx per call makes
        Ollama reload the model, and larger contexts can spill layers onto the CPU."""
        need = prompt_tokens + max_tokens + 512
        if need > self._max_ctx:
            raise LLMError(f"Prompt needs ~{need} tokens but CURIO_OLLAMA_NUM_CTX is {self._max_ctx}.")
        return self._max_ctx

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        shared_prefix: str = "",
        temperature: float | None = None,
    ) -> str:
        import ollama

        content = f"{shared_prefix}\n\n{user}" if shared_prefix else user
        num_ctx = self._num_ctx(approx_tokens(system + content), max_tokens)
        kwargs = dict(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            format=schema,
            options={
                "num_ctx": num_ctx,
                "temperature": self._temperature if temperature is None else temperature,
                "num_predict": max_tokens,
            },
            keep_alive="10m",
        )
        if self._think_supported.get(model, True):
            kwargs["think"] = False  # JSON calls don't benefit from a visible reasoning trace
        try:
            resp = self._client.chat(**kwargs)
        except ollama.ResponseError as exc:
            if "think" in str(exc).lower() and "think" in kwargs:
                self._think_supported[model] = False
                kwargs.pop("think")
                resp = self._client.chat(**kwargs)
            elif exc.status_code == 404:
                raise LLMError(f"Ollama model {model!r} is not installed: `ollama pull {model}`") from exc
            elif exc.status_code >= 500:
                raise TransientLLMError(f"Ollama error: {exc}") from exc
            else:
                raise LLMError(f"Ollama error: {exc}") from exc
        except (httpx.ConnectError, ConnectionError) as exc:
            raise LLMError("Can't reach Ollama. Is `ollama serve` running?") from exc
        except httpx.TimeoutException as exc:
            raise TransientLLMError("Ollama request timed out") from exc

        # Ollama silently drops the start of a prompt that exceeds num_ctx.
        prompt_count = resp.get("prompt_eval_count") or 0
        if prompt_count and prompt_count + max_tokens > num_ctx:
            raise LLMError(
                f"Prompt ({prompt_count} tokens) plus output budget exceeds num_ctx={num_ctx}; "
                "the transcript may have been truncated. Lower CURIO_OUTLINE_WINDOW_TOKENS."
            )
        if resp.get("done_reason") == "length":
            log.warning("Ollama output hit num_predict=%d; JSON is probably truncated", max_tokens)
        return resp["message"]["content"]
