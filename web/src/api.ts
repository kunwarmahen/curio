// Types mirror the backend's Pydantic models (src/curio_learn/domain.py).

export type Importance = 'essential' | 'recommended' | 'optional' | 'skip'
export type Profile = 'general' | 'beginner' | 'technical' | 'executive' | 'deep_dive'
export type Status = 'queued' | 'pending' | 'running' | 'done' | 'failed'
export type SectionKind =
  | 'core' | 'example' | 'demo' | 'intro' | 'recap' | 'tangent' | 'sponsor' | 'qa' | 'meta' | 'outro'

export interface VideoMetadata {
  video_id: string
  title: string
  channel: string
  duration_s: number
  thumbnail_url: string
  url: string
}

export interface Preferences {
  profile: Profile
  custom_objective: string
  target_minutes: number | null
}

export interface Section {
  id: string
  start_s: number
  end_s: number
  title: string
  summary: string
  kind: SectionKind
  key_concepts: string[]
}

export interface ScoreAdjustment {
  rule: string
  delta: number
  reason: string
}

export interface ScoredSection {
  section: Section
  evaluation: {
    rubric: Record<'educational_value' | 'novelty' | 'practical' | 'depth' | 'relevance', number>
    what_you_learn: string
    why_valuable: string
  }
  score: { weighted_rubric: number; adjustments: ScoreAdjustment[]; final: number }
  importance: Importance
}

export interface Segment {
  id: string
  section_ids: string[]
  title: string
  start_s: number
  end_s: number
  duration_s: number
  summary: string
  what_you_learn: string
  why_valuable: string
  prerequisites: string[]
  importance: Importance
  score: number
  kind: SectionKind
  in_playlist: boolean
}

export interface Skipped {
  section_id: string
  title: string
  start_s: number
  end_s: number
  reason: string
}

export interface Playlist {
  segments: Segment[]
  budget_s: number
  total_s: number
  original_s: number
  time_saved_s: number
  coverage_estimate: number
  skipped: Skipped[]
}

export interface Analysis {
  id: string
  video: VideoMetadata
  preferences: Preferences
  status: Status
  progress: string
  error: string
  overview: string
  key_concepts: string[]
  sections: ScoredSection[]
  segments: Segment[]
  playlist: Playlist | null
  validation: { ok: boolean; warnings: string[]; repairs: string[] }
  llm: { provider: string; models: Record<string, string> } | null
  transcript_language: string
  transcript_generated: boolean
  elapsed_s: number
}

export interface MapStretch {
  start_s: number
  end_s: number
  importance: Importance
  in_playlist: boolean
}

export interface RecentVideo {
  video: VideoMetadata
  analysis_id: string
  profile: Profile
  analyzed_at: string
  playlist_s: number
  watch_map: MapStretch[]
}

export type NotesScope = 'playlist' | 'video' | 'selected' | 'course'

export interface NotePoint {
  text: string
  start_s: number
}

export interface Definition {
  term: string
  definition: string
  start_s: number
  section_id: string
}

export interface SectionNotes {
  section_id: string
  title: string
  start_s: number
  end_s: number
  points: NotePoint[]
  explanation: string
  definitions: Definition[]
}

export interface Takeaway {
  text: string
  section_ids: string[]
  start_s: number
}

export interface Notes {
  id: string
  analysis_id: string
  scope: NotesScope
  segment_ids: string[]
  status: Status
  progress: string
  error: string
  summary: string
  takeaways: Takeaway[]
  sections: SectionNotes[]
  recommendations: Takeaway[]
  glossary: Definition[]
  warnings: string[]
  watch_s: number
  created_at: string
  elapsed_s: number
}

export interface Citation {
  start_s: number
  /** In a course answer: which lecture. */
  video_id: string
  analysis_id: string
  lecture: number
}

export interface AnswerPart {
  text: string
  citations: Citation[]
}

