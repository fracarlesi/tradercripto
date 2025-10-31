import React from 'react'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { Button } from '@/components/ui/button'
import { toast } from 'react-hot-toast'
import AssetCurveWithData from './AssetCurveWithData'
import AccountSelector from '@/components/layout/AccountSelector'
import TradingPanel from '@/components/trading/TradingPanel'
import { Doughnut } from 'react-chartjs-2'
import { Chart as ChartJS, ArcElement, Tooltip, Legend } from 'chart.js'
import { AIDecision } from '@/lib/api'

// Register Chart.js components for pie chart
ChartJS.register(ArcElement, Tooltip, Legend)

interface Account {
  id: number
  user_id: number
  name: string
  account_type: string
  initial_capital: number
  current_cash: number
  frozen_cash: number
}

interface Overview {
  account: Account
  total_assets: number
  positions_value: number
}

interface Position {
  id: number
  account_id?: number
  user_id?: number
  symbol: string
  name: string
  market: string
  quantity: number
  available_quantity: number
  avg_cost: number
  last_price?: number | null
  market_value?: number | null
}

interface Order {
  id: number
  order_no: string
  symbol: string
  name: string
  market: string
  side: string
  order_type: string
  price?: number
  quantity: number
  filled_quantity: number
  status: string
}

interface Trade {
  id: number
  order_id: number
  account_id?: number
  user_id?: number
  symbol: string
  name: string
  market: string
  side: string
  price: number
  quantity: number
  commission: number
  trade_time: string
}

interface AccountDataViewProps {
  overview: Overview | null
  positions: Position[]
  orders: Order[]
  trades: Trade[]
  aiDecisions: AIDecision[]
  allAssetCurves: any[]
  wsRef?: React.MutableRefObject<WebSocket | null>
  onSwitchAccount: (accountId: number) => void
  onRefreshData: () => void
  accountRefreshTrigger?: number
  showAssetCurves?: boolean
  showTradingPanel?: boolean
  accounts?: any[]
  loadingAccounts?: boolean
}

const API_BASE = typeof window !== 'undefined' ? window.location.origin : 'http://127.0.0.1:5611'

