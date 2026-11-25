import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ApiError,
  api,
  type Course,
  type CourseLecture,
  type CoursePart,
  type CoursePlan,
  type CourseRef,
  type CoursePractice,
  type CourseSectionChoice,
  type Citation,
  type CurriculumOrder,
} from '../api'
import { AskThread } from '../components/Ask'
import { FlashcardSession } from '../components/Flashcards'
import { QuizView } from '../components/Quiz'
import { Player, type Clip } from '../components/Player'
import { IMPORTANCE_LABEL, PROFILES, clock, duration, shortTitle } from '../format'
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
      <header className="results__head">
        <h1>{course.title}</h1>
        <p className="muted">
          {course.channel && `${course.channel}. `}
          {course.lectures.length} {course.curriculum ? 'lectures and videos' : 'lectures'}, {duration(total)}.{' '}
          {course.curriculum ? 'Sections scored for' : 'Goal'}: {PROFILES[prefs.profile].label}
          {prefs.custom_objective && <>, “{prefs.custom_objective}”</>}.
        </p>
      </header>
      {course.curriculum && <CurriculumPanel course={course} onChange={reload} />}
      {course.importing && (
        <p className="notes__progress" aria-live="polite">
          Checking each lecture's captions and queuing the ones not analyzed yet…
        </p>
      )}
      <PlanSection course={course} onChange={reload} />
      {course.plan?.status !== 'done' && course.lectures.some((l) => l.analysis_id) && (
        <CourseAsk
          course={course}
          citation={(c) => (
            <a
              className="timelink"
              href={`https://www.youtube.com/watch?v=${c.video_id}&t=${Math.floor(c.start_s)}s`}
              target="_blank"
              rel="noreferrer"
            >
              {lectureLabel(course, c.lecture)}, {clock(c.start_s)}
            </a>
          )}
        />
      )}
      {course.plan?.status !== 'done' && <Lectures course={course} onChange={reload} />}
    </main>
  )
}


const ORDERS = [
  [
    'lectures',
    'Whole lectures',
    'Each lecture or video plays through. A video another course needs comes before the first lecture that needs it; each course keeps its own order.',
  ],
  [
    'sections',
    'Section by section',
    "Sections from different lectures interleave: a section another one needs plays before it. Each course still keeps its own order.",
  ],
] as const

/** A curriculum's goal, how it's ordered, and why things moved. */
function CurriculumPanel({ course, onChange }: { course: Course; onChange: () => void }) {
  const cur = course.curriculum!
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const change = async (settings: { order?: CurriculumOrder; auto_order?: boolean }) => {
    setBusy(true)
    setErr('')
    try {
      await api.setCurriculum(course.id, settings)
      onChange()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not change the order.')
    } finally {
      setBusy(false)
    }
  }
  const built = course.plan?.status === 'done' ? (course.plan.order ?? 'lectures') : null
  return (
    <section className="curriculum" aria-labelledby="curriculum-heading">
      <h2 id="curriculum-heading">Curriculum: {cur.goal}</h2>
      <div className="curriculum__order">
        <span className="muted">Order:</span>
        <fieldset className="segmented" disabled={busy}>
          <legend className="visually-hidden">Order</legend>
          {ORDERS.map(([value, label]) => (
            <label key={value} className="segmented__option">
              <input
                type="radio"
                name="curriculum-order"
                checked={cur.order === value}
                onChange={() => change({ order: value })}
              />
              <span>{label}</span>
            </label>
          ))}
        </fieldset>
      </div>
      <p className="hint">{ORDERS.find(([v]) => v === cur.order)![2]}</p>
      {built && built !== cur.order && (
        <p className="hint" role="status">
          The playlist below is ordered by {built === 'sections' ? 'section' : 'whole lectures'}: use{' '}
          <strong>Build again</strong> to apply the new order.
        </p>
      )}
      {cur.auto_order ? (
        cur.notes.length > 0 && (
          <details className="curriculum__why">
            <summary>Why this order</summary>
            <ul>
              {cur.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          </details>
        )
      ) : (
        <p className="hint">
          You've put the lectures in your own order.{' '}
          <button type="button" className="button-quiet" disabled={busy} onClick={() => change({ auto_order: true })}>
            Let Curio order them
          </button>
        </p>
      )}
      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
    </section>
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
          <CoursePlayer
            course={course}
            plan={plan}
            onChange={onChange}
            lectures={
              <>
                <Lectures course={course} onChange={onChange} inTab />
                {controls(course.plan_stale ? 'Build it again' : 'Build again with this length', true, course.plan_stale)}
              </>
            }
          />
        </>
      )}
    </section>
  )
}