export interface Answer {
  id: string
  analysis_id: string
  question: string
  parts: AnswerPart[]
  in_video: boolean
  outside_knowledge: string
  context: 'full' | 'excerpts'
  search: string
  warnings: string[]
  created_at: string
  elapsed_s: number
}

export interface CourseAnswer {
  id: string
  course_id: string
  question: string
  parts: AnswerPart[]
  in_course: boolean
  outside_knowledge: string
  context: 'full' | 'excerpts'
  search: string
  lectures: number
  warnings: string[]
  created_at: string
  elapsed_s: number
}

export interface QuizQuestion {
  id: string
  question: string
  options: string[]
  answer: number
  explanation: string
  section_id: string
  start_s: number
}

export interface Flashcard {
  id: string
  front: string
  back: string
  section_id: string
  start_s: number
}

export interface StudySet {
  id: string
  analysis_id: string
  video_id: string
  scope: NotesScope
  segment_ids: string[]
  course_id: string
  status: Status
  progress: string
  error: string
  questions: QuizQuestion[]
  cards: Flashcard[]
  warnings: string[]
  created_at: string
  elapsed_s: number
}

export interface CoursePractice {
  course_id: string
  sets: { lecture: number; study: StudySet }[]
  status: Status | null
  progress: string
  stale: boolean
  questions: number
  cards: number
}

export interface QuizAttempt {
  id: string
  study_id: string
  answers: Record<string, number>
  correct: string[]
  score: number
  total: number
  created_at: string
}

export type CardGrade = 'again' | 'good' | 'easy'

export interface CardState {
  study_id: string
  card_id: string
  box: number
  due_at: string | null
  reviews: number
  lapses: number
}

export interface DueCard {
  card: Flashcard
  state: CardState
  study_id: string
  analysis_id: string
  video_title: string
  video_url: string
}

export interface FlashcardDeck {
  cards: DueCard[]
  due: number
  total: number
  next_due_at: string | null
}

export interface QueueItem {
  analysis_id: string
  video: VideoMetadata
  preferences: Preferences
  status: Status
  progress: string
  added_at: string
}

export interface QueueStatus {
  /** Local "HH:MM"; both empty when the queue runs any time the GPU is idle. */
  window_start: string
  window_end: string
  state: 'empty' | 'busy' | 'waiting_window' | 'waiting_gpu' | 'starting'
  message: string
  next_start: string | null
  items: QueueItem[]
}

export type LectureState = 'ready' | 'other_goal' | 'queued' | 'analyzing' | 'failed' | 'missing'

export interface CourseLecture {
  position: number
  label: string
  video_id: string
  title: string
  duration_s: number
  state: LectureState
  analysis_id: string | null
  pending_id: string | null
  error: string
  url: string
}

export interface CourseRef {
  lecture: number
  video_id: string
  analysis_id: string
  section_id: string
  title: string
  start_s: number
  end_s: number
}

export interface CoursePart {
  key: string
  lecture: number
  video_id: string
  analysis_id: string
  section_ids: string[]
  title: string
  start_s: number
  end_s: number
  duration_s: number
  what_you_learn: string
  why_valuable: string
  importance: Importance
  score: number
  also_in: CourseRef[]
}

export interface CourseRepeat {
  section: CourseRef
  covered_by: CourseRef
  reason: string
}

export interface CoursePair {
  a: CourseRef
  b: CourseRef
  relation: 'repeat' | 'overlap' | 'different' | ''
  keep: 'A' | 'B' | ''
  reason: string
  similarity: number
  a_in_playlist: boolean
  b_in_playlist: boolean
}

export interface CourseNeed {
  section: CourseRef // in the playlist; builds on something another source teaches
  prerequisite: string
  taught_by: CourseRef
  status: 'in_playlist' | 'added' | 'untaught'
  made_room: CourseRef[] // left out so `taught_by` fit
  why?: '' | 'worth' | 'share' | 'repeat' | 'skip' // why an untaught need wasn't taught
}

