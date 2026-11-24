import { useState } from 'react'
import { ApiError, api, type Course, type RecentVideo } from '../api'
import { duration, shortTitle } from '../format'
import { linkProps, navigate } from '../router'

function courseLine(c: Course): string {
  const ready = c.lectures.filter((l) => l.analysis_id).length
  const waiting = c.lectures.filter((l) => l.state === 'queued' || l.state === 'analyzing' || l.pending_id).length
  const parts = [`${ready} of ${c.lectures.length} lectures analyzed`]
  if (waiting) parts.push(`${waiting} queued`)
  if (c.plan?.status === 'done') parts.push(`course playlist ${duration(c.plan.total_s)}`)
  return parts.join(', ') + '.'
}

/** Courses on the home page, and making one from videos already analyzed. */
export function CourseList({ courses, recent }: { courses: Course[]; recent: RecentVideo[] }) {
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  if (!courses.length && recent.length < 2) return null

  const toggle = (id: string, on: boolean) =>
    setPicked((prev) => {
      const next = new Set(prev)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })

  const make = async () => {
    setBusy(true)
    setErr('')
    try {
      const { course_id } = await api.groupCourse(title, [...picked])
      navigate(`/c/${course_id}`)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not make the course.')
      setBusy(false)
    }
  }

  return (
    <section className="courses" aria-labelledby="courses-heading">
      <h2 id="courses-heading">Courses</h2>
      {courses.length === 0 && (
        <p className="muted">
          Paste a YouTube playlist link above to add a whole course. Curio queues its lectures and builds one
          playlist from the best parts of all of them.
        </p>
      )}
      <ul className="courses__list">
        {courses.map((c) => (
          <li key={c.id}>
            <a className="courses__item" {...linkProps(`/c/${c.id}`)}>
              <span className="courses__title">{c.title}</span>
              <span className="courses__meta">{courseLine(c)}</span>
            </a>
          </li>
        ))}
      </ul>
      {recent.length >= 2 && (
        <details className="courses__group">
          <summary>Make a course from videos you've analyzed</summary>
          <ul className="courses__pick">
            {recent.map((r) => (
              <li key={r.analysis_id}>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={picked.has(r.analysis_id)}
                    onChange={(e) => toggle(r.analysis_id, e.target.checked)}
                  />
                  {shortTitle(r.video.title)}
                </label>
              </li>
            ))}
          </ul>
          <div className="courses__group-row">
            <label className="visually-hidden" htmlFor="course-title">
              Course name
            </label>
            <input
              id="course-title"
              type="text"
              placeholder="Course name (optional)"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
            <button type="button" className="button-primary" onClick={make} disabled={busy || picked.size < 2}>
              {busy ? 'Making…' : `Make a course${picked.size >= 2 ? ` of ${picked.size} videos` : ''}`}
            </button>
          </div>
          {err && (
            <p className="form-error" role="alert">
              {err}
            </p>
          )}
        </details>
      )}
    </section>
  )
}
