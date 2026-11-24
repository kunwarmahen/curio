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
