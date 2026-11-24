import { linkProps } from '../router'

/** Curio's mark: a highlighter swipe across the line of a video, with the playhead in it.
 *  The same drawing as public/favicon.svg. */
export function Mark({ size = 28 }: { size?: number }) {
  return (
    <svg className="mark" width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <rect width="32" height="32" rx="7" fill="#1E2B4A" />
      <rect x="3" y="15" width="26" height="2" rx="1" fill="#5B6785" />
      <path
        d="M6.4 11.9 24.6 10.9Q26.2 10.8 26.3 12.4L26.6 18.4Q26.7 20 25.1 20.1L6.9 21.1Q5.3 21.2 5.2 19.6L4.9 13.6Q4.8 12 6.4 11.9Z"
        fill="#FFE34D"
      />
      <rect x="19.3" y="7" width="2.4" height="18" rx="1.2" fill="#D4413B" />
    </svg>
  )
}

/** The mark and the name, on the home page. */
export function Wordmark() {
  return (
    <p className="wordmark">
      <Mark size={34} />
      <span className="wordmark__name">Curio</span>
    </p>
  )
}

/** Back to the home page, from every other page. */
export function BackLink() {
  return (
    <a className="back" {...linkProps('/')}>
      <Mark size={20} />
      <span>All videos</span>
    </a>
  )
}
