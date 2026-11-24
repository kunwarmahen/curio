// Types mirror the backend's Pydantic models (src/curio_learn/domain.py).

export type Importance = 'essential' | 'recommended' | 'optional' | 'skip'
export type Profile = 'general' | 'beginner' | 'technical' | 'executive' | 'deep_dive'
export type Status = 'pending' | 'running' | 'done' | 'failed'
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

export type NotesScope = 'playlist' | 'video' | 'selected'

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
  analyze: (url: string, preferences: Preferences, force = false) =>
    request<{ analysis_id: string; status: Status }>('/analyses', {
      method: 'POST',
      body: JSON.stringify({ url, preferences, force }),
    }),
  analysis: (id: string) => request<Analysis>(`/analyses/${encodeURIComponent(id)}`),
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
  recent: () => request<RecentVideo[]>('/videos/recent?limit=12'),
}
