import { useEffect, useMemo, useState } from 'react'
import { ApiError, api, type Analysis, type Notes, type NotesScope } from '../api'
import { clock, duration } from '../format'
import { TimeLink } from './TimeLink'

const SCOPES: { id: NotesScope; label: string }[] = [
  { id: 'playlist', label: 'Your playlist' },
  { id: 'video', label: 'Whole video' },
  { id: 'selected', label: 'Parts I pick' },
]

const SCOPE_PHRASE: Record<NotesScope, string> = {
  playlist: 'your playlist',
  video: 'the whole video',
  selected: 'the parts you picked',
}

const sameSet = (a: string[], b: Set<string>) => a.length === b.size && a.every((x) => b.has(x))

/** Learning notes for an analysis: pick what to cover, write, read, download. */
export function NotesPanel({ analysis, onSeek }: { analysis: Analysis; onSeek: (t: number) => void }) {
  const playlistIds = useMemo(() => analysis.playlist?.segments.map((s) => s.id) ?? [], [analysis.playlist])
  const [saved, setSaved] = useState<Notes[] | null>(null)
  const [scope, setScope] = useState<NotesScope>('playlist')
  const [picked, setPicked] = useState<Set<string>>(() => new Set(playlistIds))
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    api.notesList(analysis.id).then(
      (list) => {
        setSaved(list)
        const latest = list.find((n) => n.status !== 'failed')
        if (latest) {
          setScope(latest.scope)
          if (latest.scope === 'selected') setPicked(new Set(latest.segment_ids))
        }
      },
      () => setSaved([]),
    )
  }, [analysis.id])

  // The newest notes matching the current choice (the list is newest first).
  const shown =
    saved?.find((n) => n.scope === scope && (scope !== 'selected' || sameSet(n.segment_ids, picked))) ?? null
  const running = shown?.status === 'pending' || shown?.status === 'running'

  const upsert = (n: Notes) => setSaved((list) => [n, ...(list ?? []).filter((x) => x.id !== n.id)])

  useEffect(() => {
    if (!shown || !running) return
    const timer = window.setTimeout(() => {
      api.notes(shown.id).then(upsert, () => {})
    }, 1500)
    return () => window.clearTimeout(timer)
  }, [shown, running])

  const write = async (force: boolean) => {
    setBusy(true)
    setErr('')
    try {
      const { notes_id } = await api.writeNotes(analysis.id, scope, scope === 'selected' ? [...picked] : [], force)
      upsert(await api.notes(notes_id))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not start writing notes.')
    } finally {
      setBusy(false)
    }
  }

  const coverS = useMemo(() => {
    if (scope === 'video') {
      return analysis.sections
        .filter((s) => s.section.kind !== 'sponsor' && s.section.kind !== 'meta')
        .reduce((t, s) => t + s.section.end_s - s.section.start_s, 0)
    }
    const ids = scope === 'playlist' ? new Set(playlistIds) : picked
    return analysis.segments.filter((s) => ids.has(s.id)).reduce((t, s) => t + s.duration_s, 0)
  }, [scope, picked, playlistIds, analysis])

  const segments = useMemo(() => [...analysis.segments].sort((a, b) => a.start_s - b.start_s), [analysis.segments])
  const toggle = (id: string, on: boolean) =>
    setPicked((prev) => {
      const next = new Set(prev)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })

  if (saved === null) return <p className="muted">Loading notes…</p>

  return (
    <div className="notes">
      <fieldset className="goal notes__scope">
        <legend className="field-label">Take notes on</legend>
        <div className="segmented">
          {SCOPES.map((s) => (
            <label key={s.id} className="segmented__option">
              <input type="radio" name="notes-scope" checked={scope === s.id} onChange={() => setScope(s.id)} />
              <span>{s.label}</span>
            </label>
          ))}
        </div>
      </fieldset>

      {scope === 'selected' && (
        <fieldset className="notes__pick">
          <legend className="visually-hidden">Parts to take notes on</legend>
          {segments.map((seg) => (
            <label key={seg.id} className="checkbox notes__pick-row">
              <input type="checkbox" checked={picked.has(seg.id)} onChange={(e) => toggle(seg.id, e.target.checked)} />
              <span className="time">{clock(seg.start_s)}</span>
              <span>{seg.title}</span>
            </label>
          ))}
        </fieldset>
      )}

      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}

      {!shown && (
        <div className="notes__start">
          <p className="muted">
            Curio writes study notes from the transcript of {SCOPE_PHRASE[scope]} ({duration(coverS)} of video): key
            points, explanations, definitions and takeaways, each linked to the moment it's said. With a local model
            this takes about a minute for every 20 minutes of video.
          </p>
          <button
            type="button"
            className="button-primary"
            onClick={() => write(false)}
            disabled={busy || (scope === 'selected' && picked.size === 0)}
          >
            {busy ? 'Starting…' : 'Write notes'}
          </button>
          {scope === 'selected' && picked.size === 0 && <p className="hint">Pick at least one part.</p>}
        </div>
      )}

      {shown && running && (
        <p className="notes__progress" aria-live="polite">
          {shown.progress || 'Waiting to start'}…
        </p>
      )}

      {shown?.status === 'failed' && (
        <div className="notes__start">
          <p className="form-error" role="alert">
            Writing notes stopped: {shown.error || 'unknown error.'}
          </p>
          <button type="button" className="button-primary" onClick={() => write(true)} disabled={busy}>
            Write notes again
          </button>
        </div>
      )}

      {shown?.status === 'done' && (
        <NotesSheet notes={shown} onSeek={onSeek} onRewrite={() => write(true)} busy={busy} />
      )}
    </div>
  )
}

