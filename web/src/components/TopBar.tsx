import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { linkProps, useRoute, type Route } from '../router'
import { Mark } from './Logo'

interface Counts {
  due: number
  queued: number
  courses: number
}

const ITEMS: { path: string; label: string; hint: string; route: Route['name']; count?: keyof Counts }[] = [
  { path: '/', label: 'Analyze a video', hint: 'Paste a video or a playlist', route: 'home' },
  { path: '/courses', label: 'Courses', hint: 'Playlists of lectures, as one course', route: 'courses', count: 'courses' },
  { path: '/videos', label: 'Videos', hint: 'Everything you’ve analyzed', route: 'videos' },
  { path: '/queue', label: 'Queue', hint: 'Waiting for tonight', route: 'queue', count: 'queued' },
  { path: '/gpu', label: 'GPU', hint: 'Who is using it right now', route: 'gpu' },
  { path: '/review', label: 'Review flashcards', hint: 'Cards due today', route: 'review', count: 'due' },
]

/** The bar on every page: the logo (home) and a menu that stays hidden until opened. */
export function TopBar() {
  const route = useRoute()
  // The route the menu was opened on: going anywhere else closes it.
  const [openOn, setOpenOn] = useState<Route | null>(null)
  const open = openOn === route
  const [counts, setCounts] = useState<Counts | null>(null)
  const button = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)

  const close = useCallback((focusButton: boolean) => {
    setOpenOn(null)
    if (focusButton) button.current?.focus()
  }, [])

  // Counts are fetched when the menu opens, so they're current.
  useEffect(() => {
    if (!open) return
    Promise.all([
      api.flashcards(undefined, 0).then((d) => d.due, () => 0),
      api.queue().then((q) => q.items.filter((i) => i.status === 'queued').length, () => 0),
      api.courses().then((c) => c.length, () => 0),
    ]).then(([due, queued, courses]) => setCounts({ due, queued, courses }))
    panel.current?.querySelector<HTMLAnchorElement>('a')?.focus()
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && close(true)
    const onClick = (e: MouseEvent) => {
      const t = e.target as Node
      if (!panel.current?.contains(t) && !button.current?.contains(t)) close(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onClick)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onClick)
    }
  }, [open, close])

  return (
    <header className="topbar">
      <a className="topbar__home" {...linkProps('/')} aria-label="Curio, home">
        <Mark size={26} />
        <span className="topbar__name">Curio</span>
      </a>
      <button
        ref={button}
        type="button"
        className="topbar__menu"
        aria-expanded={open}
        aria-controls="site-menu"
        onClick={() => setOpenOn(open ? null : route)}
      >
        <span className="topbar__icon" aria-hidden="true">
          <span />
          <span />
          <span />
        </span>
        Menu
      </button>
      <div ref={panel} id="site-menu" className="menu" hidden={!open}>
        <nav aria-label="Curio">
          <ul>
            {ITEMS.map((item) => {
              const n = item.count && counts ? counts[item.count] : 0
              const here = route.name === item.route
              return (
                <li key={item.path}>
                  <a className="menu__item" {...linkProps(item.path)} aria-current={here ? 'page' : undefined}>
                    <span className="menu__label">
                      {item.label}
                      {n > 0 && <span className="menu__count">{n}</span>}
                    </span>
                    <span className="menu__hint">{item.hint}</span>
                  </a>
                </li>
              )
            })}
          </ul>
        </nav>
      </div>
    </header>
  )
}
