"""Steps 4-7: contextual evaluation of every section against the whole-video outline."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Protocol

from pydantic import BaseModel

from curio_learn.analysis import prompts
from curio_learn.analysis.outline import render_outline
from curio_learn.domain import Rubric, Section, SectionEvaluation, fmt_ts
from curio_learn.errors import LLMError
from curio_learn.llm.base import LLMProvider, generate_structured
from curio_learn.processing.normalize import approx_tokens

log = logging.getLogger(__name__)


class SectionEvalOut(BaseModel):
    # Free-text judgments come before the numbers so the model reasons first.
    section_id: str
    what_you_learn: str
    why_valuable: str
    skip_reason: str
    standalone: bool
    builds_on: list[str]
    redundant_with: list[str]
    introduces_concepts: list[str]
    prerequisites: list[str]
    educational_value: int
    novelty: int
    practical: int
    depth: int
    relevance: int


class EvaluationOut(BaseModel):
    evaluations: list[SectionEvalOut]


def _clamp(v: float) -> float:
    return float(min(10, max(1, v)))


def to_evaluation(out: SectionEvalOut, valid_ids: set[str]) -> SectionEvaluation:
    def ids(values: list[str]) -> list[str]:
        cleaned = [v.strip().upper() for v in values]
        return [v for v in cleaned if v in valid_ids and v != out.section_id.upper()]

    return SectionEvaluation(
        section_id=out.section_id.strip().upper(),
        rubric=Rubric(
            educational_value=_clamp(out.educational_value),
            novelty=_clamp(out.novelty),
            practical=_clamp(out.practical),
            depth=_clamp(out.depth),
            relevance=_clamp(out.relevance),
        ),
        standalone=out.standalone,
        introduces_concepts=[c for c in out.introduces_concepts if c.strip()][:6],
        builds_on=ids(out.builds_on),
        redundant_with=ids(out.redundant_with),
        prerequisites=[p for p in out.prerequisites if p.strip()][:5],
        what_you_learn=out.what_you_learn.strip(),
        why_valuable=out.why_valuable.strip(),
        skip_reason=out.skip_reason.strip(),
    )


class Timed(Protocol):
    start_s: float
    text: str


def section_text(section: Section, units: Sequence[Timed]) -> str:
    """Transcript text that plays within the section (blocks or sentence units)."""
    return " ".join(u.text for u in units if section.start_s <= u.start_s < section.end_s)


def _batches(sections: list[Section], blocks: Sequence[Timed], batch_tokens: int) -> list[list[Section]]:
    batches: list[list[Section]] = [[]]
    tokens = 0
    for s in sections:
        t = approx_tokens(section_text(s, blocks)) + 40
        if batches[-1] and tokens + t > batch_tokens:
            batches.append([])
            tokens = 0
        batches[-1].append(s)
        tokens += t
    return batches


def evaluate_sections(
    provider: LLMProvider,
    *,
    model: str,
    title: str,
    brief: str,
    sections: list[Section],
    blocks: Sequence[Timed],
    batch_tokens: int,
    retries: int = 2,
    progress: Callable[[str], None] = lambda _: None,
    done: dict[str, SectionEvaluation] | None = None,
    on_batch: Callable[[dict[str, SectionEvaluation]], None] = lambda _: None,
) -> tuple[dict[str, SectionEvaluation], list[str]]:
    """Evaluate all sections. Returns evaluations by section id and warnings.

    `done` holds evaluations from an interrupted earlier run: a batch with any of its
    sections there is not asked again (sections the model skipped in it are asked in
    the pass for skipped sections, as usual). `on_batch` gets every evaluation so far
    after each batch, so a caller can save them."""
    shared = prompts.evaluate_shared_prefix(title=title, brief=brief, outline=render_outline(sections))
    valid_ids = {s.id for s in sections}
    results: dict[str, SectionEvaluation] = {k: v for k, v in (done or {}).items() if k in valid_ids}
    warnings: list[str] = []
    batches = _batches(sections, blocks, batch_tokens)

    def run(batch: list[Section], label: str) -> None:
        text = "\n\n".join(
            f"### {s.id} [{fmt_ts(s.start_s)}–{fmt_ts(s.end_s)}] {s.kind.value}: {s.title}\n{section_text(s, blocks)}"
            for s in batch
        )
        ids = [s.id for s in batch]
        out = generate_structured(
            provider,
            EvaluationOut,
            model=model,
            system=prompts.EVALUATE_SYSTEM,
            user=prompts.evaluate_user(sections_text=text, ids=ids),
            shared_prefix=shared,
            max_tokens=600 * len(batch) + 1000,
            retries=retries,
            task=label,
        )
        for ev in out.evaluations:
            converted = to_evaluation(ev, valid_ids)
            if converted.section_id in ids:
                results[converted.section_id] = converted

    for n, batch in enumerate(batches, 1):
        if any(s.id in results for s in batch):
            continue  # evaluated before a pause
        progress(f"Evaluating sections {batch[0].id}–{batch[-1].id} (batch {n} of {len(batches)})")
        run(batch, f"evaluate[{n}]")
        on_batch(dict(results))

    missing = [s for s in sections if s.id not in results]
    if missing:
        progress(f"Re-evaluating {len(missing)} section(s) the model skipped")
        try:
            run(missing, "evaluate[missing]")
        except LLMError as exc:
            warnings.append(f"Re-evaluation failed: {exc}")
    for s in sections:
        if s.id not in results:
            warnings.append(f"No evaluation for {s.id} ('{s.title}'); scored conservatively.")
            results[s.id] = SectionEvaluation(
                section_id=s.id,
                rubric=Rubric(educational_value=4, novelty=4, practical=4, depth=4, relevance=4),
                what_you_learn=s.summary,
                why_valuable="",
                skip_reason="",
            )
    return results, warnings
