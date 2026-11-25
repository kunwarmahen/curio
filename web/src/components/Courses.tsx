import { useState } from 'react'
import {
  ApiError,
  api,
  type Course,
  type CurriculumCandidate,
  type CurriculumOrder,
  type RecentVideo,
} from '../api'
import { duration, shortTitle } from '../format'
import { linkProps, navigate } from '../router'

function courseLine(c: Course): string {
  const ready = c.lectures.filter((l) => l.analysis_id).length
  const waiting = c.lectures.filter((l) => l.state === 'queued' || l.state === 'analyzing' || l.pending_id).length
  const parts = [`${ready} of ${c.lectures.length} ${c.curriculum ? 'lectures and videos' : 'lectures'} analyzed`]
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
              <span className="courses__title">
                {c.curriculum && <span className="courses__kind">Curriculum · </span>}
                {c.title}
              </span>
              <span className="courses__meta">{courseLine(c)}</span>
            </a>
          </li>
        ))}
      </ul>
      {(courses.length >= 1 || recent.length >= 2) && <PlanCurriculum />}
      {courses.length >= 2 && <CombineCourses courses={courses} />}
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

/** A curriculum: one goal across courses and videos from the library. Curio suggests
 *  what to include; the learner confirms and picks how it's ordered. */
function PlanCurriculum() {
  const [goal, setGoal] = useState('')
  const [title, setTitle] = useState('')
  const [order, setOrder] = useState<CurriculumOrder>('lectures')
  const [cands, setCands] = useState<CurriculumCandidate[] | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<'' | 'suggest' | 'list' | 'make'>('')
  const [err, setErr] = useState('')

  const key = (c: CurriculumCandidate) => `${c.kind}:${c.id}`
  const load = async (how: 'suggest' | 'list') => {
    setBusy(how)
    setErr('')
    try {
      const got = how === 'suggest' ? await api.suggestCurriculum(goal) : await api.curriculumCandidates()
      setCands(got)
      setPicked(new Set(got.filter((c) => c.suggested).map(key)))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not look through your library.')
    } finally {
      setBusy('')
    }
  }
  const toggle = (k: string, on: boolean) =>
    setPicked((prev) => {
      const next = new Set(prev)
      if (on) next.add(k)
      else next.delete(k)
      return next
    })
  const make = async () => {
    setBusy('make')
    setErr('')
    try {
      const sources = (cands ?? []).filter((c) => picked.has(key(c))).map((c) => ({ kind: c.kind, id: c.id }))
      const { course_id } = await api.createCurriculum(goal, sources, order, title)
      navigate(`/c/${course_id}`)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not make the curriculum.')
      setBusy('')
    }
  }

  return (
    <details className="courses__group">
      <summary>Plan a curriculum</summary>
      <p className="hint">
        Say what you want to learn. Curio suggests courses and videos from your library, you choose, and it puts them in
        the order they build on each other: one playlist that leaves out repeats and what you've already watched.
      </p>
      <div className="courses__group-row">
        <label className="visually-hidden" htmlFor="curriculum-goal">
          What do you want to learn?
        </label>
        <input
          id="curriculum-goal"
          type="text"
          placeholder="What do you want to learn? (e.g. build AI agents)"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
        />
        <button
          type="button"
          className="button-primary"
          disabled={!!busy || !goal.trim()}
          onClick={() => load('suggest')}
        >
          {busy === 'suggest' ? 'Looking through your library…' : 'Suggest'}
        </button>
        <button type="button" className="button-quiet" disabled={!!busy} onClick={() => load('list')}>
          {busy === 'list' ? 'Loading…' : 'Pick them myself'}
        </button>
      </div>
      {cands && (
        <>
          {cands.length === 0 ? (
            <p className="muted">Nothing in your library yet.</p>
          ) : (
            <ul className="courses__pick curriculum__pick">
              {cands.map((c) => (
                <li key={key(c)}>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={picked.has(key(c))}
                      onChange={(e) => toggle(key(c), e.target.checked)}
                    />
                    <span>
                      {c.kind === 'course' ? 'Course: ' : ''}
                      {shortTitle(c.title)} <span className="muted">{c.detail}</span>
                      {c.reason && <span className="curriculum__reason">{c.reason}</span>}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
          <div className="curriculum__order">
            <span className="muted">Order:</span>
            <fieldset className="segmented">
              <legend className="visually-hidden">Order</legend>
              <label className="segmented__option">
                <input type="radio" name="new-curriculum-order" checked={order === 'lectures'} onChange={() => setOrder('lectures')} />
                <span>Whole lectures</span>
              </label>
              <label className="segmented__option">
                <input type="radio" name="new-curriculum-order" checked={order === 'sections'} onChange={() => setOrder('sections')} />
                <span>Section by section</span>
              </label>
            </fieldset>
          </div>
          <div className="courses__group-row">
            <label className="visually-hidden" htmlFor="curriculum-title">
              Curriculum name
            </label>
            <input
              id="curriculum-title"
              type="text"
              placeholder="Name (optional)"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
            <button
              type="button"
              className="button-primary"
              disabled={!!busy || !goal.trim() || picked.size === 0}
              onClick={make}
            >
              {busy === 'make' ? 'Making…' : `Make the curriculum${picked.size ? ` (${picked.size})` : ''}`}
            </button>
          </div>
        </>
      )}
      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
    </details>
  )
}

/** A learning path: several courses as one, in the order picked. */
function CombineCourses({ courses }: { courses: Course[] }) {
  const [order, setOrder] = useState<string[]>([])
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const toggle = (id: string, on: boolean) => setOrder((o) => (on ? [...o, id] : o.filter((x) => x !== id)))

  const make = async () => {
    setBusy(true)
    setErr('')
    try {
      const { course_id } = await api.combineCourses(title, order)
      navigate(`/c/${course_id}`)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not combine the courses.')
      setBusy(false)
    }
  }

  return (
    <details className="courses__group">
      <summary>Combine courses into a learning path</summary>
      <p className="hint">
        Tick courses in the order to take them. The path is a course of its own: one playlist across all their
        lectures that leaves out what one teaches again, its checklist, questions and quiz.
      </p>
      <ul className="courses__pick">
        {courses.map((c) => (
          <li key={c.id}>
            <label className="checkbox">
              <input type="checkbox" checked={order.includes(c.id)} onChange={(e) => toggle(c.id, e.target.checked)} />
              {order.includes(c.id) && <span className="menu__count">{order.indexOf(c.id) + 1}</span>}
              {c.title}
            </label>
          </li>
        ))}
      </ul>
      <div className="courses__group-row">
        <label className="visually-hidden" htmlFor="path-title">
          Learning path name
        </label>
        <input
          id="path-title"
          type="text"
          placeholder="Name (optional)"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <button type="button" className="button-primary" onClick={make} disabled={busy || order.length < 2}>
          {busy ? 'Combining…' : 'Make the learning path'}
        </button>
      </div>
      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
    </details>
  )
}
