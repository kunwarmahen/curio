import { CoursePage } from './pages/Course'
import { Home } from './pages/Home'
import { Review } from './pages/Review'
import { VideoPage } from './pages/VideoPage'
import { useRoute } from './router'

export function App() {
  const route = useRoute()
  if (route.name === 'review') return <Review />
  // Keyed by id: navigating to another video or course remounts the page with fresh state.
  if (route.name === 'course') return <CoursePage key={route.id} id={route.id} />
  return route.name === 'video' ? <VideoPage key={route.id} id={route.id} /> : <Home />
}