function NotesSheet(props: { notes: Notes; onSeek: (t: number) => void; onRewrite: () => void; busy: boolean }) {
  const { notes, onSeek } = props
  return (
    <article className="notesheet" aria-label="Learning notes">
      <div className="notesheet__bar">
        <p className="muted">
          Notes on {SCOPE_PHRASE[notes.scope]}: {notes.sections.length} sections, {duration(notes.watch_s)} of video.
          Written by AI from the transcript. Select a time to watch that moment.
        </p>
        <div className="notesheet__actions">
          <a className="button-quiet" href={api.notesMarkdownUrl(notes.id)} download>
            Download as Markdown
          </a>
          <button type="button" className="button-quiet" onClick={props.onRewrite} disabled={props.busy}>
            Write again
          </button>
        </div>
      </div>

      {notes.summary && <p className="notesheet__summary">{notes.summary}</p>}

      {notes.takeaways.length > 0 && (
        <section className="notesheet__block">
          <h3>Key takeaways</h3>
          <ol className="takeaways">
            {notes.takeaways.map((t) => (
              <li key={t.text}>
                <span className="takeaways__text">{t.text}</span> <TimeLink t={t.start_s} onSeek={onSeek} />
              </li>
            ))}
          </ol>
        </section>
      )}

      <section className="notesheet__block">
        <h3>Notes by section</h3>
        {notes.sections.map((s) => (
          <section key={s.section_id} className="note">
            <h4 className="note__title">
              <TimeLink t={s.start_s} onSeek={onSeek} />
              <span>{s.title}</span>
            </h4>
            {s.points.length > 0 ? (
              <ul className="note__points">
                {s.points.map((p) => (
                  <li key={`${p.start_s}-${p.text}`}>
                    {p.text} <TimeLink t={p.start_s} onSeek={onSeek} />
                  </li>
                ))}
              </ul>
            ) : (
              !s.explanation && <p className="muted">No notes for this section.</p>
            )}
            {s.explanation && <p className="note__explain">{s.explanation}</p>}
          </section>
        ))}
      </section>

      {notes.glossary.length > 0 && (
        <section className="notesheet__block">
          <h3>Definitions</h3>
          <dl className="glossary">
            {notes.glossary.map((d) => (
              <div key={d.term}>
                <dt>{d.term}</dt>
                <dd>
                  {d.definition} <TimeLink t={d.start_s} onSeek={onSeek} />
                </dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      {notes.recommendations.length > 0 && (
        <section className="notesheet__block">
          <h3>What to do next</h3>
          <ul className="note__points">
            {notes.recommendations.map((r) => (
              <li key={r.text}>
                {r.text} <TimeLink t={r.start_s} onSeek={onSeek} />
              </li>
            ))}
          </ul>
        </section>
      )}

      {notes.warnings.length > 0 && (
        <details className="notesheet__warnings">
          <summary>{notes.warnings.length} things to know about these notes</summary>
          <ul>
            {notes.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </details>
      )}
    </article>
  )
}