export interface ChecklistItem {
  text: string
  why: string
  refs: CourseRef[]
  in_playlist: boolean
}

export interface CoursePlan {
  id: string
  course_id: string
  status: Status
  progress: string
  error: string
  budget_s: number
  total_s: number
  original_s: number
  analysis_ids: string[]
  parts: CoursePart[]
  repeats: CourseRepeat[]
  pairs?: CoursePair[] // missing on plans built before the comparisons were kept
  checklist: ChecklistItem[]
  needs?: CourseNeed[] // a curriculum's needs across sources; missing on older plans
  order?: CurriculumOrder // a curriculum section by section plays its parts in that order
  warnings: string[]
  elapsed_s: number
}

export interface Course {
  id: string
  title: string
  channel: string
  playlist_id: string
  preferences: Preferences
  lectures: CourseLecture[]
  unavailable: number
  importing: boolean
  plan: CoursePlan | null
  plan_stale: boolean
  custom_parts?: CoursePart[] | null // the learner's own course playlist, when saved
  curriculum?: CurriculumInfo | null // set when the course is a curriculum
  url: string
}

export type CurriculumOrder = 'lectures' | 'sections'

/** What makes a course a curriculum. */
export interface CurriculumInfo {
  goal: string
  order: CurriculumOrder
  auto_order: boolean // Curio orders the lectures; off once the learner reorders them
  notes: string[] // why a lecture or video was moved
}

/** A course or video Curio could put in a curriculum. */
export interface CurriculumCandidate {
  kind: 'course' | 'video'
  id: string
  title: string
  detail: string
  suggested: boolean
  reason: string
}

/** A section the learner can put in their own course playlist. */
export interface CourseSectionChoice {
  lecture: number
  video_id: string
  analysis_id: string
  section_id: string
  title: string
  start_s: number
  end_s: number
  duration_s: number
  importance: Importance
  curio_pick: boolean
  picked: boolean
  pinned: boolean // kept in Curio's picks whenever the playlist is built
  watched: boolean // in a part marked watched
}

