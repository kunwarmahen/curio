import { useEffect, useLayoutEffect, useRef, useState } from 'react'

// Minimal typing for the YouTube IFrame Player API we use.
interface YTPlayer {
  loadVideoById(opts: { videoId: string; startSeconds: number; endSeconds: number }): void
  cueVideoById(opts: { videoId: string; startSeconds: number; endSeconds: number }): void
  getCurrentTime(): number
  setPlaybackRate(rate: number): void
  getPlaybackRate(): number
  destroy(): void
}
declare global {
  interface Window {
    YT?: {
      Player: new (el: HTMLElement, opts: object) => YTPlayer
      PlayerState: { ENDED: number; PLAYING: number }
    }
    onYouTubeIframeAPIReady?: () => void
  }
}

let apiReady: Promise<void> | null = null
function loadYouTubeApi(): Promise<void> {
  if (window.YT?.Player) return Promise.resolve()
  apiReady ??= new Promise((resolve, reject) => {
    const previous = window.onYouTubeIframeAPIReady
    window.onYouTubeIframeAPIReady = () => {
      previous?.()
      resolve()
    }
    const script = document.createElement('script')
    script.src = 'https://www.youtube.com/iframe_api'
    script.onerror = () => reject(new Error('YouTube player failed to load'))
    document.head.appendChild(script)
  })
  return apiReady
}

export interface Clip {
  key: string
  start_s: number
  end_s: number
  /** Changes to restart the same clip (replay). */
  nonce: number
  /** false: cue the clip without starting playback (initial page load). */
  autoplay: boolean
}

interface Props {
  videoId: string
  clip: Clip | null
  onTime: (seconds: number) => void
  onClipEnd: () => void
}

const RATES = [1, 1.25, 1.5, 1.75, 2]

/** The original YouTube video, played one clip at a time. Never re-hosted. */
export function Player({ videoId, clip, onTime, onClipEnd }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const player = useRef<YTPlayer | null>(null)
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState(false)
  const [rate, setRate] = useState(1)
  const rateRef = useRef(1)
  const handlers = useRef({ onTime, onClipEnd })
  useLayoutEffect(() => {
    handlers.current = { onTime, onClipEnd }
  })

  useEffect(() => {
    let cancelled = false
    loadYouTubeApi()
      .then(() => {
        if (cancelled || !host.current || !window.YT) return
        player.current = new window.YT.Player(host.current, {
          host: 'https://www.youtube-nocookie.com',
          videoId,
          playerVars: { rel: 0, modestbranding: 1, playsinline: 1 },
          events: {
            onReady: () => setReady(true),
            onStateChange: (e: { data: number }) => {
              if (e.data === window.YT?.PlayerState.ENDED) handlers.current.onClipEnd()
            },
          },
        })
      })
      .catch(() => setFailed(true))
    return () => {
      cancelled = true
      player.current?.destroy()
      player.current = null
    }
  }, [videoId])

  useEffect(() => {
    if (!ready || !clip || !player.current) return
    const opts = { videoId, startSeconds: clip.start_s, endSeconds: clip.end_s }
    if (clip.autoplay) player.current.loadVideoById(opts)
    else player.current.cueVideoById(opts)
    player.current.setPlaybackRate(rateRef.current)
  }, [ready, videoId, clip])

  useEffect(() => {
    if (!ready) return
    const id = window.setInterval(() => {
      const t = player.current?.getCurrentTime()
      if (typeof t === 'number') handlers.current.onTime(t)
    }, 250)
    return () => window.clearInterval(id)
  }, [ready])

  const changeRate = (r: number) => {
    setRate(r)
    rateRef.current = r
    player.current?.setPlaybackRate(r)
  }

  return (
    <div className="player">
      <div className="player__frame">
        {failed ? (
          <p className="player__fallback">
            The YouTube player didn't load. Use the "Open on YouTube" link below to watch this part.
          </p>
        ) : (
          <div ref={host} />
        )}
      </div>
      <div className="player__rates" role="group" aria-label="Playback speed">
        {RATES.map((r) => (
          <button
            key={r}
            type="button"
            className="chip"
            aria-pressed={rate === r}
            onClick={() => changeRate(r)}
            disabled={!ready}
          >
            {r}×
          </button>
        ))}
      </div>
    </div>
  )
}
