import pytest

from curio_learn.analysis.outline import OutlineSectionOut, _windows, build_sections, generate_outline
from curio_learn.domain import SectionKind, VideoMetadata
from curio_learn.processing.normalize import build_blocks

from .conftest import FakeLLM, make_cues


def raw(start, title="t", kind="core"):
    return OutlineSectionOut(start_block=start, title=title, summary="s", kind=kind, key_concepts=["c"])


@pytest.fixture
def blocks():
    return build_blocks(make_cues(300))  # ~40 blocks over 20 minutes


def assert_tiles(sections, blocks):
    assert sections[0].start_block == 0
    assert sections[-1].end_block == len(blocks) - 1
    for a, b in zip(sections, sections[1:], strict=False):
        assert b.start_block == a.end_block + 1
        assert b.start_s == blocks[b.start_block].start_s
        assert a.end_s == blocks[a.end_block].end_s


def test_sections_tile_transcript(blocks):
    sections, repairs = build_sections([raw(0), raw(10), raw(20)], blocks)
    assert len(sections) == 3 and not repairs
    assert [s.id for s in sections] == ["S01", "S02", "S03"]
    assert_tiles(sections, blocks)


def test_sections_repair_bad_llm_output(blocks):
    out = [raw(20), raw(5), raw(5, "dup"), raw(999, "bogus"), raw(-3, "neg")]
    sections, repairs = build_sections(out, blocks)
    assert_tiles(sections, blocks)
    assert len(sections) == 2
    assert any("out-of-range" in r for r in repairs)
    assert any("duplicate" in r for r in repairs)
    assert any("Moved first section" in r for r in repairs)


def test_short_sections_are_merged(blocks):
    # Block 10 alone is ~30s or less; with min_section_s=60 it must be merged.
    sections, repairs = build_sections([raw(0), raw(10), raw(11), raw(25)], blocks, min_section_s=60)
    assert_tiles(sections, blocks)
    assert all(s.duration_s >= 60 for s in sections)
    assert any("Merged" in r for r in repairs)


def test_empty_outline_raises(blocks):
    with pytest.raises(ValueError):
        build_sections([raw(999)], blocks)


def test_windows_cover_all_blocks(blocks):
    windows = _windows(blocks, window_tokens=300)
    assert windows[0][0] == 0 and windows[-1][1] == blocks[-1].index
    for (_, a_end), (b_start, _) in zip(windows, windows[1:], strict=False):
        assert b_start == a_end + 1


def test_windowed_outline_matches_single_pass_tiling(blocks):
    meta = VideoMetadata(video_id="x" * 11, title="T", duration_s=1200)
    llm = FakeLLM()
    outline, _ = generate_outline(llm, model="m", meta=meta, blocks=blocks, window_tokens=400)
    assert llm.calls.count("OutlineOut") > 1 and "OverviewOut" in llm.calls
    assert_tiles(outline.sections, blocks)
    assert outline.overview


def test_section_kind_parsed(blocks):
    sections, _ = build_sections([raw(0, kind="sponsor"), raw(10)], blocks)
    assert sections[0].kind is SectionKind.SPONSOR


def test_outline_prompt_anchors_the_section_count():
    from curio_learn.analysis.prompts import expected_sections, outline_user

    assert expected_sections(80) == (16, 32)
    assert expected_sections(10) == (2, 4)
    text = outline_user(
        title="T", channel="", description="", chapters="", transcript="x", first_block=0, last_block=9, minutes=80
    )
    assert "roughly 16-32 sections" in text
    assert "roughly" not in outline_user(
        title="T", channel="", description="", chapters="", transcript="x", first_block=0, last_block=9, minutes=2
    )