export default function AccountDataView({
  overview,
  positions,
  orders,
  trades,
  aiDecisions,
  allAssetCurves,
  wsRef,
  onSwitchAccount,
  onRefreshData,
  accountRefreshTrigger,
  showAssetCurves = true,
  showTradingPanel = false,
  accounts,
  loadingAccounts
}: AccountDataViewProps) {

  const cancelOrder = async (orderId: number) => {
    try {
      const response = await fetch(`${API_BASE}/api/orders/cancel/${orderId}`, {
        method: 'POST'
      })

      if (response.ok) {
        toast.success('Order cancelled')
        onRefreshData()
      } else {
        throw new Error(await response.text())
      }
    } catch (error) {
      console.error('Failed to cancel order:', error)
      toast.error('Failed to cancel order')
    }
  }

  if (!overview) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-muted-foreground">Loading account data...</div>
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col space-y-6">
      {/* Main Content */}
      <div className={`grid gap-6 overflow-hidden ${showAssetCurves ? 'grid-cols-5' : 'grid-cols-1'} h-full`}>
        {/* Asset Curves */}
        {showAssetCurves && (
          <div className="col-span-3">
            <AssetCurveWithData data={allAssetCurves} wsRef={wsRef} />
          </div>
        )}

        {/* Tabs and Trading Panel */}
        <div className={`${showAssetCurves ? 'col-span-2' : 'col-span-1'} overflow-hidden flex flex-col`}>
          {/* Account Selector */}
          <div className="flex justify-end mb-4">
            <AccountSelector
              currentAccount={overview.account}
              onAccountChange={onSwitchAccount}
              refreshTrigger={accountRefreshTrigger}
              accounts={accounts}
              loadingExternal={loadingAccounts}
            />
          </div>

          {/* Content Area */}
          <div className={`flex-1 overflow-hidden ${showTradingPanel ? 'grid grid-cols-4 gap-4' : ''}`}>
            {/* Tabs */}
            <div className={`${showTradingPanel ? 'col-span-3' : 'col-span-1'} overflow-hidden`}>
              <Tabs defaultValue="ai-decisions" className="h-full flex flex-col">
                <TabsList className="grid w-full grid-cols-4">
                  <TabsTrigger value="ai-decisions">AI Decisions</TabsTrigger>
                  <TabsTrigger value="positions">Positions</TabsTrigger>
                  <TabsTrigger value="orders">Orders</TabsTrigger>
                  <TabsTrigger value="trades">Trades</TabsTrigger>
                </TabsList>

                <div className="flex-1 overflow-hidden">
                  <TabsContent value="ai-decisions" className="h-full overflow-y-auto">
                    <AIDecisionLog aiDecisions={aiDecisions} />
                  </TabsContent>

                  <TabsContent value="positions" className="h-full overflow-y-auto">
                    <div className="space-y-6">
                      <PortfolioPieChart overview={overview} positions={positions} />
                      <PositionList positions={positions} />
                    </div>
                  </TabsContent>

                  <TabsContent value="orders" className="h-full overflow-y-auto">
                    <OrderBook orders={orders} onCancelOrder={cancelOrder} />
                  </TabsContent>

                  <TabsContent value="trades" className="h-full overflow-y-auto">
                    <TradeHistory trades={trades} />
                  </TabsContent>
                </div>
              </Tabs>
            </div>

            {/* Trading Panel */}
            {showTradingPanel && (
              <div className="col-span-1 overflow-hidden">
                <TradingPanel
                  onPlace={(payload) => {
                    // Handle order placement via websocket
                    if (wsRef?.current && wsRef.current.readyState === WebSocket.OPEN) {
                      wsRef.current.send(JSON.stringify({
                        type: 'place_order',
                        ...payload
                      }))
                    }
                  }}
                  user={overview?.account ? {
                    id: overview.account.id.toString(),
                    current_cash: overview.account.current_cash,
                    frozen_cash: overview.account.frozen_cash,
                    has_password: true // Assume has password for now
                  } : undefined}
                  positions={positions.map(p => ({
                    symbol: p.symbol,
                    market: p.market,
                    available_quantity: p.available_quantity
                  }))}
                  lastPrices={Object.fromEntries(
                    positions.map(p => [`${p.symbol}.${p.market}`, p.last_price ?? null])
                  )}
                />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// Order Book Component
function OrderBook({ orders, onCancelOrder }: { orders: Order[], onCancelOrder: (id: number) => void }) {
  return (
    <div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Time</TableHead>
            <TableHead>Order No</TableHead>
            <TableHead>Symbol</TableHead>
            <TableHead>Side</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>Price</TableHead>
            <TableHead>Qty</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {orders.map(o => (
            <TableRow key={o.id}>
              <TableCell>{o.id}</TableCell>
              <TableCell>{o.order_no}</TableCell>
              <TableCell>{o.symbol}.{o.market}</TableCell>
              <TableCell>{o.side}</TableCell>
              <TableCell>{o.order_type}</TableCell>
              <TableCell>{o.price ?? '-'}</TableCell>
              <TableCell>{o.quantity}</TableCell>
              <TableCell>{o.status}</TableCell>
              <TableCell>
                {o.status === 'PENDING' ? (
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() => onCancelOrder(o.id)}
                  >
                    Cancel
                  </Button>
                ) : null}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

// Position List Component
function PositionList({ positions }: { positions: Position[] }) {
  return (
    <div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Symbol</TableHead>
            <TableHead>Name</TableHead>
            <TableHead>Qty</TableHead>
            <TableHead>Avg Cost</TableHead>
            <TableHead>Last Price</TableHead>
            <TableHead>Market Value</TableHead>
            <TableHead>P&L</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {positions.map(p => {
            const pnl = p.last_price && p.market_value ? p.market_value - (p.quantity * p.avg_cost) : 0
            const pnlPercent = p.avg_cost > 0 ? (pnl / (p.quantity * p.avg_cost)) * 100 : 0
            return (
              <TableRow key={p.id}>
                <TableCell>{p.symbol}.{p.market}</TableCell>
                <TableCell>{p.name}</TableCell>
                <TableCell>{p.quantity.toLocaleString()}</TableCell>
                <TableCell>${p.avg_cost.toFixed(4)}</TableCell>
                <TableCell>${p.last_price?.toFixed(4) ?? '-'}</TableCell>
                <TableCell>${p.market_value?.toFixed(2) ?? '-'}</TableCell>
                <TableCell className={pnl >= 0 ? 'text-green-600' : 'text-red-600'}>
                  ${pnl.toFixed(2)} ({pnlPercent.toFixed(2)}%)
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

// Trade History Component
function TradeHistory({ trades }: { trades: Trade[] }) {
  return (
    <div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Time</TableHead>
            <TableHead>Symbol</TableHead>
            <TableHead>Side</TableHead>
            <TableHead>Price</TableHead>
            <TableHead>Qty</TableHead>
            <TableHead>Commission</TableHead>
            <TableHead>Total</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {trades.map(t => (
            <TableRow key={t.id}>
              <TableCell>{formatLocalTime(t.trade_time)}</TableCell>
              <TableCell>{t.symbol}.{t.market}</TableCell>
              <TableCell className={t.side === 'BUY' ? 'text-green-600' : 'text-red-600'}>
                {t.side}
              </TableCell>
              <TableCell>${t.price.toFixed(4)}</TableCell>
              <TableCell>{t.quantity.toLocaleString()}</TableCell>
              <TableCell>${t.commission.toFixed(2)}</TableCell>
              <TableCell>${(t.price * t.quantity).toFixed(2)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

// Helper function to format timestamp to local timezone
function formatLocalTime(timestamp: string): string {
  // Add 'Z' to treat timestamp as UTC if not already present
  const utcTimestamp = timestamp.endsWith('Z') ? timestamp : timestamp + 'Z'
  const date = new Date(utcTimestamp)
  // Format: DD/MM/YYYY HH:MM:SS
  const day = date.getDate().toString().padStart(2, '0')
  const month = (date.getMonth() + 1).toString().padStart(2, '0')
  const year = date.getFullYear()
  const hours = date.getHours().toString().padStart(2, '0')
  const minutes = date.getMinutes().toString().padStart(2, '0')
  const seconds = date.getSeconds().toString().padStart(2, '0')
  return `${day}/${month}/${year} ${hours}:${minutes}:${seconds}`
}

// AI Decision Log Component
function AIDecisionLog({ aiDecisions }: { aiDecisions: AIDecision[] }) {
  return (
    <div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Time</TableHead>
            <TableHead>Operation</TableHead>
            <TableHead>Symbol</TableHead>
            <TableHead>Prev %</TableHead>
            <TableHead>Target %</TableHead>
            <TableHead>Balance</TableHead>
            <TableHead>Executed</TableHead>
            <TableHead>Reason</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {aiDecisions.map(decision => (
            <TableRow key={decision.id}>
              <TableCell>{formatLocalTime(decision.decision_time)}</TableCell>
              <TableCell>
                <span className={`px-2 py-1 rounded text-xs font-medium ${
                  decision.operation === 'buy' ? 'bg-green-100 text-green-800' :
                  decision.operation === 'sell' ? 'bg-red-100 text-red-800' :
                  'bg-gray-100 text-gray-800'
                }`}>
                  {decision.operation?.toUpperCase() || 'N/A'}
                </span>
              </TableCell>
              <TableCell>{decision.symbol || '-'}</TableCell>
              <TableCell>{((decision.prev_portion || 0) * 100).toFixed(2)}%</TableCell>
              <TableCell>{((decision.target_portion || 0) * 100).toFixed(2)}%</TableCell>
              <TableCell>${(decision.total_balance || 0).toFixed(2)}</TableCell>
              <TableCell>
                <span className={`px-2 py-1 rounded text-xs font-medium ${
                  decision.executed === 'true' ? 'bg-green-100 text-green-800' : 'bg-yellow-100 text-yellow-800'
                }`}>
                  {decision.executed === 'true' ? 'Yes' : 'No'}
                </span>
              </TableCell>
              <TableCell className="max-w-xs truncate" title={decision.reason}>
                {decision.reason || 'No reason provided'}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

// Portfolio Pie Chart Component
function PortfolioPieChart({ overview, positions }: { overview: Overview, positions: Position[] }) {
  // Calculate portfolio composition
  const totalValue = overview.total_assets
  const cashValue = overview.account.current_cash

  // Group positions by symbol for the pie chart
  const positionData = positions.reduce((acc, position) => {
    const key = `${position.symbol}.${position.market}`
    const value = position.market_value || 0
    if (acc[key]) {
      acc[key] += value
    } else {
      acc[key] = value
    }
    return acc
  }, {} as Record<string, number>)

  // Create chart data
  const labels = ['Cash', ...Object.keys(positionData)]
  const data = [cashValue, ...Object.values(positionData)]
  const colors = [
    '#e5e7eb', // Cash - gray
    '#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', 
    '#f97316', '#06b6d4', '#84cc16', '#ec4899', '#6366f1'
  ]

  const chartData = {
    labels,
    datasets: [
      {
        data,
        backgroundColor: colors.slice(0, labels.length),
        borderColor: colors.slice(0, labels.length).map(color => color + '80'),
        borderWidth: 1,
      },
    ],
  }

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    cutout: '60%', // Makes the doughnut hole larger for center text
    plugins: {
      legend: {
        position: 'bottom' as const,
        labels: {
          padding: 20,
          usePointStyle: true,
        },
      },
      tooltip: {
        callbacks: {
          label: function(context: any) {
            const value = context.parsed
            const percentage = ((value / totalValue) * 100).toFixed(1)
            return `${context.label}: $${value.toLocaleString()} (${percentage}%)`
          }
        }
      }
    },
  }

  return (
    <>
      <div className="h-80 relative">
        <Doughnut data={chartData} options={options} />
        {/* Center Text Overlay */}
        <div className="absolute inset-0 flex flex-col justify-center pointer-events-none mb-4">
          <div className="text-center">
            <div className="text-md font-bold">
              ${totalValue.toLocaleString()}
            </div>
            <div className="text-xs text-gray-500 mt-1">
              Total
            </div>
          </div>
        </div>
      </div>
    </>
  )
}