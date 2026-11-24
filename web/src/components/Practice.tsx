import { useEffect, useState } from 'react'
import { ApiError, api, type Analysis, type NotesScope, type StudySet } from '../api'
import { linkProps } from '../router'
import { FlashcardSession } from './Flashcards'
import { QuizView } from './Quiz'

const SCOPES: { id: NotesScope; label: string }[] = [
  { id: 'playlist', label: 'Your playlist' },
  { id: 'video', label: 'Whole video' },
]

/** Quiz and flashcards for an analysis: write them, take the quiz, review the cards. */
export function PracticePanel({ analysis, onSeek }: { analysis: Analysis; onSeek: (t: number) => void }) {
  const [sets, setSets] = useState<StudySet[] | null>(null)
  const [scope, setScope] = useState<NotesScope>('playlist')
  const [mode, setMode] = useState<'quiz' | 'cards'>('quiz')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    api.studyList(analysis.id).then(
      (list) => {
        setSets(list)
        const latest = list.find((s) => s.status !== 'failed' && s.scope !== 'selected')
        if (latest) setScope(latest.scope)
      },
      () => setSets([]),
    )
  }, [analysis.id])

  const shown = sets?.find((s) => s.scope === scope) ?? null
  const running = shown?.status === 'pending' || shown?.status === 'running'
  const upsert = (s: StudySet) => setSets((list) => [s, ...(list ?? []).filter((x) => x.id !== s.id)])

  useEffect(() => {
    if (!shown || !running) return
    const timer = window.setTimeout(() => {
      api.study(shown.id).then(upsert, () => {})
    }, 1500)
    return () => window.clearTimeout(timer)
  }, [shown, running])

  const write = async (force: boolean) => {
    setBusy(true)
    setErr('')
    try {
      const { study_id } = await api.writeStudy(analysis.id, scope, force)
      upsert(await api.study(study_id))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not start writing the quiz.')
    } finally {
      setBusy(false)
    }
  }

  if (sets === null) return <p className="muted">Loading…</p>

  return (
    <div className="practice">
      <fieldset className="goal notes__scope">
        <legend className="field-label">Practice on</legend>
        <div className="segmented">
          {SCOPES.map((s) => (
            <label key={s.id} className="segmented__option">
              <input type="radio" name="practice-scope" checked={scope === s.id} onChange={() => setScope(s.id)} />
              <span>{s.label}</span>
            </label>
          ))}
        </div>
      </fieldset>

      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}

      {!shown && (
        <div className="notes__start">
          <p className="muted">
            Curio writes a multiple-choice quiz and a deck of flashcards from the transcript of{' '}
            {scope === 'playlist' ? 'your playlist' : 'the whole video'}. Every question and card links to the moment
            that answers it. With a local model this takes about a minute for every 20 minutes of video.
          </p>
          <button type="button" className="button-primary" onClick={() => write(false)} disabled={busy}>
            {busy ? 'Starting…' : 'Write a quiz and flashcards'}
          </button>
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
            Writing the quiz stopped: {shown.error || 'unknown error.'}
          </p>
          <button type="button" className="button-primary" onClick={() => write(true)} disabled={busy}>
            Try again
          </button>
        </div>
      )}

      {shown?.status === 'done' && (
        <>
          <div className="practice__bar">
            <fieldset className="segmented practice__mode">
              <legend className="visually-hidden">Quiz or flashcards</legend>
              <label className="segmented__option">
                <input type="radio" name="practice-mode" checked={mode === 'quiz'} onChange={() => setMode('quiz')} />
                <span>Quiz ({shown.questions.length})</span>
              </label>
              <label className="segmented__option">
                <input type="radio" name="practice-mode" checked={mode === 'cards'} onChange={() => setMode('cards')} />
                <span>Flashcards ({shown.cards.length})</span>
              </label>
            </fieldset>
            <button type="button" className="button-quiet" onClick={() => write(true)} disabled={busy}>
              Write new questions
            </button>
          </div>
          {mode === 'quiz' &&
            (shown.questions.length ? (
              <QuizView key={shown.id} study={shown} onSeek={onSeek} />
            ) : (
              <p className="muted">No questions were written for this part of the video.</p>
            ))}
          {mode === 'cards' && (
            <>
              <FlashcardSession key={shown.id} studyId={shown.id} onSeek={onSeek} />
              <p className="hint">
                Cards you remember come back after longer and longer gaps. Review the cards from all your videos on
                the <a {...linkProps('/review')}>review page</a>.
              </p>
            </>
          )}
          {shown.warnings.length > 0 && (
            <details className="notesheet__warnings">
              <summary>{shown.warnings.length} things to know about these questions</summary>
              <ul>
                {shown.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </div>
  )
}