export class ApiError extends Error {
  code: string
  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`/api/v1${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init?.headers },
    })
  } catch {
    throw new ApiError('network', "Can't reach the Curio server. Is `uv run curio api` running?")
  }
  const body = await res.json().catch(() => null)
  if (!res.ok) {
    const detail = body?.detail
    if (detail && typeof detail === 'object' && 'message' in detail) {
      throw new ApiError(detail.code ?? 'error', detail.message)
    }
    throw new ApiError('error', `The server answered ${res.status}.`)
  }
  return body as T
}

export const api = {
  analyze: (url: string, preferences: Preferences, force = false, queue = false) =>
    request<{ analysis_id: string; status: Status }>('/analyses', {
      method: 'POST',
      body: JSON.stringify({ url, preferences, force, queue }),
    }),
  queue: () => request<QueueStatus>('/queue'),
  defaultPreferences: () => request<Preferences>('/preferences/default'),
  setDefaultPreferences: (preferences: Preferences) =>
    request<Preferences>('/preferences/default', { method: 'PUT', body: JSON.stringify({ preferences }) }),
  retargetQueue: (preferences: Preferences, analysisIds: string[] = []) =>
    request<{ updated: string[]; already_analyzed: string[] }>('/queue/preferences', {
      method: 'POST',
      body: JSON.stringify({ preferences, analysis_ids: analysisIds }),
    }),
  queueStart: (id: string) =>
    request<{ analysis_id: string; status: Status }>(`/queue/${encodeURIComponent(id)}/start`, { method: 'POST' }),
  queueRemove: (id: string) => request<{ removed: string }>(`/queue/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  analysis: (id: string) => request<Analysis>(`/analyses/${encodeURIComponent(id)}`),
  customPlaylist: (id: string) =>
    request<{ segment_ids: string[] | null }>(`/analyses/${encodeURIComponent(id)}/custom-playlist`),
  setCustomPlaylist: (id: string, segmentIds: string[] | null) =>
    request<{ segment_ids: string[] | null }>(`/analyses/${encodeURIComponent(id)}/custom-playlist`, {
      method: 'PUT',
      body: JSON.stringify({ segment_ids: segmentIds }),
    }),
  completed: (id: string) => request<string[]>(`/analyses/${encodeURIComponent(id)}/completed`),
  setCompleted: (id: string, segmentId: string, completed: boolean) =>
    request(`/analyses/${encodeURIComponent(id)}/segments/${encodeURIComponent(segmentId)}/completed`, {
      method: 'PUT',
      body: JSON.stringify({ completed }),
    }),
  notesList: (analysisId: string) => request<Notes[]>(`/analyses/${encodeURIComponent(analysisId)}/notes`),
  writeNotes: (analysisId: string, scope: NotesScope, segmentIds: string[] = [], force = false) =>
    request<{ notes_id: string; status: Status }>(`/analyses/${encodeURIComponent(analysisId)}/notes`, {
      method: 'POST',
      body: JSON.stringify({ scope, segment_ids: segmentIds, force }),
    }),
  notes: (id: string) => request<Notes>(`/notes/${encodeURIComponent(id)}`),
  notesMarkdownUrl: (id: string) => `/api/v1/notes/${encodeURIComponent(id)}/markdown`,
  conversation: (analysisId: string) => request<Answer[]>(`/analyses/${encodeURIComponent(analysisId)}/questions`),
  ask: (analysisId: string, question: string) =>
    request<Answer>(`/analyses/${encodeURIComponent(analysisId)}/questions`, {
      method: 'POST',
      body: JSON.stringify({ question }),
    }),
  clearConversation: (analysisId: string) =>
    request<{ deleted: number }>(`/analyses/${encodeURIComponent(analysisId)}/questions`, { method: 'DELETE' }),
  recent: (limit = 12) => request<RecentVideo[]>(`/videos/recent?limit=${limit}`),
  studyList: (analysisId: string) => request<StudySet[]>(`/analyses/${encodeURIComponent(analysisId)}/study`),
  writeStudy: (analysisId: string, scope: NotesScope, force = false) =>
    request<{ study_id: string; status: Status }>(`/analyses/${encodeURIComponent(analysisId)}/study`, {
      method: 'POST',
      body: JSON.stringify({ scope, force }),
    }),
  study: (id: string) => request<StudySet>(`/study/${encodeURIComponent(id)}`),
  gradeQuiz: (id: string, answers: Record<string, number>) =>
    request<QuizAttempt>(`/study/${encodeURIComponent(id)}/attempts`, {
      method: 'POST',
      body: JSON.stringify({ answers }),
    }),
  quizAttempts: (id: string) => request<QuizAttempt[]>(`/study/${encodeURIComponent(id)}/attempts`),
  /** Due cards from every video's newest set, or from one set. */
  /** Due cards from every deck, one set's, or one course's. */
  flashcards: (studyId?: string, limit = 50, courseId?: string) =>
    request<FlashcardDeck>(
      `/flashcards?limit=${limit}${studyId ? `&study_id=${encodeURIComponent(studyId)}` : ''}${
        courseId ? `&course_id=${encodeURIComponent(courseId)}` : ''
      }`,
    ),
  coursePractice: (id: string) => request<CoursePractice>(`/courses/${encodeURIComponent(id)}/practice`),
  writeCoursePractice: (id: string, force = false) =>
    request<CoursePractice>(`/courses/${encodeURIComponent(id)}/practice`, {
      method: 'POST',
      body: JSON.stringify({ force }),
    }),
  reviewCard: (studyId: string, cardId: string, grade: CardGrade) =>
    request<CardState>('/flashcards/review', {
      method: 'POST',
      body: JSON.stringify({ study_id: studyId, card_id: cardId, grade }),
    }),
  courses: () => request<Course[]>('/courses'),
  course: (id: string) => request<Course>(`/courses/${encodeURIComponent(id)}`),
  addCourse: (url: string, preferences: Preferences) =>
    request<{ course_id: string }>('/courses', { method: 'POST', body: JSON.stringify({ url, preferences }) }),
  groupCourse: (title: string, analysisIds: string[]) =>
    request<{ course_id: string }>('/courses/group', {
      method: 'POST',
      body: JSON.stringify({ title, analysis_ids: analysisIds }),
    }),
  curriculumCandidates: () => request<CurriculumCandidate[]>('/curricula/candidates'),
  suggestCurriculum: (goal: string) =>
    request<CurriculumCandidate[]>('/curricula/suggest', { method: 'POST', body: JSON.stringify({ goal }) }),
  createCurriculum: (
    goal: string,
    sources: { kind: 'course' | 'video'; id: string }[],
    order: CurriculumOrder,
    title = '',
  ) =>
    request<{ course_id: string }>('/curricula', {
      method: 'POST',
      body: JSON.stringify({ goal, sources, order, title }),
    }),
  setCurriculum: (id: string, settings: { order?: CurriculumOrder; auto_order?: boolean }) =>
    request<Course>(`/courses/${encodeURIComponent(id)}/curriculum`, {
      method: 'PUT',
      body: JSON.stringify(settings),
    }),
  combineCourses: (title: string, courseIds: string[]) =>
    request<{ course_id: string }>('/courses/combine', {
      method: 'POST',
      body: JSON.stringify({ title, course_ids: courseIds }),
    }),
  deleteCourse: (id: string) => request<{ deleted: string }>(`/courses/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  refreshCourse: (id: string) =>
    request<{ course_id: string }>(`/courses/${encodeURIComponent(id)}/refresh`, { method: 'POST' }),
  buildCoursePlan: (id: string, minutes: number | null, force = false) =>
    request<{ plan_id: string; status: Status }>(`/courses/${encodeURIComponent(id)}/plan`, {
      method: 'POST',
      body: JSON.stringify({ minutes, force }),
    }),
  courseConversation: (id: string) => request<CourseAnswer[]>(`/courses/${encodeURIComponent(id)}/questions`),
  askCourse: (id: string, question: string) =>
    request<CourseAnswer>(`/courses/${encodeURIComponent(id)}/questions`, {
      method: 'POST',
      body: JSON.stringify({ question }),
    }),
  clearCourseConversation: (id: string) =>
    request<{ deleted: number }>(`/courses/${encodeURIComponent(id)}/questions`, { method: 'DELETE' }),
  coursePlaylistSections: (id: string) =>
    request<CourseSectionChoice[]>(`/courses/${encodeURIComponent(id)}/playlist-sections`),
  setCoursePlaylist: (id: string, sections: string[] | null) =>
    request<Course>(`/courses/${encodeURIComponent(id)}/custom-playlist`, {
      method: 'PUT',
      body: JSON.stringify({ sections }),
    }),
  setCoursePins: (id: string, sections: string[]) =>
    request<string[]>(`/courses/${encodeURIComponent(id)}/pins`, {
      method: 'PUT',
      body: JSON.stringify({ sections }),
    }),
  reorderLectures: (id: string, videoIds: string[]) =>
    request<Course>(`/courses/${encodeURIComponent(id)}/lectures/order`, {
      method: 'PUT',
      body: JSON.stringify({ video_ids: videoIds }),
    }),
  courseWatched: (id: string) => request<string[]>(`/courses/${encodeURIComponent(id)}/watched`),
  setCourseWatched: (id: string, partKey: string, watched: boolean) =>
    request(`/courses/${encodeURIComponent(id)}/watched`, {
      method: 'PUT',
      body: JSON.stringify({ part_key: partKey, watched }),
    }),
}
