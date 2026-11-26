import { useEffect, useState } from 'react'

// A handful of routes don't justify a router dependency.
export type Route =
  | { name: 'home' }
  | { name: 'video'; id: string }
  | { name: 'course'; id: string }
  | { name: 'courses' }
  | { name: 'videos' }
  | { name: 'queue' }
  | { name: 'gpu' }
  | { name: 'review' }

const PAGES: Record<string, Route> = {
  review: { name: 'review' },
  courses: { name: 'courses' },
  videos: { name: 'videos' },
  queue: { name: 'queue' },
  gpu: { name: 'gpu' },
}

function parse(path: string): Route {
  const page = path.match(/^\/([a-z]+)\/?$/)
  if (page && PAGES[page[1]]) return PAGES[page[1]]
  const course = path.match(/^\/c\/([A-Za-z0-9_-]+)\/?$/)
  if (course) return { name: 'course', id: course[1] }
  const match = path.match(/^\/v\/([A-Za-z0-9_-]+)\/?$/)
  return match ? { name: 'video', id: match[1] } : { name: 'home' }
}

export function navigate(path: string) {
  if (path !== window.location.pathname) window.history.pushState(null, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
  window.scrollTo({ top: 0 })
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parse(window.location.pathname))
  useEffect(() => {
    const onPop = () => setRoute(parse(window.location.pathname))
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])
  return route
}

/** An <a> that navigates client-side but still works with modifier-clicks. */
export function linkProps(path: string) {
  return {
    href: path,
    onClick: (e: React.MouseEvent) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
      e.preventDefault()
      navigate(path)
    },
  }
}
