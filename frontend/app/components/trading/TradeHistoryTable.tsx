'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

interface CompleteTrade {
  symbol: string
  side: 'LONG' | 'SHORT'
  entry_time: string
  exit_time: string
  entry_price: number
  exit_price: number
  quantity: number
  pnl: number
  pnl_pct: number
  duration_minutes: number
  total_commission: number
  entry_trade_id: number
  exit_trade_id: number
  leverage?: number | null  // Optional leverage (null for historical trades)
  strategy?: string | null   // Optional strategy (null for historical trades)
}

interface SymbolPerformance {
  trades: number
  wins: number
  pnl: number
  win_rate: number
}

interface TradeHistoryData {
  account_id: number
  total_trades: number
  total_pnl: number
  win_rate: number
  winning_trades: number
  losing_trades: number
  avg_pnl: number
  avg_duration_minutes: number
  // Advanced metrics
  profit_factor: number
  risk_reward: number
  avg_win: number
  avg_loss: number
  best_trade: number
  worst_trade: number
  max_drawdown: number
  max_consecutive_wins: number
  max_consecutive_losses: number
  gross_profit: number
  gross_loss: number
  symbol_performance: Record<string, SymbolPerformance>
  trades: CompleteTrade[]
}

interface TradeHistoryTableProps {
  accountId: number
}

type Timeframe = '5m' | '1h' | '1d' | 'all'

