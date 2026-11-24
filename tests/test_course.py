import pytest
from fastapi.testclient import TestClient

from curio_learn.analysis.playlist import PlaylistConfig, select_sections
from curio_learn.api.app import _queue_course_safely, _run_course_plan_safely, create_app
from curio_learn.domain import AnalysisStatus, LearningPreferences, LearningProfile, LectureState
from curio_learn.errors import AnalysisError, InvalidURL
from curio_learn.ingestion.playlist import (
    PlaylistEntry,
    PlaylistInfo,
    course_order,
    extract_playlist_id,
    is_playlist_link,
    lecture_number,
)
from curio_learn.service import CurioService

from .conftest import FakeLLM, FakeMetadata, FakeTranscripts

VIDS = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
PLAYLIST = "https://www.youtube.com/playlist?list=PLtestcourse01"
TECH = LearningPreferences(profile=LearningProfile.TECHNICAL, custom_objective="Know what I missed")


class FakePlaylists:
    def get_playlist(self, playlist_id):
        entries = [PlaylistEntry(video_id=v, title=f"Lecture {n}", duration_s=1200) for n, v in enumerate(VIDS, 1)]
        return PlaylistInfo(playlist_id=playlist_id, title="Test Course", channel="Uni", entries=entries, unavailable=1)


@pytest.fixture
def service(settings):
    return CurioService(
        settings, llm=FakeLLM(), metadata=FakeMetadata(), transcripts=FakeTranscripts(), playlists=FakePlaylists()
    )


def analyze(service, vid, prefs=TECH):
    aid, _ = service.submit(f"https://youtu.be/{vid}", prefs)
    service.run(aid)
    return aid


def test_playlist_links_and_order():
    assert extract_playlist_id(PLAYLIST) == "PLtestcourse01"
    assert extract_playlist_id("https://www.youtube.com/watch?v=aaaaaaaaaaa&list=PLtestcourse01") == "PLtestcourse01"
    assert extract_playlist_id("https://www.youtube.com/watch?v=aaaaaaaaaaa&list=RDaaaaaaaaaaa") is None  # a mix
    assert extract_playlist_id("https://example.com/playlist?list=PLtestcourse01") is None
    assert is_playlist_link(PLAYLIST) and not is_playlist_link("youtube.com/watch?v=aaaaaaaaaaa&list=PLtestcourse01")
    assert lecture_number("RAG Explained | Stanford CS329Z Lecture 3 with Diyi Yang") == 3
    newest_first = [
        PlaylistEntry(video_id=str(n), title=t)
        for n, t in enumerate(["Guest talk", "Lecture 3", "Lecture 2", "Lecture 1"])
    ]
    assert [e.title for e in course_order(newest_first)] == ["Lecture 1", "Lecture 2", "Lecture 3", "Guest talk"]
    in_order = list(reversed(newest_first))
    assert course_order(in_order) == in_order


def test_selector_excludes_repeats():
    from .test_playlist import scored

    secs = [scored(1, 90, dur=100), scored(2, 80, dur=100), scored(3, 70, dur=100)]
    cfg = PlaylistConfig(lead_in_s=0, tail_s=0)
    assert select_sections(secs, 1000, cfg) == {"S01", "S02", "S03"}
    assert select_sections(secs, 1000, cfg, excludes={"S01": {"S02"}, "S02": {"S01"}}) == {"S01", "S03"}


def test_import_queues_missing_lectures_and_reports_states(service):
    done = analyze(service, VIDS[0])
    other = analyze(service, VIDS[1], LearningPreferences())  # analyzed for another goal
    course_id = service.import_course(PLAYLIST, TECH)
    assert service.course(course_id).importing
    service.queue_course(course_id)
    course = service.course(course_id)
    assert not course.importing and course.unavailable == 1 and course.title == "Test Course"
    states = [(lec.state, lec.analysis_id) for lec in course.lectures]
    assert states[0] == (LectureState.READY, done)
    assert states[1] == (LectureState.OTHER_GOAL, other) and course.lectures[1].pending_id
    assert states[2][0] is LectureState.QUEUED and course.lectures[2].pending_id
    # Importing the same playlist with the same goal reuses the course.
    assert service.import_course(PLAYLIST, TECH) == course_id
    with pytest.raises(InvalidURL):
        service.import_course("https://www.youtube.com/watch?v=aaaaaaaaaaa", TECH)