interface Now {
  part: CoursePart | null
  ref: CourseRef | null // playing a section that isn't a part (from the checklist or a repeat)
}

type CourseTab = 'miss' | 'practice' | 'ask' | 'repeats' | 'lectures'

function CoursePlayer(props: { course: Course; plan: CoursePlan; lectures: React.ReactNode; onChange: () => void }) {
  const { course, plan, lectures: lecturesTab } = props
  // The learner's own course playlist, when they saved one; `useMine` switches between it and Curio's.
  const [useMine, setUseMine] = useState(true)
  const [editing, setEditing] = useState(false)
  const custom = course.custom_parts ?? null
  const parts = custom && useMine ? custom : plan.parts
  const total = parts.reduce((n, p) => n + p.duration_s, 0)
  const [tab, setTab] = useState<CourseTab>(plan.checklist.length ? 'miss' : 'ask')
  const repeatCount = plan.pairs ? plan.pairs.filter((p) => p.relation === 'repeat').length : plan.repeats.length
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
    const part =
      parts.find(
        (p) =>
          p.video_id === ref.video_id &&
          (p.section_ids.includes(ref.section_id) || (p.start_s <= ref.start_s && ref.start_s < p.end_s)),
      ) ?? null
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

  /** A moment an answer cites: to the end of the course part around it, or 3 minutes. */
  const playAt = (c: Citation) => {
    const part = parts.find((p) => p.video_id === c.video_id && p.start_s <= c.start_s && c.start_s < p.end_s)
    playRef(
      part
        ? { lecture: c.lecture, video_id: c.video_id, analysis_id: c.analysis_id, section_id: part.section_ids[0], title: part.title, start_s: c.start_s, end_s: part.end_s }
        : { lecture: c.lecture, video_id: c.video_id, analysis_id: c.analysis_id, section_id: `t${c.start_s}`, title: '', start_s: c.start_s, end_s: c.start_s + 180 },
    )
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
        parts={parts}
        activeKey={clip?.key ?? null}
        playing={clip ? { video_id: clip.video_id ?? '', t: time } : null}
        onPlay={play}
        onPlayRef={playRef}
      />
      <p className="summary">
        Watch <strong>{duration(total)}</strong> instead of {duration(plan.original_s)}, from{' '}
        {new Set(parts.map((p) => p.analysis_id)).size || lecturesUsed} lectures.
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
              <h2 className="now__title">{now.ref.title || `From ${clock(now.ref.start_s)}`}</h2>
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
            {parts.length} parts, {duration(total)}. {done} of {parts.length} watched.
          </p>
          <div className="playlist__tools">
            {custom && (
              <fieldset className="segmented playlist__which">
                <legend className="visually-hidden">Which playlist</legend>
                <label className="segmented__option">
                  <input type="radio" name="which-course-playlist" checked={useMine} onChange={() => setUseMine(true)} />
                  <span>Yours</span>
                </label>
                <label className="segmented__option">
                  <input type="radio" name="which-course-playlist" checked={!useMine} onChange={() => setUseMine(false)} />
                  <span>Curio's picks</span>
                </label>
              </fieldset>
            )}
            <button type="button" className="button-quiet" onClick={() => setEditing((e) => !e)} aria-expanded={editing}>
              {editing ? 'Close' : 'Edit the playlist'}
            </button>
          </div>
          {editing && (
            <CoursePlaylistEditor
              course={course}
              label={label}
              showing={custom && useMine ? 'yours' : 'curio'}
              onSaved={() => {
                setUseMine(true)
                setEditing(false)
                props.onChange()
              }}
            />
          )}
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
                      className={`segment segment--${shade(p)}${p.key === now.part?.key ? ' segment--active' : ''}`}
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

      <section className="extras course__extras">
        <div className="tabs" role="tablist" aria-label="More about this course">
          {plan.checklist.length > 0 && (
            <CourseTabButton id="miss" tab={tab} setTab={setTab}>
              Easy to miss ({plan.checklist.length})
            </CourseTabButton>
          )}
          <CourseTabButton id="practice" tab={tab} setTab={setTab}>
            Quiz and flashcards
          </CourseTabButton>
          <CourseTabButton id="ask" tab={tab} setTab={setTab}>
            Ask the course
          </CourseTabButton>
          <CourseTabButton id="repeats" tab={tab} setTab={setTab}>
            Repeats{repeatCount ? ` (${repeatCount})` : ''}
          </CourseTabButton>
          <CourseTabButton id="lectures" tab={tab} setTab={setTab}>
            Lectures ({course.lectures.length})
          </CourseTabButton>
        </div>
        <div className="tabpanel" role="tabpanel" id={`course-panel-${tab}`} aria-labelledby={`course-tab-${tab}`}>
          {tab === 'miss' && (
<section className="course__section" aria-labelledby="miss-heading">
          <h2 id="miss-heading" className="visually-hidden">Easy to miss</h2>
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
          {tab === 'practice' && <CoursePracticePanel course={course} label={label} onPlay={playAt} />}
          {tab === 'ask' && (
<CourseAsk
        inTab
        course={course}
        citation={(c) => (
          <button
            type="button"
            className="timelink"
            onClick={() => playAt(c)}
            aria-label={`Play ${label(c.lecture)} from ${clock(c.start_s)}`}
          >
            {label(c.lecture)}, {clock(c.start_s)}
          </button>
        )}
      />
          )}
          {tab === 'repeats' && <RepeatsPanel plan={plan} label={label} onPlay={playRef} />}
          {tab === 'lectures' && (
            <>
              {lecturesTab}
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
          )}
        </div>
      </section>
    </>
  )
}

/** A quiz and flashcards for the whole course playlist, one set per lecture. */
function CoursePracticePanel(props: { course: Course; label: (n: number) => string; onPlay: (c: Citation) => void }) {
  const { course, label, onPlay } = props
  const [practice, setPractice] = useState<CoursePractice | null>(null)
  const [mode, setMode] = useState<'quiz' | 'cards'>('quiz')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [tick, setTick] = useState(0)
  const running = practice?.status === 'running' || practice?.status === 'pending'

  useEffect(() => {
    let timer: number | undefined
    api.coursePractice(course.id).then(
      (p) => {
        setPractice(p)
        if (p.status === 'running' || p.status === 'pending') timer = window.setTimeout(() => setTick((n) => n + 1), 3000)
      },
      () => setPractice({ course_id: course.id, sets: [], status: null, progress: '', stale: false, questions: 0, cards: 0 }),
    )
    return () => window.clearTimeout(timer)
  }, [course.id, tick])

  const write = async (force: boolean) => {
    setBusy(true)
    setErr('')
    try {
      setPractice(await api.writeCoursePractice(course.id, force))
      setTick((n) => n + 1)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not start writing the course quiz.')
    } finally {
      setBusy(false)
    }
  }

  const lectureOf = (analysisId: string) => course.lectures.find((l) => l.analysis_id === analysisId)
  const play = (analysisId: string, t: number) => {
    const lec = lectureOf(analysisId)
    if (lec) onPlay({ start_s: t, video_id: lec.video_id, analysis_id: analysisId, lecture: lec.position })
  }

  if (!practice) return <p className="muted">Loading…</p>
  const done = practice.sets.filter((x) => x.study.status === 'done' && x.study.questions.length)
  const parts = (course.custom_parts ?? course.plan?.parts)?.length ?? 0

  return (
    <section className="course__section practice" aria-labelledby="practice-heading">
      <h2 id="practice-heading" className="visually-hidden">
        Quiz and flashcards for the course
      </h2>
      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
      {practice.status === null && (
        <div className="notes__start">
          <p className="muted">
            A quiz on the {parts} parts of {course.custom_parts ? 'your' : 'the'} course playlist, one question for each section, and a flashcard for each
            section and for each point under Easy to miss. The cards join your review deck. With a local model this
            takes about half a minute per lecture.
          </p>
          <button type="button" className="button-primary" onClick={() => write(false)} disabled={busy}>
            {busy ? 'Starting…' : 'Write the course quiz and flashcards'}
          </button>
        </div>
      )}
      {running && (
        <p className="notes__progress" aria-live="polite">
          {practice.progress || 'Waiting to start'}…
        </p>
      )}
      {practice.status === 'failed' && (
        <div className="notes__start">
          <p className="form-error" role="alert">
            Writing the course quiz stopped: {practice.sets.find((x) => x.study.error)?.study.error || 'unknown error.'}
          </p>
          <button type="button" className="button-primary" onClick={() => write(true)} disabled={busy}>
            Try again
          </button>
        </div>
      )}
      {practice.status === 'done' && (
        <>
          {practice.stale && (
            <div className="course__stale" role="status">
              <p>The course playlist changed since this quiz was written. Write it again to cover the new parts.</p>
            </div>
          )}
          <div className="practice__bar">
            <fieldset className="segmented practice__mode">
              <legend className="visually-hidden">Quiz or flashcards</legend>
              <label className="segmented__option">
                <input type="radio" name="course-mode" checked={mode === 'quiz'} onChange={() => setMode('quiz')} />
                <span>Quiz ({practice.questions})</span>
              </label>
              <label className="segmented__option">
                <input type="radio" name="course-mode" checked={mode === 'cards'} onChange={() => setMode('cards')} />
                <span>Flashcards ({practice.cards})</span>
              </label>
            </fieldset>
            <button
              type="button"
              className={practice.stale ? 'button-primary' : 'button-quiet'}
              onClick={() => write(true)}
              disabled={busy}
            >
              Write new questions
            </button>
          </div>
          {mode === 'quiz' && (
            <QuizView
              key={done.map((x) => x.study.id).join()}
              sets={done.map((x) => ({
                study: x.study,
                heading: `${label(x.lecture)}: ${shortTitle(lectureOf(x.study.analysis_id)?.title ?? '')}`,
              }))}
              onSeek={(t, st) => play(st.analysis_id, t)}
            />
          )}
          {mode === 'cards' && (
            <>
              <FlashcardSession
                courseId={course.id}
                onSeek={(t, due) => play(due.analysis_id, t)}
                label={(due) => {
                  const lec = lectureOf(due.analysis_id)
                  return lec ? `${lec.label}: ${shortTitle(lec.title)}` : due.video_title
                }}
              />
              <p className="hint">
                These cards are also in your daily <a {...linkProps('/review')}>review</a>, with every video's.
              </p>
            </>
          )}
        </>
      )}
    </section>
  )
}

/** What the model found comparing similar sections of different lectures. */
function RepeatsPanel(props: { plan: CoursePlan; label: (n: number) => string; onPlay: (r: CourseRef) => void }) {
  const { plan, label, onPlay } = props
  const pairs = plan.pairs
  const leftOut = new Set(plan.repeats.map((r) => `${r.section.video_id}:${r.section.section_id}`))
  const other = (pairs ?? []).filter(
    (p) =>
      p.relation === 'repeat' &&
      !leftOut.has(`${p.a.video_id}:${p.a.section_id}`) &&
      !leftOut.has(`${p.b.video_id}:${p.b.section_id}`),
  )
  const overlaps = (pairs ?? []).filter((p) => p.relation === 'overlap')
  const different = (pairs ?? []).filter((p) => p.relation === 'different').length
  const ref = (r: CourseRef, inPlaylist?: boolean) => (
    <>
      <RefButton ref_={r} label={label(r.lecture)} onPlay={onPlay} /> <strong>{r.title}</strong>
      {inPlaylist && <span className="checklist__in">in your playlist</span>}
    </>
  )
  return (
    <section className="course__section" aria-labelledby="repeats-heading">
      <h2 id="repeats-heading" className="visually-hidden">
        Repeats across lectures
      </h2>
      {pairs ? (
        <p className="muted">
          Curio compared {pairs.length} pairs of similar sections in different lectures:{' '}
          {pairs.filter((p) => p.relation === 'repeat').length}{' '}
          teach the same thing, {overlaps.length} overlap (each adds something) and {different} only sound alike. Play
          either side to compare.
        </p>
      ) : (
        <p className="muted">
          This playlist was built before Curio kept its comparisons. Build it again (in Lectures) to see every pair it
          checked.
        </p>
      )}

      {plan.repeats.length > 0 && (
        <>
          <h3 className="repeats__heading">Left out: taught in another lecture</h3>
          <ul className="repeats">
            {plan.repeats.map((r) => (
              <li key={`${r.section.video_id}:${r.section.section_id}:${r.covered_by.section_id}`}>
                <p>
                  {ref(r.section)} is covered by {ref(r.covered_by, true)}.
                </p>
                {r.reason && <p className="muted">{r.reason}</p>}
              </li>
            ))}
          </ul>
        </>
      )}

      {other.length > 0 && (
        <>
          <h3 className="repeats__heading">Also repeats, neither left out</h3>
          <p className="muted">Teach the same thing; one half is in your playlist or neither made the cut.</p>
          <ul className="repeats">
            {other.map((p) => (
              <li key={`${p.a.video_id}:${p.a.section_id}:${p.b.section_id}`}>
                <p>
                  {ref(p.a, p.a_in_playlist)} and {ref(p.b, p.b_in_playlist)}
                </p>
                {p.reason && <p className="muted">{p.reason}</p>}
              </li>
            ))}
          </ul>
        </>
      )}

      {overlaps.length > 0 && (
        <>
          <h3 className="repeats__heading">Overlaps: the same topic, each adds something</h3>
          <ul className="repeats">
            {overlaps.map((p) => (
              <li key={`${p.a.video_id}:${p.a.section_id}:${p.b.section_id}`}>
                <p>
                  {ref(p.a, p.a_in_playlist)} and {ref(p.b, p.b_in_playlist)}
                </p>
                {p.reason && <p className="muted">{p.reason}</p>}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}

function CourseTabButton(props: {
  id: CourseTab
  tab: CourseTab
  setTab: (t: CourseTab) => void
  children: React.ReactNode
}) {
  const selected = props.tab === props.id
  return (
    <button
      type="button"
      role="tab"
      id={`course-tab-${props.id}`}
      aria-selected={selected}
      aria-controls={`course-panel-${props.id}`}
      className="tab"
      onClick={() => props.setTab(props.id)}
    >
      {props.children}
    </button>
  )
}

function lectureLabel(course: Course, position: number): string {
  return course.lectures.find((l) => l.position === position)?.label ?? `#${position}`
}

/** Ask the course: questions answered from every analyzed lecture. */
function CourseAsk(props: { course: Course; citation: (c: Citation) => React.ReactNode; inTab?: boolean }) {
  const { course, citation } = props
  const id = course.id
  const analyzed = course.lectures.filter((l) => l.analysis_id).length
  const load = useMemo(
    () => () => api.courseConversation(id).then((list) => list.map((a) => ({ ...a, covered: a.in_course }))),
    [id],
  )
  const goal = course.preferences.custom_objective
  return (
    <section className="course__section" aria-labelledby="ask-course-heading">
      <h2 id="ask-course-heading" className={props.inTab ? 'visually-hidden' : undefined}>
        Ask the course
      </h2>
      <AskThread
        subject="course"
        intro={`Ask anything about the course. Curio searches all ${analyzed} analyzed lectures and answers from their transcripts, citing the moment in each lecture. Name a lecture ("in lecture 6…") to look there first.`}
        waiting="Searching every lecture and reading what it found… This takes up to a minute with a local model."
        suggestions={[
          'Which lectures talk about evaluating agents, and what do they warn about?',
          'What does the course say about context management, across lectures?',
          goal ? 'What would someone who has built agents most likely have missed here?' : 'What are the main ideas of the course?',
          'Where do two lectures disagree or give different advice?',
        ]}
        load={load}
        send={(q) => api.askCourse(id, q).then((a) => ({ ...a, covered: a.in_course }))}
        clear={() => api.clearCourseConversation(id)}
        citation={citation}
      />
    </section>
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

/** Pick the sections of your own course playlist from every section of the analyzed lectures. */
function CoursePlaylistEditor(props: {
  course: Course
  label: (n: number) => string
  showing: 'yours' | 'curio'
  onSaved: () => void
}) {
  const { course, label, showing } = props
  const [choices, setChoices] = useState<CourseSectionChoice[] | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [pins, setPins] = useState<Set<string>>(new Set())
  const [pinsChanged, setPinsChanged] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    api.coursePlaylistSections(course.id).then(
      (cs) => {
        setChoices(cs)
        // Start from the playlist on screen: Curio's picks when that is what's showing.
        setPicked(new Set(cs.filter((c) => (showing === 'curio' ? c.curio_pick : c.picked)).map(choiceKey)))
        setPins(new Set(cs.filter((c) => c.pinned).map(choiceKey)))
      },
      (e) => setErr(e instanceof ApiError ? e.message : 'Could not load the sections.'),
    )
  }, [course.id, showing])

  const save = async (sections: string[] | null) => {
    setBusy(true)
    setErr('')
    try {
      await api.setCoursePlaylist(course.id, sections)
      props.onSaved()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not save the playlist.')
    } finally {
      setBusy(false)
    }
  }

  const pin = async (key: string, on: boolean) => {
    const next = new Set(pins)
    if (on) next.add(key)
    else next.delete(key)
    setErr('')
    try {
      setPins(new Set(await api.setCoursePins(course.id, [...next])))
      setPinsChanged(true)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not save the pin.')
    }
  }

  if (!choices)
    return err ? (
      <p className="form-error" role="alert">
        {err}
      </p>
    ) : (
      <p className="muted">Loading the sections…</p>
    )

  const groups: { lecture: number; items: CourseSectionChoice[] }[] = []
  for (const c of choices) {
    if (groups.at(-1)?.lecture !== c.lecture) groups.push({ lecture: c.lecture, items: [] })
    groups.at(-1)!.items.push(c)
  }
  const total = choices.filter((c) => picked.has(choiceKey(c))).reduce((n, c) => n + c.duration_s, 0)
  const toggle = (key: string, on: boolean) =>
    setPicked((prev) => {
      const next = new Set(prev)
      if (on) next.add(key)
      else next.delete(key)
      return next
    })

  return (
    <div className="editor">
      <p className="hint">
        Tick the sections you want to watch, from any lecture. Curio's picks are marked; sections next to each other
        play as one part. <strong>Pin</strong> a section to keep it in Curio's picks whenever the playlist is built
        again; when it's rebuilt, Curio also prefers sections you haven't watched.
      </p>
      {pinsChanged && (
        <p className="hint" role="status">
          Pins count from the next build: use <strong>Build again</strong> to apply them.
        </p>
      )}
      <div className="editor__groups">
        {groups.map((g) => (
          <div key={g.lecture} className="editor__group">
            <h3 className="editor__group-title">
              {label(g.lecture)}
              <span className="muted"> {shortTitle(course.lectures.find((l) => l.position === g.lecture)?.title ?? '')}</span>
            </h3>
            <ul className="editor__list">
              {g.items.map((c) => (
                <li key={choiceKey(c)} className="editor__item">
                  <label className="editor__row">
                    <input
                      type="checkbox"
                      checked={picked.has(choiceKey(c))}
                      onChange={(e) => toggle(choiceKey(c), e.target.checked)}
                    />
                    <span className="editor__time">{clock(c.start_s)}</span>
                    <span>
                      {c.title}
                      <span className="muted">
                        {' '}
                        {duration(c.duration_s)}
                        {c.curio_pick ? ", Curio's pick" : c.importance === 'skip' ? ', marked skip' : ''}
                        {c.watched ? ', watched' : ''}
                      </span>
                    </span>
                  </label>
                  <button
                    type="button"
                    className={`button-quiet editor__pin${pins.has(choiceKey(c)) ? ' is-pinned' : ''}`}
                    aria-pressed={pins.has(choiceKey(c))}
                    aria-label={`${pins.has(choiceKey(c)) ? 'Unpin' : 'Pin'} ${c.title}`}
                    onClick={() => pin(choiceKey(c), !pins.has(choiceKey(c)))}
                  >
                    {pins.has(choiceKey(c)) ? 'Pinned' : 'Pin'}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
      <div className="editor__actions">
        <button type="button" className="button-primary" disabled={busy || picked.size === 0} onClick={() => save([...picked])}>
          Save my playlist ({duration(total)})
        </button>
        {course.custom_parts && (
          <button type="button" className="button-quiet" disabled={busy} onClick={() => save(null)}>
            Go back to Curio's picks
          </button>
        )}
      </div>
    </div>
  )
}

/** A part's colour. A section Curio marked skip that the learner added to their own
 *  playlist is drawn like an optional part: hatching means "left out". */
function shade(p: CoursePart): CoursePart['importance'] {
  return p.importance === 'skip' ? 'optional' : p.importance
}

function choiceKey(c: CourseSectionChoice): string {
  return `${c.analysis_id}:${c.section_id}`
}

/** Every lecture as one line: the parts in the course playlist highlighted, the parts
 *  left out as repeats hatched, and the red line where playback is. */
function CourseMap(props: {
  course: Course
  plan: CoursePlan
  parts: CoursePart[] // the playlist shown: the learner's own or Curio's
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
        const parts = props.parts.filter((p) => p.video_id === lec.video_id)
        const shown = new Set(parts.flatMap((p) => p.section_ids))
        const repeats = plan.repeats.filter((r) => r.section.video_id === lec.video_id && !shown.has(r.section.section_id))
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
                  className={`stretch stretch--${shade(p)} coursemap__mark${props.activeKey === p.key ? ' stretch--active' : ''}`}
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

function Lectures({ course, onChange, inTab }: { course: Course; onChange: () => void; inTab?: boolean }) {
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

  // Moving a lecture saves at once. The arrows stay enabled (a disabled button drops
  // keyboard focus); clicks while a move is saving are ignored.
  const [moving, setMoving] = useState(false)
  const move = async (from: number, to: number) => {
    if (moving || to < 0 || to >= course.lectures.length) return
    const ids = course.lectures.map((l) => l.video_id)
    const [vid] = ids.splice(from, 1)
    ids.splice(to, 0, vid)
    setMoving(true)
    setErr('')
    try {
      await api.reorderLectures(course.id, ids)
      onChange()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not move the lecture.')
    } finally {
      setMoving(false)
    }
  }

  const remove = () => {
    if (!window.confirm(`Delete the course “${course.title}”? Its lectures' analyses stay.`)) return
    act('delete', () => api.deleteCourse(course.id)).then(() => navigate('/'))
  }

  return (
    <section className="course__section" aria-labelledby="lectures-heading">
      <h2 id="lectures-heading" className={inTab ? 'visually-hidden' : undefined}>
        Lectures
      </h2>
      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
      <ol className="lectures">
        {course.lectures.map((l, i) => (
          <li key={l.video_id} className="lecture">
            <span className="lecture__move">
              <button
                type="button"
                className="button-icon"
                aria-label={`Move ${l.label} up`}
                aria-disabled={i === 0}
                onClick={() => move(i, i - 1)}
              >
                ↑
              </button>
              <button
                type="button"
                className="button-icon"
                aria-label={`Move ${l.label} down`}
                aria-disabled={i === course.lectures.length - 1}
                onClick={() => move(i, i + 1)}
              >
                ↓
              </button>
            </span>
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
      <p className="hint">
        Use the arrows to put the lectures in the order you want. The course playlist follows at once; build it again to
        also redo which of two repeated sections is kept.
      </p>
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
