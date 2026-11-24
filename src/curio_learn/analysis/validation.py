"""Step 10: quality validation of an analysis. Repairs what it safely can."""

from __future__ import annotations

from curio_learn.domain import Block, LearningSegment, Playlist, Section, ValidationReport, fmt_ts


def validate(
    *,
    duration_s: float,
    blocks: list[Block],
    sections: list[Section],
    playlist: Playlist,
    others: list[LearningSegment],
    cue_starts: set[float] | None = None,
    cue_ends: set[float] | None = None,
) -> ValidationReport:
    report = ValidationReport()
    warn = report.warnings.append

    # Transcript coverage of the video.
    if blocks:
        covered = blocks[-1].end_s / duration_s if duration_s else 0
        if covered < 0.85:
            warn(
                f"The transcript ends at {fmt_ts(blocks[-1].end_s)} of a "
                f"{fmt_ts(duration_s)} video; later content can't be analyzed."
            )

    # Sections tile the transcript in time: ordered, no overlaps, no real gaps.
    # (Times, not block numbers: a sponsor read can be cut out of the middle of a block.)
    if sections and blocks:
        if sections[0].start_s > blocks[0].start_s + 0.01 or sections[-1].end_s < blocks[-1].end_s - 0.01:
            warn("Sections don't cover the whole transcript.")
        for a, b in zip(sections, sections[1:], strict=False):
            if a.end_s > b.start_s + 0.01:
                warn(f"{a.id} ends after {b.id} starts ({fmt_ts(a.end_s)} > {fmt_ts(b.start_s)}).")
            elif b.start_s - a.end_s > 5.0 and any(
                blk.start_s < b.start_s - 1 and blk.end_s > a.end_s + 1 for blk in blocks
            ):
                # Silence or music between sections is fine; dropped speech is not.
                warn(f"{b.start_s - a.end_s:.0f}s of transcript between {a.id} and {b.id} is in no section.")

    # Every segment boundary must be a real caption time. Either edge of a cue counts:
    # next to a sponsor read that was cut out, content ends where the ad's first cue
    # starts and resumes where its last cue ends.
    starts = {round(t, 2) for t in cue_starts} if cue_starts else {round(b.start_s, 2) for b in blocks}
    ends = {round(t, 2) for t in cue_ends} if cue_ends else {round(b.end_s, 2) for b in blocks}
    starts = ends = starts | ends
    by_id = {s.id: s for s in sections}
    for seg in playlist.segments + others:
        if not (0 <= seg.start_s < seg.end_s <= duration_s + 5):
            warn(f"{seg.id} has invalid timestamps {seg.start_s}–{seg.end_s}.")
        first, last = by_id.get(seg.section_ids[0]), by_id.get(seg.section_ids[-1])
        if not first or not last:
            warn(f"{seg.id} refers to unknown sections {seg.section_ids}.")
            continue
        if round(first.start_s, 2) not in starts or round(last.end_s, 2) not in ends:
            warn(f"{seg.id} boundaries don't match caption times.")
        if not seg.what_you_learn:
            seg.what_you_learn = seg.summary
            report.repairs.append(f"Filled empty 'what you will learn' for {seg.id} from its summary.")
        if not seg.why_valuable:
            seg.why_valuable = f"Rated {seg.score:.0f}/100 for this learning objective."
            report.repairs.append(f"Filled empty 'why watch' for {seg.id}.")

    # Playlist segments are chronological and non-overlapping.
    segs = playlist.segments
    for a, b in zip(segs, segs[1:], strict=False):
        if b.start_s < a.end_s - 0.01:
            warn(f"Playlist segments {a.id} and {b.id} overlap.")
    if playlist.total_s > playlist.budget_s * 1.25:
        warn(
            f"Playlist ({fmt_ts(playlist.total_s)}) exceeds its budget ({fmt_ts(playlist.budget_s)}) by more than 25%."
        )
    if not segs:
        warn("No segment was selected for the playlist.")

    report.ok = not report.warnings
    return report
