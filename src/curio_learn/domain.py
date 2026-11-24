"""Domain models shared by every layer. Frontends consume these as JSON."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


def fmt_ts(seconds: float) -> str:
    """Format seconds as m:ss or h:mm:ss."""
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# --------------------------------------------------------------------------- video


class Chapter(BaseModel):
    title: str
    start_s: float
    end_s: float | None = None


class VideoMetadata(BaseModel):
    video_id: str
    title: str
    channel: str = ""
    duration_s: float
    description: str = ""
    thumbnail_url: str = ""
    upload_date: str = ""
    chapters: list[Chapter] = Field(default_factory=list)

    @computed_field
    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


class TranscriptCue(BaseModel):
    start_s: float
    duration_s: float
    text: str


class Transcript(BaseModel):
    video_id: str
    language: str
    is_generated: bool
    source: str = "youtube-transcript-api"
    cues: list[TranscriptCue]


class Block(BaseModel):
    """A normalized transcript unit (~20-45 s). The atomic unit of segment boundaries."""

    id: str
    index: int
    start_s: float
    end_s: float
    text: str


# --------------------------------------------------------------------- preferences


class LearningProfile(StrEnum):
    GENERAL = "general"
    BEGINNER = "beginner"
    TECHNICAL = "technical"
    EXECUTIVE = "executive"
    DEEP_DIVE = "deep_dive"


class LearningPreferences(BaseModel):
    profile: LearningProfile = LearningProfile.GENERAL
    custom_objective: str = ""
    target_minutes: float | None = Field(default=None, ge=1, le=600)

    def cache_key(self) -> str:
        target = "" if self.target_minutes is None else f"{self.target_minutes:g}"
        return f"{self.profile.value}|{self.custom_objective.strip().lower()}|{target}"


# ------------------------------------------------------------------------ analysis


class SectionKind(StrEnum):
    CORE = "core"  # main explanation of a concept
    EXAMPLE = "example"  # worked example / case study
    DEMO = "demo"  # live demonstration, code walkthrough
    INTRO = "intro"  # course logistics, speaker intro, agenda
    RECAP = "recap"  # summary of what was already said
    TANGENT = "tangent"  # off-topic digression
    SPONSOR = "sponsor"  # ads, self-promotion
    QA = "qa"  # audience questions
    META = "meta"  # housekeeping, technical difficulties, pauses
    OUTRO = "outro"  # closing remarks


class Importance(StrEnum):
    ESSENTIAL = "essential"
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"
    SKIP = "skip"


class Section(BaseModel):
    id: str
    start_block: int
    end_block: int  # inclusive
    start_s: float
    end_s: float
    title: str
    summary: str
    kind: SectionKind
    key_concepts: list[str] = Field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


class Rubric(BaseModel):
    educational_value: float
    novelty: float
    practical: float
    depth: float
    relevance: float


class SectionEvaluation(BaseModel):
    section_id: str
    rubric: Rubric
    standalone: bool = True
    introduces_concepts: list[str] = Field(default_factory=list)
    builds_on: list[str] = Field(default_factory=list)
    redundant_with: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    what_you_learn: str = ""
    why_valuable: str = ""
    skip_reason: str = ""


class ScoreAdjustment(BaseModel):
    rule: str
    delta: float
    reason: str


class ScoreBreakdown(BaseModel):
    weighted_rubric: float  # 0-100
    adjustments: list[ScoreAdjustment] = Field(default_factory=list)
    final: float  # 0-100


class ScoredSection(BaseModel):
    section: Section
    evaluation: SectionEvaluation
    score: ScoreBreakdown
    importance: Importance


class LearningSegment(BaseModel):
    id: str
    section_ids: list[str]
    title: str
    start_s: float
    end_s: float
    summary: str
    what_you_learn: str
    why_valuable: str
    prerequisites: list[str] = Field(default_factory=list)
    importance: Importance
    score: float
    kind: SectionKind
    in_playlist: bool = True

    @computed_field
    @property
    def duration_s(self) -> float:
        return round(self.end_s - self.start_s, 2)

    @computed_field
    @property
    def timestamp(self) -> str:
        return f"{fmt_ts(self.start_s)}–{fmt_ts(self.end_s)}"


class SkippedSection(BaseModel):
    section_id: str
    title: str
    start_s: float
    end_s: float
    reason: str


class Playlist(BaseModel):
    segments: list[LearningSegment]
    budget_s: float
    total_s: float
    original_s: float
    coverage_estimate: float = Field(
        description="AI estimate (0-1) of the video's learning value covered; not an objective measure."
    )
    skipped: list[SkippedSection] = Field(default_factory=list)

    @computed_field
    @property
    def time_saved_s(self) -> float:
        return round(max(0.0, self.original_s - self.total_s), 2)


class ValidationReport(BaseModel):
    ok: bool = True
    warnings: list[str] = Field(default_factory=list)
    repairs: list[str] = Field(default_factory=list)


class AnalysisStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class LLMInfo(BaseModel):
    provider: str
    models: dict[str, str]


class VideoAnalysis(BaseModel):
    id: str
    video: VideoMetadata
    preferences: LearningPreferences
    status: AnalysisStatus = AnalysisStatus.PENDING
    progress: str = ""
    error: str = ""
    overview: str = ""
    key_concepts: list[str] = Field(default_factory=list)
    sections: list[ScoredSection] = Field(default_factory=list)
    segments: list[LearningSegment] = Field(default_factory=list)
    playlist: Playlist | None = None
    validation: ValidationReport = Field(default_factory=ValidationReport)
    llm: LLMInfo | None = None
    transcript_language: str = ""
    transcript_generated: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    elapsed_s: float = 0.0


# --------------------------------------------------------------------------- notes


class NotesScope(StrEnum):
    PLAYLIST = "playlist"  # the sections in the curated playlist
    VIDEO = "video"  # every section except sponsor reads and housekeeping
    SELECTED = "selected"  # segments the learner picked


class NotePoint(BaseModel):
    """One statement from the video, at the real cue time where it is said."""

    text: str
    start_s: float


class Definition(BaseModel):
    term: str
    definition: str
    start_s: float
    section_id: str = ""


class SectionNotes(BaseModel):
    section_id: str
    title: str
    start_s: float
    end_s: float
    points: list[NotePoint] = Field(default_factory=list)
    explanation: str = ""
    definitions: list[Definition] = Field(default_factory=list)


class Takeaway(BaseModel):
    """A takeaway or recommendation, tied to the sections it comes from."""

    text: str
    section_ids: list[str] = Field(default_factory=list)
    start_s: float = 0.0


class LearningNotes(BaseModel):
    id: str
    analysis_id: str
    video_id: str
    scope: NotesScope
    segment_ids: list[str] = Field(default_factory=list)
    status: AnalysisStatus = AnalysisStatus.PENDING
    progress: str = ""
    error: str = ""
    summary: str = ""
    takeaways: list[Takeaway] = Field(default_factory=list)
    sections: list[SectionNotes] = Field(default_factory=list)
    recommendations: list[Takeaway] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    llm: LLMInfo | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    elapsed_s: float = 0.0

    @computed_field
    @property
    def glossary(self) -> list[Definition]:
        """Every definition, first occurrence of each term, in video order."""
        seen: set[str] = set()
        out: list[Definition] = []
        for s in self.sections:
            for d in s.definitions:
                key = d.term.strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    out.append(d)
        return out

    @computed_field
    @property
    def watch_s(self) -> float:
        return round(sum(s.end_s - s.start_s for s in self.sections), 2)
