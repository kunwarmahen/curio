import { useState } from 'react'
import { ApiError, api, type QueueItem, type QueueStatus } from '../api'
import { PROFILES, duration, localTime, queueHours } from '../format'
import { linkProps, navigate } from '../router'

/** What the queue is waiting for, in a sentence. */
function stateLine(q: QueueStatus): string {
  const hours = queueHours(q.window_start, q.window_end)
  switch (q.state) {
    case 'waiting_window':
      return `Curio analyzes these one at a time ${hours}, when the GPU is free. The first starts at ${localTime(
        q.window_start,
      )}.`
    case 'waiting_gpu':
      return `${q.message} Curio will try again in a minute.`
    case 'busy':
      return 'Curio is busy with another job. The queue continues when it finishes.'
    case 'starting':
      return 'The next video starts within a minute.'
    default:
      return ''
  }
}

/** The overnight queue on the home page: waiting and running videos. */
export function QueueList({ queue, onChange }: { queue: QueueStatus; onChange: () => void }) {
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  if (queue.items.length === 0) return null
  const waiting = queue.items.filter((i) => i.status === 'queued').length

  const act = async (item: QueueItem, action: 'start' | 'remove') => {
    setBusy(item.analysis_id)
    setErr('')
    try {
      if (action === 'start') {
        await api.queueStart(item.analysis_id)
        navigate(`/v/${item.analysis_id}`)
        return
      }
      await api.queueRemove(item.analysis_id)
      onChange()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'That didn’t work. Try again.')
      onChange()
    } finally {
      setBusy('')
    }
  }

  return (
    <section className="recent queue" aria-labelledby="queue-heading">
      <h2 id="queue-heading">{waiting ? `Queued for later (${waiting})` : 'Analyzing now'}</h2>
      {waiting > 0 && <p className="muted queue__state">{stateLine(queue)}</p>}
      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
      <ul className="recent__list">
        {queue.items.map((item) => (
          <li key={item.analysis_id} className="queue__row">
            <a className="recent__item" {...linkProps(`/v/${item.analysis_id}`)}>
              <img src={item.video.thumbnail_url} alt="" loading="lazy" />
              <div className="recent__body">
                <span className="recent__title">{item.video.title}</span>
                <span className="muted">
                  {item.video.channel}. {duration(item.video.duration_s)},{' '}
                  {PROFILES[item.preferences.profile]?.label.toLowerCase() ?? item.preferences.profile} goal.
                </span>
                <span className={item.status === 'queued' ? 'queue__badge' : 'queue__badge queue__badge--live'}>
                  {item.status === 'queued' ? 'Waiting' : `Analyzing${item.progress ? `: ${item.progress}` : ''}`}
                </span>
              </div>
            </a>
            {item.status === 'queued' && (
              <div className="queue__actions">
                <button
                  type="button"
                  className="button-quiet"
                  onClick={() => act(item, 'start')}
                  disabled={!!busy}
                >
                  Analyze now
                </button>
                <button
                  type="button"
                  className="button-quiet"
                  onClick={() => act(item, 'remove')}
                  disabled={!!busy}
                  aria-label={`Remove ${item.video.title} from the queue`}
                >
                  Remove
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
