import { Bell, Shield, Database, FileText, Info, ChevronRight } from 'lucide-react'

const settingsGroups = [
  {
    title: '数据与通知',
    items: [
      { icon: Bell, label: '重大新闻推送', value: '已开启' },
      { icon: Database, label: '数据源', value: '格隆 · 桥水 · Bloomberg' },
      { icon: Shield, label: '本地隐私模式', value: '开启' },
    ],
  },
  {
    title: '关于',
    items: [
      { icon: FileText, label: '研究方法论', value: '' },
      { icon: Info, label: '版本', value: '0.1.0 (Alpha)' },
    ],
  },
]

export default function SettingsPage() {
  return (
    <div className="scroll-spring scroll-hide flex-1 px-5 pt-6 pb-28">
      <header className="mb-6">
        <p className="text-label-secondary text-sm">你负责体验，我负责专业</p>
        <h1 className="text-2xl font-semibold tracking-tight gradient-text">设置</h1>
      </header>

      <section className="rounded-3xl bg-surface p-5 mb-6 text-center">
        <div className="h-16 w-16 rounded-2xl bg-tint/10 flex items-center justify-center mx-auto mb-3">
          <span className="text-2xl font-bold text-tint">H</span>
        </div>
        <div className="text-lg font-semibold">Halo Invest</div>
        <div className="text-xs text-label-secondary mt-1">长期价值投资工作台</div>
      </section>

      {settingsGroups.map((group) => (
        <section key={group.title} className="mb-6">
          <h2 className="text-xs font-semibold text-label-secondary uppercase tracking-wider mb-3 px-1">
            {group.title}
          </h2>
          <div className="rounded-2xl bg-surface overflow-hidden">
            {group.items.map((item, idx) => {
              const Icon = item.icon
              const isLast = idx === group.items.length - 1
              return (
                <button
                  key={item.label}
                  className={`w-full flex items-center justify-between px-4 py-3.5 ${
                    !isLast ? 'border-b border-separator' : ''
                  }`}
                >
                  <div className="flex items-center gap-3">
                    <Icon size={18} className="text-tint" />
                    <span className="text-sm">{item.label}</span>
                  </div>
                  <div className="flex items-center gap-1 text-xs text-label-secondary">
                    {item.value && <span>{item.value}</span>}
                    <ChevronRight size={14} />
                  </div>
                </button>
              )
            })}
          </div>
        </section>
      ))}

      <section className="rounded-2xl bg-surface p-4">
        <h2 className="text-sm font-semibold mb-2">免责声明</h2>
        <p className="text-xs text-label-secondary leading-relaxed">
          本应用仅供学习与研究使用，不构成投资建议。所有数据与分析基于公开信息整理，投资决策请结合自身风险承受能力独立判断。
        </p>
      </section>
    </div>
  )
}
