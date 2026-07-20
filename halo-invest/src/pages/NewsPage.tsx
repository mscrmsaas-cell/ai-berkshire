import { useState } from 'react'
import { newsFeed } from '../data/mock'
import { Filter, AlertCircle, Factory, Building2, ScrollText } from 'lucide-react'
import type { NewsItem } from '../types'

const categories: { id: NewsItem['category'] | 'all'; label: string; icon: typeof AlertCircle }[] = [
  { id: 'all', label: '全部', icon: Filter },
  { id: 'macro', label: '宏观', icon: AlertCircle },
  { id: 'industry', label: '行业', icon: Factory },
  { id: 'company', label: '公司', icon: Building2 },
  { id: 'policy', label: '政策', icon: ScrollText },
]

const impactLabels = {
  high: { text: '高', color: 'bg-tint-red/15 text-tint-red' },
  medium: { text: '中', color: 'bg-tint-yellow/15 text-tint-yellow' },
  low: { text: '低', color: 'bg-label-secondary/15 text-label-secondary' },
}

export default function NewsPage() {
  const [activeCategory, setActiveCategory] = useState<NewsItem['category'] | 'all'>('all')

  const filtered = activeCategory === 'all'
    ? newsFeed
    : newsFeed.filter((n) => n.category === activeCategory)

  return (
    <div className="scroll-spring scroll-hide flex-1 px-5 pt-6 pb-28">
      <header className="mb-4">
        <p className="text-label-secondary text-sm">每日政治经济 · 行业与公司动向</p>
        <h1 className="text-2xl font-semibold tracking-tight gradient-text">新闻雷达</h1>
      </header>

      {/* Category filter */}
      <div className="flex gap-2 overflow-x-auto scroll-hide pb-4 mb-2">
        {categories.map((cat) => {
          const Icon = cat.icon
          const active = activeCategory === cat.id
          return (
            <button
              key={cat.id}
              onClick={() => setActiveCategory(cat.id)}
              className={`flex items-center gap-1.5 whitespace-nowrap rounded-full px-4 py-2 text-xs font-medium transition-colors ${
                active ? 'bg-tint text-white' : 'bg-surface text-label-secondary'
              }`}
            >
              <Icon size={12} />
              {cat.label}
            </button>
          )
        })}
      </div>

      {/* News list */}
      <div className="space-y-4 relative">
        <div className="absolute left-[11px] top-4 bottom-4 w-px bg-separator" />
        {filtered.map((news) => (
          <div key={news.id} className="relative pl-7">
            <span className={`absolute left-0 top-1.5 h-5 w-5 rounded-full border-2 border-bg flex items-center justify-center ${
              news.impact === 'high' ? 'bg-tint-red' : news.impact === 'medium' ? 'bg-tint-yellow' : 'bg-label-secondary'
            }`}>
              <span className="h-1.5 w-1.5 rounded-full bg-bg" />
            </span>
            <div className="rounded-2xl bg-surface p-4">
              <div className="flex items-start justify-between gap-3 mb-2">
                <h3 className="text-sm font-semibold leading-snug">{news.title}</h3>
                <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${impactLabels[news.impact].color}`}>
                  {impactLabels[news.impact].text}影响
                </span>
              </div>
              <div className="flex items-center gap-2 mb-3 text-[11px] text-label-secondary">
                <span>{news.source}</span>
                <span>·</span>
                <span>{news.time}</span>
              </div>
              <p className="text-xs text-label-secondary leading-relaxed mb-3">
                {news.summary}
              </p>
              {news.relatedSymbols.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {news.relatedSymbols.map((sym) => (
                    <span key={sym} className="text-[10px] px-2 py-0.5 rounded-md bg-fill text-label-secondary">
                      {sym}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
