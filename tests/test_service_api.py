import time

import pytest
from fastapi.testclient import TestClient

from curio_learn.api.app import create_app
from curio_learn.domain import AnalysisStatus, Importance, LearningPreferences
from curio_learn.errors import LLMError
from curio_learn.service import CurioService

from .conftest import FakeLLM, FakeMetadata, FakeTranscripts

URL = "https://www.youtube.com/watch?v=e_m99aR7_3Q"


@pytest.fixture
def service(settings):
    return CurioService(
        settings, llm=FakeLLM(kinds={0: "intro", 3: "sponsor"}), metadata=FakeMetadata(), transcripts=FakeTranscripts()
    )


def test_end_to_end_analysis(service):
    progress = []
    result = service.analyze(URL, progress=progress.append)
    assert result.status is AnalysisStatus.DONE
    assert result.validation.ok, result.validation.warnings
    assert result.overview and result.sections and result.playlist
    p = result.playlist
    assert p.segments and p.total_s <= p.budget_s * 1.1
    assert p.time_saved_s > 0
    # The sponsor section is always skipped, with a reason.
    sponsor = next(s for s in result.sections if s.section.kind == "sponsor")
    assert sponsor.importance is Importance.SKIP
    assert any(s.section_id == sponsor.section.id and s.reason for s in p.skipped)
    # Every timestamp comes from the transcript.
    cue_starts = {c.start_s for c in service.transcripts.cues}
    for s in result.sections:
        assert s.section.start_s in cue_starts
    assert any("Evaluating" in m for m in progress)


def test_analysis_is_cached_and_persisted(service):
    first = service.analyze(URL)
    llm_calls = len(service.llm.calls)
    second = service.analyze(URL)
    assert second.id == first.id and len(service.llm.calls) == llm_calls
    assert service.transcripts.calls == 1
    assert service.get(first.id).playlist == first.playlist
    assert [r.analysis_id for r in service.recent()] == [first.id]

    other = service.analyze(URL, LearningPreferences(profile="technical"))
    assert other.id != first.id
    forced = service.analyze(URL, force=True)
    assert forced.id != first.id


def test_failed_analysis_records_error(settings):
    svc = CurioService(settings, llm=FakeLLM(invalid_first=100), metadata=FakeMetadata(), transcripts=FakeTranscripts())
    analysis_id, _ = svc.submit(URL, LearningPreferences())
    with pytest.raises(LLMError):
        svc.run(analysis_id)
    failed = svc.get(analysis_id)
    assert failed.status is AnalysisStatus.FAILED and "outline" in failed.error


def test_segment_completion(service):
    result = service.analyze(URL)
    seg = result.playlist.segments[0].id
    service.set_completed(result.id, seg, True)
    assert service.completed(result.id) == {seg}


def test_api_flow(service):
    with TestClient(create_app(service)) as client:
        assert client.get("/api/v1/health").json()["status"] == "ok"
        r = client.post("/api/v1/analyses", json={"url": URL})
        assert r.status_code == 202
        analysis_id = r.json()["analysis_id"]
        for _ in range(100):
            body = client.get(f"/api/v1/analyses/{analysis_id}").json()
            if body["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert body["status"] == "done", body.get("error")
        assert body["playlist"]["segments"][0]["timestamp"]
        assert client.get("/api/v1/videos/recent").json()[0]["analysis_id"] == analysis_id
        assert len(client.get("/api/v1/videos/e_m99aR7_3Q/transcript").json()) > 10
        r = client.put(f"/api/v1/analyses/{analysis_id}/segments/P01/completed", json={"completed": True})
        assert r.status_code == 200
        assert client.get(f"/api/v1/analyses/{analysis_id}/completed").json() == ["P01"]
        recent = client.get("/api/v1/videos/recent").json()[0]
        assert recent["playlist_s"] == body["playlist"]["total_s"]
        stretches = recent["watch_map"]
        assert len(stretches) == len(body["sections"])
        assert any(s["in_playlist"] for s in stretches)
        assert {s["importance"] for s in stretches} <= {"essential", "recommended", "optional", "skip"}


def test_api_errors(service):
    with TestClient(create_app(service)) as client:
        r = client.post("/api/v1/analyses", json={"url": "https://vimeo.com/1"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_url"
        assert client.get("/api/v1/analyses/nope").status_code == 404


def test_stale_in_progress_analysis_is_not_reused(service):
    from datetime import UTC, datetime, timedelta

    from curio_learn.storage.orm import AnalysisRow

    prefs = LearningPreferences()
    dead_id, needs_run = service.submit(URL, prefs)
    assert needs_run
    service.repo.update_status(dead_id, AnalysisStatus.RUNNING, "Mapping topics")
    # A live run is reused by the async API...
    assert service.submit(URL, prefs) == (dead_id, False)
    # ...but one whose process died long ago is not.
    with service.repo.session() as db, db.begin():
        db.get(AnalysisRow, dead_id).updated_at = datetime.now(UTC) - timedelta(hours=1)
    new_id, needs_run = service.submit(URL, prefs)
    assert needs_run and new_id != dead_id


def test_sync_analyze_never_returns_unfinished(service):
    prefs = LearningPreferences()
    busy_id, _ = service.submit(URL, prefs)
    service.repo.update_status(busy_id, AnalysisStatus.RUNNING, "elsewhere")
    result = service.analyze(URL, prefs)
    assert result.status is AnalysisStatus.DONE and result.id != busy_id


def test_startup_cleanup_spares_live_runs_in_other_processes(service):
    from datetime import UTC, datetime, timedelta

    from curio_learn.storage.orm import AnalysisRow

    live_id, _ = service.submit(URL, LearningPreferences())
    dead_id, _ = service.submit(URL, LearningPreferences(profile="technical"))
    for aid in (live_id, dead_id):
        service.repo.update_status(aid, AnalysisStatus.RUNNING, "Mapping topics")
    with service.repo.session() as db, db.begin():
        db.get(AnalysisRow, dead_id).updated_at = datetime.now(UTC) - timedelta(hours=1)
    assert service.repo.mark_stale_running_failed() == 1
    assert service.get(live_id).status is AnalysisStatus.RUNNING
    assert service.get(dead_id).status is AnalysisStatus.FAILED


def test_mid_roll_ad_is_cut_out_and_never_in_the_playlist(settings):
    from curio_learn.domain import SectionKind, TranscriptCue

    from .conftest import make_cues

    cues = make_cues(300)
    # A 20 s ad read in the middle of the third topic.
    ad = [
        "Quick word from our sponsor.",
        "Acme makes deployment easy.",
        "Teams ship twice as fast.",
        "Start free at acme.com.",
        "That's acme.com.",
    ]
    for n, i in enumerate(range(150, 155)):
        cues[i] = TranscriptCue(start_s=cues[i].start_s, duration_s=4.0, text=ad[n])
    svc = CurioService(settings, llm=FakeLLM(), metadata=FakeMetadata(), transcripts=FakeTranscripts(cues))
    result = svc.analyze(URL, LearningPreferences(target_minutes=19))
    ads = [s for s in result.sections if s.section.kind == SectionKind.SPONSOR]
    assert len(ads) == 1
    ad = ads[0].section
    assert (ad.start_s, ad.end_s) == (600.0, 620.0)
    assert ads[0].importance is Importance.SKIP
    assert result.validation.ok, result.validation.warnings
    for seg in result.playlist.segments:
        assert seg.end_s <= ad.start_s + 1 or seg.start_s >= ad.end_s - 2, seg.timestamp
