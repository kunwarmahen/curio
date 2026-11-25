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
    QUEUED = "queued"  # waiting for the overnight queue
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
    COURSE = "course"  # sections in a course playlist (ids of sections, not segments)


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


# ---------------------------------------------------------------- ask the video


class Citation(BaseModel):
    """Where the video says it: the start of the best-matching sentence (a real cue time).
    In a course answer, also which lecture."""

    start_s: float
    video_id: str = ""
    analysis_id: str = ""
    lecture: int = 0  # position in the course; 0 outside a course


class AnswerPart(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)


class Answer(BaseModel):
    id: str
    analysis_id: str
    question: str
    parts: list[AnswerPart] = Field(default_factory=list)
    # False when the video doesn't address the question.
    in_video: bool = True
    # General knowledge that is NOT from the video, kept apart and labeled as such.
    outside_knowledge: str = ""
    # "full": the model saw the whole transcript; "excerpts": the passages search found.
    context: str = "full"
    search: str = ""
    warnings: list[str] = Field(default_factory=list)
    llm: LLMInfo | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    elapsed_s: float = 0.0


# ------------------------------------------------------------ quiz and flashcards


class QuizQuestion(BaseModel):
    """A multiple-choice question answered in the video at `start_s` (a real cue time)."""

    id: str  # q1, q2, … in video order
    question: str
    options: list[str]
    answer: int  # index into options
    explanation: str = ""
    section_id: str
    start_s: float


class Flashcard(BaseModel):
    id: str  # c1, c2, … in video order
    front: str
    back: str
    section_id: str
    start_s: float


class StudySet(BaseModel):
    """A quiz and a deck of flashcards for one analysis and scope (as for notes)."""

    id: str
    analysis_id: str
    video_id: str
    scope: NotesScope
    segment_ids: list[str] = Field(default_factory=list)  # section ids for the course scope
    course_id: str = ""  # set for the course scope
    status: AnalysisStatus = AnalysisStatus.PENDING
    progress: str = ""
    error: str = ""
    questions: list[QuizQuestion] = Field(default_factory=list)
    cards: list[Flashcard] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    llm: LLMInfo | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    elapsed_s: float = 0.0


class QuizAttempt(BaseModel):
    id: str
    study_id: str
    answers: dict[str, int]  # question id -> chosen option index
    correct: list[str]  # ids of the questions answered correctly
    score: int
    total: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CardGrade(StrEnum):
    AGAIN = "again"  # forgot: see it again in a few minutes
    GOOD = "good"  # remembered: next box
    EASY = "easy"  # remembered easily: skip a box


class CardState(BaseModel):
    """Spaced-repetition state of one flashcard (Leitner boxes; box 0 = new)."""

    study_id: str
    card_id: str
    box: int = 0
    due_at: datetime | None = None  # None = new, due now
    reviews: int = 0
    lapses: int = 0


class DueCard(BaseModel):
    card: Flashcard
    state: CardState
    study_id: str
    analysis_id: str
    video_title: str
    video_url: str


class FlashcardDeck(BaseModel):
    cards: list[DueCard]  # due now, most overdue first, then new cards
    due: int  # cards due now (including new ones)
    total: int  # cards in the deck
    next_due_at: datetime | None = None  # when the next card not due now comes due


# ------------------------------------------------------------------------- courses


class LectureState(StrEnum):
    READY = "ready"  # analyzed with the course goal
    OTHER_GOAL = "other_goal"  # analyzed only with another goal (used until the course goal's run is done)
    QUEUED = "queued"
    ANALYZING = "analyzing"
    FAILED = "failed"
    MISSING = "missing"  # not analyzed or queued (e.g. no captions)


class CourseLecture(BaseModel):
    position: int  # 1-based, in course order
    label: str = ""  # "Lecture 3" when the titles number the lectures, "Guest lecture", else "#3"
    video_id: str
    title: str
    duration_s: float = 0.0
    state: LectureState = LectureState.MISSING
    analysis_id: str | None = None  # the finished analysis the course uses
    pending_id: str | None = None  # a queued or running analysis with the course goal
    error: str = ""

    @computed_field
    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


class CourseRef(BaseModel):
    """A section of one lecture."""

    lecture: int
    video_id: str
    analysis_id: str
    section_id: str
    title: str
    start_s: float
    end_s: float


