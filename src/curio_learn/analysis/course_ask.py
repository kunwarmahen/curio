"""Ask the course: questions answered from every lecture's transcript.

All lectures' transcript blocks are numbered across the course (passage n is in one
lecture). A whole course rarely fits a local model's context, so search (BM25 and
embeddings over all blocks, fused) picks the passages, each with its neighbours. A
question that names a lecture ("in lecture 4", "the guest lecture") searches that
lecture first, and a time it names ("at 23:10 in lecture 6") is read first. The
model cites passage numbers; code turns each into the start of the best-matching
sentence of that passage in that lecture, so every timestamp is a real cue time.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from curio_learn.analysis import prompts
from curio_learn.analysis.ask import (
    ANSWER_TOKENS,
    MAX_QUESTION_CHARS,
    precise_start,
    search_query,
    select_context,
)
from curio_learn.analysis.profiles import learner_brief
from curio_learn.domain import (
    AnswerPart,
    Block,
    Citation,
    CourseAnswer,
    LearningPreferences,
    LLMInfo,
    VideoAnalysis,
    fmt_ts,
)
from curio_learn.llm.base import LLMProvider, generate_structured
from curio_learn.processing.normalize import Utterance
from curio_learn.processing.retrieval import time_hints

HISTORY_TURNS = 4
_LECTURE_RE = re.compile(r"\blecture\s*#?\s*(\d{1,3})\b", re.I)
_GUEST_RE = re.compile(r"\bguest\b", re.I)


class AnswerPartOut(BaseModel):
    text: str
    passages: list[int]


class CourseAnswerOut(BaseModel):
    parts: list[AnswerPartOut]
    in_course: bool
    outside_knowledge: str


@dataclass
class LectureText:
    position: int
    label: str  # "Lecture 3", "Guest lecture"
    analysis: VideoAnalysis
    blocks: list[Block]
    units: Sequence[Utterance]


@dataclass
class Passage:
    n: int  # numbered across the course, from 1
    lecture: LectureText
    block: Block


def course_passages(lectures: Sequence[LectureText]) -> list[Passage]:
    out: list[Passage] = []
    for lec in lectures:
        for b in lec.blocks:
            out.append(Passage(len(out) + 1, lec, b))
    return out


def named_lectures(question: str, lectures: Sequence[LectureText]) -> list[LectureText]:
    """Lectures the question names: "lecture 4", "the guest lecture"."""
    nums = {int(n) for n in _LECTURE_RE.findall(question)}
    named = [lec for lec in lectures if any(lec.label == f"Lecture {n}" for n in nums)]
    if _GUEST_RE.search(question):
        named += [lec for lec in lectures if lec.label.startswith("Guest") and lec not in named]
    return named


def render_outline(lectures: Sequence[LectureText]) -> str:
    parts = []
    for lec in lectures:
        parts.append(f"## {lec.label}: {lec.analysis.video.title}")
        for s in lec.analysis.sections:
            parts.append(f"- [{fmt_ts(s.section.start_s)}] {s.section.title}")
    return "\n".join(parts)


def render_passages(passages: Sequence[Passage], indices: list[int]) -> str:
    lines, prev = [], None
    for i in indices:
        p = passages[i]
        if prev is not None and (i != prev + 1 or passages[prev].lecture is not p.lecture):
            lines.append("…")
        lines.append(f"[{p.n} | {p.lecture.label}, {fmt_ts(p.block.start_s)}] {p.block.text}")
        prev = i
    return "\n".join(lines)


def render_history(history: Sequence[CourseAnswer]) -> str:
    turns = []
    for a in history[-HISTORY_TURNS:]:
        reply = " ".join(p.text for p in a.parts)
        if a.outside_knowledge:
            reply += f" (Not from the course: {a.outside_knowledge})"
        turns.append(f"Learner: {a.question}\nCurio: {reply}")
    return "\n\n".join(turns)


def answer_course_question(
    provider: LLMProvider,
    *,
    model: str,
    course_id: str,
    course_title: str,
    prefs: LearningPreferences,
    lectures: Sequence[LectureText],
    question: str,
    history: Sequence[CourseAnswer] = (),
    budget_tokens: int,
    ranked: Callable[[str], list[int]] = lambda _: [],
    search: str = "keyword",
    retries: int = 2,
) -> CourseAnswer:
    """`ranked(query)` returns indices into `course_passages(lectures)`, best first."""
    started = time.monotonic()
    question = " ".join(question.split())[:MAX_QUESTION_CHARS]
    passages = course_passages(lectures)
    # Blocks keyed by their place in the course, for the shared context selector.
    blocks = [p.block.model_copy(update={"index": p.n}) for p in passages]
    named = named_lectures(question, lectures)
    hints = time_hints(question) if len(named) == 1 else []

    def order() -> list[int]:
        """Named stretches first, then the named lectures' hits, then the rest."""
        hits = ranked(search_query(question, history))
        first = [
            i
            for i, p in enumerate(passages)
            if p.lecture in named and any(p.block.end_s > t - 90 and p.block.start_s < t + 150 for t in hints)
        ]
        mine = [i for i in hits if passages[i].lecture in named]
        rest = [i for i in hits if passages[i].lecture not in named]
        return list(dict.fromkeys(first + mine + rest))

    indices, full = select_context(blocks, budget_tokens=budget_tokens, ranked=order)
    shared = prompts.course_ask_shared_prefix(
        title=course_title,
        brief=learner_brief(prefs),
        outline=render_outline(lectures),
        transcript=render_passages(passages, indices),
        full=full,
    )
    notes = []
    if named:
        notes.append("The learner asks about " + " and ".join(lec.label for lec in named) + ".")
    if hints:
        notes.append("They refer to " + ", ".join(fmt_ts(t) for t in hints) + f" in {named[0].label}.")
    out = generate_structured(
        provider,
        CourseAnswerOut,
        model=model,
        system=prompts.COURSE_ASK_SYSTEM,
        user=prompts.ask_user(question=question, history=render_history(history), hints=" ".join(notes)),
        shared_prefix=shared,
        max_tokens=ANSWER_TOKENS + 500,
        retries=retries,
        task="ask[course]",
    )

    shown = {passages[i].n: passages[i] for i in indices}
    warnings: list[str] = []
    dropped = 0
    parts: list[AnswerPart] = []
    for part in out.parts:
        text = part.text.strip()
        if not text:
            continue
        cites: dict[tuple[str, float], Citation] = {}
        for n in part.passages:
            p = shown.get(n)
            if p is None:
                dropped += 1
                continue
            t = precise_start(text, p.block, p.lecture.units)
            key = (p.lecture.analysis.video.video_id, t)
            cites.setdefault(
                key,
                Citation(
                    start_s=t,
                    video_id=key[0],
                    analysis_id=p.lecture.analysis.id,
                    lecture=p.lecture.position,
                ),
            )
        ordered = sorted(cites.values(), key=lambda c: (c.lecture, c.start_s))
        parts.append(AnswerPart(text=text, citations=ordered))
    if dropped:
        warnings.append(f"Ignored {dropped} citation(s) of passages the model wasn't shown.")
    in_course = out.in_course and bool(parts)
    if in_course and not any(p.citations for p in parts):
        warnings.append("This answer cites no lecture; treat it with care.")
    if not full:
        read = {passages[i].lecture.position for i in indices}
        warnings.append(
            f"Curio searched all {len(lectures)} lectures and read {len(indices)} of {len(passages)} "
            f"passages, from {len(read)} of them."
        )
    return CourseAnswer(
        id=uuid.uuid4().hex[:12],
        course_id=course_id,
        question=question,
        parts=parts,
        in_course=in_course,
        outside_knowledge=out.outside_knowledge.strip(),
        context="full" if full else "excerpts",
        search="" if full else search,
        lectures=len(lectures),
        warnings=warnings,
        llm=LLMInfo(provider=provider.name, models={"ask": model}),
        elapsed_s=round(time.monotonic() - started, 1),
    )
