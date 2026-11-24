"""SQLAlchemy tables. SQLite by default; PostgreSQL via CURIO_DATABASE_URL."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class VideoRow(Base):
    __tablename__ = "videos"

    video_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text, default="")
    duration_s: Mapped[float] = mapped_column(Float)
    metadata_json: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class TranscriptRow(Base):
    __tablename__ = "transcripts"

    video_id: Mapped[str] = mapped_column(ForeignKey("videos.video_id"), primary_key=True)
    language: Mapped[str] = mapped_column(String(16))
    is_generated: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(64))
    transcript_json: Mapped[dict] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AnalysisRow(Base):
    __tablename__ = "analyses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.video_id"), index=True)
    preferences_key: Mapped[str] = mapped_column(Text, index=True)
    preferences_json: Mapped[dict] = mapped_column(JSON)
    llm_key: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), index=True)
    progress: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class SegmentRow(Base):
    """One row per learning segment, so progress tracking can attach to it later."""

    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), index=True)
    segment_id: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(Text)
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    importance: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(Float)
    in_playlist: Mapped[bool] = mapped_column(Boolean)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)


class NotesRow(Base):
    """Learning notes for one analysis and scope (playlist, whole video, or picked segments)."""

    __tablename__ = "notes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(16))
    scope_key: Mapped[str] = mapped_column(Text, index=True)
    segment_ids: Mapped[list] = mapped_column(JSON, default=list)
    llm_key: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), index=True)
    progress: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    notes_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class QuestionRow(Base):
    """One question and its answer, in the conversation about an analysis."""

    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EmbeddingRow(Base):
    """Embedding vectors of a video's transcript blocks, for one embedding model."""

    __tablename__ = "embeddings"

    video_id: Mapped[str] = mapped_column(ForeignKey("videos.video_id"), primary_key=True)
    model: Mapped[str] = mapped_column(String(100), primary_key=True)
    # Hash of the block texts, so a changed transcript or blocking invalidates the vectors.
    blocks_key: Mapped[str] = mapped_column(String(64))
    vectors: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