class CoursePart(BaseModel):
    """One stretch of the course playlist: part of one lecture."""

    key: str  # stable across rebuilds: "<video_id>@<start seconds>"
    lecture: int
    video_id: str
    analysis_id: str
    section_ids: list[str]
    title: str
    start_s: float
    end_s: float
    what_you_learn: str
    why_valuable: str = ""
    importance: Importance
    score: float
    also_in: list[CourseRef] = Field(default_factory=list)  # related sections in other lectures

    @computed_field
    @property
    def duration_s(self) -> float:
        return round(self.end_s - self.start_s, 2)


class CourseRepeat(BaseModel):
    """A section left out because another lecture teaches the same thing."""

    section: CourseRef
    covered_by: CourseRef
    reason: str


class CoursePair(BaseModel):
    """Two sections of different lectures the model compared (a is the earlier)."""

    a: CourseRef
    b: CourseRef
    relation: str  # "repeat", "overlap", "different", or "" when no verdict came back
    keep: str = ""  # "A" or "B": the one to watch if only one
    reason: str = ""
    similarity: float = 0.0
    a_in_playlist: bool = False
    b_in_playlist: bool = False


class ChecklistItem(BaseModel):
    text: str
    why: str = ""
    refs: list[CourseRef]
    in_playlist: bool = False  # at least one cited section is in the course playlist


class CoursePlan(BaseModel):
    id: str
    course_id: str
    status: AnalysisStatus = AnalysisStatus.PENDING
    progress: str = ""
    error: str = ""
    budget_s: float = 0.0
    total_s: float = 0.0
    original_s: float = 0.0  # length of the lectures it was built from
    analysis_ids: list[str] = Field(default_factory=list)  # the inputs, in course order
    parts: list[CoursePart] = Field(default_factory=list)
    repeats: list[CourseRepeat] = Field(default_factory=list)
    pairs: list[CoursePair] = Field(default_factory=list)  # every pair judged, closest first
    checklist: list[ChecklistItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    llm: LLMInfo | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    elapsed_s: float = 0.0


class Course(BaseModel):
    id: str
    title: str
    channel: str = ""
    playlist_id: str = ""  # empty for a course grouped by hand
    preferences: LearningPreferences
    lectures: list[CourseLecture]
    unavailable: int = 0  # playlist entries that are private or deleted
    importing: bool = False  # still checking transcripts and queuing lectures
    plan: CoursePlan | None = None  # the newest plan
    plan_stale: bool = False  # lectures were analyzed (or changed) since the plan was built
    # The learner's own course playlist, when they saved one (None: Curio's picks).
    custom_parts: list[CoursePart] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field
    @property
    def url(self) -> str:
        return f"https://www.youtube.com/playlist?list={self.playlist_id}" if self.playlist_id else ""


class CourseSectionChoice(BaseModel):
    """A section the learner can put in their own course playlist."""

    lecture: int
    video_id: str
    analysis_id: str
    section_id: str
    title: str
    start_s: float
    end_s: float
    importance: Importance
    curio_pick: bool = False  # in the course playlist Curio built
    picked: bool = False  # in the playlist shown now (the learner's, or else Curio's)
    pinned: bool = False  # kept in Curio's picks whenever the playlist is built
    watched: bool = False  # in a part the learner marked watched

    @computed_field
    @property
    def duration_s(self) -> float:
        return round(self.end_s - self.start_s, 2)


class CourseAnswer(BaseModel):
    """An answer from every lecture of a course, citing moments in specific lectures."""

    id: str
    course_id: str
    question: str
    parts: list[AnswerPart] = Field(default_factory=list)
    in_course: bool = True  # False when no lecture addresses the question
    outside_knowledge: str = ""
    context: str = "excerpts"  # "full": every lecture's whole transcript; "excerpts": search hits
    search: str = ""
    lectures: int = 0  # lectures searched
    warnings: list[str] = Field(default_factory=list)
    llm: LLMInfo | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    elapsed_s: float = 0.0


class CourseStudy(BaseModel):
    lecture: int  # position in the course
    study: StudySet


class CoursePractice(BaseModel):
    """A course's quiz and flashcards: one set per lecture of the course playlist."""

    course_id: str
    sets: list[CourseStudy] = Field(default_factory=list)  # in lecture order
    status: AnalysisStatus | None = None  # None: never written
    progress: str = ""
    stale: bool = False  # the course playlist changed since they were written
    questions: int = 0
    cards: int = 0
