"""Step 3: topic segmentation of the full transcript into contiguous sections."""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import BaseModel, Field

from curio_learn.analysis import prompts
from curio_learn.domain import Block, Section, SectionKind, VideoMetadata, fmt_ts
from curio_learn.llm.base import LLMProvider, generate_structured
from curio_learn.processing.normalize import approx_tokens, render_blocks

log = logging.getLogger(__name__)


# ----------------------------------------------------------- LLM output schemas


class OutlineSectionOut(BaseModel):
    start_block: int = Field(description="Number of the first transcript block of this section")
    title: str
    summary: str
    kind: SectionKind
    key_concepts: list[str]


class OutlineOut(BaseModel):
    overview: str
    key_concepts: list[str]
    sections: list[OutlineSectionOut]


class OverviewOut(BaseModel):
    overview: str
    key_concepts: list[str]


# --------------------------------------------------------------------- result


class Outline(BaseModel):
    overview: str
    key_concepts: list[str]
    sections: list[Section]


def _chapters_text(meta: VideoMetadata) -> str:
    return "\n".join(f"{fmt_ts(c.start_s)} {c.title}" for c in meta.chapters)


def build_sections(
    raw: list[OutlineSectionOut],
    blocks: list[Block],
    *,
    first_block: int = 0,
    min_section_s: float = 30.0,
) -> tuple[list[Section], list[str]]:
    """Turn LLM start blocks into validated, contiguous sections.

    Returns the sections and a list of repairs applied. Guarantees: sections
    tile blocks[first_block:] exactly, in order, without gaps or overlaps.
    """
    repairs: list[str] = []
    last = len(blocks) - 1
    seen: set[int] = set()
    items: list[OutlineSectionOut] = []
    for item in sorted(raw, key=lambda s: s.start_block):
        if item.start_block < first_block or item.start_block > last:
            repairs.append(f"Dropped section '{item.title}' with out-of-range block {item.start_block}")
            continue
        if item.start_block in seen:
            repairs.append(f"Dropped duplicate section start at block {item.start_block}")
            continue
        seen.add(item.start_block)
        items.append(item)
    if not items:
        raise ValueError("Outline contained no usable sections")
    if items[0].start_block != first_block:
        repairs.append(f"Moved first section start from block {items[0].start_block} to {first_block}")
        items[0] = items[0].model_copy(update={"start_block": first_block})

    # Merge sections that are too short to be watched on their own into a neighbour.
    def span_s(i: int) -> float:
        start = blocks[items[i].start_block].start_s
        end_block = items[i + 1].start_block - 1 if i + 1 < len(items) else last
        return blocks[end_block].end_s - start

    i = 0
    while len(items) > 1 and i < len(items):
        if span_s(i) < min_section_s:
            short = items.pop(i)
            if i == 0:
                # Absorb the next section into this start.
                items[0] = items[0].model_copy(update={"start_block": short.start_block})
            repairs.append(f"Merged very short section '{short.title}' into its neighbour")
            i = max(0, i - 1)
            continue
        i += 1

    sections: list[Section] = []
    for n, item in enumerate(items):
        end_block = items[n + 1].start_block - 1 if n + 1 < len(items) else last
        sections.append(
            Section(
                id=f"S{n + 1:02d}",
                start_block=item.start_block,
                end_block=end_block,
                start_s=blocks[item.start_block].start_s,
                end_s=blocks[end_block].end_s,
                title=item.title.strip() or f"Section {n + 1}",
                summary=item.summary.strip(),
                kind=item.kind,
                key_concepts=[c.strip() for c in item.key_concepts if c.strip()][:6],
            )
        )
    return sections, repairs


def render_outline(sections: list[Section]) -> str:
    return "\n".join(
        f"{s.id} [{fmt_ts(s.start_s)}–{fmt_ts(s.end_s)}] {s.kind.value}: {s.title} — {s.summary}" for s in sections
    )


