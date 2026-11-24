import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ApiError,
  api,
  type Course,
  type CourseLecture,
  type CoursePart,
  type CoursePlan,
  type CourseRef,
} from '../api'
import { Player, type Clip } from '../components/Player'
import { IMPORTANCE_LABEL, PROFILES, clock, duration, shortTitle } from '../format'
import { BackLink } from '../components/Logo'
import { linkProps, navigate } from '../router'

const MINUTES = [60, 90, 120, 180, 240, 360]

/** Mirrors the server's default: about 15 minutes a lecture. */
function defaultMinutes(lectures: number): number {
  return MINUTES.reduce((best, m) => (Math.abs(m - 15 * lectures) < Math.abs(best - 15 * lectures) ? m : best))
}

export function CoursePage({ id }: { id: string }) {
  const [course, setCourse] = useState<Course | null>(null)
  const [error, setError] = useState('')
  const [tick, setTick] = useState(0)
  const reload = useCallback(() => setTick((n) => n + 1), [])

  useEffect(() => {
    let stopped = false
    let timer: number | undefined
    api.course(id).then(
      (c) => {
        if (stopped) return
        setCourse(c)
        const building = c.plan && (c.plan.status === 'pending' || c.plan.status === 'running')
        const waiting = c.lectures.some((l) => l.state === 'queued' || l.state === 'analyzing' || l.pending_id)
        // Fast while something is being built; now and then while lectures wait for the queue.
        const delay = building || c.importing ? 1500 : waiting ? 30000 : 0
        if (delay) timer = window.setTimeout(reload, delay)
      },
      (err) => !stopped && setError(err instanceof ApiError ? err.message : 'Could not load this course.'),
    )
    return () => {
      stopped = true
      window.clearTimeout(timer)
    }
  }, [id, tick, reload])

  if (error)
    return (
      <main className="page">
        <BackLink />
        <p className="form-error" role="alert">
          {error}
        </p>
      </main>
    )
  if (!course) return <main className="page muted">Loading…</main>

  const total = course.lectures.reduce((s, l) => s + l.duration_s, 0)
  const prefs = course.preferences
  return (
    <main className="page course">
      <BackLink />
      <header className="results__head">
        <h1>{course.title}</h1>
        <p className="muted">
          {course.channel && `${course.channel}. `}
          {course.lectures.length} lectures, {duration(total)}. Goal: {PROFILES[prefs.profile].label}
          {prefs.custom_objective && <>, “{prefs.custom_objective}”</>}.
        </p>
      </header>
      {course.importing && (
        <p className="notes__progress" aria-live="polite">
          Checking each lecture's captions and queuing the ones not analyzed yet…
        </p>
      )}
      <PlanSection course={course} onChange={reload} />
      <Lectures course={course} onChange={reload} />
    </main>
  )
}


function PlanSection({ course, onChange }: { course: Course; onChange: () => void }) {
  const usable = course.lectures.filter((l) => l.analysis_id).length
  const plan = course.plan
  const [minutes, setMinutes] = useState<number>(() =>
    plan?.budget_s ? Math.round(plan.budget_s / 60) : defaultMinutes(usable),
  )
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const running = plan?.status === 'pending' || plan?.status === 'running'

  const build = async (force: boolean) => {
    setBusy(true)
    setErr('')
    try {
      await api.buildCoursePlan(course.id, minutes, force)
      onChange()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not start building the course playlist.')
    } finally {
      setBusy(false)
    }
  }

  const waiting = course.lectures.length - usable
  const controls = (label: string, force: boolean, primary: boolean) => (
    <div className="course__build">
      <label className="field-label" htmlFor="course-minutes">
        Length of the course playlist
      </label>
      <div className="course__build-row">
        <select id="course-minutes" value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
          {(MINUTES.includes(minutes) ? MINUTES : [...MINUTES, minutes].sort((a, b) => a - b)).map((m) => (
            <option key={m} value={m}>
              About {duration(m * 60)}
            </option>
          ))}
        </select>
        <button
          type="button"
          className={primary ? 'button-primary' : 'button-quiet'}
          onClick={() => build(force)}
          disabled={busy || running || usable < 2}
        >
          {busy ? 'Starting…' : label}
        </button>
      </div>
    </div>
  )

  return (
    <section className="course__plan" aria-labelledby="plan-heading">
      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
      {(!plan || plan.status === 'failed') && !running && (
        <div className="notes__start">
          <h2 id="plan-heading">One playlist for the whole course</h2>
          <p className="muted">
            Curio picks the best parts across all the lectures for one time budget, leaves out what one lecture
            teaches again from another, and lists what's easy to miss for your goal. With a local model this takes
            about two minutes.
          </p>
          {waiting > 0 && (
            <p className="hint">
              {usable} of {course.lectures.length} lectures are analyzed.{' '}
              {usable >= 2
                ? 'You can build it now and again when the rest are done.'
                : 'It needs at least two analyzed lectures.'}
            </p>
          )}
          {plan?.status === 'failed' && (
            <p className="form-error" role="alert">
              Building the course playlist stopped: {plan.error || 'unknown error.'}
            </p>
          )}
          {controls('Build the course playlist', plan?.status === 'failed', true)}
        </div>
      )}
      {running && (
        <>
          <h2 id="plan-heading">Building the course playlist</h2>
          <p className="notes__progress" aria-live="polite">
            {plan.progress || 'Waiting to start'}…
          </p>
        </>
      )}
      {plan?.status === 'done' && (
        <>
          <h2 id="plan-heading" className="visually-hidden">
            Course playlist
          </h2>
          {course.plan_stale && (
            <div className="course__stale" role="status">
              <p>
                Lectures were analyzed since this playlist was built. Build it again to include them.
              </p>
            </div>
          )}
          <CoursePlayer course={course} plan={plan} />
          {controls(course.plan_stale ? 'Build it again' : 'Build again with this length', true, course.plan_stale)}
        </>
      )}
    </section>
  )
}

