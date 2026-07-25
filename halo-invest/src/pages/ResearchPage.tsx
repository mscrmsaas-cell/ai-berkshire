import { sectorThemes } from '../data/mock'
import { Brain, Zap, Factory, Pill, Globe, ChevronRight } from 'lucide-react'

const domainIcons = {
  AI: Brain,
  能源: Zap,
  材料: Factory,
  医药: Pill,
  global: Globe,
}

const domainColors: Record<string, string> = {
  AI: 'from-blue-500/20 to-indigo-500/20',
  能源: 'from-yellow-500/20 to-orange-500/20',
  材料: 'from-emerald-500/20 to-teal-500/20',
  医药: 'from-rose-500/20 to-pink-500/20',
  global: 'from-purple-500/20 to-violet-500/20',
}

export default function ResearchPage() {
  return (
    <div className="scroll-spring scroll-hide flex-1 px-5 pt-6 pb-28">
      <header className="mb-6">
        <p className="text-label-secondary text-sm">麦肯锡 Halo 资产框架</p>
        <h1 className="text-2xl font-semibold tracking-tight gradient-text">长期研究主题</h1>
      </header>

      <section className="mb-6 rounded-2xl bg-surface p-4">
        <h2 className="text-sm font-semibold mb-2">投资纪律</h2>
        <ul className="space-y-2 text-xs text-label-secondary">
          <li className="flex gap-2">
            <span className="text-tint">·</span>
            只投能理解、能持有十年的生意
          </li>
          <li className="flex gap-2">
            <span className="text-tint">·</span>
            护城河与现金流优先于短期估值弹性
          </li>
          <li className="flex gap-2">
            <span className="text-tint">·</span>
            国内看格隆：产业趋势 + 政策拐点 + 估值安全边际
          </li>
          <li className="flex gap-2">
            <span className="text-tint">·</span>
            国际看桥水：债务/货币/经济周期位置决定仓位
          </li>
        </ul>
      </section>

      <section className="space-y-4">
        {sectorThemes.map((theme) => {
          const Icon = domainIcons[theme.domain]
          return (
            <div
              key={theme.id}
              className="relative overflow-hidden rounded-3xl bg-surface p-5 active:scale-[0.98] transition-transform"
            >
              <div className={`absolute inset-0 bg-gradient-to-br ${domainColors[theme.domain]} opacity-50`} />
              <div className="relative z-10">
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-3">
                    <div className="h-10 w-10 rounded-xl bg-surface-elevated flex items-center justify-center">
                      <Icon size={20} className="text-tint" />
                    </div>
                    <div>
                      <div className="font-semibold">{theme.name}</div>
                      <div className="text-xs text-label-secondary">置信度 {'★'.repeat(theme.conviction)}{'☆'.repeat(5 - theme.conviction)}</div>
                    </div>
                  </div>
                  <ChevronRight size={18} className="text-label-secondary" />
                </div>
                <p className="text-sm text-label-secondary leading-relaxed mb-4">
                  {theme.thesis}
                </p>
                <div className="grid grid-cols-2 gap-3 text-xs">
                  <div className="rounded-xl bg-tint-green/10 p-3">
                    <div className="text-tint-green font-medium mb-1">顺风</div>
                    <ul className="space-y-1 text-label-secondary">
                      {theme.tailwinds.slice(0, 2).map((t) => (
                        <li key={t}>+ {t}</li>
                      ))}
                    </ul>
                  </div>
                  <div className="rounded-xl bg-tint-red/10 p-3">
                    <div className="text-tint-red font-medium mb-1">逆风</div>
                    <ul className="space-y-1 text-label-secondary">
                      {theme.headwinds.slice(0, 2).map((t) => (
                        <li key={t}>- {t}</li>
                      ))}
                    </ul>
                  </div>
                </div>
                <div className="mt-4 flex flex-wrap gap-2">
                  {theme.topPicks.map((pick) => (
                    <span key={pick} className="text-[10px] px-2.5 py-1 rounded-full bg-fill text-label">
                      {pick}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )
        })}
      </section>
    </div>
  )
}
