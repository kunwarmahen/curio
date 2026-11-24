"""The overnight queue: add videos at any time, analyze them when the GPU is free.

Queued analyses wait in the database with status `queued`. The API server's queue
worker checks every `queue_check_s` seconds and starts the oldest one when

- the local time is inside the queue window (`CURIO_QUEUE_HOURS`, e.g. "1-7"),
- Curio itself has nothing in progress, and
- the GPU is idle: utilization below `CURIO_QUEUE_GPU_BUSY_PERCENT` (read with
  nvidia-smi when it's available) and no Ollama model loaded by something else.

One analysis runs at a time. The conditions are checked before each start, so an
analysis that is running when the window closes finishes, but no new one starts.
After each analysis the job also writes playlist notes and a quiz (`queue_prepare`),
checking the GPU again before each; the window isn't checked again, as for a video
that is running.
With Claude as the provider the GPU isn't involved and only the window applies.
"""

from __future__ import annotations

import logging
import re
import shutil
import statistics
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as clock_time
from typing import Literal, Protocol

from pydantic import BaseModel

from curio_learn.domain import AnalysisStatus, LearningPreferences, VideoMetadata

log = logging.getLogger(__name__)

# Curio's own Ollama calls keep their model loaded this long (keep_alive is 10m).
OWN_USE_GRACE = timedelta(minutes=15)

_HOURS_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*$")


@dataclass(frozen=True)
class Window:
    """A daily stretch of local time; it may cross midnight ("23-6")."""

    start: clock_time
    end: clock_time

    @classmethod
    def parse(cls, text: str) -> Window | None:
        """ "1-7", "1:30-6:45" or "23-6". Empty means any time (None)."""
        if not text.strip():
            return None
        m = _HOURS_RE.match(text)
        if not m:
            raise ValueError(f"Queue hours must look like 1-7 or 22:30-6, not {text!r}")
        h1, m1, h2, m2 = (int(x or 0) for x in m.groups())
        if h1 == 24:
            h1 = 0
        if h2 == 24:
            h2 = 0
        if not (h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60):
            raise ValueError(f"Queue hours out of range: {text!r}")
        return cls(clock_time(h1, m1), clock_time(h2, m2))

    def contains(self, t: clock_time) -> bool:
        if self.start == self.end:
            return True
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def next_start(self, now: datetime) -> datetime:
        candidate = now.replace(hour=self.start.hour, minute=self.start.minute, second=0, microsecond=0)
        return candidate if candidate > now else candidate + timedelta(days=1)


class GpuProbe(Protocol):
    def busy_reason(self, *, own_recent_use: bool) -> str:
        """Why the GPU counts as busy, or "" when it's free."""
        ...