interface Now {
  part: CoursePart | null
  ref: CourseRef | null // playing a section that isn't a part (from the checklist or a repeat)
}

function CoursePlayer({ course, plan }: { course: Course; plan: CoursePlan }) {
  const parts = plan.parts
  const lectures = useMemo(() => new Map(course.lectures.map((l) => [l.position, l])), [course.lectures])
  const [firstVideo] = useState(parts[0]?.video_id ?? '')
  const [watched, setWatched] = useState<Set<string>>(new Set())
  const [now, setNow] = useState<Now>({ part: parts[0] ?? null, ref: null })
  const [clip, setClip] = useState<Clip | null>(() =>
    parts[0]
      ? { key: parts[0].key, start_s: parts[0].start_s, end_s: parts[0].end_s, nonce: 0, autoplay: false, video_id: parts[0].video_id }
      : null,
  )
  const [time, setTime] = useState<number | null>(null)
  const [continuous, setContinuous] = useState(true)

  useEffect(() => {
    api.courseWatched(course.id).then((keys) => setWatched(new Set(keys)), () => {})
  }, [course.id])

  const index = now.part ? parts.findIndex((p) => p.key === now.part!.key) : -1
  const label = (n: number) => lectures.get(n)?.label ?? `#${n}`

  const play = useCallback((part: CoursePart) => {
    setNow({ part, ref: null })
    setClip((c) => ({
      key: part.key,
      start_s: part.start_s,
      end_s: part.end_s,
      nonce: (c?.nonce ?? 0) + 1,
      autoplay: true,
      video_id: part.video_id,
    }))
  }, [])

  const playRef = (ref: CourseRef) => {
    const part = parts.find((p) => p.video_id === ref.video_id && p.section_ids.includes(ref.section_id)) ?? null
    setNow({ part, ref: part ? null : ref })
    setClip((c) => ({
      key: `${ref.video_id}:${ref.section_id}`,
      start_s: ref.start_s,
      end_s: part?.end_s ?? ref.end_s,
      nonce: (c?.nonce ?? 0) + 1,
      autoplay: true,
      video_id: ref.video_id,
    }))
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    document.querySelector('.watch')?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
  }

  const setPartWatched = (key: string, value: boolean) => {
    setWatched((prev) => {
      const next = new Set(prev)
      if (value) next.add(key)
      else next.delete(key)
      return next
    })
    api.setCourseWatched(course.id, key, value).catch(() => {})
  }

  const onClipEnd = () => {
    const part = now.part
    if (!part || !clip) return
    if (clip.start_s <= part.start_s && !watched.has(part.key)) setPartWatched(part.key, true)
    if (continuous && index >= 0 && index < parts.length - 1) play(parts[index + 1])
  }

  const done = parts.filter((p) => watched.has(p.key)).length
  const byLecture = useMemo(() => {
    const groups: { lecture: number; parts: CoursePart[] }[] = []
    for (const p of parts) {
      if (groups.at(-1)?.lecture !== p.lecture) groups.push({ lecture: p.lecture, parts: [] })
      groups.at(-1)!.parts.push(p)
    }
    return groups
  }, [parts])
  const lecturesUsed = new Set(plan.analysis_ids).size

  return (
    <>
      <CourseMap
        course={course}
        plan={plan}
        activeKey={clip?.key ?? null}
        playing={clip ? { video_id: clip.video_id ?? '', t: time } : null}
        onPlay={play}
        onPlayRef={playRef}
      />
      <p className="summary">
        Watch <strong>{duration(plan.total_s)}</strong> instead of {duration(plan.original_s)}, from{' '}
        {lecturesUsed} lectures.
        {plan.repeats.length > 0 &&
          ` ${plan.repeats.length} part${plan.repeats.length === 1 ? '' : 's'} taught again in another lecture ${plan.repeats.length === 1 ? 'is' : 'are'} left out.`}
      </p>

      <div className="watch">
        <div className="watch__main">
          {firstVideo && <Player videoId={firstVideo} clip={clip} onTime={setTime} onClipEnd={onClipEnd} />}
          {now.part && (
            <div className="now">
              <div className="now__controls">
                <button type="button" className="button-quiet" onClick={() => index > 0 && play(parts[index - 1])} disabled={index <= 0}>
                  Previous part
                </button>
                <button type="button" className="button-quiet" onClick={() => play(now.part!)}>
                  Replay
                </button>
                <button
                  type="button"
                  className="button-quiet"
                  onClick={() => index < parts.length - 1 && play(parts[index + 1])}
                  disabled={index < 0 || index >= parts.length - 1}
                >
                  Next part
                </button>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={watched.has(now.part.key)}
                    onChange={(e) => setPartWatched(now.part!.key, e.target.checked)}
                  />
                  Watched
                </label>
                <label className="checkbox">
                  <input type="checkbox" checked={continuous} onChange={(e) => setContinuous(e.target.checked)} />
                  Play the next part automatically
                </label>
              </div>
              <p className="course__from">
                {label(now.part.lecture)}: {shortTitle(lectures.get(now.part.lecture)?.title ?? '')}
              </p>
              <h2 className="now__title">{now.part.title}</h2>
              <p className="muted">
                {clock(now.part.start_s)}–{clock(now.part.end_s)}, {duration(now.part.duration_s)}.{' '}
                {IMPORTANCE_LABEL[now.part.importance]}, scored {Math.round(now.part.score)} of 100.
              </p>
              <dl className="now__facts">
                <dt>What you'll learn</dt>
                <dd>{now.part.what_you_learn}</dd>
                {now.part.why_valuable && (
                  <>
                    <dt>Why it's worth watching</dt>
                    <dd>{now.part.why_valuable}</dd>
                  </>
                )}
                {now.part.also_in.length > 0 && (
                  <>
                    <dt>Also taught in</dt>
                    <dd>
                      <ul className="course__refs">
                        {now.part.also_in.map((r) => (
                          <li key={`${r.video_id}:${r.section_id}`}>
                            <RefButton ref_={r} label={label(r.lecture)} onPlay={playRef} /> {r.title}
                          </li>
                        ))}
                      </ul>
                    </dd>
                  </>
                )}
              </dl>
              <a className="external" {...linkProps(`/v/${now.part.analysis_id}`)}>
                Open this lecture's page
              </a>
            </div>
          )}
          {now.ref && (
            <div className="now">
              <p className="course__from">
                {label(now.ref.lecture)}: {shortTitle(lectures.get(now.ref.lecture)?.title ?? '')}
              </p>
              <h2 className="now__title">{now.ref.title}</h2>
              <p className="muted">
                {clock(now.ref.start_s)}–{clock(now.ref.end_s)}. Not in the course playlist.
              </p>
              <a className="external" {...linkProps(`/v/${now.ref.analysis_id}`)}>
                Open this lecture's page
              </a>
            </div>
          )}
        </div>

        <aside className="playlist" aria-labelledby="course-playlist-heading">
          <h2 id="course-playlist-heading">Course playlist</h2>
          <p className="muted">
            {parts.length} parts, {duration(plan.total_s)}. {done} of {parts.length} watched.
          </p>
          {byLecture.map((g) => (
            <div key={g.lecture} className="course__group">
              <h3 className="course__group-title">
                {label(g.lecture)}
                <span className="muted"> {shortTitle(lectures.get(g.lecture)?.title ?? '')}</span>
              </h3>
              <ol className="playlist__list">
                {g.parts.map((p) => (
                  <li key={p.key}>
                    <button
                      type="button"
                      className={`segment segment--${p.importance}${p.key === now.part?.key ? ' segment--active' : ''}`}
                      onClick={() => play(p)}
                      aria-current={p.key === now.part?.key ? 'true' : undefined}
                    >
                      <span className="segment__body">
                        <span className="segment__title">
                          {p.title}
                          {watched.has(p.key) && <span className="segment__done"> Watched</span>}
                        </span>
                        <span className="segment__meta">
                          {clock(p.start_s)}–{clock(p.end_s)}, {duration(p.duration_s)}
                        </span>
                        <span className="segment__learn">{p.what_you_learn}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ol>
            </div>
          ))}
        </aside>
      </div>

      {plan.checklist.length > 0 && (
        <section className="course__section" aria-labelledby="miss-heading">
          <h2 id="miss-heading">Easy to miss</h2>
          <p className="muted">
            {course.preferences.custom_objective
              ? `Points from the course that matter for your goal, “${course.preferences.custom_objective}”.`
              : 'Points from the course that are easy to miss.'}{' '}
            Each links to where a lecture teaches it.
          </p>
          <ul className="checklist">
            {plan.checklist.map((item) => (
              <li key={item.text} className="checklist__item">
                <p className="checklist__text">{item.text}</p>
                {item.why && <p className="checklist__why">{item.why}</p>}
                <p className="checklist__refs">
                  {item.refs.map((r) => (
                    <RefButton key={`${r.video_id}:${r.section_id}`} ref_={r} label={label(r.lecture)} onPlay={playRef} />
                  ))}
                  <span className={item.in_playlist ? 'checklist__in' : 'checklist__out'}>
                    {item.in_playlist ? 'In your course playlist' : 'Not in your course playlist'}
                  </span>
                </p>
              </li>
            ))}
          </ul>
        </section>
      )}

      {plan.repeats.length > 0 && (
        <section className="course__section" aria-labelledby="repeats-heading">
          <h2 id="repeats-heading">Left out: taught in another lecture</h2>
          <ul className="repeats">
            {plan.repeats.map((r) => (
              <li key={`${r.section.video_id}:${r.section.section_id}:${r.covered_by.section_id}`}>
                <p>
                  <RefButton ref_={r.section} label={label(r.section.lecture)} onPlay={playRef} />{' '}
                  <strong>{r.section.title}</strong> is covered by{' '}
                  <RefButton ref_={r.covered_by} label={label(r.covered_by.lecture)} onPlay={playRef} />{' '}
                  {r.covered_by.title}.
                </p>
                {r.reason && <p className="muted">{r.reason}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {plan.warnings.length > 0 && (
        <details className="notesheet__warnings">
          <summary>
            {plan.warnings.length} thing{plan.warnings.length === 1 ? '' : 's'} to know about this playlist
          </summary>
          <ul>
            {plan.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </details>
      )}
    </>
  )
}

function RefButton({ ref_, label, onPlay }: { ref_: CourseRef; label: string; onPlay: (r: CourseRef) => void }) {
  return (
    <button
      type="button"
      className="timelink"
      onClick={() => onPlay(ref_)}
      aria-label={`Play ${ref_.title}, ${label} at ${clock(ref_.start_s)}`}
    >
      {label}, {clock(ref_.start_s)}
    </button>
  )
}

/** Every lecture as one line: the parts in the course playlist highlighted, the parts
 *  left out as repeats hatched, and the red line where playback is. */
function CourseMap(props: {
  course: Course
  plan: CoursePlan
  activeKey: string | null
  playing: { video_id: string; t: number | null } | null
  onPlay: (p: CoursePart) => void
  onPlayRef: (r: CourseRef) => void
}) {
  const { course, plan } = props
  const used = new Set(plan.analysis_ids)
  const rows = course.lectures.filter((l) => l.analysis_id && used.has(l.analysis_id))
  return (
    <div className="coursemap" role="group" aria-label="The course, lecture by lecture">
      {rows.map((lec) => {
        const len = lec.duration_s || 1
        const pct = (s: number) => `${(Math.min(Math.max(s, 0), len) / len) * 100}%`
        const parts = plan.parts.filter((p) => p.video_id === lec.video_id)
        const repeats = plan.repeats.filter((r) => r.section.video_id === lec.video_id)
        const t = props.playing?.video_id === lec.video_id ? props.playing.t : null
        return (
          <div key={lec.video_id} className="coursemap__row">
            <span className="coursemap__label" title={lec.title}>
              {lec.label}
            </span>
            <div className="coursemap__track">
              {repeats.map((r) => (
                <button
                  key={`r-${r.section.section_id}`}
                  type="button"
                  className="stretch stretch--skip coursemap__mark"
                  style={{ left: pct(r.section.start_s), width: `max(4px, calc(${pct(r.section.end_s - r.section.start_s)} - 2px))` }}
                  aria-label={`${r.section.title}, left out: taught in ${props.course.lectures.find((l) => l.position === r.covered_by.lecture)?.label ?? 'another lecture'}. Play.`}
                  onClick={() => props.onPlayRef(r.section)}
                />
              ))}
              {parts.map((p) => (
                <button
                  key={p.key}
                  type="button"
                  className={`stretch stretch--${p.importance} coursemap__mark${props.activeKey === p.key ? ' stretch--active' : ''}`}
                  style={{ left: pct(p.start_s), width: `max(4px, calc(${pct(p.end_s - p.start_s)} - 2px))` }}
                  aria-label={`${lec.label}: ${p.title}, ${clock(p.start_s)} to ${clock(p.end_s)}. Play.`}
                  title={`${p.title} (${clock(p.start_s)})`}
                  onClick={() => props.onPlay(p)}
                />
              ))}
              {t != null && <span className="watchmap__playhead" style={{ left: pct(t) }} />}
            </div>
          </div>
        )
      })}
    </div>
  )
}

const STATE_LABEL: Record<CourseLecture['state'], string> = {
  ready: 'Analyzed',
  other_goal: 'Analyzed for another goal',
  queued: 'Queued for tonight',
  analyzing: 'Analyzing now',
  failed: 'Analysis failed',
  missing: 'Not analyzed',
}

function Lectures({ course, onChange }: { course: Course; onChange: () => void }) {
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')

  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key)
    setErr('')
    try {
      await fn()
      onChange()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'That didn’t work. Try again.')
    } finally {
      setBusy('')
    }
  }

  const remove = () => {
    if (!window.confirm(`Delete the course “${course.title}”? Its lectures' analyses stay.`)) return
    act('delete', () => api.deleteCourse(course.id)).then(() => navigate('/'))
  }

  return (
    <section className="course__section" aria-labelledby="lectures-heading">
      <h2 id="lectures-heading">Lectures</h2>
      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
      <ol className="lectures">
        {course.lectures.map((l) => (
          <li key={l.video_id} className="lecture">
            <span className="lecture__label">{l.label}</span>
            <div className="lecture__body">
              {l.analysis_id ? (
                <a className="lecture__title" {...linkProps(`/v/${l.analysis_id}`)}>
                  {shortTitle(l.title)}
                </a>
              ) : (
                <span className="lecture__title">{shortTitle(l.title)}</span>
              )}
              <span className="muted">
                {l.duration_s > 0 && `${duration(l.duration_s)}. `}
                <span className={`lecture__state lecture__state--${l.state}`}>{STATE_LABEL[l.state]}</span>
                {l.state === 'other_goal' && l.pending_id && '; queued again for the course goal'}
                {l.error && `: ${l.error}`}
              </span>
            </div>
            <div className="lecture__actions">
              {(l.state === 'queued' || (l.state === 'other_goal' && l.pending_id)) && (
                <button
                  type="button"
                  className="button-quiet"
                  disabled={!!busy}
                  onClick={() => act(l.video_id, () => api.queueStart(l.pending_id!))}
                >
                  Analyze now
                </button>
              )}
              {(l.state === 'failed' || (l.state === 'missing' && !course.importing)) && (
                <button
                  type="button"
                  className="button-quiet"
                  disabled={!!busy}
                  onClick={() => act(l.video_id, () => api.analyze(l.url, course.preferences, l.state === 'failed', true))}
                >
                  Queue it
                </button>
              )}
            </div>
          </li>
        ))}
      </ol>
      {course.unavailable > 0 && (
        <p className="hint">
          {course.unavailable} video{course.unavailable === 1 ? ' in the playlist is' : 's in the playlist are'} private
          or deleted and left out.
        </p>
      )}
      <div className="course__actions">
        {course.playlist_id && (
          <>
            <button
              type="button"
              className="button-quiet"
              disabled={!!busy || course.importing}
              onClick={() => act('refresh', () => api.refreshCourse(course.id))}
            >
              Check the playlist for new lectures
            </button>
            <a className="external" href={course.url} target="_blank" rel="noreferrer">
              Open the playlist on YouTube
            </a>
          </>
        )}
        <button type="button" className="button-quiet" disabled={!!busy} onClick={remove}>
          Delete this course
        </button>
      </div>
    </section>
  )
}
