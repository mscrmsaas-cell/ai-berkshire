import { Home, Binoculars, Newspaper, Orbit, Settings } from 'lucide-react'

interface BottomNavProps {
  activeTab: string
  onChange: (tab: string) => void
}

const tabs = [
  { id: 'home', label: '概览', icon: Home },
  { id: 'research', label: '研究', icon: Binoculars },
  { id: 'news', label: '雷达', icon: Newspaper },
  { id: 'cycle', label: '周期', icon: Orbit },
  { id: 'settings', label: '设置', icon: Settings },
]

export default function BottomNav({ activeTab, onChange }: BottomNavProps) {
  return (
    <nav className="glass fixed bottom-0 left-0 right-0 z-50 border-t border-separator">
      <div className="flex items-center justify-around pb-[env(safe-area-inset-bottom,0px)] pt-2">
        {tabs.map((tab) => {
          const Icon = tab.icon
          const active = activeTab === tab.id
          return (
            <button
              key={tab.id}
              onClick={() => onChange(tab.id)}
              className={`flex flex-col items-center justify-center gap-1 px-4 py-2 transition-colors duration-200 ${
                active ? 'text-tint' : 'text-label-secondary'
              }`}
            >
              <Icon size={22} strokeWidth={active ? 2.5 : 2} />
              <span className="text-[10px] font-medium tracking-wide">{tab.label}</span>
              {active && (
                <span className="absolute bottom-1.5 h-1 w-1 rounded-full bg-tint" />
              )}
            </button>
          )
        })}
      </div>
    </nav>
  )
}
