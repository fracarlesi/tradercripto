import { useState, useEffect } from 'react'
import { Card } from '@/components/ui/card'
import { Brain, TrendingUp, TrendingDown, Minus, ChevronDown, ChevronUp, Filter, Lightbulb, Check, X, AlertTriangle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/tabs'

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

interface PendingSuggestion {
  id: number
  created_at: string
  source: string
  suggestion_type: string
  symbol: string | null
  suggestion_data: {
    type: string
    from?: number
    to?: number
    boost_amount?: number
    condition: Record<string, number>
    duration_hours: number
  }
  reason: string
  evidence: {
    missed_profit: number
    return_pct: number
    score: number
    momentum: number
    support: number
  } | null
  status: string
  reviewed_at: string | null
  review_notes: string | null
}

interface AIInsightsProps {
  accountId: number
}

export default function AIInsights({ accountId }: AIInsightsProps) {
  const [snapshots, setSnapshots] = useState<DecisionSnapshot[]>([])
  const [suggestions, setSuggestions] = useState<PendingSuggestion[]>([])
  const [pendingCount, setPendingCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [suggestionsLoading, setSuggestionsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [expandedSuggestionId, setExpandedSuggestionId] = useState<number | null>(null)

  // Filters
  const [symbolFilter, setSymbolFilter] = useState<string>('all')
  const [decisionFilter, setDecisionFilter] = useState<string>('all')

  useEffect(() => {
    fetchSnapshots()
    fetchSuggestions()
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

  const fetchSuggestions = async () => {
    try {
      setSuggestionsLoading(true)
      const response = await fetch('/api/learning/suggestions?status=pending')
      if (!response.ok) {
        throw new Error(`Failed to fetch suggestions: ${response.statusText}`)
      }
      const data = await response.json()
      setSuggestions(data.suggestions || [])
      setPendingCount(data.pending_count || 0)
    } catch (err) {
      console.error('Error fetching suggestions:', err)
    } finally {
      setSuggestionsLoading(false)
    }
  }

  const dismissSuggestion = async (id: number) => {
    try {
      const response = await fetch(`/api/learning/suggestions/${id}/dismiss`, {
        method: 'POST',
      })
      if (!response.ok) {
        throw new Error('Failed to dismiss suggestion')
      }
      // Refresh suggestions
      fetchSuggestions()
    } catch (err) {
      console.error('Error dismissing suggestion:', err)
      alert('Failed to dismiss suggestion')
    }
  }

  const markApplied = async (id: number) => {
    try {
      const response = await fetch(`/api/learning/suggestions/${id}/mark-applied`, {
        method: 'POST',
      })
      if (!response.ok) {
        throw new Error('Failed to mark suggestion as applied')
      }
      // Refresh suggestions
      fetchSuggestions()
    } catch (err) {
      console.error('Error marking suggestion as applied:', err)
      alert('Failed to mark suggestion as applied')
    }
  }

  const toggleExpand = (id: number) => {
    setExpandedId(expandedId === id ? null : id)
  }

  const toggleSuggestionExpand = (id: number) => {
    setExpandedSuggestionId(expandedSuggestionId === id ? null : id)
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
          <h2 className="text-2xl font-bold">Strategy Learning</h2>
        </div>
      </div>

      <Tabs defaultValue="suggestions" className="w-full">
        <TabsList className="grid w-full grid-cols-2">
          <TabsTrigger value="suggestions" className="flex items-center gap-2">
            <Lightbulb className="w-4 h-4" />
            Suggestions
            {pendingCount > 0 && (
              <span className="ml-1 px-2 py-0.5 text-xs bg-primary text-primary-foreground rounded-full">
                {pendingCount}
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="history">Decision History</TabsTrigger>
        </TabsList>

        {/* Suggestions Tab */}
        <TabsContent value="suggestions" className="space-y-4">
          <div className="flex items-center justify-between">
            <div className="text-sm text-muted-foreground">
              {suggestions.length} pending suggestions
            </div>
            <Button variant="outline" size="sm" onClick={fetchSuggestions}>
              Refresh
            </Button>
          </div>

          {suggestionsLoading ? (
            <div className="flex items-center justify-center h-32">
              <div className="text-muted-foreground">Loading suggestions...</div>
            </div>
          ) : suggestions.length === 0 ? (
            <Card className="p-8 text-center">
              <Lightbulb className="w-12 h-12 mx-auto mb-4 text-muted-foreground/50" />
              <p className="text-muted-foreground">No pending suggestions</p>
              <p className="text-sm text-muted-foreground mt-2">
                Suggestions will appear here when the system identifies opportunities to improve the strategy.
              </p>
            </Card>
          ) : (
            <div className="space-y-3">
              {suggestions.map(suggestion => (
                <Card key={suggestion.id} className="overflow-hidden">
                  <div
                    className="p-4 cursor-pointer hover:bg-muted/50 transition-colors"
                    onClick={() => toggleSuggestionExpand(suggestion.id)}
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-4">
                        <AlertTriangle className="w-5 h-5 text-yellow-500" />
                        <div>
                          <div className="font-semibold">
                            {suggestion.suggestion_type === 'threshold_adjustment' ? 'Lower Threshold' : 'Score Boost'}
                            {suggestion.symbol && ` - ${suggestion.symbol}`}
                          </div>
                          <div className="text-sm text-muted-foreground">
                            {formatTimestamp(suggestion.created_at)}
                          </div>
                        </div>
                      </div>

                      <div className="flex items-center gap-3">
                        {suggestion.evidence && (
                          <div className="text-right">
                            <div className="text-sm font-medium text-destructive">
                              -${suggestion.evidence.missed_profit.toFixed(2)}
                            </div>
                            <div className="text-xs text-muted-foreground">
                              missed profit
                            </div>
                          </div>
                        )}

                        {expandedSuggestionId === suggestion.id ? (
                          <ChevronUp className="w-5 h-5 text-muted-foreground" />
                        ) : (
                          <ChevronDown className="w-5 h-5 text-muted-foreground" />
                        )}
                      </div>
                    </div>
                  </div>

                  {/* Expanded Details */}
                  {expandedSuggestionId === suggestion.id && (
                    <div className="border-t bg-muted/20 p-4 space-y-4">
                      {/* Reason */}
                      <div>
                        <h4 className="font-semibold mb-2">Reason</h4>
                        <div className="text-sm text-muted-foreground bg-background p-3 rounded-md">
                          {suggestion.reason}
                        </div>
                      </div>

                      {/* Suggestion Details */}
                      <div>
                        <h4 className="font-semibold mb-2">Suggested Change</h4>
                        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                          <div className="bg-background p-3 rounded-md">
                            <div className="text-xs text-muted-foreground">Type</div>
                            <div className="text-sm font-medium">{suggestion.suggestion_data.type}</div>
                          </div>
                          {suggestion.suggestion_data.from !== undefined && (
                            <div className="bg-background p-3 rounded-md">
                              <div className="text-xs text-muted-foreground">From → To</div>
                              <div className="text-sm font-medium">
                                {suggestion.suggestion_data.from} → {suggestion.suggestion_data.to}
                              </div>
                            </div>
                          )}
                          {suggestion.suggestion_data.boost_amount !== undefined && (
                            <div className="bg-background p-3 rounded-md">
                              <div className="text-xs text-muted-foreground">Boost Amount</div>
                              <div className="text-sm font-medium">+{suggestion.suggestion_data.boost_amount}</div>
                            </div>
                          )}
                          <div className="bg-background p-3 rounded-md">
                            <div className="text-xs text-muted-foreground">Duration</div>
                            <div className="text-sm font-medium">{suggestion.suggestion_data.duration_hours}h</div>
                          </div>
                        </div>
                      </div>

                      {/* Conditions */}
                      <div>
                        <h4 className="font-semibold mb-2">Conditions</h4>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                          {Object.entries(suggestion.suggestion_data.condition).map(([key, value]) => (
                            <div key={key} className="bg-background p-3 rounded-md">
                              <div className="text-xs text-muted-foreground">{key}</div>
                              <div className="text-sm font-medium">{value}</div>
                            </div>
                          ))}
                        </div>
                      </div>

                      {/* Evidence */}
                      {suggestion.evidence && (
                        <div>
                          <h4 className="font-semibold mb-2">Evidence</h4>
                          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                            <div className="bg-background p-3 rounded-md">
                              <div className="text-xs text-muted-foreground">Return %</div>
                              <div className="text-sm font-medium text-green-500">
                                +{suggestion.evidence.return_pct.toFixed(1)}%
                              </div>
                            </div>
                            <div className="bg-background p-3 rounded-md">
                              <div className="text-xs text-muted-foreground">Score</div>
                              <div className="text-sm font-medium">{suggestion.evidence.score.toFixed(2)}</div>
                            </div>
                            <div className="bg-background p-3 rounded-md">
                              <div className="text-xs text-muted-foreground">Momentum</div>
                              <div className="text-sm font-medium">{suggestion.evidence.momentum.toFixed(2)}</div>
                            </div>
                            <div className="bg-background p-3 rounded-md">
                              <div className="text-xs text-muted-foreground">Support</div>
                              <div className="text-sm font-medium">{suggestion.evidence.support.toFixed(2)}</div>
                            </div>
                            <div className="bg-background p-3 rounded-md border-2 border-destructive/30">
                              <div className="text-xs text-muted-foreground">Missed Profit</div>
                              <div className="text-sm font-medium text-destructive">
                                ${suggestion.evidence.missed_profit.toFixed(2)}
                              </div>
                            </div>
                          </div>
                        </div>
                      )}

                      {/* Actions */}
                      <div className="flex gap-3 pt-2">
                        <Button
                          variant="outline"
                          size="sm"
                          className="flex-1"
                          onClick={(e) => {
                            e.stopPropagation()
                            dismissSuggestion(suggestion.id)
                          }}
                        >
                          <X className="w-4 h-4 mr-2" />
                          Dismiss
                        </Button>
                        <Button
                          size="sm"
                          className="flex-1"
                          onClick={(e) => {
                            e.stopPropagation()
                            markApplied(suggestion.id)
                          }}
                        >
                          <Check className="w-4 h-4 mr-2" />
                          Mark Applied
                        </Button>
                      </div>
                    </div>
                  )}
                </Card>
              ))}
            </div>
          )}
        </TabsContent>

        {/* Decision History Tab */}
        <TabsContent value="history" className="space-y-4">
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
        </TabsContent>
      </Tabs>
    </div>
  )
}
