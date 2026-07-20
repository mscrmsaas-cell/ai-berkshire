import { TrendingUp, TrendingDown, Eye, Sparkles } from 'lucide-react'
import { watchlist, portfolioSnapshot } from '../data/mock'

function formatCurrency(n: number) {
  return `¥${n.toLocaleString('zh-CN')}`
}

export default function HomePage() {
  const isPositive = portfolioSnapshot.dayChange >= 0

  return (
    <div className="scroll-spring scroll-hide flex-1 px-5 pt-6 pb-28">
      {/* Header greeting */}
      <header className="mb-6 flex items-center justify-between">
        <div>
          <p className="text-label-secondary text-sm">2026年7月20日 · 长期价值投资</p>
          <h1 className="text-2xl font-semibold tracking-tight gradient-text">Halo 投资组合</h1>
        </div>
        <div className="h-10 w-10 rounded-full bg-surface-elevated flex items-center justify-center">
          <Sparkles size={18} className="text-tint" />
        </div>
      </header>

      {/* Portfolio card */}
      <section className="relative overflow-hidden rounded-3xl bg-surface p-5 mb-6">
        <div className="absolute -right-10 -top-10 h-40 w-40 rounded-full bg-tint/10 halo-ring blur-2xl" />
        <div className="relative z-10">
          <div className="flex items-center justify-between mb-2">
            <span className="text-label-secondary text-sm">总资产</span>
            <span className="text-label-secondary text-xs">年初至今 +{portfolioSnapshot.ytdReturn}%</span>
          </div>
          <div className="text-4xl font-bold tracking-tight mb-3">
            {formatCurrency(portfolioSnapshot.totalValue)}
          </div>
          <div className={`flex items-center gap-2 text-sm font-medium ${isPositive ? 'text-tint-green' : 'text-tint-red'}`}>
            {isPositive ? <TrendingUp size={16} /> : <TrendingDown size={16} />}
            <span>{isPositive ? '+' : ''}{formatCurrency(portfolioSnapshot.dayChange)} ({portfolioSnapshot.dayChangePercent}%)</span>
          </div>
        </div>
      </section>

      {/* Allocation chips */}
      <section className="mb-6">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">配置结构</h2>
          <span className="text-label-secondary text-xs">目标：长期复利</span>
        </div>
        <div className="flex flex-wrap gap-2">
          {portfolioSnapshot.allocation.map((item) => (
            <div key={item.name} className="flex items-center gap-2 rounded-full bg-surface px-3 py-1.5">
              <span className="h-2 w-2 rounded-full bg-tint" />
              <span className="text-xs font-medium">{item.name}</span>
              <span className="text-xs text-label-secondary">{item.value}%</span>
            </div>
          ))}
        </div>
      </section>

      {/* Watchlist */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">核心持仓</h2>
          <button className="flex items-center gap-1 text-xs text-tint">
            <Eye size={12} /> 管理
          </button>
        </div>
        <div className="space-y-3">
          {watchlist.map((stock) => {
            const positive = stock.change >= 0
            return (
              <div
                key={stock.symbol}
                className="rounded-2xl bg-surface p-4 active:scale-[0.98] transition-transform"
              >
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-3">
                    <div className="h-10 w-10 rounded-xl bg-surface-elevated flex items-center justify-center text-xs font-bold text-label-secondary">
                      {stock.name.slice(0, 2)}
                    </div>
                    <div>
                      <div className="font-semibold text-sm">{stock.name}</div>
                      <div className="text-xs text-label-secondary">{stock.symbol} · {stock.sector}</div>
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="font-semibold text-sm">{stock.price.toFixed(2)}</div>
                    <div className={`text-xs font-medium ${positive ? 'text-tint-green' : 'text-tint-red'}`}>
                      {positive ? '+' : ''}{stock.changePercent}%
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                  {stock.tags.map((tag) => (
                    <span key={tag} className="text-[10px] px-2 py-0.5 rounded-full bg-fill text-label-secondary">
                      {tag}
                    </span>
                  ))}
                  {stock.moat && (
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-tint/10 text-tint">
                      {stock.moat}护城河
                    </span>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </section>
    </div>
  )
}
