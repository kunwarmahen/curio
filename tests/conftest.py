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
        self.prompts: list[str] = []
        self.invalid_first = invalid_first
        self.kinds = kinds or {}

    def schema_for(self, model_cls: type[BaseModel]) -> dict:
        return {"_model": model_cls.__name__}

    def complete_json(self, *, model, system, user, schema, max_tokens, shared_prefix=""):
        name = schema["_model"]
        self.calls.append(name)
        self.prompts.append(f"{shared_prefix}\n\n{user}")
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
        if name == "NotesOut":
            import re

            entries = []
            for part in user.split("### ")[1:]:
                sid = part.split(" ", 1)[0]
                n = len(re.findall(r"^\d+\. ", part, re.M))
                entries.append(
                    {
                        "section_id": sid,
                        "points": [
                            {"text": f"{sid} opens its topic.", "sentence": 1},
                            {"text": f"{sid} ends with a result.", "sentence": n},
                        ],
                        "explanation": f"How {sid} works." if sid.endswith(("1", "3")) else "",
                        "definitions": [{"term": f"term{sid}", "definition": f"Defined in {sid}.", "sentence": 2}],
                    }
                )
            return json.dumps({"sections": entries})
        if name == "QuizReviewOut":
            import re

            # Shortens nothing; replaces q1's wrong options and drops an entry for an unknown id.
            ids = re.findall(r"^id: (q\d+)$", user, re.M)
            entries = [{"id": "q999", "wrong_options": ["x", "y", "z"], "right_option": "r"}]
            if ids:
                right = re.search(r"^right answer: (.*)$", user, re.M).group(1)
                entries.append(
                    {
                        "id": ids[0],
                        "wrong_options": ["New wrong 1", "New wrong 2", "New wrong 3"],
                        "right_option": right,
                    }
                )
            return json.dumps({"questions": entries})
        if name == "StudyOut":
            import re

            entries = []
            for part in user.split("### ")[1:]:
                sid = part.split(" ", 1)[0]
                n = len(re.findall(r"^\d+\. ", part, re.M))
                questions = [
                    {
                        "question": f"What does {sid} show?",
                        "wrong_options": ["Something else", "A third thing", "  "],
                        "right_option": f"The idea of {sid}",
                        "explanation": f"{sid} says so.",
                        "sentence": 2,
                    }
                ]
                if sid == "S01":  # an unusable question: only one distinct wrong option
                    broken = {"question": "Broken?", "wrong_options": ["a", "A", " "], "right_option": "b"}
                    questions.append({**broken, "explanation": "", "sentence": 1})
                entries.append(
                    {
                        "section_id": sid,
                        "questions": questions,
                        "cards": [
                            {"front": f"Term {sid}", "back": f"Meaning of {sid}.", "sentence": 1},
                            {"front": f"Why {sid}?", "back": "Because.", "sentence": n + 5},
                        ],
                    }
                )
            return json.dumps({"sections": entries})
        if name == "NotesSummaryOut":
            import re

            ids = re.findall(r"^(S\d+) \[", user, re.M)
            return json.dumps(
                {
                    "summary": "These parts explain the topics.",
                    "takeaways": [
                        {"text": "The first idea matters most.", "section_ids": [ids[-1], ids[0]]},
                        {"text": "Something the notes never said.", "section_ids": ["S99"]},
                    ],
                    "recommendations": [{"text": "Try the method yourself.", "section_ids": [ids[0]]}],
                }
            )
        if name == "OverlapOut":
            import re

            # The first pair is a repeat (keep B), the second an overlap, the rest different.
            ids = re.findall(r"^### Pair (p\d+)$", user, re.M)
            relations = ["repeat", "overlap"]
            verdicts = [
                {"pair": pid, "relation": relations[i] if i < 2 else "different", "keep": "B", "reason": f"Both {pid}."}
                for i, pid in enumerate(ids)
            ]
            return json.dumps({"verdicts": verdicts})
        if name == "ChecklistOut":
            import re

            keys = re.findall(r"^(L\d+\.S\d+) \[", user, re.M)
            return json.dumps(
                {
                    "points": [
                        {
                            "text": "Watch out for the first idea.",
                            "why": "It matters.",
                            "sections": [keys[-1], keys[0]],
                        },
                        {"text": "A point with no real section.", "why": "", "sections": ["L9.S99"]},
                    ]
                }
            )
        if name == "ChecklistCardsOut":
            import re

            nums = [int(n) for n in re.findall(r"^(\d+)\. ", user, re.M)]
            return json.dumps(
                {"cards": [{"n": n, "front": f"What is point {n}?"} for n in nums] + [{"n": 99, "front": "x"}]}
            )
        if name == "CourseAnswerOut":
            import re

            shown = [int(n) for n in re.findall(r"^\[(\d+) \| ", shared_prefix, re.M)]
            return json.dumps(
                {
                    "parts": [
                        {"text": "Both lectures explain idea0.", "passages": [shown[0], shown[-1], 99999]},
                        {"text": "Nothing else.", "passages": []},
                    ],
                    "in_course": True,
                    "outside_knowledge": "",
                }
            )
        if name == "AnswerOut":
            import re

            shown = [int(n) for n in re.findall(r"^\[(\d+) \| ", shared_prefix, re.M)]
            if "weather" in user:
                return json.dumps(
                    {
                        "parts": [{"text": "The video doesn't discuss the weather.", "passages": []}],
                        "in_video": False,
                        "outside_knowledge": "Weather depends on where you are.",
                    }
                )
            return json.dumps(
                {
                    "parts": [
                        {"text": f"Block {shown[0]} explains idea0 and word4.", "passages": [shown[0], 9999]},
                        {"text": "It then moves on.", "passages": shown[1:2]},
                    ],
                    "in_video": True,
                    "outside_knowledge": "",
                }
            )
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
        embed_model="",
        queue_check_s=0,
    )
