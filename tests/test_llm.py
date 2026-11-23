import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from curio_learn.analysis.evaluate import EvaluationOut
from curio_learn.analysis.outline import OutlineOut
from curio_learn.errors import LLMError
from curio_learn.llm.anthropic_provider import AnthropicProvider
from curio_learn.llm.base import TransientLLMError, _extract_json, generate_structured
from curio_learn.llm.ollama_provider import OllamaProvider
from curio_learn.llm.schema import strict_object_schema


class Answer(BaseModel):
    value: int


class ScriptedProvider:
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def schema_for(self, model_cls):
        return strict_object_schema(model_cls)

    def complete_json(self, *, model, system, user, schema, max_tokens, shared_prefix=""):
        self.prompts.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_retries_with_validation_error_fed_back():
    p = ScriptedProvider(['{"value": "abc"}', '{"value": 3}'])
    assert generate_structured(p, Answer, model="m", system="s", user="u").value == 3
    assert "rejected" in p.prompts[1] and "value" in p.prompts[1]


def test_gives_up_after_retries():
    p = ScriptedProvider(["nope"] * 3)
    with pytest.raises(LLMError, match="failed after 3 attempts"):
        generate_structured(p, Answer, model="m", system="s", user="u", retries=2)


def test_transient_errors_retried(monkeypatch):
    monkeypatch.setattr("curio_learn.llm.base.time.sleep", lambda _: None)
    p = ScriptedProvider([TransientLLMError("overloaded"), '{"value": 1}'])
    assert generate_structured(p, Answer, model="m", system="s", user="u").value == 1


def test_extract_json_tolerates_fences_and_prose():
    assert json.loads(_extract_json('```json\n{"a": 1}\n```')) == {"a": 1}
    assert json.loads(_extract_json('Here you go: {"a": {"b": 2}} done')) == {"a": {"b": 2}}


def test_strict_schema_is_flat_and_closed():
    schema = strict_object_schema(OutlineOut)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    item = schema["properties"]["sections"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])
    assert "sponsor" in item["properties"]["kind"]["enum"]
    # Regression: a property literally named "title" must survive annotation stripping.
    assert item["properties"]["title"] == {"type": "string"}
    assert "title" in item["required"]
    assert "title" not in schema  # the schema's own annotation is dropped


def test_anthropic_schema_accepted_by_sdk_transform():
    schema = AnthropicProvider(client=object()).schema_for(EvaluationOut)
    assert schema["type"] == "object" and schema["additionalProperties"] is False


class _FakeStream:
    def __init__(self, message, captured, kwargs):
        self.message, self.captured, self.kwargs = message, captured, kwargs

    def __enter__(self):
        self.captured.update(self.kwargs)
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def _fake_client(message, captured):
    stream = lambda **kw: _FakeStream(message, captured, kw)  # noqa: E731
    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))


def _message(text, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
    )


def test_anthropic_request_shape():
    captured = {}
    p = AnthropicProvider(effort="high", client=_fake_client(_message('{"value": 2}'), captured))
    out = generate_structured(p, Answer, model="claude-opus-5-5", system="sys", user="hello", shared_prefix="outline")
    assert out.value == 2
    assert captured["model"] == "claude-opus-5-5"
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"]["effort"] == "high"
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["fallbacks"] == "default"
    content = captured["messages"][0]["content"]
    assert content[0]["cache_control"] == {"type": "ephemeral"} and content[1]["text"] == "hello"
    assert "temperature" not in captured


def test_anthropic_refusal_is_an_error():
    p = AnthropicProvider(client=_fake_client(_message("", stop="refusal"), {}))
    with pytest.raises(LLMError, match="declined"):
        p.complete_json(model="claude-opus-5-5", system="s", user="u", schema={}, max_tokens=10)


class _FakeOllama:
    def __init__(self):
        self.kwargs = None

    def chat(self, **kwargs):
        self.kwargs = kwargs
        return {"message": {"content": '{"value": 5}'}, "done_reason": "stop"}


def test_ollama_request_shape_and_fixed_context():
    client = _FakeOllama()
    p = OllamaProvider("http://x", max_ctx=32768, client=client)
    out = generate_structured(p, Answer, model="qwen", system="s", user="word " * 10000, max_tokens=4000)
    assert out.value == 5
    assert client.kwargs["format"]["properties"]["value"]["type"] == "integer"
    assert client.kwargs["think"] is False
    assert client.kwargs["options"]["num_ctx"] == 32768
    p.complete_json(model="qwen", system="s", user="tiny", schema={}, max_tokens=100)
    assert client.kwargs["options"]["num_ctx"] == 32768  # same size: no model reload


def test_ollama_detects_silent_truncation():
    client = _FakeOllama()
    client.chat = lambda **kw: {"message": {"content": "{}"}, "prompt_eval_count": 32700, "done_reason": "stop"}
    p = OllamaProvider("http://x", max_ctx=32768, client=client)
    with pytest.raises(LLMError, match="truncated"):
        p.complete_json(model="q", system="s", user="short", schema={}, max_tokens=4000)


def test_ollama_rejects_prompt_larger_than_context():
    p = OllamaProvider("http://x", max_ctx=8192, client=_FakeOllama())
    with pytest.raises(LLMError, match="NUM_CTX"):
        p.complete_json(model="q", system="s", user="word " * 10000, schema={}, max_tokens=100)
