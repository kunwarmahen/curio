"""Learning notes: structured notes, takeaways, definitions and recommendations.

Two steps, both grounded in the transcript:

1. Section notes, in batches like the evaluation step. Each section's transcript is
   shown as numbered sentences, and every point or definition the model writes names
   the sentence it comes from. Code turns that number into the sentence's real cue
   time, so each note links to where it is said; the model never writes a timestamp.
2. One short call over those notes writes the summary, the key takeaways and the
   recommendations, each tied to the sections it comes from.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

from pydantic import BaseModel

from curio_learn.analysis import prompts
from curio_learn.analysis.profiles import learner_brief
from curio_learn.analysis.scoring import ALWAYS_SKIP
from curio_learn.domain import (
    AnalysisStatus,
    Definition,
    LearningNotes,
    LLMInfo,
    NotePoint,
    NotesScope,
    Section,
    SectionNotes,
    Takeaway,
    VideoAnalysis,
    fmt_ts,
)
from curio_learn.errors import AnalysisError, LLMError
from curio_learn.llm.base import LLMProvider, generate_structured
from curio_learn.processing.normalize import Utterance, approx_tokens

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


class PointOut(BaseModel):
    text: str
    sentence: int


class DefinitionOut(BaseModel):
    term: str
    definition: str
    sentence: int


class SectionNotesOut(BaseModel):
    section_id: str
    points: list[PointOut]
    explanation: str
    definitions: list[DefinitionOut]


class NotesOut(BaseModel):
    sections: list[SectionNotesOut]


class TakeawayOut(BaseModel):
    text: str
    section_ids: list[str]


class NotesSummaryOut(BaseModel):
    summary: str
    takeaways: list[TakeawayOut]
    recommendations: list[TakeawayOut]


# ------------------------------------------------------------------ scope


def scope_sections(analysis: VideoAnalysis, scope: NotesScope, segment_ids: Sequence[str] = ()) -> list[Section]:
    """Sections the notes cover, in video order."""
    by_id = {s.section.id: s.section for s in analysis.sections}
    if scope is NotesScope.VIDEO:
        ids = [s.section.id for s in analysis.sections if s.section.kind not in ALWAYS_SKIP]
    elif scope is NotesScope.COURSE:
        ids = list(segment_ids)
        unknown = set(ids) - set(by_id)
        if unknown:
            raise AnalysisError(f"Unknown section ids: {', '.join(sorted(unknown))}.")
    else:
        if scope is NotesScope.PLAYLIST:
            segments = analysis.playlist.segments if analysis.playlist else []
        else:
            wanted = set(segment_ids)
            unknown = wanted - {seg.id for seg in analysis.segments}
            if unknown:
                raise AnalysisError(f"Unknown segment ids: {', '.join(sorted(unknown))}.")
            segments = [seg for seg in analysis.segments if seg.id in wanted]
        ids = [sid for seg in segments for sid in seg.section_ids]
    sections = sorted({sid: by_id[sid] for sid in ids if sid in by_id}.values(), key=lambda s: s.start_s)
    if not sections:
        raise AnalysisError("There is nothing to take notes on: no sections are selected.")
    return sections


def scope_key(scope: NotesScope, segment_ids: Sequence[str] = ()) -> str:
    return f"{scope.value}|{','.join(sorted(set(segment_ids))) if scope is NotesScope.SELECTED else ''}"


# ------------------------------------------------------------- step 1


def section_units(section: Section, units: Sequence[Utterance]) -> list[Utterance]:
    return [u for u in units if section.start_s <= u.start_s < section.end_s]


def _render_section(section: Section, units: Sequence[Utterance]) -> str:
    lines = "\n".join(f"{k}. {u.text}" for k, u in enumerate(units, 1))
    span = f"{fmt_ts(section.start_s)}–{fmt_ts(section.end_s)}"
    return f"### {section.id} [{span}] {section.kind.value}: {section.title}\n{lines}"


def _batches(sections: list[Section], units_by: dict[str, list[Utterance]], batch_tokens: int) -> list[list[Section]]:
    batches: list[list[Section]] = [[]]
    tokens = 0
    for s in sections:
        # Numbering adds about two tokens per sentence.
        t = approx_tokens(" ".join(u.text for u in units_by[s.id])) + 2 * len(units_by[s.id]) + 40
        if batches[-1] and tokens + t > batch_tokens:
            batches.append([])
            tokens = 0
        batches[-1].append(s)
        tokens += t
    return batches


def to_section_notes(out: SectionNotesOut, section: Section, units: list[Utterance]) -> tuple[SectionNotes, int]:
    """Convert model output, mapping sentence numbers to cue times.

    Returns the notes and how many sentence numbers were out of range (those notes
    fall back to the section's start, which is still a real cue time)."""
    bad = 0

    def at(n: int) -> float:
        nonlocal bad
        if 1 <= n <= len(units):
            return units[n - 1].start_s
        bad += 1
        return section.start_s

    points = [NotePoint(text=p.text.strip(), start_s=at(p.sentence)) for p in out.points if p.text.strip()]
    definitions = [
        Definition(term=d.term.strip(), definition=d.definition.strip(), start_s=at(d.sentence), section_id=section.id)
        for d in out.definitions
        if d.term.strip() and d.definition.strip()
    ]
    notes = SectionNotes(
        section_id=section.id,
        title=section.title,
        start_s=section.start_s,
        end_s=section.end_s,
        points=sorted(points, key=lambda p: p.start_s)[:8],
        explanation=out.explanation.strip(),
        definitions=definitions[:6],
    )
    return notes, bad


def write_section_notes(
    provider: LLMProvider,
    *,
    model: str,
    shared_prefix: str,
    sections: list[Section],
    units: Sequence[Utterance],
    batch_tokens: int,
    retries: int = 2,
    progress: Progress = lambda _: None,
) -> tuple[list[SectionNotes], list[str]]:
    units_by = {s.id: section_units(s, units) for s in sections}
    results: dict[str, SectionNotes] = {}
    warnings: list[str] = []
    bad_refs = 0

    def run(batch: list[Section], label: str) -> None:
        nonlocal bad_refs
        ids = [s.id for s in batch]
        text = "\n\n".join(_render_section(s, units_by[s.id]) for s in batch)
        out = generate_structured(
            provider,
            NotesOut,
            model=model,
            system=prompts.NOTES_SYSTEM,
            user=prompts.notes_user(sections_text=text, ids=ids),
            shared_prefix=shared_prefix,
            max_tokens=700 * len(batch) + 800,
            retries=retries,
            task=label,
        )
        by_id = {s.id: s for s in batch}
        for entry in out.sections:
            sid = entry.section_id.strip().upper()
            if sid in by_id and sid not in results:
                results[sid], bad = to_section_notes(entry, by_id[sid], units_by[sid])
                bad_refs += bad

    batches = _batches(sections, units_by, batch_tokens)
    for n, batch in enumerate(batches, 1):
        progress(f"Writing notes for {batch[0].id}–{batch[-1].id} (batch {n} of {len(batches)})")
        run(batch, f"notes[{n}]")
    missing = [s for s in sections if s.id not in results]
    if missing:
        progress(f"Writing notes for {len(missing)} section(s) the model skipped")
        try:
            run(missing, "notes[missing]")
        except LLMError as exc:
            warnings.append(f"Retrying skipped sections failed: {exc}")
    for s in sections:
        if s.id not in results:
            warnings.append(f"No notes for {s.id} ('{s.title}').")
            results[s.id] = SectionNotes(section_id=s.id, title=s.title, start_s=s.start_s, end_s=s.end_s)
    if bad_refs:
        warnings.append(
            f"{bad_refs} note(s) cited a sentence that doesn't exist; they link to their section's start instead."
        )
    return [results[s.id] for s in sections], warnings


# ------------------------------------------------------------- step 2


def render_notes_for_summary(notes: list[SectionNotes]) -> str:
    parts = []
    for n in notes:
        lines = [f"{n.section_id} [{fmt_ts(n.start_s)}] {n.title}"]
        lines += [f"- {p.text}" for p in n.points]
        if n.explanation:
            lines.append(f"  Explanation: {n.explanation}")
        lines += [f"  Defines {d.term}: {d.definition}" for d in n.definitions]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def to_takeaways(items: list[TakeawayOut], notes: list[SectionNotes]) -> tuple[list[Takeaway], int]:
    """Keep only section ids in scope; a takeaway links to its earliest section."""
    start = {n.section_id: n.start_s for n in notes}
    out, dropped = [], 0
    for item in items:
        text = item.text.strip()
        if not text:
            continue
        ids = sorted({i.strip().upper() for i in item.section_ids} & start.keys(), key=start.__getitem__)
        if not ids:
            dropped += 1
            continue
        out.append(Takeaway(text=text, section_ids=ids, start_s=start[ids[0]]))
    return out, dropped


# --------------------------------------------------------------- driver


def generate_notes(
    provider: LLMProvider,
    *,
    model: str,
    analysis: VideoAnalysis,
    units: Sequence[Utterance],
    scope: NotesScope,
    segment_ids: Sequence[str] = (),
    notes_id: str,
    batch_tokens: int,
    retries: int = 2,
    progress: Progress = lambda _: None,
) -> LearningNotes:
    started = time.monotonic()
    sections = scope_sections(analysis, scope, segment_ids)
    brief = learner_brief(analysis.preferences)
    shared = f"Video: {analysis.video.title}\n\n{brief}"

    notes, warnings = write_section_notes(
        provider,
        model=model,
        shared_prefix=shared,
        sections=sections,
        units=units,
        batch_tokens=batch_tokens,
        retries=retries,
        progress=progress,
    )

    summary, takeaways, recommendations = "", [], []
    if any(n.points for n in notes):
        progress("Writing key takeaways")
        try:
            out = generate_structured(
                provider,
                NotesSummaryOut,
                model=model,
                system=prompts.NOTES_SUMMARY_SYSTEM,
                user=prompts.notes_summary_user(
                    title=analysis.video.title, brief=brief, notes=render_notes_for_summary(notes)
                ),
                max_tokens=2500,
                retries=retries,
                task="notes[summary]",
            )
            summary = out.summary.strip()
            takeaways, d1 = to_takeaways(out.takeaways, notes)
            recommendations, d2 = to_takeaways(out.recommendations, notes)
            if d1 + d2:
                warnings.append(f"Dropped {d1 + d2} takeaway(s) that didn't name a section from these notes.")
        except LLMError as exc:
            warnings.append(f"Couldn't write the key takeaways ({exc}).")
    else:
        warnings.append("The model wrote no notes for these sections.")

    return LearningNotes(
        id=notes_id,
        analysis_id=analysis.id,
        video_id=analysis.video.video_id,
        scope=scope,
        segment_ids=sorted(set(segment_ids)) if scope is NotesScope.SELECTED else [],
        status=AnalysisStatus.DONE,
        progress="Done",
        summary=summary,
        takeaways=takeaways,
        sections=notes,
        recommendations=recommendations,
        warnings=warnings,
        llm=LLMInfo(provider=provider.name, models={"notes": model}),
        elapsed_s=round(time.monotonic() - started, 1),
    )


# ------------------------------------------------------------- markdown

SCOPE_LABEL = {
    NotesScope.PLAYLIST: "your playlist",
    NotesScope.VIDEO: "the whole video",
    NotesScope.SELECTED: "the parts you picked",
}


def to_markdown(notes: LearningNotes, analysis: VideoAnalysis) -> str:
    url = analysis.video.url

    def link(t: float) -> str:
        return f"[{fmt_ts(t)}]({url}&t={int(t)}s)"

    out = [f"# Notes: {analysis.video.title}", ""]
    meta = analysis.video.channel + ", " if analysis.video.channel else ""
    out += [
        f"{meta}{url}",
        "",
        f"Notes on {SCOPE_LABEL[notes.scope]}: {len(notes.sections)} sections, "
        f"{fmt_ts(notes.watch_s)} of {fmt_ts(analysis.video.duration_s)}. "
        "Written by AI from the transcript; each timestamp links to where it is said.",
        "",
    ]
    if notes.summary:
        out += [notes.summary, ""]
    if notes.takeaways:
        out += ["## Key takeaways", ""] + [f"- {t.text} ({link(t.start_s)})" for t in notes.takeaways] + [""]
    out += ["## Notes by section", ""]
    for s in notes.sections:
        out += [f"### {s.title} ({link(s.start_s)}–{fmt_ts(s.end_s)})", ""]
        out += [f"- {p.text} ({link(p.start_s)})" for p in s.points]
        if s.points:
            out.append("")
        if s.explanation:
            out += [s.explanation, ""]
        if not s.points and not s.explanation:
            out += ["_No notes for this section._", ""]
    if notes.glossary:
        out += ["## Definitions", ""]
        out += [f"- **{d.term}**: {d.definition} ({link(d.start_s)})" for d in notes.glossary] + [""]
    if notes.recommendations:
        out += ["## What to do next", ""] + [f"- {r.text} ({link(r.start_s)})" for r in notes.recommendations]
        out.append("")
    return "\n".join(out)
