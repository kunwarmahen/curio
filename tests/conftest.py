"""Shared fixtures. Tests never touch the network or a real LLM."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from curio_learn.config import Settings
from curio_learn.domain import Transcript, TranscriptCue, VideoMetadata


def make_cues(n: int = 300, step: float = 4.0, start: float = 0.0) -> list[TranscriptCue]:
    """n cues of `step` seconds; every 3rd cue ends a sentence."""
    cues = []
    for i in range(n):
        text = f"topic{i // 30} word{i} explains idea{i // 10}"
        if i % 3 == 2:
            text += "."
        cues.append(TranscriptCue(start_s=start + i * step, duration_s=step, text=text))
    return cues


class FakeLLM:
    """Deterministic provider: outlines every ~8 blocks and scores sections by position."""

    name = "fake"

    def __init__(self, invalid_first: int = 0, kinds: dict[int, str] | None = None):
        self.calls: list[str] = []
        self.invalid_first = invalid_first
        self.kinds = kinds or {}

    def schema_for(self, model_cls: type[BaseModel]) -> dict:
        return {"_model": model_cls.__name__}

    def complete_json(self, *, model, system, user, schema, max_tokens, shared_prefix=""):
        name = schema["_model"]
        self.calls.append(name)
        if self.invalid_first > 0:
            self.invalid_first -= 1
            return '{"not": "valid"}'
        if name == "OutlineOut":
            import re

            first, last = map(int, re.search(r"Transcript blocks (\d+)-(\d+)", user).groups())
            sections = []
            for n, start in enumerate(range(first, last + 1, 8)):
                sections.append(
                    {
                        "start_block": start,
                        "title": f"Topic at block {start}",
                        "summary": f"Explains the idea starting at block {start}.",
                        "kind": self.kinds.get(n, "core"),
                        "key_concepts": [f"concept{start}"],
                    }
                )
            return json.dumps({"overview": "A test video.", "key_concepts": ["a", "b"], "sections": sections})
        if name == "BoundariesOut":
            import re

            # Move each boundary one sentence past the current guess.
            choices = [
                {"boundary_id": bid, "first_sentence": int(guess) + 1}
                for bid, guess in re.findall(r"### Boundary (\S+)\n.*?Current guess: sentence (\d+)", user, re.S)
            ]
            return json.dumps({"boundaries": choices})
        if name == "AdsOut":
            import re

            # Treat every sentence mentioning a .com address as the ad.
            windows = []
            for part in user.split("### Window ")[1:]:
                wid = part.split("\n", 1)[0]
                nums = [int(m) for m in re.findall(r"^\s*(\d+)\. .*(?:\.com|sponsor)", part, re.M)]
                windows.append(
                    {
                        "window_id": wid,
                        "is_ad": bool(nums),
                        "first_sentence": min(nums, default=0),
                        "last_sentence": max(nums, default=0),
                        "sponsor": "Acme" if nums else "",
                    }
                )
            return json.dumps({"windows": windows})
        if name == "OverviewOut":
            return json.dumps({"overview": "A test video.", "key_concepts": ["a"]})
        if name == "EvaluationOut":
            import re

            ids = re.search(r"in order: ([^\n]+)\.", user).group(1).split(", ")
            evals = []
            for sid in ids:
                k = int(sid[1:])
                v = 3 + (k * 7) % 8  # varied, deterministic 3..10
                evals.append(
                    {
                        "section_id": sid,
                        "what_you_learn": f"Learn {sid}.",
                        "why_valuable": f"Because {sid}.",
                        "skip_reason": "",
                        "standalone": True,
                        "builds_on": [],
                        "redundant_with": [],
                        "introduces_concepts": [f"new{sid}"],
                        "prerequisites": [],
                        "educational_value": v,
                        "novelty": v,
                        "practical": v,
                        "depth": v,
                        "relevance": v,
                    }
                )
            return json.dumps({"evaluations": evals})
        raise AssertionError(name)


class FakeMetadata:
    def __init__(self, duration_s: float = 1200.0):
        self.duration_s = duration_s

    def get_metadata(self, video_id: str) -> VideoMetadata:
        return VideoMetadata(
            video_id=video_id, title="Test Lecture", channel="Test Channel", duration_s=self.duration_s
        )


class FakeTranscripts:
    def __init__(self, cues: list[TranscriptCue] | None = None):
        self.cues = cues or make_cues()
        self.calls = 0

    def get_transcript(self, video_id: str, languages: list[str]) -> Transcript:
        self.calls += 1
        return Transcript(video_id=video_id, language="en", is_generated=False, cues=self.cues)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        llm_provider="ollama",
        llm_model="fake-model",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        outline_window_tokens=100000,
        evaluate_batch_tokens=2000,
        budget_min_minutes=2,
        budget_max_minutes=35,
    )
