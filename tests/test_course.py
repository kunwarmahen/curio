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
        titles = ["Lecture 1", "Lecture 2", "Benchmarks | Guest Lecture with A. Speaker"]
        entries = [PlaylistEntry(video_id=v, title=t, duration_s=1200) for v, t in zip(VIDS, titles, strict=True)]
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
    assert [lec.label for lec in course.lectures] == ["Lecture 1", "Lecture 2", "Guest lecture"]
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


def test_queue_rebuilds_the_course_after_its_last_lecture(service):
    from curio_learn.api.app import _run_queued
    from curio_learn.overnight import QueueWorker

    analyze(service, VIDS[0])
    analyze(service, VIDS[1], LearningPreferences())  # used meanwhile; queued again for the course goal
    course_id = service.import_course(PLAYLIST, TECH)
    service.queue_course(course_id)
    service.course_plan(course_id, minutes=10)
    worker = QueueWorker(service.repo, window=None, probe=None, active=lambda: False, start=lambda _: None)

    # One queued lecture finishes while another still waits: no rebuild yet.
    second = service.course(course_id).lectures[1].pending_id
    service.repo.claim_queued(second)
    _run_queued(service, second, worker)
    course = service.course(course_id)
    assert course.plan_stale and course.lectures[2].state is LectureState.QUEUED

    # The last one finishes: the course playlist is rebuilt with the same length.
    third = course.lectures[2].pending_id
    service.repo.claim_queued(third)
    _run_queued(service, third, worker)
    course = service.course(course_id)
    assert not course.plan_stale and course.plan.status is AnalysisStatus.DONE
    assert course.plan.analysis_ids == [lec.analysis_id for lec in course.lectures]
    assert course.plan.budget_s == 600 and second in course.plan.analysis_ids


def test_rebuild_is_skipped_when_the_gpu_is_busy(service):
    a, b = analyze(service, VIDS[0]), analyze(service, VIDS[1])
    course_id = service.group_course("", [a, b])
    assert service.rebuild_courses_after(b, gpu_busy=lambda: "The GPU is in use.") == []
    assert service.course(course_id).plan.status is AnalysisStatus.FAILED
    assert len(service.rebuild_courses_after(b)) == 1
    assert service.course(course_id).plan.status is AnalysisStatus.DONE


def test_ask_the_course_cites_moments_in_lectures(service):
    from curio_learn.analysis.course_ask import LectureText, course_passages, named_lectures

    a, b = analyze(service, VIDS[0]), analyze(service, VIDS[1])
    course_id = service.import_course(PLAYLIST, TECH)
    service.queue_course(course_id)
    service.settings.ask_context_tokens = 800  # force search over excerpts
    answer = service.ask_course(course_id, "What do the lectures say about idea0?")
    assert answer.in_course and answer.lectures == 2 and answer.context == "excerpts"
    cites = answer.parts[0].citations
    assert {c.analysis_id for c in cites} <= {a, b} and all(c.lecture in (1, 2) for c in cites)
    assert any("Ignored 1 citation" in w for w in answer.warnings)
    assert any("searched all 2 lectures" in w for w in answer.warnings)
    # Every timestamp is a real cue start.
    starts = {c.start_s for c in service.repo.get_transcript(VIDS[0]).cues}
    assert all(c.start_s in starts for c in cites)
    assert [x.id for x in service.course_conversation(course_id)] == [answer.id]

    course = service.course(course_id)
    texts = service._course_texts(course)
    assert [t.label for t in texts] == ["Lecture 1", "Lecture 2"]
    assert len(course_passages(texts)) == sum(len(t.blocks) for t in texts)
    assert named_lectures("What does lecture 2 say?", texts) == [texts[1]]
    guest = LectureText(3, "Guest lecture", texts[0].analysis, [], [])
    assert named_lectures("and the guest lecture?", [*texts, guest]) == [guest]
    assert service.clear_course_conversation(course_id) == 1


def test_api_ask_the_course(service):
    analyze(service, VIDS[0])
    course_id = service.import_course(PLAYLIST, TECH)
    service.queue_course(course_id)
    with TestClient(create_app(service)) as client:
        r = client.post(f"/api/v1/courses/{course_id}/questions", json={"question": "What is idea0?"})
        assert r.status_code == 200 and r.json()["parts"][0]["citations"][0]["video_id"] == VIDS[0]
        assert len(client.get(f"/api/v1/courses/{course_id}/questions").json()) == 1
        assert client.delete(f"/api/v1/courses/{course_id}/questions").json() == {"deleted": 1}
        assert client.post("/api/v1/courses/nope/questions", json={"question": "x"}).status_code == 404
