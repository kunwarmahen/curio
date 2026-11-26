import time
from datetime import datetime, timedelta
from datetime import time as clock_time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from curio_learn.api.app import create_app
from curio_learn.domain import AnalysisStatus, LearningPreferences
from curio_learn.errors import AnalysisError
from curio_learn.overnight import LocalGpuProbe, QueueWorker, Window
from curio_learn.service import CurioService

from .conftest import FakeLLM, FakeMetadata, FakeTranscripts

URL = "https://www.youtube.com/watch?v=e_m99aR7_3Q"
URL2 = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
NIGHT = datetime(2026, 11, 24, 2, 30).astimezone()
DAY = datetime(2026, 11, 24, 14, 0).astimezone()


@pytest.fixture
def service(settings):
    return CurioService(settings, llm=FakeLLM(), metadata=FakeMetadata(), transcripts=FakeTranscripts())


class FakeProbe:
    def __init__(self, reason=""):
        self.reason = reason
        self.calls = []

    def busy_reason(self, *, own_recent_use):
        self.calls.append(own_recent_use)
        return self.reason


def worker(service, *, now=NIGHT, probe=None, active=False, hours="1-7"):
    started = []
    w = QueueWorker(
        service.repo,
        window=Window.parse(hours),
        probe=probe or FakeProbe(),
        active=lambda: active,
        start=started.append,
        now=lambda: now,
    )
    return w, started


def test_window():
    w = Window.parse("1-7")
    assert w.contains(clock_time(1, 0)) and w.contains(clock_time(6, 59))
    assert not w.contains(clock_time(7, 0)) and not w.contains(clock_time(0, 59))
    late = Window.parse("22:30-6")
    assert late.contains(clock_time(23, 0)) and late.contains(clock_time(5, 0))
    assert not late.contains(clock_time(12, 0)) and not late.contains(clock_time(22, 29))
    assert Window.parse("") is None
    assert Window.parse("0-0").contains(clock_time(15, 0))
    assert w.next_start(DAY) == DAY.replace(day=25, hour=1, minute=0)
    assert w.next_start(DAY.replace(hour=0, minute=10)) == DAY.replace(hour=1, minute=0)
    for bad in ("night", "1-25", "7:75-8"):
        with pytest.raises(ValueError):
            Window.parse(bad)


