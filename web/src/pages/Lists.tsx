import { useCallback, useEffect, useState } from 'react'
import { api, type Course, type Preferences, type QueueStatus, type RecentVideo } from '../api'
import { CourseList } from '../components/Courses'
import { QueueList } from '../components/Queue'
import { VideoList } from '../components/VideoList'
import { queueHours } from '../format'
import { linkProps } from '../router'

/** Every video analyzed. */
export function VideosPage() {
  const [videos, setVideos] = useState<RecentVideo[] | null>(null)
  useEffect(() => {
    api.recent(50).then(setVideos, () => setVideos([]))
  }, [])
  return (
    <main className="page">
      <h1>Videos</h1>
      <p className="muted page__lede">Every video you've analyzed, newest first, with its watch map.</p>
      {videos === null && <p className="muted">Loading…</p>}
      {videos?.length === 0 && (
        <p className="muted">
          Nothing yet. <a {...linkProps('/')}>Paste a video</a> and its playlist will be here.
        </p>
      )}
      {videos && <VideoList videos={videos} />}
    </main>
  )
}

/** Courses, and making one from analyzed videos. */
export function CoursesPage() {
  const [courses, setCourses] = useState<Course[] | null>(null)
  const [videos, setVideos] = useState<RecentVideo[]>([])
  useEffect(() => {
    api.courses().then(setCourses, () => setCourses([]))
    api.recent(50).then(setVideos, () => {})
  }, [])
  return (
    <main className="page">
      <h1>Courses</h1>
      <p className="muted page__lede">
        A course is a YouTube playlist of lectures (paste its link on the <a {...linkProps('/')}>first page</a>) or
        videos you've grouped. Curio builds one playlist from the best parts of all of them.
      </p>
      {courses === null ? <p className="muted">Loading…</p> : <CourseList courses={courses} recent={videos} />}
    </main>
  )
}

/** The overnight queue. */
export function QueuePage() {
  const [queue, setQueue] = useState<QueueStatus | null>(null)
  const [goal, setGoal] = useState<Preferences | null>(null)
  const load = useCallback(() => {
    api.queue().then(setQueue, () => {})
  }, [])
  useEffect(() => {
    load()
    api.defaultPreferences().then(setGoal, () => {})
    const timer = window.setInterval(load, 15000)
    return () => window.clearInterval(timer)
  }, [load])
  return (
    <main className="page">
      <h1>Queue</h1>
      {queue && (
        <p className="muted page__lede">
          Videos queued for later are analyzed one at a time {queueHours(queue.window_start, queue.window_end)},
          while the GPU is free, with their notes and quiz written right after.
        </p>
      )}
      {queue === null && <p className="muted">Loading…</p>}
      {queue && queue.items.length === 0 && <p className="muted">Nothing is waiting. Use “Queue for later” on the first page.</p>}
      {queue && goal && <QueueList queue={queue} onChange={load} goal={goal} />}
    </main>
  )
}