def _windows(blocks: list[Block], window_tokens: int) -> list[tuple[int, int]]:
    """Split block indices into consecutive windows of about `window_tokens`."""
    windows: list[tuple[int, int]] = []
    start, tokens = 0, 0
    for b in blocks:
        t = approx_tokens(b.text) + 8
        if tokens + t > window_tokens and b.index > start:
            windows.append((start, b.index - 1))
            start, tokens = b.index, 0
        tokens += t
    windows.append((start, blocks[-1].index))
    return windows


def generate_outline(
    provider: LLMProvider,
    *,
    model: str,
    meta: VideoMetadata,
    blocks: list[Block],
    window_tokens: int,
    retries: int = 2,
    progress: Callable[[str], None] = lambda _: None,
) -> tuple[Outline, list[str]]:
    total_tokens = approx_tokens(render_blocks(blocks))
    if total_tokens <= window_tokens:
        progress("Mapping the topics of the whole transcript")
        out = generate_structured(
            provider,
            OutlineOut,
            model=model,
            system=prompts.OUTLINE_SYSTEM,
            user=prompts.outline_user(
                title=meta.title,
                channel=meta.channel,
                description=meta.description,
                chapters=_chapters_text(meta),
                transcript=render_blocks(blocks),
                first_block=0,
                last_block=blocks[-1].index,
                minutes=(blocks[-1].end_s - blocks[0].start_s) / 60,
            ),
            max_tokens=8000,
            retries=retries,
            task="outline",
        )
        sections, repairs = build_sections(out.sections, blocks)
        return Outline(overview=out.overview, key_concepts=out.key_concepts, sections=sections), repairs

    # Long transcript: windowed passes. Each window re-segments from the start of
    # the previous window's last section, so no boundary is decided without context.
    collected: list[OutlineSectionOut] = []
    repairs: list[str] = []
    start = 0
    n_windows = len(_windows(blocks, window_tokens))
    k = 0
    while start <= blocks[-1].index:
        k += 1
        remaining = blocks[start:]
        end = _windows(remaining, window_tokens)[0][1]
        progress(f"Mapping topics, part {k} of ~{n_windows} (blocks {start}-{end})")
        previous = "\n".join(f"- {s.title}: {s.summary}" for s in collected[-12:])
        out = generate_structured(
            provider,
            OutlineOut,
            model=model,
            system=prompts.OUTLINE_SYSTEM,
            user=prompts.outline_user(
                title=meta.title,
                channel=meta.channel,
                description=meta.description if k == 1 else "",
                chapters=_chapters_text(meta),
                transcript=render_blocks(blocks[start : end + 1]),
                first_block=start,
                last_block=end,
                minutes=(blocks[end].end_s - blocks[start].start_s) / 60,
                previous_outline=previous,
                want_overview=False,
            ),
            max_tokens=8000,
            retries=retries,
            task=f"outline[{k}]",
        )
        window_sections = sorted(
            (s for s in out.sections if start <= s.start_block <= end), key=lambda s: s.start_block
        )
        if not window_sections:
            raise ValueError(f"Outline window {start}-{end} returned no sections")
        if window_sections[0].start_block != start:
            window_sections[0] = window_sections[0].model_copy(update={"start_block": start})
        is_last = end >= blocks[-1].index
        if is_last or len(window_sections) == 1:
            collected.extend(window_sections)
            start = end + 1
        else:
            # Keep all but the last section; it may continue into the next window.
            collected.extend(window_sections[:-1])
            start = window_sections[-1].start_block

    sections, more = build_sections(collected, blocks)
    repairs += more
    progress("Writing the video overview")
    ov = generate_structured(
        provider,
        OverviewOut,
        model=model,
        system=prompts.OVERVIEW_SYSTEM,
        user=f"Video: {meta.title}\n\nOutline:\n{render_outline(sections)}\n\nWrite `overview` "
        "(2-4 sentences) and `key_concepts` (5-10 items).",
        max_tokens=2000,
        retries=retries,
        task="overview",
    )
    return Outline(overview=ov.overview, key_concepts=ov.key_concepts, sections=sections), repairs
