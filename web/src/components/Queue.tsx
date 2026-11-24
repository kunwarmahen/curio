import { useState } from 'react'
import { ApiError, api, type Preferences, type QueueItem, type QueueStatus } from '../api'
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

const sameGoal = (a: Preferences, b: Preferences) =>
  a.profile === b.profile &&
  a.custom_objective.trim().toLowerCase() === b.custom_objective.trim().toLowerCase() &&
  (a.target_minutes ?? null) === (b.target_minutes ?? null)

/** "Technical goal: how GEPA optimizes prompts, about 20 minutes." */
function goalLine(p: Preferences): string {
  const profile = PROFILES[p.profile]?.label ?? p.profile
  const parts = [p.custom_objective ? `${profile} goal: ${p.custom_objective}` : `${profile} goal`]
  if (p.target_minutes) parts.push(`about ${p.target_minutes} minutes`)
  return parts.join(', ')
}

/** The overnight queue on the home page: waiting and running videos. */
export function QueueList(props: { queue: QueueStatus; onChange: () => void; goal: Preferences }) {
  const { queue, onChange, goal } = props
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [note, setNote] = useState('')
  if (queue.items.length === 0) return null
  const waitingItems = queue.items.filter((i) => i.status === 'queued')
  const waiting = waitingItems.length
  const differ = waitingItems.filter((i) => !sameGoal(i.preferences, goal)).length

  const retarget = async () => {
    setBusy('all')
    setErr('')
    try {
      const r = await api.retargetQueue(goal)
      const done = r.already_analyzed.length
      setNote(
        `Updated ${r.updated.length} waiting video${r.updated.length === 1 ? '' : 's'}.` +
          (done ? ` ${done} already had a result with this goal and left the queue.` : ''),
      )
      onChange()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not change the goal. Try again.')
    } finally {
      setBusy('')
    }
  }

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
      {differ > 0 && (
        <div className="queue__retarget">
          <button type="button" className="button-quiet" onClick={retarget} disabled={!!busy}>
            {busy === 'all'
              ? 'Updating…'
              : differ === waiting
                ? `Use the goal above for ${waiting === 1 ? 'the waiting video' : `all ${waiting} waiting videos`}`
                : `Use the goal above for the ${differ} waiting video${differ === 1 ? '' : 's'} with another goal`}
          </button>
          <span className="hint">{goalLine(goal)}.</span>
        </div>
      )}
      {note && (
        <p className="hint" role="status">
          {note}
        </p>
      )}
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
                  {item.video.channel}. {duration(item.video.duration_s)}. {goalLine(item.preferences)}.
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
