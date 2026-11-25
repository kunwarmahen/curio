from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from curio_learn.analysis.curriculum import (
    Placed,
    Source,
    concept_tokens,
    matches,
    order_sections,
    order_sources,
    outside_needs,
    source_edges,
)
from curio_learn.api.app import create_app
from curio_learn.domain import AnalysisStatus, CurriculumSource
from curio_learn.errors import AnalysisError
from curio_learn.service import CurioService

from .conftest import FakeLLM, FakeMetadata, FakeTranscripts
from .test_course import VIDS, FakePlaylists, analyze
from .test_playlist import scored


def sec(i, *, teaches=(), needs=(), start=None):
    s = scored(i, 80)
    s = s.model_copy(
        update={
            "evaluation": s.evaluation.model_copy(
                update={"introduces_concepts": list(teaches), "prerequisites": list(needs)}
            )
        }
    )
    if start is not None:
        s = s.model_copy(update={"section": s.section.model_copy(update={"start_s": start, "end_s": start + 60})})
    return s


def src(key, rank, sections, group=""):
    return Source(key=key, analysis=SimpleNamespace(sections=sections), rank=rank, group=group)


def test_matching_needs_to_concepts():
    assert matches(concept_tokens("Basic neural network concepts"), concept_tokens("Neural network as a function"))
    assert not matches(concept_tokens("Basic vector search"), concept_tokens("Vectors"))
    assert not matches(concept_tokens("Understanding of models"), concept_tokens("Model"))  # too broad
    assert matches(concept_tokens("Familiarity with RAG pipelines"), concept_tokens("RAG pipeline"))


def test_a_foundation_video_goes_before_the_lecture_that_needs_it_and_courses_keep_their_order():
    l1 = src("l1", 0, [sec(0, teaches=["agent loop"])], group="course")
    l2 = src("l2", 1, [sec(0, needs=["Basic neural networks", "Backpropagation basics"])], group="course")
    l3 = src("l3", 2, [sec(0, teaches=["memory"], needs=["agent loop"])], group="course")  # l1 teaches it: no need
    nn = src("nn", 3, [sec(0, teaches=["Neural networks"]), sec(1, teaches=["Backpropagation"])])
    sleep = src("sleep", 4, [sec(0, teaches=["Sleep pressure"])])
    sources = [l1, l2, l3, nn, sleep]
    assert outside_needs(sources)["l3"] == []
    edges = source_edges(sources)
    assert [(e.before, e.after) for e in edges] == [("nn", "l2")]
    order, kept = order_sources(sources, edges)
    assert order == ["l1", "nn", "l2", "l3", "sleep"]
    assert kept[0].examples == ["Basic neural networks", "Backpropagation basics"]
    # Lectures of one course never reorder, whatever they need from each other.
    late = src("l0", 0, [sec(0, needs=["Agent memory", "Memory design"])], group="course")
    teach = src("t", 1, [sec(0, teaches=["Agent memory", "Memory design"])], group="course")
    assert order_sources([late, teach], source_edges([late, teach]))[0] == ["l0", "t"]


def test_section_by_section_moves_a_teacher_ahead_and_keeps_time_order():
    a = src("a", 0, [sec(0, start=0), sec(1, needs=["Gradient descent", "Loss landscapes"], start=100)], group="c")
    b = src("b", 1, [sec(0, teaches=["Gradient descent", "Loss landscape"], start=0), sec(1, start=100)])
    placed = [
        Placed("A0", "a", "S00", 0),
        Placed("A1", "a", "S01", 100),
        Placed("B0", "b", "S00", 0),
        Placed("B1", "b", "S01", 100),
    ]
    assert order_sections(placed, [a, b], ["a", "b"]) == ["A0", "B0", "A1", "B1"]


@pytest.fixture
def service(settings):
    return CurioService(
        settings, llm=FakeLLM(), metadata=FakeMetadata(), transcripts=FakeTranscripts(), playlists=FakePlaylists()
    )


def _library(service):
    a, b, c = (analyze(service, v) for v in VIDS)
    course_id = service.group_course("Agents course", [a, b])
    return course_id, a, b, c


def test_create_order_and_build_a_curriculum(service):
    course_id, a, b, c = _library(service)
    cands = service.curriculum_candidates()
    assert {(x.kind, x.id) for x in cands} == {("course", course_id), ("video", c)}
    with pytest.raises(AnalysisError):
        service.create_curriculum("", [CurriculumSource(kind="video", id=c)])
    with pytest.raises(AnalysisError):
        service.create_curriculum("Agents", [CurriculumSource(kind="video", id=c)])  # one video only
    cid = service.create_curriculum(
        "Build agents", [CurriculumSource(kind="course", id=course_id), CurriculumSource(kind="video", id=c)]
    )
    course = service.course(cid)
    assert course.curriculum.goal == "Build agents" and course.curriculum.order == "lectures"
    assert [lec.video_id for lec in course.lectures] == VIDS  # nothing needs anything: the given order
    assert cid not in {x.id for x in service.curriculum_candidates()}  # curricula aren't candidates

    plan = service.course_plan(cid, minutes=10)
    assert plan.status is AnalysisStatus.DONE and plan.order == "lectures"
    # Section by section: a new plan (the order is part of its key), parts in that order.
    course = service.set_curriculum(cid, order="sections")
    assert course.curriculum.order == "sections"
    plan2 = service.course_plan(cid, minutes=10)
    assert plan2.id != plan.id and plan2.order == "sections" and plan2.parts
    # Reordering by hand turns Curio's order off; turning it on again reorders.
    course = service.reorder_course(cid, list(reversed(VIDS)))
    assert course.curriculum.auto_order is False
    service.submit_course_plan(cid, 10)
    assert [lec.video_id for lec in service.course(cid).lectures] == list(reversed(VIDS))
    course = service.set_curriculum(cid, auto_order=True)
    assert course.curriculum.auto_order is True
    with pytest.raises(AnalysisError):
        service.set_curriculum(cid, order="random")
    with pytest.raises(KeyError):
        service.set_curriculum(course_id, order="sections")  # a plain course


