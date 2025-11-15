'use client'

import { useEffect, useState } from 'react'
import { AlertTriangle, X } from 'lucide-react'
import { getWebSocketHealth, type WebSocketHealthResponse } from '@/lib/api'

export default function WebSocketWarningBanner() {
  const [wsHealth, setWsHealth] = useState<WebSocketHealthResponse | null>(null)
  const [dismissed, setDismissed] = useState(false)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const checkHealth = async () => {
      try {
        const health = await getWebSocketHealth()
        setWsHealth(health)
      } catch (error) {
        console.error('Failed to check WebSocket health:', error)
        // If the health check fails, assume unhealthy
        setWsHealth({
          healthy: false,
          connected: false,
          symbols_cached: 0,
          total_candles: 0,
          memory_mb: 0,
          message: 'Unable to check WebSocket status'
        })
      } finally {
        setLoading(false)
      }
    }

    // Check immediately
    checkHealth()

    // Check every 30 seconds
    const interval = setInterval(checkHealth, 30000)

    return () => clearInterval(interval)
  }, [])

  // Don't show banner if loading, dismissed, or WebSocket is healthy
  if (loading || dismissed || !wsHealth || wsHealth.healthy) {
    return null
  }

  // Determine banner severity based on status
  const isDown = !wsHealth.connected || wsHealth.symbols_cached === 0
  const isWarming = wsHealth.connected && wsHealth.symbols_cached > 0 && wsHealth.symbols_cached < 100

  return (
    <div
      className={`w-full ${
        isDown
          ? 'bg-red-600 text-white'
          : isWarming
          ? 'bg-yellow-600 text-white'
          : 'bg-orange-600 text-white'
      } px-4 py-3 flex items-center justify-between shadow-lg`}
    >
      <div className="flex items-center gap-3">
        <AlertTriangle className="w-5 h-5 flex-shrink-0" />
        <div>
          <p className="font-semibold text-sm">
            {isDown
              ? 'TRADING SUSPENDED: Market Data Unavailable'
              : isWarming
              ? 'Trading Warming Up: Limited Data'
              : 'Trading Warning'}
          </p>
          <p className="text-xs mt-0.5">{wsHealth.message}</p>
          {isDown && (
            <p className="text-xs mt-1 opacity-90">
              The AI cannot make informed trading decisions without real-time data. Trading will resume automatically
              when the connection is restored.
            </p>
          )}
          {isWarming && (
            <p className="text-xs mt-1 opacity-90">
              Cache: {wsHealth.symbols_cached}/221 symbols. Trading will start when sufficient data is available (100+
              symbols).
            </p>
          )}
        </div>
      </div>
      <button
        onClick={() => setDismissed(true)}
        className="flex-shrink-0 p-1 hover:bg-white/20 rounded transition-colors"
        aria-label="Dismiss warning"
      >
        <X className="w-5 h-5" />
      </button>
    </div>
  )
}
