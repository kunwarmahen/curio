"""Transcript normalization.

Turns raw caption cues into clean, non-overlapping cues and then merges them
into *blocks* of roughly 20-45 seconds. Blocks are the atomic unit for segment
boundaries: the LLM refers to block numbers, and code converts them back to the
original cue timestamps, so a timestamp can never be invented.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from curio_learn.domain import Block, TranscriptCue

_ANNOTATION_RE = re.compile(
    r"\[(?:[^\]]{0,40})\]|\((?:music|applause|laughter|inaudible|"
    r"silence|laughs|cheering)[^)]*\)",
    re.IGNORECASE,
)
_SPEAKER_RE = re.compile(r"^\s*(>>+|-\s+)")
_WS_RE = re.compile(r"\s+")
_SENTENCE_END_RE = re.compile(r"[.!?…][\"')\]]*$")


@dataclass
class CleanCue:
    start_s: float
    end_s: float
    text: str
    speaker_change: bool = False


def clean_text(text: str) -> tuple[str, bool]:
    """Return cleaned text and whether the cue starts with a speaker-change marker."""
    text = html.unescape(text).replace("\n", " ")
    speaker_change = bool(_SPEAKER_RE.match(text))
    text = _SPEAKER_RE.sub("", text)
    text = _ANNOTATION_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text, speaker_change


def _strip_rolling_overlap(prev: str, cur: str, max_words: int = 12) -> str:
    """Auto-captions often repeat the tail of the previous cue at the start of the next."""
    if not prev or not cur:
        return cur
    p, c = prev.split(), cur.split()
    for n in range(min(max_words, len(p), len(c)), 2, -1):
        if [w.lower() for w in p[-n:]] == [w.lower() for w in c[:n]]:
            return " ".join(c[n:])
    return cur


def clean_cues(cues: list[TranscriptCue]) -> list[CleanCue]:
    ordered = sorted(cues, key=lambda c: c.start_s)
    out: list[CleanCue] = []
    for i, cue in enumerate(ordered):
        raw, spk = clean_text(cue.text)
        end = cue.start_s + max(cue.duration_s, 0.0)
        if i + 1 < len(ordered):
            end = min(end, ordered[i + 1].start_s) if ordered[i + 1].start_s > cue.start_s else end
        end = max(end, cue.start_s + 0.01)
        if not raw:
            continue  # only an annotation like [Music]
        if (
            out
            and cue.start_s - out[-1].end_s < 1.0
            and (raw == out[-1].text or not _strip_rolling_overlap(out[-1].text, raw))
        ):
            # A cue that only repeats the previous one: keep its time, not its words.
            out[-1].end_s = max(out[-1].end_s, end)
            continue
        text = _strip_rolling_overlap(out[-1].text, raw) if out else raw
        out.append(CleanCue(cue.start_s, end, text, spk))
    return out


def build_blocks(
    cues: list[TranscriptCue],
    *,
    min_s: float = 20.0,
    max_s: float = 45.0,
    pause_s: float = 1.2,
    tail_merge_s: float = 8.0,
) -> list[Block]:
    """Merge cues into blocks that prefer to end at sentence ends or pauses."""
    clean = clean_cues(cues)
    blocks: list[Block] = []
    cur: list[CleanCue] = []

    def flush() -> None:
        if not cur:
            return
        idx = len(blocks)
        blocks.append(
            Block(
                id=f"B{idx}",
                index=idx,
                start_s=round(cur[0].start_s, 3),
                end_s=round(cur[-1].end_s, 3),
                text=" ".join(c.text for c in cur),
            )
        )
        cur.clear()

    for i, cue in enumerate(clean):
        if cur:
            span = cue.end_s - cur[0].start_s
            if cue.speaker_change and cue.start_s - cur[0].start_s >= min_s / 2 or span > max_s:
                flush()
        cur.append(cue)
        span = cue.end_s - cur[0].start_s
        nxt = clean[i + 1] if i + 1 < len(clean) else None
        gap = (nxt.start_s - cue.end_s) if nxt else 0.0
        natural_break = bool(_SENTENCE_END_RE.search(cue.text)) or gap >= pause_s
        if span >= min_s and natural_break:
            flush()
    flush()

    # Merge a very short trailing block into its predecessor.
    if len(blocks) >= 2 and blocks[-1].end_s - blocks[-1].start_s < tail_merge_s:
        last = blocks.pop()
        prev = blocks[-1]
        blocks[-1] = prev.model_copy(update={"end_s": last.end_s, "text": f"{prev.text} {last.text}"})
    return blocks


_MID_SENTENCE_START_RE = re.compile(r"[.!?…][\"')\]]*\s+[A-Z]")


@dataclass
class Utterance:
    """A sentence-sized run of cues. Its times are real cue times (captions carry no
    word timings), which makes it the finest boundary Curio can place honestly."""

    index: int
    start_s: float
    end_s: float
    text: str


def build_utterances(cues: list[TranscriptCue], *, max_s: float = 12.0, pause_s: float = 1.0) -> list[Utterance]:
    """Group clean cues into sentence-sized units.

    A new unit starts at a cue when the previous cue ended a sentence, after a pause,
    after a speaker change, or when the unit grows past `max_s` (auto-captions have
    no punctuation). A cue in which a new sentence begins mid-cue also starts a new
    unit: starting a few words early is better than cutting off a topic's first words.
    """
    clean = clean_cues(cues)
    units: list[Utterance] = []
    cur: list[CleanCue] = []

    def flush() -> None:
        if cur:
            units.append(
                Utterance(len(units), round(cur[0].start_s, 3), round(cur[-1].end_s, 3), " ".join(c.text for c in cur))
            )
            cur.clear()

    for cue in clean:
        if cur:
            prev = cur[-1]
            if (
                _SENTENCE_END_RE.search(prev.text)
                or cue.start_s - prev.end_s >= pause_s
                or cue.speaker_change
                or cue.end_s - cur[0].start_s > max_s
                or _MID_SENTENCE_START_RE.search(cue.text)
            ):
                flush()
        cur.append(cue)
    flush()
    return units


def is_unpunctuated(cues: list[TranscriptCue], *, min_marks_per_100_words: float = 5.0) -> bool:
    """True when sentence ends aren't reliably marked.

    Measured (2026-11-23): human captions ≈ 7 marks per 100 words (14-15-word
    sentences); YouTube auto-captions 0-3.8 (none at all, or 26-word "sentences"
    where most sentence ends are missing).
    """
    words = marks = 0
    for c in cues:
        words += len(c.text.split())
        marks += len(re.findall(r"[.!?…](?=\s|$)", c.text))
    return words > 0 and 100 * marks / words < min_marks_per_100_words


def boundary_units(cues: list[TranscriptCue]) -> list[Utterance]:
    """Units for placing boundaries: sentences, or single cues when captions have no
    punctuation (a 12 s chunk of unpunctuated text rarely starts where a topic does)."""
    return build_utterances(cues, max_s=0.0 if is_unpunctuated(cues) else 12.0)


def approx_tokens(text: str) -> int:
    """Cheap token estimate (~1.35 tokens per English word)."""
    return int(len(text.split()) * 1.35) + 1


def render_blocks(blocks: list[Block]) -> str:
    """Prompt representation: one line per block, `[n | m:ss] text`."""
    from curio_learn.domain import fmt_ts

    return "\n".join(f"[{b.index} | {fmt_ts(b.start_s)}] {b.text}" for b in blocks)
