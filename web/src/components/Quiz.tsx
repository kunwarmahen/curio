import { useEffect, useState } from 'react'
import { ApiError, api, type QuizAttempt, type StudySet } from '../api'
import { TimeLink } from './TimeLink'

const LETTERS = 'ABCDE'

/** A multiple-choice quiz. After checking, each question shows the right answer, an
 *  explanation, and the moment in the video that answers it. */
export function QuizView({ study, onSeek }: { study: StudySet; onSeek: (t: number) => void }) {
  const [answers, setAnswers] = useState<Record<string, number>>({})
  const [result, setResult] = useState<QuizAttempt | null>(null)
  const [previous, setPrevious] = useState<QuizAttempt[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    api.quizAttempts(study.id).then(setPrevious, () => {})
  }, [study.id])

  const answered = Object.keys(answers).length
  const total = study.questions.length

  const check = async () => {
    setBusy(true)
    setErr('')
    try {
      const attempt = await api.gradeQuiz(study.id, answers)
      setResult(attempt)
      setPrevious((list) => [attempt, ...list])
      window.scrollTo({ top: (document.querySelector('.quiz')?.getBoundingClientRect().top ?? 0) + window.scrollY - 16 })
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not check the answers. Try again.')
    } finally {
      setBusy(false)
    }
  }

  const retry = () => {
    setAnswers({})
    setResult(null)
  }

  const best = previous.reduce((b, a) => Math.max(b, a.score), 0)

  return (
    <div className="quiz">
      {result ? (
        <div className="quiz__score" role="status">
          <p className="quiz__score-line">
            {result.score} of {result.total} right.
          </p>
          <p className="muted">
            {result.score === result.total
              ? 'Every answer is right.'
              : 'Each question below shows the right answer and the moment in the video that explains it.'}
          </p>
          <button type="button" className="button-quiet" onClick={retry}>
            Take the quiz again
          </button>
        </div>
      ) : (
        <p className="muted">
          {total} questions on what this part of the video teaches.
          {previous.length > 0 &&
            ` You've taken it ${previous.length === 1 ? 'once' : `${previous.length} times`}; best score ${best} of ${total}.`}
        </p>
      )}

      <ol className="quiz__list">
        {study.questions.map((q, n) => {
          const chosen = result ? result.answers[q.id] : answers[q.id]
          const right = result ? result.correct.includes(q.id) : false
          return (
            <li key={q.id} className="quiz__q">
              <fieldset disabled={!!result}>
                <legend>
                  <span className="quiz__num">{n + 1}.</span> {q.question}
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
                          name={`${study.id}-${q.id}`}
                          checked={chosen === i}
                          onChange={() => setAnswers((a) => ({ ...a, [q.id]: i }))}
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
                    Explained at <TimeLink t={q.start_s} onSeek={onSeek} />
                  </p>
                </div>
              )}
            </li>
          )
        })}
      </ol>

      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
      {!result && (
        <div className="quiz__submit">
          <button type="button" className="button-primary" onClick={check} disabled={busy || answered === 0}>
            {busy ? 'Checking…' : 'Check answers'}
          </button>
          {answered > 0 && answered < total && (
            <span className="hint">
              {total - answered} not answered yet; they count as wrong.
            </span>
          )}
        </div>
      )}
    </div>
  )
}
