"""FastAPI app exposing CurioService over HTTP (for a future React frontend)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from curio_learn.config import PROJECT_ROOT, get_settings
from curio_learn.domain import (
    AnalysisStatus,
    Answer,
    Block,
    LearningNotes,
    LearningPreferences,
    NotesScope,
    VideoAnalysis,
)
from curio_learn.errors import CurioError, InvalidURL
from curio_learn.logging_setup import configure_logging
from curio_learn.overnight import LocalGpuProbe, QueueStatus, QueueWorker, Window
from curio_learn.service import CurioService
from curio_learn.storage.repository import RecentVideo

log = logging.getLogger(__name__)


class AnalyzeRequest(BaseModel):
    url: str
    preferences: LearningPreferences = LearningPreferences()
    force: bool = False
    # Wait for the overnight queue instead of starting now.
    queue: bool = False


class AnalyzeAccepted(BaseModel):
    analysis_id: str
    status: AnalysisStatus


class CompletedRequest(BaseModel):
    completed: bool


class NotesRequest(BaseModel):
    scope: NotesScope = NotesScope.PLAYLIST
    segment_ids: list[str] = []
    force: bool = False


class AskRequest(BaseModel):
    question: str


class NotesAccepted(BaseModel):
    notes_id: str
    status: AnalysisStatus


_STATUS = {
    "invalid_url": 422,
    "video_unavailable": 404,
    "transcript_unavailable": 422,
    "rate_limited": 429,
    "llm_error": 502,
}


class Jobs:
    """One job at a time (local LLMs can't usefully run several in parallel), with a
    count of jobs submitted and not yet finished, so the queue knows when Curio is idle."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="curio-analysis")
        self._lock = threading.Lock()
        self._inflight = 0
        self.on_done: Callable[[], None] = lambda: None

    def submit(self, fn: Callable, *args) -> None:
        with self._lock:
            self._inflight += 1

        def run() -> None:
            try:
                fn(*args)
            finally:
                with self._lock:
                    self._inflight -= 1
                self.on_done()

        self._executor.submit(run)

    def active(self) -> bool:
        with self._lock:
            return self._inflight > 0

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def build_queue(service: CurioService, jobs: Jobs) -> QueueWorker:
    s = service.settings
    try:
        window = Window.parse(s.queue_hours)
    except ValueError as exc:
        log.error("%s; using 1-7", exc)
        window = Window.parse("1-7")
    probe = None
    if s.resolved_provider == "ollama":
        own = {s.model_for(t) for t in ("outline", "evaluate", "notes", "ask")} | {s.embed_model} - {""}
        probe = LocalGpuProbe(busy_percent=s.queue_gpu_busy_percent, ollama_host=s.ollama_host, own_models=own)
    worker = QueueWorker(
        service.repo,
        window=window,
        probe=probe,
        active=jobs.active,
        start=lambda analysis_id: jobs.submit(_run_safely, service, analysis_id),
    )
    jobs.on_done = worker.touch
    return worker


def create_app(service: CurioService | None = None) -> FastAPI:
    jobs = Jobs()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is None:
            configure_logging(get_settings().log_level)
            app.state.service = CurioService()
        else:
            app.state.service = service
        stale = app.state.service.repo.mark_stale_running_failed()
        if stale:
            log.warning("Marked %d interrupted analyses as failed", stale)
        app.state.queue = build_queue(app.state.service, jobs)
        if app.state.service.settings.queue_check_s > 0:
            app.state.queue.run_forever(app.state.service.settings.queue_check_s)
        yield
        app.state.queue.stop()
        jobs.shutdown()

    app = FastAPI(title="Curio API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def svc(request: Request) -> CurioService:
        return request.app.state.service

    @app.exception_handler(CurioError)
    async def curio_error(_: Request, exc: CurioError):
        return JSONResponse(
            status_code=_STATUS.get(exc.code, 400), content={"detail": {"code": exc.code, "message": exc.message}}
        )

    @app.get("/api/v1/health")
    def health(request: Request):
        s = svc(request)
        return {
            "status": "ok",
            "llm_provider": s.settings.resolved_provider,
            "models": {"outline": s.settings.model_for("outline"), "evaluate": s.settings.model_for("evaluate")},
            "queue_hours": s.settings.queue_hours,
        }

    @app.post("/api/v1/analyses", status_code=202, response_model=AnalyzeAccepted)
    def analyze(body: AnalyzeRequest, request: Request):
        s = svc(request)
        if not body.url.strip():
            raise InvalidURL("A YouTube URL is required.")
        analysis_id, needs_run = s.submit(body.url, body.preferences, force=body.force, queue=body.queue)
        if needs_run:
            jobs.submit(_run_safely, s, analysis_id)
        current = s.get(analysis_id)
        return AnalyzeAccepted(analysis_id=analysis_id, status=current.status if current else AnalysisStatus.PENDING)

    @app.get("/api/v1/analyses/{analysis_id}", response_model=VideoAnalysis)
    def get_analysis(analysis_id: str, request: Request):
        result = svc(request).get(analysis_id)
        if result is None:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown analysis id"})
        return result

    @app.put("/api/v1/analyses/{analysis_id}/segments/{segment_id}/completed")
    def set_completed(analysis_id: str, segment_id: str, body: CompletedRequest, request: Request):
        svc(request).set_completed(analysis_id, segment_id, body.completed)
        return {"analysis_id": analysis_id, "segment_id": segment_id, "completed": body.completed}

    @app.get("/api/v1/analyses/{analysis_id}/completed", response_model=list[str])
    def completed(analysis_id: str, request: Request):
        return sorted(svc(request).completed(analysis_id))

    @app.post("/api/v1/analyses/{analysis_id}/notes", status_code=202, response_model=NotesAccepted)
    def create_notes(analysis_id: str, body: NotesRequest, request: Request):
        s = svc(request)
        try:
            notes_id, needs_run = s.submit_notes(analysis_id, body.scope, body.segment_ids, force=body.force)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown analysis id"}) from None
        if needs_run:
            jobs.submit(_run_notes_safely, s, notes_id)
        current = s.get_notes(notes_id)
        return NotesAccepted(notes_id=notes_id, status=current.status if current else AnalysisStatus.PENDING)

    @app.get("/api/v1/analyses/{analysis_id}/notes", response_model=list[LearningNotes])
    def list_notes(analysis_id: str, request: Request):
        return svc(request).list_notes(analysis_id)

    @app.get("/api/v1/notes/{notes_id}", response_model=LearningNotes)
    def get_notes(notes_id: str, request: Request):
        notes = svc(request).get_notes(notes_id)
        if notes is None:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown notes id"})
        return notes

    @app.get("/api/v1/notes/{notes_id}/markdown", response_class=PlainTextResponse)
    def notes_markdown(notes_id: str, request: Request):
        text = svc(request).notes_markdown(notes_id)
        if text is None:
            raise HTTPException(404, {"code": "not_found", "message": "No finished notes with this id"})
        return PlainTextResponse(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="curio-notes-{notes_id}.md"'},
        )

    @app.post("/api/v1/analyses/{analysis_id}/questions", response_model=Answer)
    def ask(analysis_id: str, body: AskRequest, request: Request):
        # Synchronous: an answer takes seconds, not minutes, and runs beside analyses
        # (Ollama queues concurrent requests itself).
        try:
            return svc(request).ask(analysis_id, body.question)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown analysis id"}) from None
        finally:
            request.app.state.queue.touch()

    @app.get("/api/v1/analyses/{analysis_id}/questions", response_model=list[Answer])
    def conversation(analysis_id: str, request: Request):
        return svc(request).conversation(analysis_id)

    @app.delete("/api/v1/analyses/{analysis_id}/questions")
    def clear_conversation(analysis_id: str, request: Request):
        return {"deleted": svc(request).clear_conversation(analysis_id)}

    @app.get("/api/v1/queue", response_model=QueueStatus)
    def queue_status(request: Request):
        return request.app.state.queue.status()

    @app.post("/api/v1/queue/{analysis_id}/start", status_code=202, response_model=AnalyzeAccepted)
    def queue_start(analysis_id: str, request: Request):
        s = svc(request)
        try:
            s.start_queued(analysis_id)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown analysis id"}) from None
        jobs.submit(_run_safely, s, analysis_id)
        return AnalyzeAccepted(analysis_id=analysis_id, status=AnalysisStatus.PENDING)

    @app.delete("/api/v1/queue/{analysis_id}")
    def queue_remove(analysis_id: str, request: Request):
        if not svc(request).remove_queued(analysis_id):
            raise HTTPException(404, {"code": "not_found", "message": "No queued video with this id"})
        return {"removed": analysis_id}

    @app.get("/api/v1/videos/recent", response_model=list[RecentVideo])
    def recent(request: Request, limit: int = 10):
        return svc(request).recent(min(max(limit, 1), 50))

    @app.get("/api/v1/videos/{video_id}/transcript", response_model=list[Block])
    def transcript(video_id: str, request: Request):
        blocks = svc(request).blocks(video_id)
        if not blocks:
            raise HTTPException(404, {"code": "not_found", "message": "No stored transcript"})
        return blocks

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built React app (web/dist) at /, with client-side routes falling back
    to index.html. In development, Vite serves the UI and proxies /api here instead."""
    dist = PROJECT_ROOT / "web" / "dist"
    if not (dist / "index.html").exists():
        log.info("No built frontend at %s; run `npm run build` in web/ to serve the UI", dist)
        return
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, {"code": "not_found", "message": "Unknown API route"})
        file = (dist / path).resolve()
        if path and file.is_file() and file.is_relative_to(dist.resolve()):
            return FileResponse(file)
        return FileResponse(dist / "index.html")


def _run_safely(service: CurioService, analysis_id: str) -> None:
    try:
        service.run(analysis_id)
    except Exception:  # status is already recorded as failed by the service
        log.warning("Analysis %s failed", analysis_id, exc_info=True)


def _run_notes_safely(service: CurioService, notes_id: str) -> None:
    try:
        service.run_notes(notes_id)
    except Exception:
        log.warning("Notes %s failed", notes_id, exc_info=True)


app = create_app()
