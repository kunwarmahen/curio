import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from curio_learn.analysis.notes import (
    DefinitionOut,
    PointOut,
    SectionNotesOut,
    TakeawayOut,
    scope_sections,
    section_units,
    to_section_notes,
    to_takeaways,
)
from curio_learn.api.app import create_app
from curio_learn.domain import AnalysisStatus, NotesScope, SectionKind, SectionNotes
from curio_learn.errors import AnalysisError
from curio_learn.processing.normalize import boundary_units
from curio_learn.service import CurioService
from curio_learn.storage.orm import NotesRow

from .conftest import FakeLLM, FakeMetadata, FakeTranscripts
from .test_ads import section

URL = "https://www.youtube.com/watch?v=e_m99aR7_3Q"


@pytest.fixture
def service(settings):
    return CurioService(
        settings, llm=FakeLLM(kinds={0: "intro", 3: "sponsor"}), metadata=FakeMetadata(), transcripts=FakeTranscripts()
    )


@pytest.fixture
def analysis(service):
    return service.analyze(URL)


def test_sentence_numbers_become_real_cue_times():
    sec = section(1, 100.0, 200.0)
    units = [u for u in boundary_units(FakeTranscripts().cues) if 0 <= u.start_s < 400]
    inside = section_units(sec, units)
    out = SectionNotesOut(
        section_id="S01",
        points=[PointOut(text="Later point.", sentence=3), PointOut(text="First point.", sentence=1)],
        explanation=" How it works. ",
        definitions=[DefinitionOut(term="Widget", definition="A thing.", sentence=99)],
    )
    notes, bad = to_section_notes(out, sec, inside)
    assert [p.text for p in notes.points] == ["First point.", "Later point."]  # sorted by time
    assert notes.points[0].start_s == inside[0].start_s and notes.points[1].start_s == inside[2].start_s
    assert notes.explanation == "How it works."
    # Sentence 99 doesn't exist: the definition links to the section start and is counted.
    assert bad == 1 and notes.definitions[0].start_s == 100.0


def test_takeaways_keep_only_sections_in_scope():
    notes = [
        SectionNotes(section_id="S02", title="b", start_s=50, end_s=90),
        SectionNotes(section_id="S05", title="e", start_s=300, end_s=400),
    ]
    items = [
        TakeawayOut(text="Both.", section_ids=["s05", "S02"]),
        TakeawayOut(text="Nowhere.", section_ids=["S09"]),
        TakeawayOut(text="  ", section_ids=["S02"]),
    ]
    out, dropped = to_takeaways(items, notes)
    assert [(t.text, t.section_ids, t.start_s) for t in out] == [("Both.", ["S02", "S05"], 50)]
    assert dropped == 1


def test_scopes(analysis):
    playlist_ids = {sid for seg in analysis.playlist.segments for sid in seg.section_ids}
    assert {s.id for s in scope_sections(analysis, NotesScope.PLAYLIST)} == playlist_ids

    video = scope_sections(analysis, NotesScope.VIDEO)
    assert all(s.kind is not SectionKind.SPONSOR for s in video)
    assert len(video) == len(analysis.sections) - 1
    assert [s.start_s for s in video] == sorted(s.start_s for s in video)

    other = next(seg for seg in analysis.segments if not seg.in_playlist)
    picked = scope_sections(analysis, NotesScope.SELECTED, [other.id])
    assert [s.id for s in picked] == other.section_ids

    with pytest.raises(AnalysisError, match="Unknown segment"):
        scope_sections(analysis, NotesScope.SELECTED, ["P99"])
    with pytest.raises(AnalysisError, match="nothing to take notes on"):
        scope_sections(analysis, NotesScope.SELECTED, [])


def test_notes_end_to_end(service, analysis):
    progress = []
    notes = service.notes(analysis.id, NotesScope.PLAYLIST, progress=progress.append)
    assert notes.status is AnalysisStatus.DONE
    assert notes.summary and notes.sections
    cue_starts = {c.start_s for c in service.transcripts.cues}
    for s in notes.sections:
        assert s.points
        for p in s.points:
            assert p.start_s in cue_starts and s.start_s <= p.start_s < s.end_s
    # The takeaway naming S99 was dropped; the other links to its earliest section.
    assert [t.text for t in notes.takeaways] == ["The first idea matters most."]
    assert notes.takeaways[0].start_s == notes.sections[0].start_s
    assert notes.recommendations and any("Dropped 1 takeaway" in w for w in notes.warnings)
    assert len(notes.glossary) == len(notes.sections)
    assert notes.watch_s == pytest.approx(sum(s.end_s - s.start_s for s in notes.sections))
    assert any("Writing notes" in m for m in progress) and any("takeaways" in m for m in progress)

    # Saved and reused; force writes new notes.
    calls = len(service.llm.calls)
    again = service.notes(analysis.id, NotesScope.PLAYLIST)
    assert again.id == notes.id and len(service.llm.calls) == calls
    assert service.notes(analysis.id, NotesScope.PLAYLIST, force=True).id != notes.id
    assert service.notes(analysis.id, NotesScope.VIDEO).id != notes.id
    assert len(service.list_notes(analysis.id)) == 3

    md = service.notes_markdown(notes.id)
    assert md.startswith("# Notes: Test Lecture")
    assert "## Key takeaways" in md and "## Definitions" in md and "## What to do next" in md
    assert f"{analysis.video.url}&t={int(notes.sections[0].points[0].start_s)}s" in md


def test_notes_need_a_finished_analysis(service):
    analysis_id, _ = service.submit(URL, analysis_prefs())
    with pytest.raises(AnalysisError, match="once the analysis has finished"):
        service.submit_notes(analysis_id)
    with pytest.raises(KeyError):
        service.submit_notes("nope")


def analysis_prefs():
    from curio_learn.domain import LearningPreferences

    return LearningPreferences(profile="beginner")


def test_stale_notes_are_marked_failed(service, analysis):
    notes_id, _ = service.submit_notes(analysis.id)
    with service.repo.session() as db, db.begin():
        db.get(NotesRow, notes_id).updated_at = datetime.now(UTC) - timedelta(hours=1)
    assert service.repo.mark_stale_running_failed() == 1
    assert service.get_notes(notes_id).status is AnalysisStatus.FAILED
    # Failed notes aren't reused.
    assert service.submit_notes(analysis.id)[0] != notes_id


def test_api_notes_flow(service, analysis):
    with TestClient(create_app(service)) as client:
        r = client.post(f"/api/v1/analyses/{analysis.id}/notes", json={"scope": "video"})
        assert r.status_code == 202
        notes_id = r.json()["notes_id"]
        for _ in range(100):
            body = client.get(f"/api/v1/notes/{notes_id}").json()
            if body["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert body["status"] == "done", body.get("error")
        assert body["glossary"] and body["watch_s"] > 0
        assert [n["id"] for n in client.get(f"/api/v1/analyses/{analysis.id}/notes").json()] == [notes_id]

        md = client.get(f"/api/v1/notes/{notes_id}/markdown")
        assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
        assert "attachment" in md.headers["content-disposition"]

        assert client.post("/api/v1/analyses/nope/notes", json={}).status_code == 404
        bad = client.post(f"/api/v1/analyses/{analysis.id}/notes", json={"scope": "selected", "segment_ids": ["X"]})
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "analysis_error"
        assert client.get("/api/v1/notes/nope").status_code == 404
        assert client.get("/api/v1/notes/nope/markdown").status_code == 404
