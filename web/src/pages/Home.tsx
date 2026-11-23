import { useEffect, useState } from 'react'
import { ApiError, api, type Profile, type RecentVideo } from '../api'
import { WatchMap } from '../components/WatchMap'
import { PROFILES, duration } from '../format'
import { linkProps, navigate } from '../router'

const WATCH_TIMES = [10, 15, 20, 30, 45]

export function Home() {
  const [url, setUrl] = useState('')
  const [profile, setProfile] = useState<Profile>('general')
  const [goal, setGoal] = useState('')
  const [minutes, setMinutes] = useState<number | null>(null)
  const [force, setForce] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [recent, setRecent] = useState<RecentVideo[] | null>(null)

  useEffect(() => {
    api.recent().then(setRecent, () => setRecent([]))
  }, [])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!url.trim()) {
      setError('Paste a YouTube link first.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const { analysis_id } = await api.analyze(
        url.trim(),
        { profile, custom_objective: goal.trim(), target_minutes: minutes },
        force,
      )
      navigate(`/v/${analysis_id}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong starting the analysis.')
      setBusy(false)
    }
  }

  return (
    <main className="home">
      <header className="home__intro">
        <p className="wordmark">Curio</p>
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
          <button type="submit" className="button-primary" disabled={busy}>
            {busy ? 'Fetching transcript…' : 'Find the best parts'}
          </button>
        </div>
        {error && (
          <p id="url-error" className="form-error" role="alert">
            {error}
          </p>
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

        <details className="more-options">
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
