"""Step 6c: find sponsor reads hidden inside content sections and cut them out.

Podcasts often place ad reads mid-conversation, introduced by a segue that
sounds like content ("One of my favorite quotes is…"). The outline can miss them,
which would send a learner into an ad. Code finds sentences that look like ad copy
(a web address, "sponsor", "promo code"…) and groups them into windows. One LLM call
confirms each window and gives the ad's exact first and last sentence. Each
confirmed ad becomes its own sponsor section, carved out of whatever it overlapped.
"""

from __future__ import annotations

import bisect
import logging
import re
from collections.abc import Callable

from pydantic import BaseModel

from curio_learn.analysis import prompts
from curio_learn.domain import Block, Section, SectionKind, fmt_ts
from curio_learn.errors import LLMError
from curio_learn.llm.base import LLMProvider, generate_structured
from curio_learn.processing.normalize import Utterance

log = logging.getLogger(__name__)

AD_SIGNAL_RE = re.compile(
    r"\b[a-z0-9-]+\s?(?:\.|dot)\s?(?:com|io|co|ai|org|net)\b(?:/\w+)?"
    r"|\bsponsor(?:ed|s|ing)?\b|\bbrought to you by\b|\bpromo code\b|\buse (?:the )?code\b"
    r"|\blink in the (?:video )?description\b|\bpatreon\b|\bfree trial\b",
    re.IGNORECASE,
)


class AdWindowOut(BaseModel):
    window_id: str
    is_ad: bool
    first_sentence: int
    last_sentence: int
    sponsor: str


class AdsOut(BaseModel):
    windows: list[AdWindowOut]


def candidate_windows(
    units: list[Utterance], *, lead_s: float = 75.0, tail_s: float = 25.0, join_s: float = 120.0
) -> list[tuple[int, int]]:
    """Unit index ranges around clusters of ad-like sentences.

    The window reaches back `lead_s` before the first signal because ad reads usually
    name the sponsor (or its web address) only after a segue.
    """
    hits = [i for i, u in enumerate(units) if AD_SIGNAL_RE.search(u.text)]
    clusters: list[list[int]] = []
    for i in hits:
        if clusters and units[i].start_s - units[clusters[-1][-1]].start_s <= join_s:
            clusters[-1].append(i)
        else:
            clusters.append([i])
    starts = [u.start_s for u in units]
    windows = []
    for c in clusters:
        lo = bisect.bisect_left(starts, units[c[0]].start_s - lead_s)
        hi = bisect.bisect_right(starts, units[c[-1]].end_s + tail_s) - 1
        windows.append((lo, max(hi, c[-1])))
    return windows


