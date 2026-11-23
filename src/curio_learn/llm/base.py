"""Provider-independent structured generation with validation and retry."""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

from pydantic import BaseModel, ValidationError

from curio_learn.errors import LLMError

log = logging.getLogger(__name__)


class LLMProvider(Protocol):
    name: str

    def schema_for(self, model_cls: type[BaseModel]) -> dict:
        """JSON schema in the dialect this provider's constrained decoding accepts."""
        ...

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        shared_prefix: str = "",
    ) -> str:
        """Return raw JSON text conforming (ideally) to `schema`.

        `shared_prefix` is content repeated across several calls (for example the
        video outline). Providers that support prompt caching cache it; others
        prepend it to `user`.
        """
        ...


class TransientLLMError(LLMError):
    """A failure worth retrying (network, overload, timeout)."""


def _extract_json(text: str) -> str:
    """Tolerate code fences or prose around a JSON object."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def generate_structured[T: BaseModel](
    provider: LLMProvider,
    model_cls: type[T],
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 8000,
    shared_prefix: str = "",
    retries: int = 2,
    task: str = "",
) -> T:
    """Call the provider and validate the output against `model_cls`.

    On invalid output the request is retried with the validation error appended,
    so the model can correct itself. Transient provider errors are retried with
    exponential backoff.
    """
    schema = provider.schema_for(model_cls)
    prompt = user
    last_error = ""
    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            raw = provider.complete_json(
                model=model,
                system=system,
                user=prompt,
                schema=schema,
                max_tokens=max_tokens,
                shared_prefix=shared_prefix,
            )
        except TransientLLMError as exc:
            last_error = str(exc)
            log.warning("[%s] transient LLM error (attempt %d): %s", task, attempt + 1, exc)
            time.sleep(min(30, 2 ** (attempt + 1)))
            continue
        log.info(
            "[%s] %s/%s answered in %.1fs (%d chars)", task, provider.name, model, time.monotonic() - started, len(raw)
        )
        try:
            return model_cls.model_validate_json(_extract_json(raw))
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            last_error = _short_error(exc)
            log.warning("[%s] invalid structured output (attempt %d): %s", task, attempt + 1, last_error)
            prompt = (
                f"{user}\n\nYour previous answer was rejected because it did not match the "
                f"required JSON schema:\n{last_error}\nReturn a corrected, complete JSON object."
            )
    raise LLMError(f"{task or 'LLM call'} failed after {retries + 1} attempts: {last_error}")


def _short_error(exc: Exception, limit: int = 1200) -> str:
    text = str(exc)
    return text if len(text) <= limit else text[:limit] + " …"
