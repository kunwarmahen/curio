import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError, api, type Analysis, type Segment } from '../api'
import { AskPanel } from '../components/Ask'
import { NotesPanel } from '../components/Notes'
import { PracticePanel } from '../components/Practice'
import { Player, type Clip } from '../components/Player'
import { WatchMap, WatchMapLegend, type Stretch } from '../components/WatchMap'
import { IMPORTANCE_LABEL, PROFILES, clock, duration, queueHours } from '../format'
import { linkProps, navigate } from '../router'

export function VideoPage({ id }: { id: string }) {
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [error, setError] = useState('')
  const [steps, setSteps] = useState<string[]>([])
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let timer: number | undefined
    let stopped = false
    const poll = async () => {
      try {
        const a = await api.analysis(id)
        if (stopped) return
        setAnalysis(a)
        if (a.progress) setSteps((s) => (s[s.length - 1] === a.progress ? s : [...s, a.progress]))
        if (a.status === 'pending' || a.status === 'running') timer = window.setTimeout(poll, 1500)
        // A queued video may start overnight; check now and then.
        if (a.status === 'queued') timer = window.setTimeout(poll, 30000)
      } catch (err) {
        if (!stopped) setError(err instanceof ApiError ? err.message : 'Could not load this analysis.')
      }
    }
    poll()
    return () => {
      stopped = true
      window.clearTimeout(timer)
    }
  }, [id, reload])

  if (error) {
    return (
      <main className="page">
        <BackLink />
        <p className="form-error" role="alert">
          {error}
        </p>
      </main>
    )
  }
  if (!analysis) return <main className="page muted">Loading…</main>
  if (analysis.status === 'failed') return <Failed analysis={analysis} />
  if (analysis.status === 'queued') return <Queued analysis={analysis} onStarted={() => setReload((n) => n + 1)} />
  if (analysis.status !== 'done' || !analysis.playlist) return <Progress analysis={analysis} steps={steps} />
  return <Results analysis={analysis} />
}

function BackLink() {
  return (
    <a className="back" {...linkProps('/')}>
      ← All videos
    </a>
  )
}

function Progress({ analysis, steps }: { analysis: Analysis; steps: string[] }) {
  return (
    <main className="page progress">
      <BackLink />
      <h1>{analysis.video.title}</h1>
      <p className="muted">{analysis.video.channel}</p>
      <div className="progress__track" aria-hidden />
      <ol className="progress__steps" aria-live="polite">
        {steps.length === 0 && <li className="progress__current">Waiting to start</li>}
        {steps.map((s, i) => (
          <li key={s} className={i === steps.length - 1 ? 'progress__current' : 'progress__done'}>
            {s}
          </li>
        ))}
      </ol>
      <p className="muted">
        Curio reads the entire transcript, maps its topics, then judges each section against the whole video.
        With a local model this takes a few minutes. You can leave this page; the result will be under
        Recently analyzed.
      </p>
    </main>
  )
}

function Queued({ analysis, onStarted }: { analysis: Analysis; onStarted: () => void }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [hours, setHours] = useState('')
  useEffect(() => {
    api.queue().then((q) => setHours(queueHours(q.window_start, q.window_end)), () => {})
  }, [])
  const act = async (action: 'start' | 'remove') => {
    setBusy(true)
    setErr('')
    try {
      if (action === 'start') {
        await api.queueStart(analysis.id)
        onStarted()
      } else {
        await api.queueRemove(analysis.id)
        navigate('/')
      }
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'That didn’t work. Try again.')
    }
    setBusy(false)
  }
  return (
    <main className="page">
      <BackLink />
      <h1>{analysis.video.title}</h1>
      <p className="muted">{analysis.video.channel}</p>
      <p className="queued__lede">
        Waiting in the queue. Curio analyzes queued videos one at a time{hours ? ` ${hours}` : ''}, when the GPU
        is free, and writes its notes and a quiz with flashcards too. This page updates when it's done.
      </p>
      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
      <div className="queued__actions">
        <button type="button" className="button-primary" onClick={() => act('start')} disabled={busy}>
          Analyze now
        </button>
        <button type="button" className="button-quiet" onClick={() => act('remove')} disabled={busy}>
          Remove from the queue
        </button>
      </div>
    </main>
  )
}

