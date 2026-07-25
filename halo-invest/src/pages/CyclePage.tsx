import { cycleNodes } from '../data/mock'
import { Globe, Landmark, TrendingDown, TrendingUp, Zap } from 'lucide-react'

const typeIcons = {
  global: Globe,
  china: Landmark,
  policy: Zap,
  crisis: TrendingDown,
  recovery: TrendingUp,
}

const typeColors: Record<string, string> = {
  global: 'bg-blue-500/20 text-blue-400',
  china: 'bg-red-500/20 text-red-400',
  policy: 'bg-yellow-500/20 text-yellow-400',
  crisis: 'bg-rose-500/20 text-rose-400',
  recovery: 'bg-green-500/20 text-green-400',
}

const typeLabels = {
  global: '全球事件',
  china: '中国周期',
  policy: '政策节点',
  crisis: '危机冲击',
  recovery: '复苏交易',
}

export default function CyclePage() {
  return (
    <div className="scroll-spring scroll-hide flex-1 px-5 pt-6 pb-28">
      <header className="mb-6">
        <p className="text-label-secondary text-sm">桥水 · 债务 / 货币 / 经济周期</p>
        <h1 className="text-2xl font-semibold tracking-tight gradient-text">周期时钟</h1>
      </header>

      {/* Current regime card */}
      <section className="rounded-3xl bg-surface p-5 mb-6 relative overflow-hidden">
        <div className="absolute -right-8 -top-8 h-32 w-32 rounded-full bg-tint/10 blur-2xl halo-ring" />
        <div className="relative z-10">
          <div className="text-xs text-label-secondary mb-1">当前位置（2026-07）</div>
          <div className="text-xl font-semibold mb-3">美国降息周期 · 中国低利率复苏</div>
          <div className="grid grid-cols-2 gap-3 text-xs">
            <div className="rounded-xl bg-fill p-3">
              <div className="text-label-secondary mb-1">全球</div>
              <div className="font-medium">宽松拐点已现</div>
              <div className="text-label-secondary mt-1">利好：黄金、成长股、新兴市场</div>
            </div>
            <div className="rounded-xl bg-fill p-3">
              <div className="text-label-secondary mb-1">中国</div>
              <div className="font-medium">债务重组后早期</div>
              <div className="text-label-secondary mt-1">利好：高股息、消费龙头、AI应用</div>
            </div>
          </div>
        </div>
      </section>

      {/* Timeline */}
      <section>
        <h2 className="text-lg font-semibold mb-4">关键节点</h2>
        <div className="space-y-0 relative">
          <div className="absolute left-[19px] top-3 bottom-3 w-px bg-separator" />
          {cycleNodes.map((node, idx) => {
            const Icon = typeIcons[node.type]
            const isLast = idx === cycleNodes.length - 1
            return (
              <div key={node.date} className="relative pl-12 pb-6">
                <div className={`absolute left-0 top-0 h-10 w-10 rounded-full border-2 border-bg flex items-center justify-center ${
                  isLast ? 'bg-tint text-white ring-4 ring-tint/20' : typeColors[node.type]
                }`}>
                  <Icon size={16} />
                </div>
                <div className="rounded-2xl bg-surface p-4">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-medium text-tint">{node.date}</span>
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-fill text-label-secondary">
                      {typeLabels[node.type]}
                    </span>
                  </div>
                  <div className="font-semibold mb-1">{node.label}</div>
                  <p className="text-xs text-label-secondary leading-relaxed">{node.description}</p>
                </div>
              </div>
            )
          })}
        </div>
      </section>

      {/* Bridgewater principles */}
      <section className="mt-2 rounded-2xl bg-surface p-4">
        <h2 className="text-sm font-semibold mb-3">桥水原则映射</h2>
        <div className="space-y-3 text-xs text-label-secondary">
          <div className="flex gap-3">
            <span className="text-tint font-mono">1</span>
            <span>不要让债务增长速度超过收入。</span>
          </div>
          <div className="flex gap-3">
            <span className="text-tint font-mono">2</span>
            <span>不要让收入增长速度超过生产率。</span>
          </div>
          <div className="flex gap-3">
            <span className="text-tint font-mono">3</span>
            <span>尽一切努力提高生产率，因为长期来看，这才是最重要的。</span>
          </div>
        </div>
      </section>
    </div>
  )
}
