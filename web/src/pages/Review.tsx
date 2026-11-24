import { FlashcardSession } from '../components/Flashcards'
import { linkProps } from '../router'

/** Flashcards due from every video, in one session. */
export function Review() {
  return (
    <main className="page review">
      <a className="back" {...linkProps('/')}>
        ← All videos
      </a>
      <h1>Review flashcards</h1>
      <p className="muted review__lede">
        The cards due today from all your videos. Say honestly whether you remembered each one: Curio brings back
        the ones you forgot in a few minutes and spaces out the ones you know.
      </p>
      <FlashcardSession />
    </main>
  )
}
