import { useCallback, useEffect, useState } from 'react'
import { ApiError, api, type CardGrade, type DueCard, type FlashcardDeck } from '../api'
import { BOX_DAYS, clock, intervalLabel, until } from '../format'
import { TimeLink } from './TimeLink'

const GRADES: { grade: CardGrade; label: string; key: string }[] = [
  { grade: 'again', label: 'Forgot', key: '1' },
  { grade: 'good', label: 'Remembered', key: '2' },
  { grade: 'easy', label: 'Easy', key: '3' },
]

function nextLabel(box: number, grade: CardGrade): string {
  if (grade === 'again') return 'again in 10 min'
  const next = Math.min(box + (grade === 'easy' ? 2 : 1), 6)
  return `next in ${intervalLabel(BOX_DAYS[next])}`
}

/** Review the flashcards that are due: one set's (in a video's tab), one course's, or
 *  every deck's. With `onSeek`, a card's time plays in the page's player; otherwise it
 *  opens YouTube. `label` names a card's lecture in a course. */
export function FlashcardSession(props: {
  studyId?: string
  courseId?: string
  onSeek?: (t: number, due: DueCard) => void
  label?: (due: DueCard) => string
}) {
  const { studyId, courseId, onSeek, label } = props
  const [deck, setDeck] = useState<FlashcardDeck | null>(null)
  const [index, setIndex] = useState(0)
  const [revealed, setRevealed] = useState(false)
  const [reviewed, setReviewed] = useState(0)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(() => {
    api.flashcards(studyId, 50, courseId).then(
      (d) => {
        setDeck(d)
        setIndex(0)
        setRevealed(false)
      },
      (e) => setErr(e instanceof ApiError ? e.message : 'Could not load the flashcards.'),
    )
  }, [studyId, courseId])
  useEffect(load, [load])

  const due = deck?.cards[index]

  const grade = useCallback(
    async (g: CardGrade) => {
      if (!due || busy) return
      setBusy(true)
      setErr('')
      try {
        await api.reviewCard(due.study_id, due.card.id, g)
        setReviewed((n) => n + 1)
        if (deck && index + 1 < deck.cards.length) {
          setIndex(index + 1)
          setRevealed(false)
        } else {
          load()
        }
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : 'Could not save that review. Try again.')
      } finally {
        setBusy(false)
      }
    },
    [due, busy, deck, index, load],
  )

  // Space or Enter shows the answer; 1, 2, 3 grade it. Not while typing or on a button
  // (the button's own key handling already clicks it).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement
      const typing =
        /^(TEXTAREA|SELECT|BUTTON)$/.test(el.tagName) ||
        el.isContentEditable ||
        (el instanceof HTMLInputElement && el.type !== 'radio' && el.type !== 'checkbox')
      if (e.metaKey || e.ctrlKey || e.altKey || typing) return
      if (!due) return
      if (!revealed && (e.key === ' ' || e.key === 'Enter')) {
        e.preventDefault()
        setRevealed(true)
      } else if (revealed) {
        const g = GRADES.find((x) => x.key === e.key)
        if (g) grade(g.grade)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [due, revealed, grade])

  if (err && !deck) {
    return (
      <p className="form-error" role="alert">
        {err}
      </p>
    )
  }
  if (!deck) return <p className="muted">Loading flashcards…</p>
  if (deck.total === 0) return <p className="muted">No flashcards yet.</p>

  if (!due) {
    return (
      <div className="cards__done" role="status">
        <p className="cards__done-title">{reviewed ? `Done: ${reviewed} reviewed.` : 'Nothing to review right now.'}</p>
        <p className="muted">
          {deck.next_due_at
            ? `The next card is due ${until(deck.next_due_at)}. Curio spaces reviews out: each card you remember comes back later than the last time.`
            : 'Every card has been reviewed.'}
        </p>
      </div>
    )
  }

  const more = deck.due - deck.cards.length
  const t = due.card.start_s
  return (
    <div className="cards">
      <p className="cards__count muted">
        Card {index + 1} of {deck.cards.length}
        {more > 0 ? ` (${more} more after these)` : ''}
        {due.state.box === 0 ? '. A new card.' : '.'}
      </p>

      <article className="indexcard" aria-live="polite">
        {!studyId && <p className="indexcard__video">{label ? label(due) : due.video_title}</p>}
        <p className="indexcard__front">{due.card.front}</p>
        {revealed && (
          <div className="indexcard__back">
            <p>{due.card.back}</p>
            <p className="indexcard__source">
              {onSeek ? (
                <>
                  Said at <TimeLink t={t} onSeek={(x) => onSeek(x, due)} />
                </>
              ) : (
                <a href={`${due.video_url}&t=${Math.floor(t)}s`} target="_blank" rel="noreferrer">
                  Watch where it's said ({clock(t)}) on YouTube
                </a>
              )}
            </p>
          </div>
        )}
      </article>

      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}

      {!revealed ? (
        <div className="cards__actions">
          <button type="button" className="button-primary" onClick={() => setRevealed(true)}>
            Show answer
          </button>
          <span className="hint">or press Space</span>
        </div>
      ) : (
        <>
        <div className="cards__actions" role="group" aria-label="How well did you remember it?">
          {GRADES.map((g) => (
            <button
              key={g.grade}
              type="button"
              className={g.grade === 'good' ? 'button-primary cards__grade' : 'button-quiet cards__grade'}
              onClick={() => grade(g.grade)}
              disabled={busy}
            >
              <span>{g.label}</span>
              <span className="cards__next">{nextLabel(due.state.box, g.grade)}</span>
            </button>
          ))}
        </div>
        <p className="hint">Keys: 1 Forgot, 2 Remembered, 3 Easy.</p>
        </>
      )}
    </div>
  )
}