class LocalGpuProbe:
    """nvidia-smi for utilization, Ollama's loaded models for other LLM users."""

    def __init__(
        self,
        *,
        busy_percent: float,
        ollama_host: str = "",
        own_models: set[str] | None = None,
        samples: int = 5,
        nvidia_smi: str | None = None,
        ollama_client=None,
    ):
        self.busy_percent = busy_percent
        self.own_models = {_base(m) for m in own_models or set()}
        self.samples = samples
        self.nvidia_smi = nvidia_smi if nvidia_smi is not None else (shutil.which("nvidia-smi") or "")
        self._ollama = ollama_client
        if self._ollama is None and ollama_host:
            import ollama

            self._ollama = ollama.Client(host=ollama_host, timeout=10)

    def utilization(self) -> float | None:
        """Median utilization of the busiest GPU over a few one-second samples (the
        median ignores the brief spikes a desktop causes)."""
        if not self.nvidia_smi:
            return None
        readings = []
        for i in range(self.samples):
            if i:
                time.sleep(1)
            try:
                out = subprocess.run(
                    [self.nvidia_smi, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                ).stdout
                readings.append(max(float(x) for x in out.split()))
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                log.warning("nvidia-smi failed (%s); not checking GPU utilization", exc)
                return None
        return statistics.median(readings)

    def loaded_models(self) -> list[str] | None:
        if self._ollama is None:
            return None
        try:
            return [m.model for m in self._ollama.ps().models]
        except Exception as exc:
            log.warning("Couldn't ask Ollama which models are loaded (%s)", exc)
            return None

    def busy_reason(self, *, own_recent_use: bool) -> str:
        models = self.loaded_models() or []
        others = [m for m in models if not (own_recent_use and _base(m) in self.own_models)]
        if others:
            return f"Ollama has {', '.join(others)} loaded for something else."
        util = self.utilization()
        if util is not None and util >= self.busy_percent:
            return f"The GPU is in use ({util:.0f}% busy)."
        return ""


def _base(model: str) -> str:
    return model.removesuffix(":latest")


# ------------------------------------------------------------------ the worker


class QueueItem(BaseModel):
    analysis_id: str
    video: VideoMetadata
    preferences: LearningPreferences
    status: AnalysisStatus
    progress: str = ""
    added_at: datetime


class QueueStatus(BaseModel):
    # Local times as "HH:MM"; both empty when the queue runs at any time.
    window_start: str = ""
    window_end: str = ""
    state: Literal["empty", "busy", "waiting_window", "waiting_gpu", "starting"]
    message: str
    next_start: datetime | None = None
    items: list[QueueItem] = []


class QueueWorker:
    """Starts queued analyses one at a time, when the window is open and the GPU idle.

    `active` says whether Curio has a job in progress; `start` hands an analysis id to
    whatever runs jobs (the API's executor)."""

    def __init__(
        self,
        repo,
        *,
        window: Window | None,
        probe: GpuProbe | None,
        active: Callable[[], bool],
        start: Callable[[str], None],
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self.repo = repo
        self.window = window
        self.probe = probe
        self.active = active
        self.start_job = start
        self.now = now
        self.last_own_use: datetime | None = None
        self._gpu_reason = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def touch(self) -> None:
        """Curio just used the LLM (its model being loaded is then no sign of others)."""
        self.last_own_use = self.now()

    def _own_recent_use(self) -> bool:
        return self.last_own_use is not None and self.now() - self.last_own_use < OWN_USE_GRACE

    def tick(self) -> str | None:
        """Start the next queued analysis if everything allows it. Returns its id."""
        with self._lock:
            queued = self.repo.queued_ids()
            if not queued or self.active():
                return None
            if self.window and not self.window.contains(self.now().time()):
                return None
            reason = self.probe.busy_reason(own_recent_use=self._own_recent_use()) if self.probe else ""
            if reason and reason.split("(")[0] != self._gpu_reason.split("(")[0]:
                log.info("Queue waiting: %s", reason)  # once per kind of reason, not every check
            self._gpu_reason = reason
            if reason:
                return None
            for analysis_id in queued:
                if self.repo.claim_queued(analysis_id, "Starting from the queue"):
                    log.info("Queue starting analysis %s (%d queued)", analysis_id, len(queued))
                    self.start_job(analysis_id)
                    return analysis_id
            return None

    def gpu_busy(self) -> str:
        """For a queued job between its steps: why the GPU is busy with something other
        than Curio, or "". Curio's own model counts as its own (its job is running)."""
        return self.probe.busy_reason(own_recent_use=True) if self.probe else ""

    def status(self) -> QueueStatus:
        items = [QueueItem(**x) for x in self.repo.queue_items()]
        w = self.window
        base = {
            "window_start": w.start.strftime("%H:%M") if w else "",
            "window_end": w.end.strftime("%H:%M") if w else "",
            "items": items,
        }
        if not any(i.status == AnalysisStatus.QUEUED for i in items):
            return QueueStatus(**base, state="empty", message="Nothing is waiting in the queue.")
        now = self.now()
        if w and not w.contains(now.time()):
            nxt = w.next_start(now)
            return QueueStatus(**base, state="waiting_window", message="Waiting for the queue hours.", next_start=nxt)
        if self.active():
            return QueueStatus(**base, state="busy", message="Curio is working on something; the queue is next.")
        if self._gpu_reason:
            return QueueStatus(**base, state="waiting_gpu", message=f"Waiting: {self._gpu_reason}")
        return QueueStatus(**base, state="starting", message="Starting when Curio next checks the GPU.")

    # ------------------------------------------------------------- background

    def run_forever(self, interval_s: float) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval_s):
                try:
                    self.tick()
                except Exception:
                    log.exception("Queue check failed")

        self._thread = threading.Thread(target=loop, name="curio-queue", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
