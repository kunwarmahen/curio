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
    monkeypatch.setattr(
        course_mod, "order_sections", lambda placed, sources, order, similar=None: [p.key for p in placed][::-1]
    )
    plan = service.course_plan(cid, minutes=10)
    assert plan.order == "sections"
    mine = [f"{p.analysis_id}:{sid}" for p in plan.parts for sid in p.section_ids]
    course = service.set_course_playlist(cid, mine[:-1])  # Curio's picks minus one: the learner's own
    lectures = [p.lecture for p in course.custom_parts]
    assert lectures == sorted(lectures, reverse=True)  # the curriculum's order, not lecture order


def test_acronyms_the_library_writes_in_capitals():
    from curio_learn.analysis.curriculum import library_acronyms

    acr = library_acronyms(["LLM Inference", "Agentic RAGs", "Supervised fine-tuning (SFT)", "Neural networks"])
    assert acr == {"llm", "rag", "sft"}
    need = concept_tokens("Basic knowledge of LLMs", acr)
    assert matches(need, concept_tokens("Large Language Models", acr))
    assert matches(concept_tokens("Understanding of supervised fine-tuning", acr), concept_tokens("SFT", acr))
    assert not matches(need, concept_tokens("Large Language Models"))  # without the acronyms


def test_needs_close_in_meaning_count_when_words_differ():
    l1 = src(
        "l1", 0, [sec(0, needs=["Understanding of agent architectures", "Knowledge of agent architecture patterns"])]
    )
    v = src("v", 1, [sec(0, teaches=["Agent Architecture"])])
    assert outside_needs([l1, v])["l1"] == []  # both words are broad: no word match
    close = {("Understanding of agent architectures", "Agent Architecture")}
    needs = outside_needs([l1, v], similar=lambda p, c: (p, c) in close)["l1"]
    assert [n.prerequisite for n in needs] == ["Understanding of agent architectures"]


def test_semantic_matcher_threshold():
    from curio_learn.analysis.curriculum import SemanticMatcher

    m = SemanticMatcher({"a": [1, 0], "b": [0.9, 0.1], "c": [0, 1]}, threshold=0.8)
    assert m("a", "b") and not m("a", "c") and not m("a", "unknown")


def test_phrase_vectors_are_saved_and_reused(service):
    class Emb:
        name = "fake/emb"
        calls = 0

        def embed(self, texts, *, kind):
            Emb.calls += 1
            return [[1.0, float(len(t) % 3)] for t in texts]

    course_id, a, b, c = _library(service)
    service._embedder = Emb()
    lecs = service._course_lectures(service.course(course_id).lectures)
    sources = service._lecture_sources(course_id, lecs)
    assert service._phrase_matcher(sources) is not None and Emb.calls == 1
    assert service._phrase_matcher(sources) is not None and Emb.calls == 1  # all saved now

    class Broken(Emb):
        def embed(self, texts, *, kind):
            raise RuntimeError("Ollama is down")

    service._embedder = Broken()
    service.repo.save_phrase_vectors("fake/emb", {})  # nothing new saved under the broken one's name
    assert service._phrase_matcher(sources) is not None  # same name: everything is cached, no call
    Broken.name = "other/emb"
    assert service._phrase_matcher(sources) is None  # words only


def test_a_video_is_weighed_against_the_whole_course_and_repeats_count_once():
    # Lectures 2 and 3 each need one thing the video teaches: together enough to move it.
    l1 = src("l1", 0, [sec(0)], group="c")
    l2 = src("l2", 1, [sec(0, needs=["Neural networks"])], group="c")
    l3 = src("l3", 2, [sec(0, needs=["Neural network weights", "Neural network weights"])], group="c")
    nn = src("nn", 3, [sec(0, teaches=["Neural networks", "Neural network weights"])])
    edges = source_edges([l1, l2, l3, nn])
    assert [(e.before, e.after, e.weight) for e in edges] == [("nn", "l2", 2.0)]  # l3's repeat counts once
    assert order_sources([l1, l2, l3, nn], edges)[0] == ["l1", "nn", "l2", "l3"]


def test_a_one_word_need_is_taught_only_by_that_concept_but_covered_by_any_at_home():
    assert not matches(concept_tokens("Basic Python"), concept_tokens("Python Interpreter"))
    assert matches(concept_tokens("Basic Python"), concept_tokens("Python"))
    assert matches(concept_tokens("Basic Python"), concept_tokens("Python Interpreter"), strict=False)
    # Taught earlier in its own video by a longer concept: not an outside need.
    me = src("me", 0, [sec(0, teaches=["Python interpreter"]), sec(1, needs=["Python basics"])])
    other = src("o", 1, [sec(0, teaches=["Python"])])
    assert outside_needs([me, other])["me"] == []
    # Another source's longer concept doesn't teach it.
    alone = src("a", 0, [sec(0, needs=["Python basics"])])
    tool = src("t", 1, [sec(0, teaches=["Python Interpreter"])])
    assert outside_needs([alone, tool])["a"] == []


