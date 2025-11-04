import React from 'react'
import { Button } from '@/components/ui/button'

interface PositionLite { 
  symbol: string
  market: string
  available_quantity: number 
}

interface TradeButtonsProps {
  symbol: string
  market: string
  orderType: 'MARKET' | 'LIMIT'
  price: number
  quantity: number
  user?: {
    // REMOVED: current_cash, frozen_cash (deprecated - use AccountOverview data)
    has_password: boolean
  }
  positions?: PositionLite[]
  lastPrices?: Record<string, number | null>
  onBuy: () => void
  onSell: () => void
}

export default function TradeButtons({
  symbol,
  market,
  orderType,
  price,
  quantity,
  user,
  positions = [],
  lastPrices = {},
  onBuy,
  onSell
}: TradeButtonsProps) {
  // US market only - USD currency
  const currencySymbol = '$'

  const amount = price * quantity
  // REMOVED: cashAvailable, frozenCash (deprecated - balance data from AccountOverview API)

  const positionAvailable = React.useMemo(() => {
    const p = positions.find(p => p.symbol === symbol && p.market === market)
    return p?.available_quantity || 0
  }, [positions, symbol, market])

  const effectivePrice = orderType === 'MARKET' ? (lastPrices[`${symbol}.${market}`] ?? price) : price
  // REMOVED: maxBuyable calculation (requires balance data from AccountOverview)

  return (
    <div className="space-y-4">
      {/* Trading Information */}
      <div className="space-y-3 pt-4">
        <div className="flex justify-between">
          <span className="text-xs">Amount</span>
          <span className="text-xs">{currencySymbol}{amount.toFixed(2)}</span>
        </div>
        {/* REMOVED: Available Cash, Frozen Cash displays (deprecated - use AccountOverview data) */}
        <div className="flex justify-between">
          <span className="text-xs">Sellable Position</span>
          <span className="text-xs text-destructive">{positionAvailable}</span>
        </div>
        {/* REMOVED: Max Buyable display (deprecated - requires AccountOverview data) */}
      </div>

      {/* Buy/Sell buttons */}
      <div className="flex gap-2 pt-4">
        <Button 
          className="flex-1 text-xs h-6 rounded-xl bg-destructive hover:bg-destructive/90 text-destructive-foreground"
          onClick={onBuy}
        >
          Buy
        </Button>
        <Button 
          className="flex-1 text-xs h-6 rounded-xl bg-green-600 hover:bg-green-500 text-white"
          onClick={onSell}
        >
          Sell
        </Button>
      </div>
    </div>
  )
}