export default function TradeHistoryTable({ accountId }: TradeHistoryTableProps) {
  const [data, setData] = useState<TradeHistoryData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [timeframe, setTimeframe] = useState<Timeframe>('1d')
  const [symbolFilter, setSymbolFilter] = useState<string>('all')

  useEffect(() => {
    fetchTradeHistory()
  }, [accountId, timeframe, symbolFilter])

  const fetchTradeHistory = async () => {
    try {
      setLoading(true)
      setError(null)

      // Build query parameters
      const params = new URLSearchParams()
      if (timeframe !== 'all') {
        params.append('timeframe', timeframe)
      }
      if (symbolFilter !== 'all') {
        params.append('symbol', symbolFilter)
      }

      const response = await fetch(`/api/trade-history/${accountId}?${params}`)

      if (!response.ok) {
        throw new Error(`Failed to fetch trade history: ${response.statusText}`)
      }

      const historyData = await response.json()
      setData(historyData)
    } catch (err) {
      console.error('Error fetching trade history:', err)
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  const formatDate = (dateString: string) => {
    const date = new Date(dateString)
    return date.toLocaleString('it-IT', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    })
  }

  const formatDuration = (minutes: number) => {
    if (minutes < 60) {
      return `${minutes}m`
    }
    const hours = Math.floor(minutes / 60)
    const mins = minutes % 60
    return `${hours}h ${mins}m`
  }

  // Extract unique symbols for filter dropdown
  const uniqueSymbols = data?.trades
    ? Array.from(new Set(data.trades.map(t => t.symbol))).sort()
    : []

  return (
    <div className="space-y-4">
      {/* Statistics Summary Cards - Row 1: Basic Metrics */}
      {data && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Total Trades</CardDescription>
                <CardTitle className="text-2xl">{data.total_trades}</CardTitle>
              </CardHeader>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Total P&L</CardDescription>
                <CardTitle className={`text-2xl ${data.total_pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                  ${data.total_pnl.toFixed(2)}
                </CardTitle>
              </CardHeader>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Win Rate</CardDescription>
                <CardTitle className="text-2xl">{data.win_rate.toFixed(1)}%</CardTitle>
              </CardHeader>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Avg Duration</CardDescription>
                <CardTitle className="text-2xl">{formatDuration(data.avg_duration_minutes)}</CardTitle>
              </CardHeader>
            </Card>
          </div>

          {/* Row 2: Advanced Metrics */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Profit Factor</CardDescription>
                <CardTitle className={`text-2xl ${data.profit_factor >= 1 ? 'text-green-600' : 'text-red-600'}`}>
                  {data.profit_factor.toFixed(2)}
                </CardTitle>
              </CardHeader>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Risk/Reward</CardDescription>
                <CardTitle className={`text-2xl ${data.risk_reward >= 1 ? 'text-green-600' : 'text-yellow-600'}`}>
                  {data.risk_reward.toFixed(2)}
                </CardTitle>
              </CardHeader>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Avg Win</CardDescription>
                <CardTitle className="text-2xl text-green-600">${data.avg_win.toFixed(2)}</CardTitle>
              </CardHeader>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Avg Loss</CardDescription>
                <CardTitle className="text-2xl text-red-600">-${data.avg_loss.toFixed(2)}</CardTitle>
              </CardHeader>
            </Card>
          </div>

          {/* Row 3: Risk Metrics */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Best Trade</CardDescription>
                <CardTitle className="text-2xl text-green-600">${data.best_trade.toFixed(2)}</CardTitle>
              </CardHeader>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Worst Trade</CardDescription>
                <CardTitle className="text-2xl text-red-600">${data.worst_trade.toFixed(2)}</CardTitle>
              </CardHeader>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Max Drawdown</CardDescription>
                <CardTitle className="text-2xl text-red-600">${data.max_drawdown.toFixed(2)}</CardTitle>
              </CardHeader>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardDescription>Max Wins/Losses</CardDescription>
                <CardTitle className="text-xl">
                  <span className="text-green-600">{data.max_consecutive_wins}W</span>
                  {' / '}
                  <span className="text-red-600">{data.max_consecutive_losses}L</span>
                </CardTitle>
              </CardHeader>
            </Card>
          </div>

        </>
      )}

      {/* Filters */}
      <Card>
        <CardHeader>
          <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
            <CardTitle>Trade History</CardTitle>
            <div className="flex items-center gap-4">
              <Tabs value={timeframe} onValueChange={(value) => setTimeframe(value as Timeframe)}>
                <TabsList>
                  <TabsTrigger value="5m">5 Minutes</TabsTrigger>
                  <TabsTrigger value="1h">1 Hour</TabsTrigger>
                  <TabsTrigger value="1d">1 Day</TabsTrigger>
                  <TabsTrigger value="all">All Time</TabsTrigger>
                </TabsList>
              </Tabs>

              <Select value={symbolFilter} onValueChange={setSymbolFilter}>
                <SelectTrigger className="w-[140px]">
                  <SelectValue placeholder="Symbol" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All symbols</SelectItem>
                  {uniqueSymbols.map(symbol => (
                    <SelectItem key={symbol} value={symbol}>{symbol}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        </CardHeader>

        <CardContent>
          {loading && (
            <div className="text-center py-8 text-muted-foreground">
              Loading trade history...
            </div>
          )}

          {error && (
            <div className="text-center py-8 text-red-600">
              Error: {error}
            </div>
          )}

          {!loading && !error && data && data.trades.length === 0 && (
            <div className="text-center py-8 text-muted-foreground">
              No completed trades found for this period
            </div>
          )}

          {!loading && !error && data && data.trades.length > 0 && (
            <div className="overflow-auto max-h-[600px]">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Symbol</TableHead>
                    <TableHead>Side</TableHead>
                    <TableHead>Entry Time</TableHead>
                    <TableHead>Exit Time</TableHead>
                    <TableHead className="text-right">Entry Price</TableHead>
                    <TableHead className="text-right">Exit Price</TableHead>
                    <TableHead className="text-right">Quantity</TableHead>
                    <TableHead className="text-right">Leverage</TableHead>
                    <TableHead className="text-right">P&L</TableHead>
                    <TableHead className="text-right">P&L %</TableHead>
                    <TableHead className="text-right">Strategy</TableHead>
                    <TableHead className="text-right">Duration</TableHead>
                    <TableHead className="text-right">Commission</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.trades.map((trade, index) => (
                    <TableRow key={`${trade.entry_trade_id}-${trade.exit_trade_id}-${index}`}>
                      <TableCell className="font-medium">{trade.symbol}</TableCell>
                      <TableCell>
                        <span className={`px-2 py-1 rounded text-xs ${
                          trade.side === 'LONG' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                        }`}>
                          {trade.side}
                        </span>
                      </TableCell>
                      <TableCell className="text-xs">{formatDate(trade.entry_time)}</TableCell>
                      <TableCell className="text-xs">{formatDate(trade.exit_time)}</TableCell>
                      <TableCell className="text-right">${trade.entry_price.toFixed(2)}</TableCell>
                      <TableCell className="text-right">${trade.exit_price.toFixed(2)}</TableCell>
                      <TableCell className="text-right">{trade.quantity.toFixed(6)}</TableCell>
                      <TableCell className="text-right text-xs">
                        {trade.leverage ? `${trade.leverage.toFixed(1)}x` : '-'}
                      </TableCell>
                      <TableCell className={`text-right font-medium ${
                        trade.pnl >= 0 ? 'text-green-600' : 'text-red-600'
                      }`}>
                        ${trade.pnl.toFixed(2)}
                      </TableCell>
                      <TableCell className={`text-right ${
                        trade.pnl_pct >= 0 ? 'text-green-600' : 'text-red-600'
                      }`}>
                        {trade.pnl_pct >= 0 ? '+' : ''}{trade.pnl_pct.toFixed(2)}%
                      </TableCell>
                      <TableCell className="text-right text-xs">
                        {trade.strategy || '-'}
                      </TableCell>
                      <TableCell className="text-right text-xs">{formatDuration(trade.duration_minutes)}</TableCell>
                      <TableCell className="text-right text-xs">${trade.total_commission.toFixed(4)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
