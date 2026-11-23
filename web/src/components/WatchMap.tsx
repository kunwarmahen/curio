import { useState } from 'react'
import type { Importance } from '../api'
import { IMPORTANCE_LABEL, clock } from '../format'

export interface Stretch {
  key: string
  start_s: number
  end_s: number
  importance: Importance
  in_playlist: boolean
  title?: string
}

interface Props {
  duration: number
  stretches: Stretch[]
  /** Interactive full-size map; otherwise a small read-only one. */
  onPick?: (stretch: Stretch) => void
  playhead?: number | null
  activeKey?: string | null
}

/**
 * The whole video as one line, marked up the way a student marks a text:
 * highlight strength shows how much a stretch is worth, an ink underline shows
 * what is in your playlist, and the red line is where playback is.
 */
export function WatchMap({ duration, stretches, onPick, playhead, activeKey }: Props) {
  const [hover, setHover] = useState<Stretch | null>(null)
  const pct = (s: number) => `${(Math.min(Math.max(s, 0), duration) / duration) * 100}%`
  const mini = !onPick
  const ticks = mini ? [] : axisTicks(duration)

  return (
    <div className={mini ? 'watchmap watchmap--mini' : 'watchmap'}>
      <div className="watchmap__track" role={mini ? 'img' : 'group'} aria-label={mini ? mapSummary(stretches) : 'Video sections'}>
        {stretches.map((s) => {
          const style = { left: pct(s.start_s), width: `calc(${pct(s.end_s - s.start_s)} - 2px)` }
          const cls = [
            'stretch',
            `stretch--${s.importance}`,
            s.in_playlist && 'stretch--playlist',
            activeKey === s.key && 'stretch--active',
          ]
            .filter(Boolean)
            .join(' ')
          if (mini) return <span key={s.key} className={cls} style={style} />
          return (
            <button
              key={s.key}
              type="button"
              className={cls}
              style={style}
              aria-label={`${s.title}, ${clock(s.start_s)} to ${clock(s.end_s)}, ${IMPORTANCE_LABEL[s.importance]}${s.in_playlist ? ', in your playlist' : ''}. Play.`}
              onClick={() => onPick(s)}
              onMouseEnter={() => setHover(s)}
              onMouseLeave={() => setHover(null)}
              onFocus={() => setHover(s)}
              onBlur={() => setHover(null)}
            />
          )
        })}
        {playhead != null && <span className="watchmap__playhead" style={{ left: pct(playhead) }} aria-hidden />}
        {hover && (
          <div
            className="watchmap__tip"
            style={{ left: pct((hover.start_s + hover.end_s) / 2) }}
            role="presentation"
          >
            <strong>{hover.title}</strong>
            <span>
              {clock(hover.start_s)}–{clock(hover.end_s)}, {IMPORTANCE_LABEL[hover.importance].toLowerCase()}
              {hover.in_playlist ? ', in your playlist' : ''}
            </span>
          </div>
        )}
      </div>
      {ticks.length > 0 && (
        <div className="watchmap__axis" aria-hidden>
          {ticks.map((t) => (
            <span key={t} style={{ left: pct(t) }}>
              {clock(t)}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function axisTicks(duration: number): number[] {
  const steps = [60, 300, 600, 900, 1200, 1800, 3600]
  const step = steps.find((s) => duration / s <= 8) ?? 3600
  const ticks: number[] = []
  for (let t = 0; t < duration - step * 0.4; t += step) ticks.push(t)
  return ticks
}

function mapSummary(stretches: Stretch[]): string {
  const n = stretches.filter((s) => s.in_playlist).length
  return `${n} of ${stretches.length} sections are in the playlist`
}

export function WatchMapLegend() {
  return (
    <ul className="legend" aria-label="Watch map key">
      {(['essential', 'recommended', 'optional', 'skip'] as Importance[]).map((i) => (
        <li key={i}>
          <span className={`legend__swatch stretch--${i}`} />
          {IMPORTANCE_LABEL[i]}
        </li>
      ))}
      <li>
        <span className="legend__swatch legend__swatch--underline" />
        In your playlist
      </li>
    </ul>
  )
}