def carve(
    sections: list[Section],
    ads: list[tuple[float, float, str]],
    blocks: list[Block],
    *,
    min_piece_s: float = 20.0,
) -> tuple[list[Section], list[str]]:
    """Rebuild the section list with each ad interval as its own sponsor section.

    Content outside the ads keeps its section's title and judgments; a section split
    in two gets "(continued)" on the second part. Content pieces shorter than `min_piece_s`
    join a neighbouring content piece (or the ad, if ads surround them). Sections stay
    contiguous and ordered.
    """
    if not ads:
        return sections, []
    ads = sorted(ads)
    block_starts = [b.start_s for b in blocks]

    def block_at(t: float) -> int:
        return max(0, bisect.bisect_right(block_starts, t) - 1)

    pieces: list[dict] = []
    for s in sections:
        cursor = s.start_s
        part = 0
        for a0, a1, name in ads:
            if a1 <= cursor or a0 >= s.end_s:
                continue
            if a0 > cursor:
                pieces.append({"src": s, "start": cursor, "end": a0, "part": part})
                part += 1
            pieces.append({"ad": name, "start": max(a0, cursor), "end": min(a1, s.end_s)})
            cursor = min(a1, s.end_s)
        if cursor < s.end_s:
            pieces.append({"src": s, "start": cursor, "end": s.end_s, "part": part})

    # Merge adjacent ad pieces. A tiny content fragment joins the neighbouring content
    # piece (previous first) so content is never hidden inside an ad; only a fragment
    # with ads on both sides is absorbed into the ad.
    merged: list[dict] = []
    for p in pieces:
        if merged and "ad" in p and "ad" in merged[-1]:
            merged[-1]["end"] = p["end"]
            continue
        merged.append(p)
    out: list[dict] = []
    for i, p in enumerate(merged):
        if "src" not in p or p["end"] - p["start"] >= min_piece_s:
            out.append(p)
            continue
        nxt = merged[i + 1] if i + 1 < len(merged) else None
        if out and "src" in out[-1]:
            out[-1]["end"] = p["end"]
        elif nxt is not None and "src" in nxt:
            nxt["start"] = p["start"]
        elif out:
            out[-1]["end"] = p["end"]
        elif nxt is not None:
            nxt["start"] = p["start"]
        else:
            out.append(p)

    repairs, result = [], []
    for n, p in enumerate(out, 1):
        sid = f"S{n:02d}"
        common = {
            "id": sid,
            "start_s": p["start"],
            "end_s": p["end"],
            "start_block": block_at(p["start"]),
            "end_block": block_at(max(p["start"], p["end"] - 0.01)),
        }
        if "ad" in p:
            name = p["ad"].strip()
            result.append(
                Section(
                    **common,
                    title=f"Sponsor message: {name}" if name else "Sponsor message",
                    summary=f"Advertisement{f' for {name}' if name else ''}.",
                    kind=SectionKind.SPONSOR,
                    key_concepts=[],
                )
            )
            repairs.append(
                f"Cut out a sponsor read ({fmt_ts(p['start'])}–{fmt_ts(p['end'])}"
                f"{f', {name}' if name else ''}) as its own section."
            )
        else:
            src: Section = p["src"]
            title = src.title if p["part"] == 0 else f"{src.title} (continued)"
            result.append(src.model_copy(update={**common, "title": title}))
    return result, repairs


def remove_ads(
    provider: LLMProvider,
    *,
    model: str,
    sections: list[Section],
    blocks: list[Block],
    units: list[Utterance],
    retries: int = 2,
    progress: Callable[[str], None] = lambda _: None,
) -> tuple[list[Section], list[str], list[str]]:
    """Return sections with confirmed ad reads cut out, plus repairs and warnings."""
    windows = candidate_windows(units)
    if not windows:
        return sections, [], []
    progress("Checking for sponsor reads inside sections")
    parts, by_id = [], {}
    for n, (lo, hi) in enumerate(windows, 1):
        wid = f"W{n}"
        by_id[wid] = (lo, hi)
        lines = "\n".join(f"  {k}. [{fmt_ts(u.start_s)}] {u.text}" for k, u in enumerate(units[lo : hi + 1], 1))
        parts.append(f"### Window {wid}\nSentences:\n{lines}")
    try:
        out = generate_structured(
            provider,
            AdsOut,
            model=model,
            system=prompts.ADS_SYSTEM,
            user=prompts.ads_user(parts=parts, ids=list(by_id)),
            max_tokens=80 * len(windows) + 400,
            retries=retries,
            task="ads",
        )
    except LLMError as exc:
        return sections, [], [f"Couldn't check for sponsor reads inside sections ({exc})."]

    ads: list[tuple[float, float, str]] = []
    repairs: list[str] = []
    for w in out.windows:
        if not w.is_ad or w.window_id not in by_id:
            continue
        lo, hi = by_id[w.window_id]
        n = hi - lo + 1
        if not (1 <= w.first_sentence <= w.last_sentence <= n):
            repairs.append(
                f"Ignored sponsor read {w.window_id}: sentence numbers {w.first_sentence}-"
                f"{w.last_sentence} are outside 1-{n}."
            )
            continue
        a0, a1 = units[lo + w.first_sentence - 1].start_s, units[lo + w.last_sentence - 1].end_s
        if a1 - a0 < 10:
            continue  # a passing mention, not an ad read
        ads.append((a0, a1, w.sponsor))
        log.info("Sponsor read %s–%s (%s)", fmt_ts(a0), fmt_ts(a1), w.sponsor)
    carved, more = carve(sections, ads, blocks)
    return carved, repairs + more, []
