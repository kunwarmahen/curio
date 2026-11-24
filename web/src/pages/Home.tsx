import { useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  api,
  type Course,
  type FlashcardDeck,
  type Preferences,
  type Profile,
  type QueueStatus,
  type RecentVideo,
} from '../api'
import { CourseList } from '../components/Courses'
import { Wordmark } from '../components/Logo'
import { QueueList } from '../components/Queue'
import { WatchMap } from '../components/WatchMap'
import { PROFILES, duration, isPlaylistLink, playlistOfVideo, queueHours } from '../format'
import { linkProps, navigate } from '../router'

const WATCH_TIMES = [10, 15, 20, 30, 45]

export function Home() {
  const [url, setUrl] = useState('')
  const [profile, setProfile] = useState<Profile>('general')
  const [goal, setGoal] = useState('')
  const [minutes, setMinutes] = useState<number | null>(null)
  const [force, setForce] = useState(false)
  const [busy, setBusy] = useState<'' | 'now' | 'queue'>('')
  const [error, setError] = useState('')
  const [queued, setQueued] = useState('')
  const [recent, setRecent] = useState<RecentVideo[] | null>(null)
  const [queue, setQueue] = useState<QueueStatus | null>(null)
  const [showMore, setShowMore] = useState(false)
  const [deck, setDeck] = useState<FlashcardDeck | null>(null)
  const [courses, setCourses] = useState<Course[]>([])
  const prefs: Preferences = { profile, custom_objective: goal.trim(), target_minutes: minutes }

  const loadQueue = useCallback(() => {
    api.queue().then(setQueue, () => {})
  }, [])

  useEffect(() => {
    api.recent().then(setRecent, () => setRecent([]))
    api.flashcards(undefined, 0).then(setDeck, () => {})
    api.courses().then(setCourses, () => {})
    loadQueue()
    // Start from the goal used last time (saved on the server, so every device shares it).
    api.defaultPreferences().then((p) => {
      setProfile(p.profile)
      setGoal(p.custom_objective)
      setMinutes(p.target_minutes)
      if (p.custom_objective || p.target_minutes) setShowMore(true)
    }, () => {})
  }, [loadQueue])

  // While something waits or runs, keep its state current (it changes overnight).
  const hasItems = !!queue?.items.length
  useEffect(() => {
    if (!hasItems) return
    const timer = window.setInterval(() => {
      loadQueue()
      api.recent().then(setRecent, () => {})
    }, 15000)
    return () => window.clearInterval(timer)
  }, [hasItems, loadQueue])

  const playlist = isPlaylistLink(url)
  const inPlaylist = !playlist && playlistOfVideo(url)

  /** A playlist link becomes a course: its lectures are queued with this goal. */
  async function addCourse() {
    setBusy('now')
    setError('')
    try {
      const { course_id } = await api.addCourse(url.trim(), prefs)
      api.setDefaultPreferences(prefs).catch(() => {})
      navigate(`/c/${course_id}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong reading the playlist.')
      setBusy('')
    }
  }

  async function start(mode: 'now' | 'queue') {
    if (!url.trim()) {
      setError('Paste a YouTube link first.')
      return
    }
    if (playlist) return addCourse()
    setBusy(mode)
    setError('')
    setQueued('')
    try {
      const { analysis_id, status } = await api.analyze(url.trim(), prefs, force, mode === 'queue')
      api.setDefaultPreferences(prefs).catch(() => {})
      if (status !== 'queued') {
        navigate(`/v/${analysis_id}`)
        return
      }
      setUrl('')
      setQueued('Added to the queue.')
      loadQueue()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong starting the analysis.')
    }
    setBusy('')
  }

  function submit(e: React.FormEvent) {
    e.preventDefault()
    start('now')
  }

  return (
    <main className="home">
      <header className="home__intro">
        <Wordmark />
        <h1>Find the parts of a long video worth your time.</h1>
        <p className="lede">
          Paste a lecture, talk or podcast from YouTube. Curio reads the whole transcript and builds a
          short playlist of the sections that teach the most, with what you'll learn from each.
        </p>
      </header>

      <form className="analyze" onSubmit={submit} noValidate>
        <label className="field-label" htmlFor="url">
          YouTube link
        </label>
        <div className="analyze__row">
          <input
            id="url"
            type="url"
            inputMode="url"
            autoComplete="off"
            placeholder="https://www.youtube.com/watch?v=…"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            aria-invalid={!!error}
            aria-describedby={error ? 'url-error' : undefined}
          />
          <button type="submit" className="button-primary" disabled={!!busy}>
            {busy === 'now'
              ? playlist
                ? 'Reading the playlist…'
                : 'Fetching transcript…'
              : playlist
                ? 'Add the course'
                : 'Find the best parts'}
          </button>
        </div>
        {error && (
          <p id="url-error" className="form-error" role="alert">
            {error}
          </p>
        )}
        {playlist ? (
          <p className="hint analyze__later">
            This is a playlist. Curio adds it as a course: it queues the lectures not analyzed yet
            {queue ? ` (analyzed ${queueHours(queue.window_start, queue.window_end)})` : ''} and builds one playlist
            from the best parts of all of them.
          </p>
        ) : (
          <div className="analyze__later">
            <button type="button" className="button-quiet" onClick={() => start('queue')} disabled={!!busy}>
              {busy === 'queue' ? 'Adding…' : 'Queue for later'}
            </button>
            <span className="hint" aria-live="polite">
              {queued ||
                (queue &&
                  `Queued videos are analyzed ${queueHours(queue.window_start, queue.window_end)}, when the GPU is free.`)}
            </span>
          </div>
        )}
        {inPlaylist && (
          <div className="analyze__playlist">
            <span className="hint">This video is part of a playlist.</span>
            <button type="button" className="button-quiet" onClick={addCourse} disabled={!!busy}>
              Add the whole playlist as a course
            </button>
          </div>
        )}

        <fieldset className="goal">
          <legend className="field-label">What are you watching for?</legend>
          <div className="segmented">
            {(Object.keys(PROFILES) as Profile[]).map((p) => (
              <label key={p} className="segmented__option">
                <input
                  type="radio"
                  name="profile"
                  value={p}
                  checked={profile === p}
                  onChange={() => setProfile(p)}
                />
                <span>{PROFILES[p].label}</span>
              </label>
            ))}
          </div>
          <p className="hint">{PROFILES[profile].hint}</p>
        </fieldset>

        <details
          className="more-options"
          open={showMore}
          onToggle={(e) => setShowMore((e.currentTarget as HTMLDetailsElement).open)}
        >
          <summary>Describe your own goal or set a watch time</summary>
          <label className="field-label" htmlFor="goal">
            Your goal, in your words
          </label>
          <textarea
            id="goal"
            rows={3}
            placeholder="I want to understand how prompt optimizers like GEPA work. Skip the course logistics."
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
          />
          <label className="field-label" htmlFor="minutes">
            How long do you want to watch?
          </label>
          <select
            id="minutes"
            value={minutes ?? ''}
            onChange={(e) => setMinutes(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">Let Curio decide (about a third of the video)</option>
            {WATCH_TIMES.map((m) => (
              <option key={m} value={m}>
                About {m} minutes
              </option>
            ))}
          </select>
          <label className="checkbox">
            <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
            Analyze again even if Curio already has a result for this video
          </label>
        </details>
      </form>

      {deck && deck.due > 0 && (
        <a className="due" {...linkProps('/review')}>
          <span className="due__count">{deck.due}</span>
          <span>
            flashcard{deck.due === 1 ? '' : 's'} to review today. <span className="due__go">Review now</span>
          </span>
        </a>
      )}

      {queue && <QueueList queue={queue} onChange={loadQueue} goal={prefs} />}

      <CourseList courses={courses} recent={recent ?? []} />

      <section className="recent" aria-labelledby="recent-heading">
        <h2 id="recent-heading">Recently analyzed</h2>
        {recent === null && <p className="muted">Loading…</p>}
        {recent?.length === 0 && (
          <p className="muted">Videos you analyze appear here, so you can come back to their playlists.</p>
        )}
        <ul className="recent__list">
          {recent?.map((r) => (
            <li key={r.analysis_id}>
              <a className="recent__item" {...linkProps(`/v/${r.analysis_id}`)}>
                <img src={r.video.thumbnail_url} alt="" loading="lazy" />
                <div className="recent__body">
                  <span className="recent__title">{r.video.title}</span>
                  <span className="muted">
                    {r.video.channel}. Watch {duration(r.playlist_s)} of {duration(r.video.duration_s)},{' '}
                    {PROFILES[r.profile]?.label.toLowerCase() ?? r.profile} goal.
                  </span>
                  <WatchMap
                    duration={r.video.duration_s}
                    stretches={r.watch_map.map((s, i) => ({ ...s, key: String(i) }))}
                  />
                </div>
              </a>
            </li>
          ))}
        </ul>
      </section>
    </main>
  )
}
