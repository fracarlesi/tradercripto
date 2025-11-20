import { useState, useEffect } from 'react'
import { Card } from '@/components/ui/card'
import { Calendar, TrendingUp, TrendingDown, AlertCircle, CheckCircle, ChevronDown, ChevronUp } from 'lucide-react'
import { Button } from '@/components/ui/button'

interface SkillMetrics {
  total_trades: number
  winning_trades: number
  losing_trades: number
  win_rate_pct: number
  profit_factor: number
  risk_reward_ratio: number
  max_drawdown_pct: number
  sharpe_ratio: number
  sortino_ratio: number
  entry_timing_quality_pct: number
  exit_timing_quality_pct: number
  false_signal_rate_pct: number
  avg_hold_time_hours: number
  total_decisions: number
}

interface IndicatorPerformance {
  [key: string]: {
    accuracy_pct: number
    times_used: number
    win_rate: number
    notes: string
  }
}

interface Mistake {
  trade_symbol: string
  mistake: string
  cost_usd: number
  lesson: string
}

interface DeepSeekAnalysis {
  summary: string
  indicator_performance: IndicatorPerformance
  worst_mistakes?: Mistake[]
  systematic_errors?: string[]
}

interface DailyReport {
  id: number
  report_date: string
  skill_metrics: SkillMetrics
  deepseek_analysis: DeepSeekAnalysis
  suggested_weights: Record<string, number> | null
  suggested_prompt_changes: {
    add_rules?: string[]
    remove_rules?: string[]
  } | null
  status: string
  reviewed_at: string | null
}

interface DailyReportsProps {
  accountId: number
}

