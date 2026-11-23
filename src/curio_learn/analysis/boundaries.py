"""Step 6b: refine section starts from block level (20-45 s) to sentence level.

The outline places each section start on a transcript block, but the real topic
change can fall anywhere inside a block. For every boundary the LLM sees the
sentences around it and names the one where the next section begins. Code then
moves the boundary to that sentence's cue time. Every timestamp still comes from
the original captions, and choices that would break the section order are
rejected.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import BaseModel

from curio_learn.analysis import prompts
from curio_learn.domain import Block, Section, fmt_ts
from curio_learn.errors import LLMError
from curio_learn.llm.base import LLMProvider, generate_structured
from curio_learn.processing.normalize import Utterance

log = logging.getLogger(__name__)


class BoundaryChoice(BaseModel):
    boundary_id: str
    first_sentence: int


class BoundariesOut(BaseModel):
    boundaries: list[BoundaryChoice]


def _candidates(prev: Section, nxt: Section, blocks: list[Block], units: list[Utterance]) -> list[Utterance]:
    """Sentences from one block before the boundary to one block after it, kept
    inside the two sections involved so a boundary can never jump past another."""
    lo_block = max(prev.start_block + 1, nxt.start_block - 1)
    hi_block = min(nxt.end_block, nxt.start_block + 1)
    lo, hi = blocks[lo_block].start_s, blocks[hi_block].end_s
    return [u for u in units if lo <= u.start_s < hi]


def refine_boundaries(
    provider: LLMProvider,
    *,
    model: str,
    sections: list[Section],
    blocks: list[Block],
    units: list[Utterance],
    min_section_s: float = 30.0,
    retries: int = 2,
    progress: Callable[[str], None] = lambda _: None,
) -> tuple[list[Section], list[str], list[str]]:
    """Return sections with sentence-level starts, plus repairs and warnings."""
    if len(sections) < 2:
        return sections, [], []
    progress("Placing section boundaries at sentence level")

    windows: dict[str, list[Utterance]] = {}
    parts: list[str] = []
    for prev, nxt in zip(sections, sections[1:], strict=False):
        cands = _candidates(prev, nxt, blocks, units)
        if len(cands) < 2:
            continue
        bid = f"{prev.id}/{nxt.id}"
        windows[bid] = cands
        current = next((n for n, u in enumerate(cands, 1) if u.start_s >= nxt.start_s), 1)
        lines = "\n".join(f"  {n}. [{fmt_ts(u.start_s)}] {u.text}" for n, u in enumerate(cands, 1))
        parts.append(
            f"### Boundary {bid}\n"
            f"Ending: {prev.title}: {prev.summary}\n"
            f"Starting: {nxt.title}: {nxt.summary}\n"
            f"Current guess: sentence {current}\nSentences:\n{lines}"
        )
    if not windows:
        return sections, [], []

    try:
        out = generate_structured(
            provider,
            BoundariesOut,
            model=model,
            system=prompts.BOUNDARY_SYSTEM,
            user=prompts.boundary_user(parts=parts, ids=list(windows)),
            max_tokens=60 * len(windows) + 500,
            retries=retries,
            task="boundaries",
        )
    except LLMError as exc:
        return sections, [], [f"Kept block-level boundaries: sentence refinement failed ({exc})."]

    chosen = {c.boundary_id.strip(): c.first_sentence for c in out.boundaries}
    repairs: list[str] = []
    starts = [s.start_s for s in sections]
    for i in range(1, len(sections)):
        prev, nxt = sections[i - 1], sections[i]
        bid = f"{prev.id}/{nxt.id}"
        if bid not in windows or bid not in chosen:
            continue
        cands, k = windows[bid], chosen[bid]
        if not 1 <= k <= len(cands):
            repairs.append(f"Ignored sentence {k} for boundary {bid}: there are only {len(cands)} sentences.")
            continue
        new_start = cands[k - 1].start_s
        next_end = sections[i + 1].start_s if i + 1 < len(sections) else nxt.end_s
        # Both sections involved must keep a watchable length.
        if new_start - starts[i - 1] < min_section_s or next_end - new_start < min_section_s:
            repairs.append(f"Kept the block-level start of {nxt.id}: the chosen sentence left a section too short.")
            continue
        if abs(new_start - starts[i]) >= 0.5:
            log.info("Boundary %s moved %+.1fs", bid, new_start - starts[i])
        starts[i] = new_start

    refined: list[Section] = []
    for i, s in enumerate(sections):
        end = starts[i + 1] if i + 1 < len(sections) else s.end_s
        # Ends on the last sentence before the next section begins.
        end = max((u.end_s for u in units if starts[i] <= u.start_s < end), default=end)
        refined.append(s.model_copy(update={"start_s": starts[i], "end_s": end}))
    return refined, repairs, []
