import { useState, useEffect } from 'react'
import { Card } from '@/components/ui/card'
import { Brain, TrendingUp, TrendingDown, Minus, ChevronDown, ChevronUp, Filter } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

interface DecisionSnapshot {
  id: number
  timestamp: string
  symbol: string
  actual_decision: 'LONG' | 'SHORT' | 'HOLD'
  actual_size_pct: number | null
  entry_price: number
  exit_price_24h: number | null
  deepseek_reasoning: string
  indicators_snapshot: string  // JSON string

  // Counterfactual analysis (filled after 24h)
  actual_pnl: number | null
  counterfactual_long_pnl: number | null
  counterfactual_short_pnl: number | null
  counterfactual_hold_pnl: number | null
  optimal_action: string | null
  regret: number | null
}

interface AIInsightsProps {
  accountId: number
}

export default function AIInsights({ accountId }: AIInsightsProps) {
  const [snapshots, setSnapshots] = useState<DecisionSnapshot[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<number | null>(null)

  // Filters
  const [symbolFilter, setSymbolFilter] = useState<string>('all')
  const [decisionFilter, setDecisionFilter] = useState<string>('all')

  useEffect(() => {
    fetchSnapshots()
  }, [accountId])

  const fetchSnapshots = async () => {
    try {
      setLoading(true)
      setError(null)

      const response = await fetch(`/api/learning/snapshots/${accountId}?limit=50`)
      if (!response.ok) {
        throw new Error(`Failed to fetch snapshots: ${response.statusText}`)
      }

      const data = await response.json()
      setSnapshots(data.snapshots || [])
    } catch (err) {
      console.error('Error fetching snapshots:', err)
      setError(err instanceof Error ? err.message : 'Failed to load AI decisions')
    } finally {
      setLoading(false)
    }
  }

  const toggleExpand = (id: number) => {
    setExpandedId(expandedId === id ? null : id)
  }

  const parseIndicators = (jsonString: string) => {
    try {
      return JSON.parse(jsonString)
    } catch {
      return {}
    }
  }

  const formatTimestamp = (timestamp: string) => {
    return new Date(timestamp).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  const getDecisionIcon = (decision: string) => {
    if (decision === 'LONG') return <TrendingUp className="w-4 h-4 text-green-500" />
    if (decision === 'SHORT') return <TrendingDown className="w-4 h-4 text-red-500" />
    return <Minus className="w-4 h-4 text-gray-500" />
  }

  const getDecisionColor = (decision: string) => {
    if (decision === 'LONG') return 'text-green-500 bg-green-500/10'
    if (decision === 'SHORT') return 'text-red-500 bg-red-500/10'
    return 'text-gray-500 bg-gray-500/10'
  }

  // Filter snapshots
  const filteredSnapshots = snapshots.filter(snapshot => {
    if (symbolFilter !== 'all' && snapshot.symbol !== symbolFilter) return false
    if (decisionFilter !== 'all' && snapshot.actual_decision !== decisionFilter) return false
    return true
  })

  // Get unique symbols for filter
  const uniqueSymbols = Array.from(new Set(snapshots.map(s => s.symbol))).sort()

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-muted-foreground">Loading AI decisions...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-destructive">{error}</div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Brain className="w-6 h-6 text-primary" />
          <h2 className="text-2xl font-bold">AI Decision Analysis</h2>
        </div>
        <div className="text-sm text-muted-foreground">
          {filteredSnapshots.length} decisions
        </div>
      </div>

      {/* Filters */}
      <Card className="p-4">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Filter className="w-4 h-4 text-muted-foreground" />
            <span className="text-sm text-muted-foreground">Filters:</span>
          </div>

          <Select value={symbolFilter} onValueChange={setSymbolFilter}>
            <SelectTrigger className="w-40">
              <SelectValue placeholder="All Symbols" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All Symbols</SelectItem>
              {uniqueSymbols.map(symbol => (
                <SelectItem key={symbol} value={symbol}>{symbol}</SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={decisionFilter} onValueChange={setDecisionFilter}>
            <SelectTrigger className="w-40">
              <SelectValue placeholder="All Decisions" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All Decisions</SelectItem>
              <SelectItem value="LONG">LONG</SelectItem>
              <SelectItem value="SHORT">SHORT</SelectItem>
              <SelectItem value="HOLD">HOLD</SelectItem>
            </SelectContent>
          </Select>

          <Button variant="outline" size="sm" onClick={fetchSnapshots}>
            Refresh
          </Button>
        </div>
      </Card>

      {/* Decisions List */}
      <div className="space-y-3 overflow-auto max-h-[600px]">
        {filteredSnapshots.length === 0 ? (
          <Card className="p-8 text-center text-muted-foreground">
            No AI decisions found with current filters.
          </Card>
        ) : (
          filteredSnapshots.map(snapshot => (
            <Card key={snapshot.id} className="overflow-hidden">
              {/* Summary Row */}
              <div
                className="p-4 cursor-pointer hover:bg-muted/50 transition-colors"
                onClick={() => toggleExpand(snapshot.id)}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-4">
                    {getDecisionIcon(snapshot.actual_decision)}
                    <div>
                      <div className="font-semibold">{snapshot.symbol}</div>
                      <div className="text-sm text-muted-foreground">
                        {formatTimestamp(snapshot.timestamp)}
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-4">
                    <div className={`px-3 py-1 rounded-full text-sm font-medium ${getDecisionColor(snapshot.actual_decision)}`}>
                      {snapshot.actual_decision}
                    </div>

                    {snapshot.exit_price_24h && snapshot.optimal_action && (
                      <div className="text-right">
                        <div className={`text-sm font-medium ${snapshot.regret && snapshot.regret > 0 ? 'text-destructive' : 'text-green-500'}`}>
                          {snapshot.regret !== null ? `${snapshot.regret >= 0 ? '-' : '+'}$${Math.abs(snapshot.regret).toFixed(2)}` : '-'}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          Optimal: {snapshot.optimal_action}
                        </div>
                      </div>
                    )}

                    {expandedId === snapshot.id ? (
                      <ChevronUp className="w-5 h-5 text-muted-foreground" />
                    ) : (
                      <ChevronDown className="w-5 h-5 text-muted-foreground" />
                    )}
                  </div>
                </div>
              </div>

              {/* Expanded Details */}
              {expandedId === snapshot.id && (
                <div className="border-t bg-muted/20 p-4 space-y-4">
                  {/* AI Reasoning */}
                  <div>
                    <h4 className="font-semibold mb-2">DeepSeek AI Reasoning</h4>
                    <div className="text-sm text-muted-foreground whitespace-pre-wrap bg-background p-3 rounded-md">
                      {snapshot.deepseek_reasoning}
                    </div>
                  </div>

                  {/* Market Indicators */}
                  <div>
                    <h4 className="font-semibold mb-2">Market Indicators at Decision Time</h4>
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                      {(() => {
                        const indicators = parseIndicators(snapshot.indicators_snapshot)
                        return Object.entries(indicators).map(([key, value]) => (
                          <div key={key} className="bg-background p-2 rounded-md">
                            <div className="text-xs text-muted-foreground">{key}</div>
                            <div className="text-sm font-medium">
                              {typeof value === 'number' ? value.toFixed(2) : String(value)}
                            </div>
                          </div>
                        ))
                      })()}
                    </div>
                  </div>

                  {/* Counterfactual Analysis */}
                  {snapshot.exit_price_24h && (
                    <div>
                      <h4 className="font-semibold mb-2">Counterfactual Analysis (24h later)</h4>
                      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                        <div className="bg-background p-3 rounded-md">
                          <div className="text-xs text-muted-foreground">Entry Price</div>
                          <div className="text-sm font-medium">${snapshot.entry_price.toFixed(2)}</div>
                        </div>
                        <div className="bg-background p-3 rounded-md">
                          <div className="text-xs text-muted-foreground">Exit Price (24h)</div>
                          <div className="text-sm font-medium">${snapshot.exit_price_24h.toFixed(2)}</div>
                        </div>

                        <div className="bg-background p-3 rounded-md">
                          <div className="text-xs text-muted-foreground">If LONG</div>
                          <div className={`text-sm font-medium ${snapshot.counterfactual_long_pnl && snapshot.counterfactual_long_pnl > 0 ? 'text-green-500' : 'text-red-500'}`}>
                            {snapshot.counterfactual_long_pnl !== null ? `$${snapshot.counterfactual_long_pnl.toFixed(2)}` : '-'}
                          </div>
                        </div>

                        <div className="bg-background p-3 rounded-md">
                          <div className="text-xs text-muted-foreground">If SHORT</div>
                          <div className={`text-sm font-medium ${snapshot.counterfactual_short_pnl && snapshot.counterfactual_short_pnl > 0 ? 'text-green-500' : 'text-red-500'}`}>
                            {snapshot.counterfactual_short_pnl !== null ? `$${snapshot.counterfactual_short_pnl.toFixed(2)}` : '-'}
                          </div>
                        </div>

                        <div className="bg-background p-3 rounded-md">
                          <div className="text-xs text-muted-foreground">If HOLD</div>
                          <div className="text-sm font-medium">
                            {snapshot.counterfactual_hold_pnl !== null ? `$${snapshot.counterfactual_hold_pnl.toFixed(2)}` : '$0.00'}
                          </div>
                        </div>

                        <div className="bg-background p-3 rounded-md">
                          <div className="text-xs text-muted-foreground">Actual P&L</div>
                          <div className={`text-sm font-medium ${snapshot.actual_pnl && snapshot.actual_pnl > 0 ? 'text-green-500' : 'text-red-500'}`}>
                            {snapshot.actual_pnl !== null ? `$${snapshot.actual_pnl.toFixed(2)}` : '-'}
                          </div>
                        </div>

                        <div className="bg-background p-3 rounded-md border-2 border-primary/30">
                          <div className="text-xs text-muted-foreground">Optimal Action</div>
                          <div className="text-sm font-bold text-primary">
                            {snapshot.optimal_action || '-'}
                          </div>
                        </div>

                        <div className="bg-background p-3 rounded-md">
                          <div className="text-xs text-muted-foreground">Regret</div>
                          <div className={`text-sm font-medium ${snapshot.regret && snapshot.regret > 0 ? 'text-destructive' : 'text-green-500'}`}>
                            {snapshot.regret !== null ? `$${snapshot.regret.toFixed(2)}` : '-'}
                          </div>
                        </div>
                      </div>
                    </div>
                  )}

                  {!snapshot.exit_price_24h && (
                    <div className="text-sm text-muted-foreground italic">
                      Counterfactual analysis will be available 24h after this decision.
                    </div>
                  )}
                </div>
              )}
            </Card>
          ))
        )}
      </div>
    </div>
  )
}