export default function DailyReports({ accountId }: DailyReportsProps) {
  const [reports, setReports] = useState<DailyReport[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<number | null>(null)

  useEffect(() => {
    fetchReports()
  }, [accountId])

  const fetchReports = async () => {
    try {
      setLoading(true)
      setError(null)

      const response = await fetch(`/api/learning/reports/${accountId}?limit=30`)
      if (!response.ok) {
        throw new Error(`Failed to fetch reports: ${response.statusText}`)
      }

      const data = await response.json()
      setReports(data.reports || [])
    } catch (err) {
      console.error('Error fetching daily reports:', err)
      setError(err instanceof Error ? err.message : 'Failed to load daily reports')
    } finally {
      setLoading(false)
    }
  }

  const toggleExpand = (id: number) => {
    setExpandedId(expandedId === id ? null : id)
  }

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    })
  }

  const getMetricColor = (value: number, threshold: { good: number; bad: number }, inverse = false) => {
    if (inverse) {
      if (value <= threshold.good) return 'text-green-500'
      if (value >= threshold.bad) return 'text-red-500'
    } else {
      if (value >= threshold.good) return 'text-green-500'
      if (value <= threshold.bad) return 'text-red-500'
    }
    return 'text-yellow-500'
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-muted-foreground">Loading daily reports...</div>
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

  if (reports.length === 0) {
    return (
      <Card className="p-8 text-center">
        <Calendar className="w-12 h-12 mx-auto mb-4 text-muted-foreground/50" />
        <p className="text-muted-foreground">No daily reports yet</p>
        <p className="text-sm text-muted-foreground mt-2">
          Reports are generated daily at 21:00 UTC analyzing your trading performance.
        </p>
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="text-sm text-muted-foreground">
          {reports.length} daily reports
        </div>
        <Button variant="outline" size="sm" onClick={fetchReports}>
          Refresh
        </Button>
      </div>

      <div className="space-y-3">
        {reports.map(report => {
          const metrics = report.skill_metrics
          const analysis = report.deepseek_analysis

          return (
            <Card key={report.id} className="overflow-hidden">
              {/* Summary Row */}
              <div
                className="p-4 cursor-pointer hover:bg-muted/50 transition-colors"
                onClick={() => toggleExpand(report.id)}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-4">
                    <Calendar className="w-5 h-5 text-primary" />
                    <div>
                      <div className="font-semibold">{formatDate(report.report_date)}</div>
                      <div className="text-sm text-muted-foreground">
                        {metrics.total_trades} trades • {metrics.total_decisions} decisions
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-4">
                    {/* Win Rate */}
                    <div className="text-right">
                      <div className={`text-sm font-medium ${getMetricColor(metrics.win_rate_pct, { good: 50, bad: 45 })}`}>
                        {metrics.win_rate_pct.toFixed(1)}%
                      </div>
                      <div className="text-xs text-muted-foreground">Win Rate</div>
                    </div>

                    {/* Profit Factor */}
                    <div className="text-right">
                      <div className={`text-sm font-medium ${getMetricColor(metrics.profit_factor, { good: 1.5, bad: 1.0 })}`}>
                        {metrics.profit_factor.toFixed(2)}
                      </div>
                      <div className="text-xs text-muted-foreground">Profit Factor</div>
                    </div>

                    {/* Sharpe Ratio */}
                    <div className="text-right">
                      <div className={`text-sm font-medium ${getMetricColor(metrics.sharpe_ratio, { good: 1.0, bad: 0.5 })}`}>
                        {metrics.sharpe_ratio.toFixed(2)}
                      </div>
                      <div className="text-xs text-muted-foreground">Sharpe</div>
                    </div>

                    {expandedId === report.id ? (
                      <ChevronUp className="w-5 h-5 text-muted-foreground" />
                    ) : (
                      <ChevronDown className="w-5 h-5 text-muted-foreground" />
                    )}
                  </div>
                </div>
              </div>

              {/* Expanded Details */}
              {expandedId === report.id && (
                <div className="border-t bg-muted/20 p-4 space-y-6">
                  {/* Summary */}
                  <div>
                    <h4 className="font-semibold mb-2 flex items-center gap-2">
                      <AlertCircle className="w-4 h-4" />
                      AI Summary
                    </h4>
                    <div className="text-sm text-muted-foreground bg-background p-3 rounded-md">
                      {analysis.summary}
                    </div>
                  </div>

                  {/* Skill Metrics */}
                  <div>
                    <h4 className="font-semibold mb-3">📊 Skill-Based Metrics</h4>
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                      <div className="bg-background p-3 rounded-md">
                        <div className="text-xs text-muted-foreground">Win Rate</div>
                        <div className={`text-sm font-medium ${getMetricColor(metrics.win_rate_pct, { good: 50, bad: 45 })}`}>
                          {metrics.win_rate_pct.toFixed(1)}%
                        </div>
                      </div>
                      <div className="bg-background p-3 rounded-md">
                        <div className="text-xs text-muted-foreground">Profit Factor</div>
                        <div className={`text-sm font-medium ${getMetricColor(metrics.profit_factor, { good: 1.5, bad: 1.0 })}`}>
                          {metrics.profit_factor.toFixed(2)}
                        </div>
                      </div>
                      <div className="bg-background p-3 rounded-md">
                        <div className="text-xs text-muted-foreground">Risk/Reward</div>
                        <div className={`text-sm font-medium ${getMetricColor(metrics.risk_reward_ratio, { good: 1.5, bad: 1.0 })}`}>
                          {metrics.risk_reward_ratio.toFixed(2)}
                        </div>
                      </div>
                      <div className="bg-background p-3 rounded-md">
                        <div className="text-xs text-muted-foreground">Max Drawdown</div>
                        <div className={`text-sm font-medium ${getMetricColor(metrics.max_drawdown_pct, { good: 5, bad: 10 }, true)}`}>
                          {metrics.max_drawdown_pct.toFixed(1)}%
                        </div>
                      </div>
                      <div className="bg-background p-3 rounded-md">
                        <div className="text-xs text-muted-foreground">Sharpe Ratio</div>
                        <div className={`text-sm font-medium ${getMetricColor(metrics.sharpe_ratio, { good: 1.0, bad: 0.5 })}`}>
                          {metrics.sharpe_ratio.toFixed(2)}
                        </div>
                      </div>
                      <div className="bg-background p-3 rounded-md">
                        <div className="text-xs text-muted-foreground">Entry Timing</div>
                        <div className={`text-sm font-medium ${getMetricColor(metrics.entry_timing_quality_pct, { good: 60, bad: 40 })}`}>
                          {metrics.entry_timing_quality_pct.toFixed(1)}%
                        </div>
                      </div>
                      <div className="bg-background p-3 rounded-md">
                        <div className="text-xs text-muted-foreground">Exit Timing</div>
                        <div className={`text-sm font-medium ${getMetricColor(metrics.exit_timing_quality_pct, { good: 60, bad: 40 })}`}>
                          {metrics.exit_timing_quality_pct.toFixed(1)}%
                        </div>
                      </div>
                      <div className="bg-background p-3 rounded-md">
                        <div className="text-xs text-muted-foreground">False Signals</div>
                        <div className={`text-sm font-medium ${getMetricColor(metrics.false_signal_rate_pct, { good: 15, bad: 25 }, true)}`}>
                          {metrics.false_signal_rate_pct.toFixed(1)}%
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* Indicator Performance */}
                  {analysis.indicator_performance && (
                    <div>
                      <h4 className="font-semibold mb-3">📈 Indicator Performance</h4>
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        {Object.entries(analysis.indicator_performance).map(([indicator, perf]) => (
                          <div key={indicator} className="bg-background p-3 rounded-md">
                            <div className="flex items-center justify-between mb-2">
                              <div className="font-medium text-sm uppercase">{indicator}</div>
                              <div className={`text-sm font-bold ${getMetricColor(perf.accuracy_pct, { good: 60, bad: 50 })}`}>
                                {perf.accuracy_pct.toFixed(0)}%
                              </div>
                            </div>
                            <div className="text-xs text-muted-foreground space-y-1">
                              <div>Used {perf.times_used} times • {perf.win_rate.toFixed(0)}% win rate</div>
                              <div className="italic">{perf.notes}</div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Worst Mistakes */}
                  {analysis.worst_mistakes && analysis.worst_mistakes.length > 0 && (
                    <div>
                      <h4 className="font-semibold mb-3 flex items-center gap-2">
                        <AlertCircle className="w-4 h-4 text-red-500" />
                        Worst Mistakes
                      </h4>
                      <div className="space-y-2">
                        {analysis.worst_mistakes.map((mistake, idx) => (
                          <div key={idx} className="bg-background p-3 rounded-md border-l-2 border-red-500">
                            <div className="flex items-center justify-between mb-1">
                              <div className="font-medium text-sm">{mistake.trade_symbol}</div>
                              <div className="text-sm font-bold text-red-500">
                                -${mistake.cost_usd.toFixed(2)}
                              </div>
                            </div>
                            <div className="text-xs text-muted-foreground space-y-1">
                              <div><span className="font-medium">Mistake:</span> {mistake.mistake}</div>
                              <div><span className="font-medium">Lesson:</span> {mistake.lesson}</div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Systematic Errors */}
                  {analysis.systematic_errors && analysis.systematic_errors.length > 0 && (
                    <div>
                      <h4 className="font-semibold mb-3 flex items-center gap-2">
                        <TrendingDown className="w-4 h-4 text-yellow-500" />
                        Systematic Errors
                      </h4>
                      <div className="space-y-2">
                        {analysis.systematic_errors.map((error, idx) => (
                          <div key={idx} className="bg-background p-2 rounded-md text-sm text-muted-foreground flex items-start gap-2">
                            <span className="text-yellow-500">•</span>
                            <span>{error}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Suggested Weights */}
                  {report.suggested_weights && (
                    <div>
                      <h4 className="font-semibold mb-3 flex items-center gap-2">
                        <TrendingUp className="w-4 h-4 text-green-500" />
                        Suggested Indicator Weights
                      </h4>
                      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                        {Object.entries(report.suggested_weights).map(([indicator, weight]) => (
                          <div key={indicator} className="bg-background p-2 rounded-md">
                            <div className="text-xs text-muted-foreground uppercase">{indicator}</div>
                            <div className="text-sm font-medium">{weight.toFixed(2)}</div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Prompt Changes */}
                  {report.suggested_prompt_changes && (
                    <div>
                      <h4 className="font-semibold mb-3">📝 Suggested Rule Changes</h4>

                      {report.suggested_prompt_changes.add_rules && report.suggested_prompt_changes.add_rules.length > 0 && (
                        <div className="mb-3">
                          <div className="text-sm font-medium mb-2 flex items-center gap-2">
                            <CheckCircle className="w-4 h-4 text-green-500" />
                            Add These Rules
                          </div>
                          <div className="space-y-2">
                            {report.suggested_prompt_changes.add_rules.map((rule, idx) => (
                              <div key={idx} className="bg-background p-2 rounded-md text-sm border-l-2 border-green-500">
                                {rule}
                              </div>
                            ))}
                          </div>
                        </div>
                      )}

                      {report.suggested_prompt_changes.remove_rules && report.suggested_prompt_changes.remove_rules.length > 0 && (
                        <div>
                          <div className="text-sm font-medium mb-2 flex items-center gap-2">
                            <AlertCircle className="w-4 h-4 text-red-500" />
                            Remove These Rules
                          </div>
                          <div className="space-y-2">
                            {report.suggested_prompt_changes.remove_rules.map((rule, idx) => (
                              <div key={idx} className="bg-background p-2 rounded-md text-sm border-l-2 border-red-500">
                                {rule}
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Status */}
                  <div className="text-xs text-muted-foreground border-t pt-3">
                    Status: <span className="font-medium">{report.status}</span>
                    {report.reviewed_at && ` • Reviewed: ${formatDate(report.reviewed_at)}`}
                  </div>
                </div>
              )}
            </Card>
          )
        })}
      </div>
    </div>
  )
}
