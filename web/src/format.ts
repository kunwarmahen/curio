import type { Importance, Profile } from './api'

/** 272 -> "4:32", 3725 -> "1:02:05" (timestamps) */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = String(s % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`
}

/** 4834 -> "1 h 21 min", 1445 -> "24 min", 40 -> "40 s" (durations) */
export function duration(seconds: number): string {
  const s = Math.round(seconds)
  if (s < 60) return `${s} s`
  const totalMin = Math.round(s / 60)
  const h = Math.floor(totalMin / 60)
  const m = totalMin % 60
  if (!h) return `${m} min`
  return m ? `${h} h ${m} min` : `${h} h`
}

export const PROFILES: Record<Profile, { label: string; hint: string }> = {
  general: { label: 'General', hint: 'The most important ideas, balanced between foundations and examples.' },
  beginner: { label: 'Beginner', hint: 'Clear foundations and definitions. Intros and recaps can help.' },
  technical: { label: 'Technical', hint: 'Implementation, architecture, code and demos. Less motivation, more how.' },
  executive: { label: 'Executive', hint: 'Strategy, trade-offs, decisions and conclusions, in less time.' },
  deep_dive: { label: 'Deep dive', hint: 'Advanced ideas, subtle arguments and expert detail. A longer playlist.' },
}

export const IMPORTANCE_LABEL: Record<Importance, string> = {
  essential: 'Essential',
  recommended: 'Recommended',
  optional: 'Optional',
  skip: 'Skip',
}
