from curio_learn.domain import TranscriptCue, fmt_ts
from curio_learn.processing.normalize import build_blocks, clean_cues, clean_text, render_blocks

from .conftest import make_cues


def cue(start, dur, text):
    return TranscriptCue(start_s=start, duration_s=dur, text=text)


def test_clean_text_removes_annotations_and_speaker_markers():
    text, speaker = clean_text(">> [Music] Hello&#39;s\nworld (applause) ")
    assert text == "Hello's world"
    assert speaker is True


def test_clean_cues_clips_overlaps_and_rolling_duplicates():
    cues = [cue(0, 5, "so today we talk about"), cue(3, 5, "we talk about gradient descent"), cue(6, 2, "[Music]")]
    out = clean_cues(cues)
    assert [c.text for c in out] == ["so today we talk about", "gradient descent"]
    assert out[0].end_s == 3  # clipped to the next cue's start


def test_clean_cues_merges_exact_duplicates():
    out = clean_cues([cue(0, 2, "hello there."), cue(2, 2, "hello there.")])
    assert len(out) == 1 and out[0].end_s == 4


def test_blocks_preserve_original_timestamps_and_cover_everything():
    cues = make_cues(200, step=4.0, start=5.0)
    blocks = build_blocks(cues)
    assert blocks[0].start_s == 5.0
    assert blocks[-1].end_s == 5.0 + 200 * 4.0
    starts = {c.start_s for c in cues}
    for prev, nxt in zip(blocks, blocks[1:], strict=False):
        assert nxt.start_s in starts  # boundaries are real cue starts
        assert nxt.start_s >= prev.end_s
    assert [b.index for b in blocks] == list(range(len(blocks)))
    words = sum(len(c.text.split()) for c in cues)
    assert sum(len(b.text.split()) for b in blocks) == words


def test_block_lengths_respect_bounds():
    blocks = build_blocks(make_cues(300, step=4.0), min_s=20, max_s=45)
    for b in blocks[:-1]:
        assert 20 <= b.end_s - b.start_s <= 45 + 4


def test_blocks_prefer_sentence_ends():
    blocks = build_blocks(make_cues(120, step=4.0))
    assert all(b.text.endswith(".") for b in blocks[:-1])


def test_unpunctuated_auto_captions_split_at_max():
    cues = [cue(i * 3.0, 3.0, f"word{i} more words") for i in range(100)]
    blocks = build_blocks(cues, min_s=20, max_s=45)
    assert len(blocks) >= 6
    assert all(b.end_s - b.start_s <= 48 for b in blocks)


def test_short_tail_is_merged():
    cues = make_cues(30, step=4.0) + [cue(120.5, 2.0, "bye.")]
    blocks = build_blocks(cues)
    assert blocks[-1].text.endswith("bye.")
    assert blocks[-1].end_s - blocks[-1].start_s > 8


def test_render_blocks_format():
    blocks = build_blocks(make_cues(30))
    line = render_blocks(blocks).splitlines()[1]
    assert line.startswith(f"[1 | {fmt_ts(blocks[1].start_s)}] ")


def test_fmt_ts():
    assert fmt_ts(0) == "0:00"
    assert fmt_ts(272.4) == "4:32"
    assert fmt_ts(3725) == "1:02:05"


def test_repeated_cues_keep_their_time():
    # Regression: a cue repeating the previous one used to vanish with its seconds.
    cues = [
        cue(0, 4, "Try Acme today."),
        cue(4, 4, "Try Acme today."),
        cue(8, 4, "Try Acme today."),
        cue(12, 4, "Back to the topic."),
    ]
    out = clean_cues(cues)
    assert [(c.text, c.start_s, c.end_s) for c in out] == [("Try Acme today.", 0, 12), ("Back to the topic.", 12, 16)]
