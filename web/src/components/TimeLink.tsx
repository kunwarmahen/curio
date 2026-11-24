import { clock } from '../format'

/** A timestamp that plays the video from that moment. */
export function TimeLink({ t, onSeek }: { t: number; onSeek: (t: number) => void }) {
  return (
    <button type="button" className="timelink" onClick={() => onSeek(t)} aria-label={`Watch from ${clock(t)}`}>
      {clock(t)}
    </button>
  )
}