def test_suggesting_sources_for_a_goal(service):
    course_id, a, b, c = _library(service)
    service.repo.update_course(course_id, title="AI agents course")
    picks = service.suggest_curriculum("Build agents")
    assert picks[0].id == course_id and picks[0].suggested and picks[0].reason
    assert not next(p for p in picks if p.id == c).suggested
    with pytest.raises(AnalysisError):
        service.suggest_curriculum("  ")


def test_watched_anywhere_counts_for_a_course(service):
    course_id, a, b, c = _library(service)
    analysis = service.get(a)
    seg = analysis.playlist.segments[0]
    service.set_completed(a, seg.id, True)  # watched in the video's own playlist
    watched = service._watched_sections(service.course(course_id))
    assert {(a, sid) for sid in seg.section_ids} <= watched


def test_api_curriculum_flow(service):
    course_id, a, b, c = _library(service)
    with TestClient(create_app(service)) as client:
        assert len(client.get("/api/v1/curricula/candidates").json()) == 2
        r = client.post("/api/v1/curricula/suggest", json={"goal": "agents"})
        assert r.status_code == 200 and "suggested" in r.json()[0]
        r = client.post(
            "/api/v1/curricula",
            json={
                "goal": "agents",
                "sources": [{"kind": "course", "id": course_id}, {"kind": "video", "id": c}],
                "order": "sections",
            },
        )
        assert r.status_code == 201
        cid = r.json()["course_id"]
        got = client.get(f"/api/v1/courses/{cid}").json()
        assert got["curriculum"]["order"] == "sections"
        r = client.put(f"/api/v1/courses/{cid}/curriculum", json={"order": "lectures"})
        assert r.status_code == 200 and r.json()["curriculum"]["order"] == "lectures"
        assert client.put(f"/api/v1/courses/{course_id}/curriculum", json={"order": "lectures"}).status_code == 404


def test_section_by_section_keeps_a_courses_lectures_in_order():
    # v (a video, ranked first) needs what l3 teaches: l3 comes ahead of v, and l2 with it.
    l2 = src("l2", 1, [sec(0, start=0)], group="c")
    l3 = src("l3", 2, [sec(0, teaches=["Retrieval augmented generation", "Vector index"], start=0)], group="c")
    v = src("v", 0, [sec(0, needs=["Retrieval augmented generation", "Vector index"], start=0)])
    placed = [Placed("V0", "v", "S00", 0), Placed("L20", "l2", "S00", 0), Placed("L30", "l3", "S00", 0)]
    assert order_sections(placed, [v, l2, l3], ["v", "l2", "l3"]) == ["L20", "L30", "V0"]


def test_the_queue_rebuilds_a_curriculum_like_a_course(service):
    course_id, a, b, c = _library(service)
    cid = service.create_curriculum(
        "Build agents", [CurriculumSource(kind="course", id=course_id), CurriculumSource(kind="video", id=c)]
    )
    built = service.rebuild_courses_after(c)  # the video finished: every course it's in is rebuilt
    plans = {service.repo.get_course_plan(p).course_id for p in built}
    assert cid in plans


def test_a_curriculum_queues_only_lectures_never_analyzed(service):
    from curio_learn.domain import LearningPreferences, LectureState

    a, b = analyze(service, VIDS[0]), analyze(service, VIDS[1])
    other = analyze(service, VIDS[2], LearningPreferences())  # another goal: used as it is
    course_id = service.group_course("Agents course", [a, b])
    data = service.repo.get_course(course_id)
    new = {"video_id": "ddddddddddd", "title": "Lecture 3", "duration_s": 600}
    service.repo.update_course(course_id, lectures=[*data["lectures"], new])  # a lecture never analyzed
    cid = service.create_curriculum(
        "Build agents", [CurriculumSource(kind="course", id=course_id), CurriculumSource(kind="video", id=other)]
    )
    assert service.curriculum_missing(cid) == ["ddddddddddd"]
    service.queue_course(cid)
    states = {lec.video_id: lec.state for lec in service.course(cid).lectures}
    assert states["ddddddddddd"] is LectureState.QUEUED
    assert states[VIDS[2]] is LectureState.OTHER_GOAL  # not queued again for this goal
    assert service.curriculum_missing(cid) == []


def test_your_own_playlist_follows_a_section_by_section_curriculum(service, monkeypatch):
    import curio_learn.analysis.course as course_mod

    course_id, a, b, c = _library(service)
    cid = service.create_curriculum(
        "Build agents",
        [CurriculumSource(kind="course", id=course_id), CurriculumSource(kind="video", id=c)],
        order="sections",
    )
    # Stand in for a real reorder: the curriculum's section order reversed.
    monkeypatch.setattr(course_mod, "order_sections", lambda placed, sources, order: [p.key for p in placed][::-1])
    plan = service.course_plan(cid, minutes=10)
    assert plan.order == "sections"
    mine = [f"{p.analysis_id}:{sid}" for p in plan.parts for sid in p.section_ids]
    course = service.set_course_playlist(cid, mine[:-1])  # Curio's picks minus one: the learner's own
    lectures = [p.lecture for p in course.custom_parts]
    assert lectures == sorted(lectures, reverse=True)  # the curriculum's order, not lecture order