def test_course_plan_skips_repeats_and_cites_real_sections(service):
    a, b = analyze(service, VIDS[0]), analyze(service, VIDS[1])
    course_id = service.group_course("", [b, a])
    course = service.course(course_id)
    assert {lec.analysis_id for lec in course.lectures} == {a, b}
    assert all(lec.state is LectureState.READY for lec in course.lectures)
    with pytest.raises(AnalysisError):
        service.group_course("", [a])

    plan = service.course_plan(course_id, minutes=10)
    assert plan.status is AnalysisStatus.DONE and plan.parts
    assert plan.total_s <= plan.budget_s * 1.1 + 10
    assert plan.analysis_ids == [lec.analysis_id for lec in course.lectures]
    # The fake judges the closest pair a repeat: only one half is in the playlist.
    assert len(plan.repeats) == 1
    rep = plan.repeats[0]
    chosen = {(p.video_id, sid) for p in plan.parts for sid in p.section_ids}
    assert (rep.covered_by.video_id, rep.covered_by.section_id) in chosen
    assert (rep.section.video_id, rep.section.section_id) not in chosen
    # Parts play in lecture order; keys are stable.
    assert [p.lecture for p in plan.parts] == sorted(p.lecture for p in plan.parts)
    assert all(p.key == f"{p.video_id}@{int(p.start_s)}" for p in plan.parts)
    # One checklist point cited a section that doesn't exist and was dropped.
    assert len(plan.checklist) == 1 and plan.checklist[0].refs
    assert any("Dropped 1 checklist point" in w for w in plan.warnings)
    # Reused until the inputs change.
    assert service.submit_course_plan(course_id, 10) == (plan.id, False)
    assert not service.course(course_id).plan_stale


def test_plan_goes_stale_when_a_lecture_finishes(service):
    a, b = analyze(service, VIDS[0]), analyze(service, VIDS[1])
    course_id = service.import_course(PLAYLIST, TECH)
    service.queue_course(course_id)
    service.course_plan(course_id, minutes=10)
    assert not service.course(course_id).plan_stale
    third = service.course(course_id).lectures[2].pending_id
    service.run(third)
    course = service.course(course_id)
    assert course.plan_stale and course.lectures[2].state is LectureState.READY
    rebuilt = service.course_plan(course_id, minutes=10)
    assert rebuilt.analysis_ids == [a, b, third]


def test_api_course_flow(service):
    analyze(service, VIDS[0])
    analyze(service, VIDS[1])
    with TestClient(create_app(service)) as client:
        r = client.post("/api/v1/courses", json={"url": PLAYLIST, "preferences": TECH.model_dump()})
        assert r.status_code == 202
        course_id = r.json()["course_id"]
        _queue_course_safely(service, course_id)  # the import thread may not have finished
        body = client.get(f"/api/v1/courses/{course_id}").json()
        assert [lec["state"] for lec in body["lectures"]] == ["ready", "ready", "queued"]
        assert body["url"] == "https://www.youtube.com/playlist?list=PLtestcourse01"

        r = client.post(f"/api/v1/courses/{course_id}/plan", json={"minutes": 10})
        assert r.status_code == 202
        _run_course_plan_safely(service, r.json()["plan_id"])
        plan = client.get(f"/api/v1/courses/{course_id}").json()["plan"]
        assert plan["status"] == "done" and plan["parts"]
        key = plan["parts"][0]["key"]
        assert (
            client.put(f"/api/v1/courses/{course_id}/watched", json={"part_key": key, "watched": True}).status_code
            == 200
        )
        assert client.get(f"/api/v1/courses/{course_id}/watched").json() == [key]
        assert len(client.get("/api/v1/courses").json()) == 1
        assert client.post("/api/v1/courses", json={"url": "https://youtu.be/aaaaaaaaaaa"}).status_code == 422
        assert client.delete(f"/api/v1/courses/{course_id}").status_code == 200
        assert client.get(f"/api/v1/courses/{course_id}").status_code == 404
