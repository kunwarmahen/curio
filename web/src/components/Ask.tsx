import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { ApiError, api, type Analysis, type Answer } from '../api'
import { TimeLink } from './TimeLink'

/** Ask the Video: questions answered from the transcript, with timestamps. */
export function AskPanel({ analysis, onSeek }: { analysis: Analysis; onSeek: (t: number) => void }) {
  const [answers, setAnswers] = useState<Answer[] | null>(null)
  const [question, setQuestion] = useState('')
  const [pending, setPending] = useState('')
  const [err, setErr] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    api.conversation(analysis.id).then(setAnswers, () => setAnswers([]))
  }, [analysis.id])

  const suggestions = useMemo(() => {
    const [a, b] = analysis.key_concepts
    const out = ['What are the main arguments of this video?']
    if (a) out.push(`Where does the speaker explain ${a}?`)
    if (a && b) out.push(`Compare ${a} and ${b}.`)
    out.push('Give me a practical example from the video.')
    return out
  }, [analysis.key_concepts])

  const ask = async (q: string) => {
    const text = q.trim()
    if (!text || pending) return
    setPending(text)
    setQuestion('')
    setErr('')
    try {
      const a = await api.ask(analysis.id, text)
      setAnswers((list) => [...(list ?? []), a])
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not answer that question.')
      setQuestion(text)
    } finally {
      setPending('')
      requestAnimationFrame(() => endRef.current?.scrollIntoView({ block: 'nearest' }))
    }
  }

  const submit = (e: FormEvent) => {
    e.preventDefault()
    ask(question)
  }
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      ask(question)
    }
  }
  const clear = async () => {
    await api.clearConversation(analysis.id).catch(() => {})
    setAnswers([])
  }

  if (answers === null) return <p className="muted">Loading…</p>

  return (
    <div className="ask">
      {answers.length === 0 && !pending && (
        <div className="ask__empty">
          <p className="muted">
            Ask anything about this video. Answers come from the transcript, with the moments they're based on.
            Anything that isn't from the video is marked as such.
          </p>
          <ul className="ask__suggestions" aria-label="Example questions">
            {suggestions.map((s) => (
              <li key={s}>
                <button type="button" className="chip" onClick={() => ask(s)}>
                  {s}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {answers.length > 0 && (
        <ol className="ask__thread" aria-label="Your questions">
          {answers.map((a) => (
            <li key={a.id} className="qa">
              <AnswerView answer={a} onSeek={onSeek} />
            </li>
          ))}
        </ol>
      )}

      {pending && (
        <div className="qa qa--pending" aria-live="polite">
          <p className="qa__question">{pending}</p>
          <p className="muted">
            Reading the transcript… The first question about a video takes longest; follow-ups are quicker.
          </p>
        </div>
      )}
      <div ref={endRef} />

      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}

      <form className="ask__form" onSubmit={submit}>
        <label htmlFor="ask-input" className="field-label">
          {answers.length ? 'Ask a follow-up' : 'Your question'}
        </label>
        <div className="ask__row">
          <textarea
            id="ask-input"
            rows={2}
            maxLength={1000}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={onKey}
            placeholder="What did the speaker mean by…"
            disabled={!!pending}
          />
          <button type="submit" className="button-primary" disabled={!!pending || !question.trim()}>
            {pending ? 'Answering…' : 'Ask'}
          </button>
        </div>
        {answers.length > 0 && (
          <button type="button" className="ask__clear" onClick={clear} disabled={!!pending}>
            Clear the conversation
          </button>
        )}
      </form>
    </div>
  )
}

function AnswerView({ answer, onSeek }: { answer: Answer; onSeek: (t: number) => void }) {
  return (
    <>
      <p className="qa__question">{answer.question}</p>
      <div className="qa__answer">
        {!answer.in_video && <p className="qa__notcovered">The video doesn't cover this.</p>}
        {answer.parts.map((p) => (
          <p key={p.text}>
            {p.text}
            {p.citations.map((c) => (
              <span key={c.start_s}>
                {' '}
                <TimeLink t={c.start_s} onSeek={onSeek} />
              </span>
            ))}
          </p>
        ))}
        {answer.outside_knowledge && (
          <aside className="qa__outside" aria-label="Not from the video">
            <strong>Not from the video.</strong> {answer.outside_knowledge}
          </aside>
        )}
        {answer.warnings.length > 0 && (
          <ul className="qa__warnings">
            {answer.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        )}
      </div>
    </>
  )
}
