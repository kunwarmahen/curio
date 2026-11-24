import { useEffect, useState } from 'react'
import { ApiError, api, type QuizAttempt, type StudySet } from '../api'
import { TimeLink } from './TimeLink'

const LETTERS = 'ABCDE'

interface QuizPart {
  study: StudySet
  /** Shown above this set's questions (a lecture, in a course quiz). */
  heading?: string
}

/** A multiple-choice quiz: one video's set, or a course's sets (one per lecture) checked
 *  together. After checking, each question shows the right answer, an explanation, and
 *  the moment in the video that answers it. */
export function QuizView(props: {
  study?: StudySet
  sets?: QuizPart[]
  onSeek: (t: number, study: StudySet) => void
}) {
  const parts: QuizPart[] = props.sets ?? (props.study ? [{ study: props.study }] : [])
  const single = parts.length === 1 ? parts[0].study : null
  const [answers, setAnswers] = useState<Record<string, number>>({})
  const [results, setResults] = useState<Record<string, QuizAttempt> | null>(null)
  const [previous, setPrevious] = useState<QuizAttempt[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const singleId = single?.id

  useEffect(() => {
    if (singleId) api.quizAttempts(singleId).then(setPrevious, () => {})
  }, [singleId])

  const key = (s: StudySet, qid: string) => `${s.id}:${qid}`
  const answered = Object.keys(answers).length
  const total = parts.reduce((n, p) => n + p.study.questions.length, 0)

  const check = async () => {
    setBusy(true)
    setErr('')
    try {
      const graded = await Promise.all(
        parts.map((p) => {
          const mine: Record<string, number> = {}
          for (const q of p.study.questions) {
            const a = answers[key(p.study, q.id)]
            if (a !== undefined) mine[q.id] = a
          }
          return api.gradeQuiz(p.study.id, mine)
        }),
      )
      setResults(Object.fromEntries(graded.map((a, i) => [parts[i].study.id, a])))
      if (single) setPrevious((list) => [graded[0], ...list])
      window.scrollTo({ top: (document.querySelector('.quiz')?.getBoundingClientRect().top ?? 0) + window.scrollY - 16 })
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not check the answers. Try again.')
    } finally {
      setBusy(false)
    }
  }

  const retry = () => {
    setAnswers({})
    setResults(null)
  }

  const score = results ? Object.values(results).reduce((n, a) => n + a.score, 0) : 0
  const best = previous.reduce((b, a) => Math.max(b, a.score), 0)
  // Questions are numbered across the whole quiz.
  const offsets = parts.map((_, i) => parts.slice(0, i).reduce((n, p) => n + p.study.questions.length, 0))

  return (
    <div className="quiz">
      {results ? (
        <div className="quiz__score" role="status">
          <p className="quiz__score-line">
            {score} of {total} right.
          </p>
          <p className="muted">
            {score === total
              ? 'Every answer is right.'
              : 'Each question below shows the right answer and the moment in the video that explains it.'}
          </p>
          <button type="button" className="button-quiet" onClick={retry}>
            Take the quiz again
          </button>
        </div>
      ) : (
        <p className="muted">
          {total} questions on what {single ? 'this part of the video teaches' : 'the course playlist teaches'}.
          {previous.length > 0 &&
            ` You've taken it ${previous.length === 1 ? 'once' : `${previous.length} times`}; best score ${best} of ${total}.`}
        </p>
      )}

      {parts.map((part, pi) => {
        const result = results?.[part.study.id] ?? null
        return (
          <div key={part.study.id} className="quiz__part">
            {part.heading && <h3 className="quiz__heading">{part.heading}</h3>}
            <ol className="quiz__list" start={offsets[pi] + 1}>
              {part.study.questions.map((q, qi) => {
                const number = offsets[pi] + qi + 1
                const k = key(part.study, q.id)
                const chosen = result ? result.answers[q.id] : answers[k]
                const right = result ? result.correct.includes(q.id) : false
                return (
                  <li key={q.id} className="quiz__q">
                    <fieldset disabled={!!result}>
                      <legend>
                        <span className="quiz__num">{number}.</span> {q.question}
                      </legend>
                      <div className="quiz__options">
                        {q.options.map((o, i) => {
                          let cls = 'quiz__option'
                          if (result && i === q.answer) cls += ' quiz__option--answer'
                          if (result && i === chosen && i !== q.answer) cls += ' quiz__option--wrong'
                          return (
                            <label key={o} className={cls}>
                              <input
                                type="radio"
                                name={k}
                                checked={chosen === i}
                                onChange={() => setAnswers((a) => ({ ...a, [k]: i }))}
                              />
                              <span className="quiz__letter" aria-hidden>
                                {LETTERS[i]}
                              </span>
                              <span>{o}</span>
                              {result && i === q.answer && <span className="visually-hidden"> (right answer)</span>}
                              {result && i === chosen && i !== q.answer && (
                                <span className="visually-hidden"> (your answer)</span>
                              )}
                            </label>
                          )
                        })}
                      </div>
                    </fieldset>
                    {result && (
                      <div className="quiz__feedback">
                        <p className={right ? 'quiz__verdict' : 'quiz__verdict quiz__verdict--wrong'}>
                          {right ? 'Right.' : chosen === undefined ? 'Not answered.' : 'Not quite.'}
                        </p>
                        {q.explanation && <p>{q.explanation}</p>}
                        <p className="quiz__source">
                          Explained at <TimeLink t={q.start_s} onSeek={(t) => props.onSeek(t, part.study)} />
                        </p>
                      </div>
                    )}
                  </li>
                )
              })}
            </ol>
          </div>
        )
      })}

      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
      {!results && (
        <div className="quiz__submit">
          <button type="button" className="button-primary" onClick={check} disabled={busy || answered === 0}>
            {busy ? 'Checking…' : 'Check answers'}
          </button>
          {answered > 0 && answered < total && (
            <span className="hint">{total - answered} not answered yet; they count as wrong.</span>
          )}
        </div>
      )}
    </div>
  )
}
