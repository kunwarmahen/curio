"""FastAPI app exposing CurioService over HTTP (for a future React frontend)."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from curio_learn.config import PROJECT_ROOT, get_settings
from curio_learn.domain import AnalysisStatus, Block, LearningPreferences, VideoAnalysis
from curio_learn.errors import CurioError, InvalidURL
from curio_learn.logging_setup import configure_logging
from curio_learn.service import CurioService
from curio_learn.storage.repository import RecentVideo

log = logging.getLogger(__name__)


class AnalyzeRequest(BaseModel):
    url: str
    preferences: LearningPreferences = LearningPreferences()
    force: bool = False


class AnalyzeAccepted(BaseModel):
    analysis_id: str
    status: AnalysisStatus


class CompletedRequest(BaseModel):
    completed: bool


_STATUS = {
    "invalid_url": 422,
    "video_unavailable": 404,
    "transcript_unavailable": 422,
    "rate_limited": 429,
    "llm_error": 502,
}


def create_app(service: CurioService | None = None) -> FastAPI:
    # One analysis at a time: local LLMs can't usefully run several in parallel.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="curio-analysis")

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
        yield
        executor.shutdown(wait=False, cancel_futures=True)

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
        }

    @app.post("/api/v1/analyses", status_code=202, response_model=AnalyzeAccepted)
    def analyze(body: AnalyzeRequest, request: Request):
        s = svc(request)
        if not body.url.strip():
            raise InvalidURL("A YouTube URL is required.")
        analysis_id, needs_run = s.submit(body.url, body.preferences, force=body.force)
        if needs_run:
            executor.submit(_run_safely, s, analysis_id)
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


app = create_app()