def test_a_teacher_pulled_ahead_brings_its_own_teacher_along():
    # The course needs what v2 teaches; v2 needs what v1 teaches, and v1 already comes
    # before v2. Pulling v2 ahead of the course must pull v1 too.
    c = src("c", 0, [sec(0, needs=["Gradient descent", "Loss landscapes"], start=0)], group="course")
    v1 = src("v1", 1, [sec(0, teaches=["Neural networks", "Backpropagation"], start=0)])
    v2 = src("v2", 2, [sec(0, teaches=["Gradient descent", "Loss landscape"], needs=["Neural networks"], start=0)])
    placed = [Placed("C0", "c", "S00", 0), Placed("V1", "v1", "S00", 0), Placed("V2", "v2", "S00", 0)]
    assert order_sections(placed, [c, v1, v2], ["c", "v1", "v2"]) == ["V1", "V2", "C0"]


def test_a_curriculum_picks_for_its_goal_and_saves_the_ratings(service):
    course_id, a, b, c = _library(service)
    sources = [CurriculumSource(kind="course", id=course_id), CurriculumSource(kind="video", id=c)]
    cid = service.create_curriculum("Build agents", sources)
    fake = service.llm
    plan = service.course_plan(cid, minutes=10)
    assert "GoalFitsOut" in fake.calls and not any("goal" in w for w in plan.warnings)
    assert {p.goal_fit for p in plan.parts} == {"supporting"}  # the fake rates all supporting
    before = {(p.analysis_id, s) for p in plan.parts for s in p.section_ids}
    # The same ratings at another length: nothing is rated again.
    fake.calls.clear()
    service.course_plan(cid, minutes=12)
    assert "GoalFitsOut" not in fake.calls
    # The goal decides: Curio's picks rated aside, one section left out rated core.
    choices = service.course_playlist_sections(cid)
    assert {ch.goal_fit for ch in choices if ch.picked} == {"supporting"}
    left = next(ch for ch in choices if not ch.curio_pick and ch.importance.value != "skip")
    keys = {(ch.analysis_id, ch.section_id): f"L{ch.lecture}.{ch.section_id}" for ch in choices}
    fake.fits = {keys[k]: "aside" for k in before} | {keys[(left.analysis_id, left.section_id)]: "core"}
    service.repo.delete_setting("goal_fit:" + cid)
    fake.calls.clear()
    fake.prompts.clear()
    plan = service.course_plan(cid, minutes=10, force=True)
    after = {(p.analysis_id, s) for p in plan.parts for s in p.section_ids}
    assert (left.analysis_id, left.section_id) in after
    assert any(p.goal_fit == "spine" for p in plan.parts)  # the only core section: the spine
    assert "GoalSpineOut" in fake.calls
    saved = service.goal_fits(cid)
    assert list(saved.values()).count("spine") == 1 and "aside" in saved.values()
    checklist_prompt = fake.prompts[fake.calls.index("ChecklistOut")]
    assert 'goal: "Build agents"' in checklist_prompt and "[core]" in checklist_prompt
    fake.calls.clear()
    service.course_plan(cid, minutes=12, force=True)
    assert "GoalSpineOut" not in fake.calls and "GoalFitsOut" not in fake.calls  # both saved
    assert service.goal_fits(cid)  # saved, by video and section
    service.delete_course(cid)
    assert service.repo.get_setting("goal_fit:" + cid) is None


def test_without_ratings_a_curriculum_picks_as_before(service, monkeypatch):
    course_id, a, b, c = _library(service)
    sources = [CurriculumSource(kind="course", id=course_id), CurriculumSource(kind="video", id=c)]
    plain = service.course_plan(course_id, minutes=10)  # a plain course: no goal, no rating
    assert "GoalFitsOut" not in service.llm.calls and all(p.goal_fit == "" for p in plain.parts)
    cid = service.create_curriculum("Build agents", sources)
    from curio_learn.analysis import course as C

    monkeypatch.setattr(C, "rate_goal_fit", lambda *a, **k: ({}, len(a[1])))
    plan = service.course_plan(cid, minutes=10)
    assert any("Couldn't rate the sections" in w for w in plan.warnings)
    assert all(p.goal_fit == "" for p in plan.parts)
    assert service.goal_fits(cid) == {}


def test_goal_score_and_batches_keep_a_lecture_together():
    from curio_learn.analysis.course import goal_score, rate_goal_fit

    assert goal_score(40, "spine") == 100 and goal_score(40, "spine", share=0.0) == 40
    assert goal_score(90, "core") == 85 and goal_score(90, "aside") == 55
    assert goal_score(90, None) == goal_score(90, "supporting") == 72.5
    assert goal_score(80, "core", share=0.0) == 80

    class Lec:
        def __init__(self, n):
            self.name, self.analysis = f"Lecture {n}", SimpleNamespace(video=SimpleNamespace(title=f"T{n}"))

    l1, l2 = Lec(1), Lec(2)
    items = [
        SimpleNamespace(key=f"L1.S0{i}", lecture=l1, section=SimpleNamespace(title="t", summary="s")) for i in range(3)
    ]
    items += [
        SimpleNamespace(key=f"L2.S0{i}", lecture=l2, section=SimpleNamespace(title="t", summary="s")) for i in range(3)
    ]
    fake = FakeLLM()
    fake.fits = {"L2.S01": "core"}
    fits, missing = rate_goal_fit(
        fake, items, [l1, l2], goal="g", model="m", shared="", retries=0, progress=lambda _: None, batch=4
    )
    assert missing == 0 and fits["L2.S01"] == "core" and fits["L1.S00"] == "supporting"
    batches = [p.rsplit("Rate each of: ", 1)[1] for p in fake.prompts]
    assert batches == ["L1.S00, L1.S01, L1.S02.", "L2.S00, L2.S01, L2.S02."]  # lecture 2 didn't fit in the first