def test_gpu_probe(monkeypatch):
    readings = iter(["3\n", "40\n", "5\n", "60\n", "16\n"])
    monkeypatch.setattr("curio_learn.overnight.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=next(readings)))
    monkeypatch.setattr("curio_learn.overnight.time.sleep", lambda _: None)
    loaded = []
    client = SimpleNamespace(ps=lambda: SimpleNamespace(models=[SimpleNamespace(model=m) for m in loaded]))
    probe = LocalGpuProbe(busy_percent=15, own_models={"qwen3.8-64k"}, nvidia_smi="nvidia-smi", ollama_client=client)
    assert probe.busy_reason(own_recent_use=False) == "The GPU is in use (16% busy)."

    # One spike among quiet readings isn't use.
    readings = iter(["2\n", "2\n", "90\n", "3\n", "1\n"] + ["2\n"] * 20)
    assert probe.busy_reason(own_recent_use=False) == ""
    # A model left loaded by Curio's own last job doesn't count; anyone else's does.
    loaded[:] = ["qwen3.8-64k:latest"]
    assert probe.busy_reason(own_recent_use=True) == ""
    assert "qwen3.8-64k:latest" in probe.busy_reason(own_recent_use=False)
    loaded[:] = ["qwen3.8-64k:latest", "llama3:8b"]
    assert probe.busy_reason(own_recent_use=True) == "Ollama has llama3:8b loaded for something else."
    # No nvidia-smi: only Ollama is checked.
    loaded[:] = []
    assert LocalGpuProbe(busy_percent=15, nvidia_smi="", ollama_client=client).busy_reason(own_recent_use=False) == ""


def test_queued_analysis_waits_for_the_window_and_the_gpu(service):
    first, needs_run = service.submit(URL, LearningPreferences(), queue=True)
    second, _ = service.submit(URL2, LearningPreferences(), queue=True)
    assert not needs_run and service.get(first).status is AnalysisStatus.QUEUED
    # Queuing the same video again reuses the queued analysis.
    assert service.submit(URL, LearningPreferences(), queue=True) == (first, False)

    w, started = worker(service, now=DAY)
    assert w.tick() is None and not started
    status = w.status()
    assert status.state == "waiting_window" and status.window_start == "01:00" and status.window_end == "07:00"
    assert status.next_start == DAY.replace(day=25, hour=1, minute=0)
    assert [i.analysis_id for i in status.items] == [first, second]

    w, started = worker(service, probe=FakeProbe("The GPU is in use (80% busy)."))
    assert w.tick() is None and not started
    assert w.status().state == "waiting_gpu" and "80%" in w.status().message

    w, started = worker(service, active=True)
    assert w.tick() is None and w.status().state == "busy"

    w, started = worker(service)
    assert w.status().state == "starting"
    assert w.tick() == first and started == [first]
    assert service.get(first).status is AnalysisStatus.PENDING
    service.run(first)
    assert service.get(first).status is AnalysisStatus.DONE
    # The running or finished item leaves the queue; the next one is still waiting.
    assert [i.analysis_id for i in w.status().items] == [second]


def test_own_recent_use_is_passed_to_the_probe(service):
    service.submit(URL, LearningPreferences(), queue=True)
    probe = FakeProbe("busy")
    w, _ = worker(service, probe=probe)
    w.tick()
    w.touch()
    w.tick()
    w.now = lambda: NIGHT + timedelta(minutes=20)
    w.tick()
    assert probe.calls == [False, True, False]


def test_start_now_and_remove(service):
    a, _ = service.submit(URL, LearningPreferences(), queue=True)
    b, _ = service.submit(URL2, LearningPreferences(), queue=True)
    # Analyzing a queued video now takes it out of the queue instead of making another.
    assert service.submit(URL, LearningPreferences()) == (a, True)
    assert service.get(a).status is AnalysisStatus.PENDING
    with pytest.raises(AnalysisError):
        service.start_queued(a)
    with pytest.raises(KeyError):
        service.start_queued("nope")
    assert service.remove_queued(b) and service.get(b) is None
    assert not service.remove_queued(a)  # not queued any more
    # Queued rows are never "interrupted": they wait without updates.
    c, _ = service.submit(URL2, LearningPreferences(), queue=True)
    assert service.repo.mark_stale_running_failed(stale_after=timedelta(0)) == 1  # only `a`
    assert service.get(c).status is AnalysisStatus.QUEUED


URL3 = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


def test_run_now_skips_the_window_but_not_the_gpu(service):
    a, _ = service.submit(URL, LearningPreferences(), queue=True)
    b, _ = service.submit(URL2, LearningPreferences(), queue=True)
    c, _ = service.submit(URL3, LearningPreferences(), queue=True)
    w, started = worker(service, now=DAY)
    assert w.run_now([c, "nope"]) == [c]
    assert w.status().state == "starting" and w.status().run_now == [c]
    # Still waits for a free GPU.
    busy, none = worker(service, now=DAY, probe=FakeProbe("The GPU is in use (80% busy)."))
    assert busy.tick() is None and not none and busy.status().state == "waiting_gpu"
    # Only the chosen video runs outside the hours.
    assert w.tick() == c and started == [c]
    service.run(c)
    assert w.tick() is None and started == [c]
    assert w.status().state == "waiting_window" and w.status().run_now == []
    # All of them, then back to the hours.
    assert w.run_now() == [a, b]
    w.stop_run_now()
    assert w.tick() is None and w.status().run_now == []


def test_run_now_picks_go_first_in_the_hours_and_survive_a_pause(service):
    a, _ = service.submit(URL, LearningPreferences(), queue=True)
    b, _ = service.submit(URL2, LearningPreferences(), queue=True)
    w, started = worker(service)
    w.run_now([b])
    assert w.tick() == b
    # Paused for the GPU, it goes back to waiting and is still the learner's pick.
    service.repo.requeue(b, "Paused")
    assert w.status().run_now == [b]
    w2, started2 = worker(service, now=DAY)
    assert w2.tick() == b and started2 == [b]


def test_api_queue_flow(service):
    with TestClient(create_app(service)) as client:
        r = client.post("/api/v1/analyses", json={"url": URL, "queue": True})
        assert r.status_code == 202 and r.json()["status"] == "queued"
        analysis_id = r.json()["analysis_id"]
        other = client.post("/api/v1/analyses", json={"url": URL2, "queue": True}).json()["analysis_id"]
        q = client.get("/api/v1/queue").json()
        assert [i["analysis_id"] for i in q["items"]] == [analysis_id, other]
        assert q["window_start"] == "01:00" and q["items"][0]["video"]["title"] == "Test Lecture"

        assert client.delete(f"/api/v1/queue/{other}").status_code == 200
        assert client.delete(f"/api/v1/queue/{other}").status_code == 404

        assert client.post(f"/api/v1/queue/{analysis_id}/start").status_code == 202
        for _ in range(100):
            body = client.get(f"/api/v1/analyses/{analysis_id}").json()
            if body["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert body["status"] == "done", body.get("error")
        assert client.post(f"/api/v1/queue/{analysis_id}/start").status_code == 400
        assert client.post("/api/v1/queue/nope/start").status_code == 404
        later = client.post("/api/v1/analyses", json={"url": URL2, "queue": True}).json()["analysis_id"]
        r = client.post("/api/v1/queue/run-now", json={})
        assert r.status_code == 200 and r.json()["run_now"] == [later]
        assert client.delete("/api/v1/queue/run-now").json()["run_now"] == []
        assert client.delete(f"/api/v1/queue/{later}").status_code == 200
        assert client.get("/api/v1/queue").json()["state"] == "empty"


def test_default_goal_and_retargeting_the_queue(service):
    assert service.default_preferences() == LearningPreferences()
    goal = LearningPreferences(profile="technical", custom_objective="How agents use tools", target_minutes=20)
    service.set_default_preferences(goal)
    assert service.default_preferences() == goal

    done = service.analyze(URL, goal)  # this video already has a result with the new goal
    a, _ = service.submit(URL, LearningPreferences(), queue=True)
    b, _ = service.submit(URL2, LearningPreferences(), queue=True)
    result = service.retarget_queued(goal)
    assert result == {"updated": [b], "already_analyzed": [a]}
    assert service.get(a) is None and service.get(done.id).status is AnalysisStatus.DONE
    assert service.get(b).preferences == goal
    # Queuing the same video with the same goal now finds the retargeted row.
    assert service.submit(URL2, goal, queue=True) == (b, False)
    # Only queued videos change.
    service.run(b)
    assert service.retarget_queued(LearningPreferences(), [b]) == {"updated": [], "already_analyzed": []}
    assert service.get(b).preferences == goal


def test_cli_uses_the_saved_goal(service):
    from argparse import Namespace

    from curio_learn.cli import cli_preferences

    service.set_default_preferences(LearningPreferences(profile="beginner", custom_objective="basics"))
    flags = Namespace(profile=None, objective=None, minutes=None)
    assert cli_preferences(flags, service) == LearningPreferences(profile="beginner", custom_objective="basics")
    flags = Namespace(profile="technical", objective="", minutes=15)
    assert cli_preferences(flags, service) == LearningPreferences(profile="technical", target_minutes=15)


def test_api_default_goal(service):
    with TestClient(create_app(service)) as client:
        assert client.get("/api/v1/preferences/default").json()["profile"] == "general"
        goal = {"profile": "executive", "custom_objective": "costs", "target_minutes": None}
        assert client.put("/api/v1/preferences/default", json={"preferences": goal}).status_code == 200
        assert client.get("/api/v1/preferences/default").json() == goal
        queued = client.post("/api/v1/analyses", json={"url": URL, "queue": True}).json()["analysis_id"]
        other = {"profile": "deep_dive", "custom_objective": "", "target_minutes": 30}
        r = client.post("/api/v1/queue/preferences", json={"preferences": other}).json()
        assert r == {"updated": [queued], "already_analyzed": []}
        assert client.get("/api/v1/queue").json()["items"][0]["preferences"] == other
        assert client.get("/api/v1/preferences/default").json() == other  # saved as the default too


def test_prepare_writes_notes_and_a_quiz_once(service):
    analysis_id, _ = service.submit(URL, LearningPreferences())
    service.run(analysis_id)
    assert service.prepare(analysis_id) == ["notes", "quiz"]
    assert [n.status for n in service.list_notes(analysis_id)] == [AnalysisStatus.DONE]
    [study] = service.list_study(analysis_id)
    assert study.status is AnalysisStatus.DONE and study.questions
    # Already there: nothing new.
    assert service.prepare(analysis_id) == []


def test_prepare_stops_when_the_gpu_gets_busy(service):
    analysis_id, _ = service.submit(URL, LearningPreferences())
    service.run(analysis_id)
    answers = iter(["", "The GPU is in use (90% busy)."])
    assert service.prepare(analysis_id, gpu_busy=lambda: next(answers)) == ["notes"]
    assert service.list_study(analysis_id) == []


def test_queued_job_analyzes_then_prepares(service):
    from curio_learn.api.app import _run_queued

    analysis_id, _ = service.submit(URL, LearningPreferences(), queue=True)
    w, _ = worker(service)
    assert w.tick() == analysis_id
    _run_queued(service, analysis_id, w)
    assert service.get(analysis_id).status is AnalysisStatus.DONE
    assert service.list_notes(analysis_id) and service.list_study(analysis_id)

    other, _ = service.submit(URL2, LearningPreferences(), queue=True)
    service.settings.queue_prepare = False
    assert w.tick() == other
    _run_queued(service, other, w)
    assert service.get(other).status is AnalysisStatus.DONE and not service.list_notes(other)


def test_a_queued_analysis_pauses_when_the_gpu_gets_busy(service):
    from curio_learn.errors import Paused

    analysis_id, _ = service.submit(URL, LearningPreferences(), queue=True)
    w, _ = worker(service)
    assert w.tick() == analysis_id
    answers = iter(["", "", "The GPU is in use (85% busy)."])
    with pytest.raises(Paused):
        service.run(analysis_id, should_pause=lambda: next(answers, ""))
    a = service.get(analysis_id)
    assert a.status is AnalysisStatus.QUEUED and a.progress == "Paused: The GPU is in use (85% busy)."
    assert w.status().items[0].progress.startswith("Paused")
    # Later, with the GPU free, it starts over and finishes.
    assert w.tick() == analysis_id
    service.run(analysis_id)
    assert service.get(analysis_id).status is AnalysisStatus.DONE


def test_pause_checks_are_throttled():
    from curio_learn.api.app import _every

    calls = []
    check = _every(60.0, lambda: calls.append(1) or "busy")
    assert check() == "" and calls == []  # the queue checked the GPU just before starting


def _pause_after(service, analysis_id, call):
    """Run until the LLM has answered `call`; the next progress step finds the GPU busy."""
    from curio_learn.errors import Paused

    service.llm.calls.clear()
    with pytest.raises(Paused):
        service.run(analysis_id, should_pause=lambda: "The GPU is in use." if call in service.llm.calls else "")
    assert service.get(analysis_id).status is AnalysisStatus.QUEUED


def test_a_paused_analysis_resumes_from_its_finished_steps(service):
    analysis_id, _ = service.submit(URL, LearningPreferences(), queue=True)
    _pause_after(service, analysis_id, "OutlineOut")  # the outline is done
    assert service.repo.get_checkpoint(analysis_id)["step"] == "outline"

    service.llm.calls.clear()
    seen: list[str] = []
    resumed = service.run(analysis_id, progress=seen.append)
    assert service.llm.calls == ["BoundariesOut", "EvaluationOut"]
    assert any(m.startswith("Resuming") for m in seen)
    assert service.repo.get_checkpoint(analysis_id) is None

    # The same result as a run that was never paused.
    fresh = service.analyze(URL, LearningPreferences(), force=True)
    assert [s.model_dump() for s in resumed.sections] == [s.model_dump() for s in fresh.sections]
    assert resumed.playlist.model_dump() == fresh.playlist.model_dump()
    assert resumed.validation.repairs == fresh.validation.repairs


def test_a_new_goal_keeps_the_outline_but_redoes_the_evaluations(service):
    analysis_id, _ = service.submit(URL, LearningPreferences(), queue=True)
    _pause_after(service, analysis_id, "EvaluationOut")  # every LLM step done
    assert service.repo.get_checkpoint(analysis_id)["step"] == "evaluate"

    service.llm.calls.clear()
    service.run(analysis_id)  # same goal: no LLM calls at all
    assert service.llm.calls == []

    other, _ = service.submit(URL2, LearningPreferences(), queue=True)
    _pause_after(service, other, "EvaluationOut")
    goal = LearningPreferences(profile="technical")
    assert service.retarget_queued(goal, [other])["updated"] == [other]
    service.llm.calls.clear()
    result = service.run(other)
    assert set(service.llm.calls) == {"EvaluationOut"}
    assert result.preferences.profile == "technical"


def test_a_checkpoint_from_other_models_is_not_used(service):
    analysis_id, _ = service.submit(URL, LearningPreferences(), queue=True)
    _pause_after(service, analysis_id, "OutlineOut")
    data = service.repo.get_checkpoint(analysis_id)
    service.repo.save_checkpoint(analysis_id, {**data, "key": "other|key"})
    service.llm.calls.clear()
    service.run(analysis_id)
    assert "OutlineOut" in service.llm.calls


def test_removing_a_paused_video_drops_its_checkpoint(service):
    analysis_id, _ = service.submit(URL, LearningPreferences(), queue=True)
    _pause_after(service, analysis_id, "OutlineOut")
    assert service.repo.get_checkpoint(analysis_id) is not None
    assert service.remove_queued(analysis_id)
    assert service.repo.get_checkpoint(analysis_id) is None


def test_a_pause_during_the_evaluations_keeps_the_finished_batches(settings):
    from curio_learn.errors import Paused

    small = settings.model_copy(update={"evaluate_batch_tokens": 150})  # several batches
    svc = CurioService(small, llm=FakeLLM(), metadata=FakeMetadata(), transcripts=FakeTranscripts())
    svc.analyze(URL, LearningPreferences(profile="technical"))
    batches = svc.llm.calls.count("EvaluationOut")
    assert batches >= 4

    analysis_id, _ = svc.submit(URL, LearningPreferences(), queue=True)
    svc.llm.calls.clear()
    with pytest.raises(Paused):
        svc.run(analysis_id, should_pause=lambda: "busy" if svc.llm.calls.count("EvaluationOut") == 2 else "")
    cp = svc.repo.get_checkpoint(analysis_id)
    assert cp["step"] != "evaluate" and len(cp["partial_evaluations"]) >= 2

    svc.llm.calls.clear()
    seen: list[str] = []
    resumed = svc.run(analysis_id, progress=seen.append)
    assert svc.llm.calls == ["EvaluationOut"] * (batches - 2)
    assert any("kept the evaluations" in m for m in seen)
    again = svc.analyze(URL, LearningPreferences(), force=True)
    assert [s.model_dump() for s in resumed.sections] == [s.model_dump() for s in again.sections]
    assert resumed.playlist.model_dump() == again.playlist.model_dump()

    # Batches written for another goal are not reused.
    other, _ = svc.submit(URL2, LearningPreferences(), queue=True)
    with pytest.raises(Paused):
        svc.llm.calls.clear()
        svc.run(other, should_pause=lambda: "busy" if svc.llm.calls.count("EvaluationOut") == 2 else "")
    svc.retarget_queued(LearningPreferences(profile="technical"), [other])
    seen.clear()
    svc.run(other, progress=seen.append)
    assert not any("kept the evaluations" in m for m in seen)
    assert svc.repo.get_checkpoint(other) is None


def test_a_pause_while_mapping_topics_keeps_the_finished_windows(settings):
    from curio_learn.errors import Paused

    small = settings.model_copy(update={"outline_window_tokens": 120, "ollama_num_ctx": 32768})
    svc = CurioService(small, llm=FakeLLM(), metadata=FakeMetadata(), transcripts=FakeTranscripts())
    fresh = svc.analyze(URL, LearningPreferences(profile="technical"))
    windows = svc.llm.calls.count("OutlineOut")
    assert windows >= 4 and "OverviewOut" in svc.llm.calls

    def pause_after(n_outline):
        analysis_id, _ = svc.submit(URL, LearningPreferences(profile="technical"), force=True)
        svc.llm.calls.clear()
        with pytest.raises(Paused):
            svc.run(analysis_id, should_pause=lambda: "busy" if svc.llm.calls.count("OutlineOut") == n_outline else "")
        cp = svc.repo.get_checkpoint(analysis_id)
        assert cp["step"] == "" and cp["partial_outline"]["k"] == n_outline
        svc.llm.calls.clear()
        seen: list[str] = []
        result = svc.run(analysis_id, progress=seen.append)
        assert any("kept" in m and "topic map" in m for m in seen)
        return result

    # Paused between windows: only the rest are read.
    resumed = pause_after(2)
    assert svc.llm.calls.count("OutlineOut") == windows - 2
    assert [s.model_dump() for s in resumed.sections] == [s.model_dump() for s in fresh.sections]
    assert resumed.overview == fresh.overview
    # Paused before the overview: no window is read again.
    resumed = pause_after(windows)
    assert svc.llm.calls.count("OutlineOut") == 0 and svc.llm.calls.count("OverviewOut") == 1
    assert [s.model_dump() for s in resumed.sections] == [s.model_dump() for s in fresh.sections]


def test_relabeling_saved_analyses_changes_only_labels(service):
    from curio_learn.analysis.playlist import BEYOND_BUDGET
    from curio_learn.domain import Importance

    a = service.analyze(URL, LearningPreferences())
    chosen = {sid for seg in a.playlist.segments for sid in seg.section_ids}
    outside = next(s for s in a.sections if s.section.id not in chosen and s.importance is not Importance.SKIP)
    # Saved before the rule: an essential section outside the playlist.
    old = a.model_copy(
        update={
            "sections": [
                s.model_copy(update={"importance": Importance.ESSENTIAL}) if s is outside else s for s in a.sections
            ],
            "segments": [
                g.model_copy(update={"importance": Importance.ESSENTIAL}) if outside.section.id in g.section_ids else g
                for g in a.segments
            ],
        }
    )
    service.repo.save_result(old)
    mine = service.set_custom_playlist(a.id, [g.id for g in a.segments][:2])

    assert service.relabel_saved() == {a.id: 1}
    new = service.get(a.id)
    fixed = next(s for s in new.sections if s.section.id == outside.section.id)
    assert fixed.importance is Importance.RECOMMENDED
    assert fixed.score.adjustments[-1].rule == BEYOND_BUDGET and fixed.score.final == outside.score.final
    seg = next(g for g in new.segments if outside.section.id in g.section_ids)
    assert seg.importance is Importance.RECOMMENDED
    assert new.playlist.model_dump() == a.playlist.model_dump()
    assert [g.id for g in new.segments] == [g.id for g in a.segments]
    assert service.custom_playlist(a.id) == mine
    assert service.relabel_saved() == {}  # nothing left to change
