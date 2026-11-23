import { Home } from './pages/Home'
import { VideoPage } from './pages/VideoPage'
import { useRoute } from './router'

export function App() {
  const route = useRoute()
  // Keyed by id: navigating to another video remounts the page with fresh state.
  return route.name === 'video' ? <VideoPage key={route.id} id={route.id} /> : <Home />
}
