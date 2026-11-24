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

/** "01:00" -> "1 AM", "22:30" -> "10:30 PM" (in the viewer's locale). */
export function localTime(hhmm: string): string {
  const [h, m] = hhmm.split(':').map(Number)
  const d = new Date(2000, 0, 1, h, m)
  return d.toLocaleTimeString([], { hour: 'numeric', minute: m ? '2-digit' : undefined })
}

/** When queued videos are analyzed: "between 1 AM and 7 AM" or "whenever". */
export function queueHours(start: string, end: string): string {
  return start ? `between ${localTime(start)} and ${localTime(end)}` : 'at any time'
}

/** Days until a flashcard in each Leitner box comes back (mirrors analysis/study.py). */
export const BOX_DAYS: Record<number, number> = { 1: 1, 2: 3, 3: 7, 4: 16, 5: 35, 6: 90 }

/** "10 min", "1 day", "3 days", "3 months". */
export function intervalLabel(days: number): string {
  if (days < 1) return `${Math.round(days * 24 * 60)} min`
  if (days >= 60) return `${Math.round(days / 30)} months`
  return days === 1 ? '1 day' : `${days} days`
}

/** "in 8 minutes", "in 5 hours", "tomorrow", "in 3 days". */
export function until(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now()
  const min = Math.max(1, Math.round(ms / 60000))
  if (min < 60) return `in ${min} minute${min === 1 ? '' : 's'}`
  const h = Math.round(min / 60)
  if (h < 20) return `in ${h} hour${h === 1 ? '' : 's'}`
  const d = Math.round(h / 24)
  return d <= 1 ? 'tomorrow' : `in ${d} days`
}
