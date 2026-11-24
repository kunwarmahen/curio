import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'
import { ApiError, api, type Analysis, type AnswerPart, type Citation } from '../api'
import { TimeLink } from './TimeLink'

/** What the thread needs from an answer, from a video or a whole course. */
interface Reply {
  id: string
  question: string
  parts: AnswerPart[]
  covered: boolean
  outside_knowledge: string
  warnings: string[]
}

interface ThreadProps {
  /** "video" or "course": the words the thread uses. */
  subject: string
  intro: string
  waiting: string
  suggestions: string[]
  load: () => Promise<Reply[]>
  send: (question: string) => Promise<Reply>
  clear: () => Promise<unknown>
  citation: (c: Citation) => ReactNode
}

/** A conversation of questions answered from transcripts, with timestamps. */
export function AskThread(props: ThreadProps) {
  const { load } = props
  const [answers, setAnswers] = useState<Reply[] | null>(null)
  const [question, setQuestion] = useState('')
  const [pending, setPending] = useState('')
  const [err, setErr] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    load().then(setAnswers, () => setAnswers([]))
  }, [load])

  const ask = async (q: string) => {
    const text = q.trim()
    if (!text || pending) return
    setPending(text)
    setQuestion('')
    setErr('')
    try {
      const a = await props.send(text)
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
    await props.clear().catch(() => {})
    setAnswers([])
  }

  if (answers === null) return <p className="muted">Loading…</p>

  return (
    <div className="ask">
      {answers.length === 0 && !pending && (
        <div className="ask__empty">
          <p className="muted">{props.intro}</p>
          <ul className="ask__suggestions" aria-label="Example questions">
            {props.suggestions.map((s) => (
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
              <p className="qa__question">{a.question}</p>
              <div className="qa__answer">
                {!a.covered && <p className="qa__notcovered">The {props.subject} doesn't cover this.</p>}
                {a.parts.map((p) => (
                  <p key={p.text}>
                    {p.text}
                    {p.citations.map((c) => (
                      <span key={`${c.video_id}:${c.start_s}`}> {props.citation(c)}</span>
                    ))}
                  </p>
                ))}
                {a.outside_knowledge && (
                  <aside className="qa__outside" aria-label={`Not from the ${props.subject}`}>
                    <strong>Not from the {props.subject}.</strong> {a.outside_knowledge}
                  </aside>
                )}
                {a.warnings.length > 0 && (
                  <ul className="qa__warnings">
                    {a.warnings.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}

      {pending && (
        <div className="qa qa--pending" aria-live="polite">
          <p className="qa__question">{pending}</p>
          <p className="muted">{props.waiting}</p>
        </div>
      )}
      <div ref={endRef} />

      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}

      <form className="ask__form" onSubmit={submit}>
        <label htmlFor={`ask-input-${props.subject}`} className="field-label">
          {answers.length ? 'Ask a follow-up' : 'Your question'}
        </label>
        <div className="ask__row">
          <textarea
            id={`ask-input-${props.subject}`}
            rows={2}
            maxLength={1000}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={onKey}
            placeholder={props.subject === 'course' ? 'Which lectures cover…' : 'What did the speaker mean by…'}
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

/** Ask the Video: questions answered from the transcript, with timestamps. */
export function AskPanel({ analysis, onSeek }: { analysis: Analysis; onSeek: (t: number) => void }) {
  const id = analysis.id
  const suggestions = useMemo(() => {
    const [a, b] = analysis.key_concepts
    const out = ['What are the main arguments of this video?']
    if (a) out.push(`Where does the speaker explain ${a}?`)
    if (a && b) out.push(`Compare ${a} and ${b}.`)
    out.push('Give me a practical example from the video.')
    return out
  }, [analysis.key_concepts])
  const load = useMemo(
    () => () => api.conversation(id).then((list) => list.map((a) => ({ ...a, covered: a.in_video }))),
    [id],
  )
  return (
    <AskThread
      subject="video"
      intro="Ask anything about this video. Answers come from the transcript, with the moments they're based on. Anything that isn't from the video is marked as such."
      waiting="Reading the transcript… The first question about a video takes longest; follow-ups are quicker."
      suggestions={suggestions}
      load={load}
      send={(q) => api.ask(id, q).then((a) => ({ ...a, covered: a.in_video }))}
      clear={() => api.clearConversation(id)}
      citation={(c) => <TimeLink t={c.start_s} onSeek={onSeek} />}
    />
  )
}
