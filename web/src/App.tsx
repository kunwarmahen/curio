import { TopBar } from './components/TopBar'
import { CoursePage } from './pages/Course'
import { Home } from './pages/Home'
import { CoursesPage, QueuePage, VideosPage } from './pages/Lists'
import { Review } from './pages/Review'
import { VideoPage } from './pages/VideoPage'
import { useRoute, type Route } from './router'

function Page({ route }: { route: Route }) {
  // Keyed by id: navigating to another video or course remounts the page with fresh state.
  switch (route.name) {
    case 'video':
      return <VideoPage key={route.id} id={route.id} />
    case 'course':
      return <CoursePage key={route.id} id={route.id} />
    case 'courses':
      return <CoursesPage />
    case 'videos':
      return <VideosPage />
    case 'queue':
      return <QueuePage />
    case 'review':
      return <Review />
    default:
      return <Home />
  }
}

export function App() {
  const route = useRoute()
  return (
    <>
      <TopBar />
      <Page route={route} />
    </>
  )
}
