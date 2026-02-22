# HLQuantBot Architecture (updated 2026-02-22)

## Overview
Trading bot for Hyperliquid DEX. Scans ALL assets (mode: "all"), not just BTC.
Strategy: trend_momentum (EMA9/EMA21 crossover + RSI filter on 15m).

## Architecture: Microservices + Message Bus
Event-driven pub/sub with async services. NO dashboard, NO analytics DB.

## Services (startup order)
1. KillSwitchService - Circuit breaker (8% daily, 15% weekly, 30% max DD)
2. MarketStateService - OHLCV + indicators (EMA, RSI, ATR, ADX) + regime detection
3. LLMVetoService - DeepSeek trade confirmation/veto
4. RiskManagerService - Position sizing + in-memory trade counter (no DB)
5. ExecutionEngineService - Order execution on Hyperliquid + TP/SL management
6. Protections - 4 proactive protections (StoplossGuard, MaxDrawdown, Cooldown, LowPerformance)

## Key Config
- Universe: ALL assets (min_volume_24h: 100k), excludes stablecoins + delisted
- Leverage: 10x, max 3 positions, max 8 trades/day
- TP/SL: 0.8% / 0.4% (fixed), 1:2 R:R
- Notifications: ntfy.sh push (Telegram disabled)

## What was REMOVED (Feb 22, 2026)
- Flask dashboard (~2,000+ lines)
- Analytics DB tables (fills, trades, signals, agent_activity, etc.)
- DB persistence from execution_engine, market_state, kill_switch
- Risk manager now uses in-memory trade counter (not DB queries)
- database/db.py kept minimal: only cooldowns + protections tables

## Entry Point
```bash
cd simple_bot && python3 main.py
```

## Key Files
- simple_bot/main.py - HLQuantBot orchestrator
- simple_bot/strategies/momentum_scalper.py - trend_momentum strategy
- simple_bot/services/execution_engine.py - Orders + TP/SL
- simple_bot/services/risk_manager.py - Sizing + validation
- simple_bot/config/trading.yaml - All configuration
