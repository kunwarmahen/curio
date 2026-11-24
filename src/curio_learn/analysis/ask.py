"""Ask the Video: answers grounded in the transcript, with clickable timestamps.

The model sees the outline plus either the whole transcript (when it fits the
context budget: about 95 minutes of speech with a 32k local context, always with
Claude) or the passages that search found. It answers in parts, each citing passage
numbers. Code turns every citation into the start of the sentence in that passage
that best matches the part, so a timestamp is always a real cue time. Knowledge that
isn't from the video comes back in its own field and is shown separately.

The transcript is the first part of the prompt and doesn't change between
questions, so providers that cache prompt prefixes (Claude, and Ollama's KV cache)
only process the new question.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import Callable, Sequence

from pydantic import BaseModel

from curio_learn.analysis import prompts
from curio_learn.analysis.outline import render_outline
from curio_learn.analysis.profiles import learner_brief
from curio_learn.analysis.redundancy import tokenize
from curio_learn.domain import Answer, AnswerPart, Block, Citation, LLMInfo, VideoAnalysis, fmt_ts
from curio_learn.llm.base import LLMProvider, generate_structured
from curio_learn.processing.normalize import Utterance, approx_tokens
from curio_learn.processing.retrieval import time_hints

log = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 1000
HISTORY_TURNS = 4
ANSWER_TOKENS = 2000


class AnswerPartOut(BaseModel):
    text: str
    passages: list[int]


class AnswerOut(BaseModel):
    parts: list[AnswerPartOut]
    in_video: bool
    outside_knowledge: str


def _block_tokens(b: Block) -> int:
    return approx_tokens(b.text) + 8


def select_context(
    blocks: Sequence[Block],
    *,
    budget_tokens: int,
    hints: Sequence[float] = (),
    ranked: Callable[[], list[int]] = lambda: [],
    hint_before_s: float = 90.0,
    hint_after_s: float = 150.0,
) -> tuple[list[int], bool]:
    """Indices of the blocks to show, in order, and whether that is the whole transcript.

    When everything doesn't fit, the stretches the question points at by time come
    first, then each search hit with its neighbours (so a passage keeps its setup)."""
    if sum(_block_tokens(b) for b in blocks) <= budget_tokens:
        return list(range(len(blocks))), True
    chosen: set[int] = set()
    used = 0

    def add(i: int) -> bool:
        nonlocal used
        if i in chosen or not 0 <= i < len(blocks):
            return True
        t = _block_tokens(blocks[i])
        if used + t > budget_tokens:
            return False
        chosen.add(i)
        used += t
        return True

    for t in hints:
        for i, b in enumerate(blocks):
            if b.end_s > t - hint_before_s and b.start_s < t + hint_after_s:
                add(i)
    for i in ranked():
        if not all(add(j) for j in (i, i - 1, i + 1)):
            break
    return sorted(chosen), False


def render_context(blocks: Sequence[Block], indices: list[int]) -> str:
    lines, prev = [], None
    for i in indices:
        if prev is not None and i != prev + 1:
            lines.append("…")
        b = blocks[i]
        lines.append(f"[{b.index} | {fmt_ts(b.start_s)}] {b.text}")
        prev = i
    return "\n".join(lines)


def precise_start(text: str, block: Block, units: Sequence[Utterance]) -> float:
    """Start of the sentence in `block` that shares the most words with `text`; the
    block's start when none does. Either way a real cue time."""
    words = set(tokenize(text))
    best, best_score = block.start_s, 0.0
    for u in units:
        if not (block.start_s <= u.start_s < block.end_s):
            continue
        toks = set(tokenize(u.text))
        score = len(words & toks) / math.sqrt(len(toks) + 1)
        if score > best_score:
            best, best_score = u.start_s, score
    return best


def render_history(history: Sequence[Answer]) -> str:
    turns = []
    for a in history[-HISTORY_TURNS:]:
        reply = " ".join(p.text for p in a.parts)
        if a.outside_knowledge:
            reply += f" (Not from the video: {a.outside_knowledge})"
        turns.append(f"Learner: {a.question}\nCurio: {reply}")
    return "\n\n".join(turns)


def search_query(question: str, history: Sequence[Answer]) -> str:
    """Follow-ups ("and how does it compare?") lean on the previous question."""
    return f"{question} {history[-1].question}" if history else question


def answer_question(
    provider: LLMProvider,
    *,
    model: str,
    analysis: VideoAnalysis,
    blocks: Sequence[Block],
    units: Sequence[Utterance],
    question: str,
    history: Sequence[Answer] = (),
    budget_tokens: int,
    ranked: Callable[[str], list[int]] = lambda _: [],
    search: str = "keyword",
    retries: int = 2,
) -> Answer:
    started = time.monotonic()
    question = " ".join(question.split())[:MAX_QUESTION_CHARS]
    hints = time_hints(question)
    indices, full = select_context(
        blocks, budget_tokens=budget_tokens, hints=hints, ranked=lambda: ranked(search_query(question, history))
    )
    sections = [s.section for s in analysis.sections]
    shared = prompts.ask_shared_prefix(
        title=analysis.video.title,
        brief=learner_brief(analysis.preferences),
        outline=render_outline(sections),
        transcript=render_context(blocks, indices),
        full=full,
    )
    hint_text = ""
    if hints:
        hint_text = "The learner refers to " + ", ".join(fmt_ts(t) for t in hints) + " in the video."
    out = generate_structured(
        provider,
        AnswerOut,
        model=model,
        system=prompts.ASK_SYSTEM,
        user=prompts.ask_user(question=question, history=render_history(history), hints=hint_text),
        shared_prefix=shared,
        max_tokens=ANSWER_TOKENS,
        retries=retries,
        task="ask",
    )

    shown = {blocks[i].index: blocks[i] for i in indices}
    warnings: list[str] = []
    dropped = 0
    parts: list[AnswerPart] = []
    for p in out.parts:
        text = p.text.strip()
        if not text:
            continue
        times: list[float] = []
        for n in p.passages:
            if n not in shown:
                dropped += 1
                continue
            t = precise_start(text, shown[n], units)
            if t not in times:
                times.append(t)
        parts.append(AnswerPart(text=text, citations=[Citation(start_s=t) for t in sorted(times)]))
    if dropped:
        warnings.append(f"Ignored {dropped} citation(s) of passages the model wasn't shown.")
    in_video = out.in_video and bool(parts)
    if in_video and not any(p.citations for p in parts):
        warnings.append("This answer cites no part of the video; treat it with care.")
    if not full:
        warnings.append(
            f"The transcript is too long to read in full here, so Curio searched it and read "
            f"{len(indices)} of {len(blocks)} passages."
        )
    return Answer(
        id=uuid.uuid4().hex[:12],
        analysis_id=analysis.id,
        question=question,
        parts=parts,
        in_video=in_video,
        outside_knowledge=out.outside_knowledge.strip(),
        context="full" if full else "excerpts",
        search="" if full else search,
        warnings=warnings,
        llm=LLMInfo(provider=provider.name, models={"ask": model}),
        elapsed_s=round(time.monotonic() - started, 1),
    )
