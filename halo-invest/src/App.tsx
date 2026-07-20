import { useState, useEffect } from 'react'
import BottomNav from './components/BottomNav'
import HomePage from './pages/HomePage'
import ResearchPage from './pages/ResearchPage'
import NewsPage from './pages/NewsPage'
import CyclePage from './pages/CyclePage'
import SettingsPage from './pages/SettingsPage'

export default function App() {
  const [activeTab, setActiveTab] = useState('home')

  useEffect(() => {
    // Lock app to portrait on mobile / Capacitor environments
    if ('screen' in window && 'orientation' in window.screen) {
      try {
        const screen = window.screen as unknown as { orientation?: { lock: (mode: string) => Promise<void> } }
        screen.orientation?.lock('portrait').catch(() => {})
      } catch {
        // ignore
      }
    }
  }, [])

  return (
    <div className="flex h-full w-full flex-col bg-bg">
      <main className="flex flex-1 overflow-hidden">
        {activeTab === 'home' && <HomePage />}
        {activeTab === 'research' && <ResearchPage />}
        {activeTab === 'news' && <NewsPage />}
        {activeTab === 'cycle' && <CyclePage />}
        {activeTab === 'settings' && <SettingsPage />}
      </main>
      <BottomNav activeTab={activeTab} onChange={setActiveTab} />
    </div>
  )
}