function Failed({ analysis }: { analysis: Analysis }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const retry = async () => {
    setBusy(true)
    try {
      const { analysis_id } = await api.analyze(analysis.video.url, analysis.preferences, true)
      navigate(`/v/${analysis_id}`)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not restart the analysis.')
      setBusy(false)
    }
  }
  return (
    <main className="page">
      <BackLink />
      <h1>{analysis.video.title}</h1>
      <p className="form-error" role="alert">
        The analysis stopped: {analysis.error || 'unknown error.'}
      </p>
      {err && <p className="form-error">{err}</p>}
      <button type="button" className="button-primary" onClick={retry} disabled={busy}>
        {busy ? 'Starting…' : 'Analyze again'}
      </button>
    </main>
  )
}

type Tab = 'notes' | 'practice' | 'ask' | 'more' | 'skip' | 'scoring'

function Results({ analysis }: { analysis: Analysis }) {
  const playlist = analysis.playlist!
  const segs = playlist.segments
  const others = analysis.segments.filter((s) => !s.in_playlist)
  const [completed, setCompleted] = useState<Set<string>>(new Set())
  const [currentId, setCurrentId] = useState<string | null>(segs[0]?.id ?? others[0]?.id ?? null)
  const [clip, setClip] = useState<Clip | null>(() => {
    const first = segs[0] ?? others[0]
    return first ? { key: first.id, start_s: first.start_s, end_s: first.end_s, nonce: 0, autoplay: false } : null
  })
  const [time, setTime] = useState<number | null>(null)
  const [continuous, setContinuous] = useState(true)
  const [tab, setTab] = useState<Tab>('notes')

  useEffect(() => {
    api.completed(analysis.id).then((ids) => setCompleted(new Set(ids)), () => {})
  }, [analysis.id])

  const bySection = useMemo(() => {
    const map = new Map<string, Segment>()
    for (const seg of analysis.segments) for (const sid of seg.section_ids) map.set(sid, seg)
    return map
  }, [analysis.segments])

  const stretches: Stretch[] = analysis.sections.map((s) => ({
    key: s.section.id,
    start_s: s.section.start_s,
    end_s: s.section.end_s,
    importance: s.importance,
    in_playlist: bySection.get(s.section.id)?.in_playlist ?? false,
    title: s.section.title,
  }))
  const current = analysis.segments.find((s) => s.id === currentId) ?? null
  const index = segs.findIndex((s) => s.id === currentId)

  const play = useCallback((seg: { id: string; start_s: number; end_s: number }) => {
    setCurrentId(seg.id)
    setClip((c) => ({ key: seg.id, start_s: seg.start_s, end_s: seg.end_s, nonce: (c?.nonce ?? 0) + 1, autoplay: true }))
  }, [])

  const toggleCompleted = (segId: string, value: boolean) => {
    setCompleted((prev) => {
      const next = new Set(prev)
      if (value) next.add(segId)
      else next.delete(segId)
      return next
    })
    api.setCompleted(analysis.id, segId, value).catch(() => {})
  }

  const onClipEnd = () => {
    if (!current) return
    // A clip started from a note's timestamp is only part of the segment: don't mark it watched.
    const whole = clip != null && clip.start_s <= current.start_s
    if (whole && current.in_playlist && !completed.has(current.id)) toggleCompleted(current.id, true)
    if (continuous && index >= 0 && index < segs.length - 1) play(segs[index + 1])
  }

  /** Play from a moment cited in the notes to the end of its segment (or section). */
  const seek = (t: number) => {
    const sec = analysis.sections.find((s) => s.section.start_s <= t && t < s.section.end_s)?.section
    const seg = sec ? bySection.get(sec.id) : undefined
    setCurrentId(seg?.id ?? null)
    setClip((c) => ({
      key: seg?.id ?? sec?.id ?? 'note',
      start_s: t,
      end_s: seg?.end_s ?? sec?.end_s ?? t + 60,
      nonce: (c?.nonce ?? 0) + 1,
      autoplay: true,
    }))
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    document.querySelector('.watch')?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
  }

  const pickStretch = (s: Stretch) => {
    const seg = bySection.get(s.key)
    play(seg ?? { id: s.key, start_s: s.start_s, end_s: s.end_s })
  }

  const done = segs.filter((s) => completed.has(s.id)).length
  const prefs = analysis.preferences
  const activeKeys = current ? current.section_ids : clip ? [clip.key] : []

  return (
    <main className="page results">
      <BackLink />
      <header className="results__head">
        <h1>{analysis.video.title}</h1>
        <p className="muted">
          {analysis.video.channel}, {duration(analysis.video.duration_s)}. Goal: {PROFILES[prefs.profile].label}
          {prefs.custom_objective && <>, “{prefs.custom_objective}”</>}.
        </p>
      </header>

      <section className="map-section" aria-label="Watch map">
        <WatchMap
          duration={analysis.video.duration_s}
          stretches={stretches}
          onPick={pickStretch}
          playhead={time}
          activeKey={activeKeys.length === 1 ? activeKeys[0] : null}
        />
        <WatchMapLegend />
      </section>

      <p className="summary">
        Watch <strong>{duration(playlist.total_s)}</strong> instead of {duration(playlist.original_s)}. Curio
        estimates this covers about {Math.round(playlist.coverage_estimate * 100)}% of what the video teaches
        <span className="muted"> (an AI estimate, not a measurement)</span>.
      </p>

      <div className="watch">
        <div className="watch__main">
          <Player videoId={analysis.video.video_id} clip={clip} onTime={setTime} onClipEnd={onClipEnd} />
          {current && (
            <NowPlaying
              seg={current}
              videoUrl={analysis.video.url}
              index={index}
              total={segs.length}
              completed={completed.has(current.id)}
              onPrev={() => index > 0 && play(segs[index - 1])}
              onNext={() => index < segs.length - 1 && play(segs[index + 1])}
              onReplay={() => play(current)}
              onCompleted={(v) => toggleCompleted(current.id, v)}
              continuous={continuous}
              onContinuous={setContinuous}
            />
          )}
        </div>

        <aside className="playlist" aria-labelledby="playlist-heading">
          <h2 id="playlist-heading">Your playlist</h2>
          <p className="muted">
            {segs.length} parts, {duration(playlist.total_s)}. {done} of {segs.length} watched.
          </p>
          <ol className="playlist__list">
            {segs.map((seg, i) => (
              <li key={seg.id}>
                <SegmentRow
                  seg={seg}
                  number={i + 1}
                  active={seg.id === currentId}
                  done={completed.has(seg.id)}
                  onPlay={() => play(seg)}
                />
              </li>
            ))}
          </ol>
        </aside>
      </div>

      <section className="overview">
        <h2>What this video covers</h2>
        <p>{analysis.overview}</p>
        {analysis.key_concepts.length > 0 && (
          <ul className="concepts" aria-label="Key concepts">
            {analysis.key_concepts.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        )}
      </section>

      <section className="extras">
        <div className="tabs" role="tablist" aria-label="More about this video">
          <TabButton id="notes" tab={tab} setTab={setTab}>
            Notes
          </TabButton>
          <TabButton id="practice" tab={tab} setTab={setTab}>
            Quiz and flashcards
          </TabButton>
          <TabButton id="ask" tab={tab} setTab={setTab}>
            Ask the video
          </TabButton>
          <TabButton id="more" tab={tab} setTab={setTab}>
            More to watch ({others.length})
          </TabButton>
          <TabButton id="skip" tab={tab} setTab={setTab}>
            Safe to skip ({playlist.skipped.length})
          </TabButton>
          <TabButton id="scoring" tab={tab} setTab={setTab}>
            How it was scored
          </TabButton>
        </div>
        <div className="tabpanel" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
          {tab === 'notes' && <NotesPanel analysis={analysis} onSeek={seek} />}
          {tab === 'practice' && <PracticePanel analysis={analysis} onSeek={seek} />}
          {tab === 'ask' && <AskPanel analysis={analysis} onSeek={seek} />}
          {tab === 'more' && (
            <>
              <p className="muted">
                Worth watching if you have more time. These didn't fit the playlist's time budget or scored lower.
              </p>
              <ul className="plain-list">
                {others.map((seg) => (
                  <li key={seg.id}>
                    <SegmentRow seg={seg} active={seg.id === currentId} done={completed.has(seg.id)} onPlay={() => play(seg)} />
                  </li>
                ))}
              </ul>
            </>
          )}
          {tab === 'skip' && (
            <>
              <p className="muted">Sections you can skip for this goal, and why.</p>
              <ul className="skip-list">
                {playlist.skipped.map((s) => (
                  <li key={s.section_id}>
                    <span className="time">
                      {clock(s.start_s)}–{clock(s.end_s)}
                    </span>
                    <div>
                      <strong>{s.title}</strong>
                      <p>{s.reason}</p>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
          {tab === 'scoring' && <Scoring analysis={analysis} />}
        </div>
      </section>
    </main>
  )
}

function TabButton(props: { id: Tab; tab: Tab; setTab: (t: Tab) => void; children: React.ReactNode }) {
  const selected = props.tab === props.id
  return (
    <button
      type="button"
      role="tab"
      id={`tab-${props.id}`}
      aria-selected={selected}
      aria-controls={`panel-${props.id}`}
      className="tab"
      onClick={() => props.setTab(props.id)}
    >
      {props.children}
    </button>
  )
}

function NowPlaying(props: {
  seg: Segment
  videoUrl: string
  index: number
  total: number
  completed: boolean
  onPrev: () => void
  onNext: () => void
  onReplay: () => void
  onCompleted: (v: boolean) => void
  continuous: boolean
  onContinuous: (v: boolean) => void
}) {
  const { seg } = props
  const inPlaylist = props.index >= 0
  return (
    <div className="now">
      <div className="now__controls">
        <button type="button" className="button-quiet" onClick={props.onPrev} disabled={!inPlaylist || props.index === 0}>
          Previous part
        </button>
        <button type="button" className="button-quiet" onClick={props.onReplay}>
          Replay
        </button>
        <button
          type="button"
          className="button-quiet"
          onClick={props.onNext}
          disabled={!inPlaylist || props.index >= props.total - 1}
        >
          Next part
        </button>
        <label className="checkbox">
          <input type="checkbox" checked={props.completed} onChange={(e) => props.onCompleted(e.target.checked)} />
          Watched
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={props.continuous} onChange={(e) => props.onContinuous(e.target.checked)} />
          Play the next part automatically
        </label>
      </div>
      <h2 className="now__title">{seg.title}</h2>
      <p className="muted">
        {clock(seg.start_s)}–{clock(seg.end_s)}, {duration(seg.duration_s)}. {IMPORTANCE_LABEL[seg.importance]}, scored{' '}
        {Math.round(seg.score)} of 100.
      </p>
      <dl className="now__facts">
        <dt>What you'll learn</dt>
        <dd>{seg.what_you_learn}</dd>
        <dt>Why it's worth watching</dt>
        <dd>{seg.why_valuable}</dd>
        {seg.prerequisites.length > 0 && (
          <>
            <dt>Helps to know first</dt>
            <dd>{seg.prerequisites.join('; ')}</dd>
          </>
        )}
      </dl>
      <a className="external" href={`${props.videoUrl}&t=${Math.floor(seg.start_s)}s`} target="_blank" rel="noreferrer">
        Open on YouTube at {clock(seg.start_s)}
      </a>
    </div>
  )
}

function SegmentRow(props: { seg: Segment; number?: number; active: boolean; done: boolean; onPlay: () => void }) {
  const { seg } = props
  return (
    <button
      type="button"
      className={`segment segment--${seg.importance}${props.active ? ' segment--active' : ''}`}
      onClick={props.onPlay}
      aria-current={props.active ? 'true' : undefined}
    >
      {props.number != null && <span className="segment__number">{props.number}</span>}
      <span className="segment__body">
        <span className="segment__title">
          {seg.title}
          {props.done && <span className="segment__done"> Watched</span>}
        </span>
        <span className="segment__meta">
          {clock(seg.start_s)}–{clock(seg.end_s)}, {duration(seg.duration_s)}, {IMPORTANCE_LABEL[seg.importance].toLowerCase()}
        </span>
        <span className="segment__learn">{seg.what_you_learn}</span>
      </span>
    </button>
  )
}

function Scoring({ analysis }: { analysis: Analysis }) {
  const v = analysis.validation
  return (
    <>
      <p className="muted">
        Each section's score is the AI's 1–10 ratings, weighted (educational value 30%, novelty 20%, practical 20%,
        depth 15%, relevance 15%, adjusted for your goal), plus rule-based adjustments listed with their reasons.
      </p>
      <div className="table-wrap">
        <table className="scores">
          <thead>
            <tr>
              <th scope="col">Section</th>
              <th scope="col">Time</th>
              <th scope="col" title="Educational value">Edu</th>
              <th scope="col">Novelty</th>
              <th scope="col">Practical</th>
              <th scope="col">Depth</th>
              <th scope="col">Relevance</th>
              <th scope="col">Rated</th>
              <th scope="col">Adjustments</th>
              <th scope="col">Score</th>
            </tr>
          </thead>
          <tbody>
            {analysis.sections.map((s) => {
              const r = s.evaluation.rubric
              return (
                <tr key={s.section.id}>
                  <th scope="row">
                    {s.section.title}
                    <span className="muted"> ({s.section.kind})</span>
                  </th>
                  <td className="num">{clock(s.section.start_s)}</td>
                  <td className="num">{r.educational_value}</td>
                  <td className="num">{r.novelty}</td>
                  <td className="num">{r.practical}</td>
                  <td className="num">{r.depth}</td>
                  <td className="num">{r.relevance}</td>
                  <td className="num">{s.score.weighted_rubric.toFixed(0)}</td>
                  <td>
                    {s.score.adjustments.map((a) => (
                      <div key={a.rule}>
                        {a.delta > 0 ? '+' : '−'}
                        {Math.abs(a.delta).toFixed(0)} {a.reason}
                      </div>
                    ))}
                  </td>
                  <td className="num">
                    <strong>{s.score.final.toFixed(0)}</strong> {IMPORTANCE_LABEL[s.importance].toLowerCase()}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="muted">
        Analyzed in {Math.round(analysis.elapsed_s)} s with {analysis.llm?.provider}{' '}
        ({[...new Set(Object.values(analysis.llm?.models ?? {}))].join(', ')}). Transcript language:{' '}
        {analysis.transcript_language}
        {analysis.transcript_generated ? ', auto-generated captions' : ''}.
      </p>
      {v.warnings.length > 0 && (
        <div className="notice">
          <strong>Checks that didn't pass</strong>
          <ul>
            {v.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      )}
      {v.repairs.length > 0 && (
        <details>
          <summary>{v.repairs.length} automatic fixes to the AI's output</summary>
          <ul>
            {v.repairs.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </details>
      )}
    </>
  )
}
