import type { RecentVideo } from '../api'
import { PROFILES, duration } from '../format'
import { linkProps } from '../router'
import { WatchMap } from './WatchMap'

/** Analyzed videos, newest first, each with its watch map. */
export function VideoList({ videos }: { videos: RecentVideo[] }) {
  return (
    <ul className="recent__list">
      {videos.map((r) => (
        <li key={r.analysis_id}>
          <a className="recent__item" {...linkProps(`/v/${r.analysis_id}`)}>
            <img src={r.video.thumbnail_url} alt="" loading="lazy" />
            <div className="recent__body">
              <span className="recent__title">{r.video.title}</span>
              <span className="muted">
                {r.video.channel}. Watch {duration(r.playlist_s)} of {duration(r.video.duration_s)},{' '}
                {PROFILES[r.profile]?.label.toLowerCase() ?? r.profile} goal.
              </span>
              <WatchMap duration={r.video.duration_s} stretches={r.watch_map.map((s, i) => ({ ...s, key: String(i) }))} />
            </div>
          </a>
        </li>
      ))}
    </ul>
  )
}
