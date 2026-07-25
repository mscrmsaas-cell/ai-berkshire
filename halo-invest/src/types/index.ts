export interface Stock {
  symbol: string
  name: string
  sector: string
  market: 'A股' | '港股' | '美股'
  price: number
  change: number
  changePercent: number
  marketCap?: string
  pe?: number
  pb?: number
  dividendYield?: number
  moat?: '宽' | '中' | '窄'
  fairValueGap?: number
  tags: string[]
}

export interface NewsItem {
  id: string
  title: string
  source: string
  time: string
  category: 'macro' | 'industry' | 'company' | 'policy'
  impact: 'high' | 'medium' | 'low'
  summary: string
  relatedSymbols: string[]
}

export interface CycleNode {
  date: string
  label: string
  type: 'global' | 'china' | 'policy' | 'crisis' | 'recovery'
  description: string
}

export interface SectorTheme {
  id: string
  name: string
  domain: 'AI' | '能源' | '材料' | '医药' | 'global'
  thesis: string
  conviction: 1 | 2 | 3 | 4 | 5
  headwinds: string[]
  tailwinds: string[]
  topPicks: string[]
}
