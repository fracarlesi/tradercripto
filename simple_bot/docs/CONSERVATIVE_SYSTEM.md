# HLQuantBot Conservative Trading System

## Overview

The Conservative Trading System is a "boring but scalable" approach to algorithmic trading on Hyperliquid DEX.

**Target Performance:**
- Monthly return: 1-3%
- Trades per month: 5-15
- Maximum drawdown: 15%

## Architecture

```
MarketState → Strategy → LLMVeto → RiskManager → Execution
```

### Services

1. **MarketStateService** - Fetches data for BTC/ETH only (not 200+ coins)
2. **KillSwitchService** - CRITICAL safety system (NON-NEGOTIABLE)
3. **LLMVetoService** - Trade filter (not decision maker)
4. **RiskManagerService** - Risk-based position sizing
5. **ExecutionEngineService** - Order execution on Hyperliquid

### Strategies

1. **TrendFollowStrategy** (Primary)
   - Trades only in TREND regime (ADX > 25)
   - Breakout entry with ATR-based stops
   - Let winners run, cut losers short

2. **MeanReversionStrategy** (Optional, disabled by default)
   - Trades only in RANGE regime (ADX < 20)
   - Bollinger Band + RSI entry
   - Exit at mid-band

## Risk Management

### Per-Trade Risk
- Default: 0.5% of equity per trade
- Maximum: 1.0% per trade
- Formula: `position_size = risk_amount / stop_distance`

### Kill Switch Limits
| Trigger | Limit | Action |
|---------|-------|--------|
| Daily loss | 2% | Pause until tomorrow |
| Weekly loss | 5% | Pause for 3 days |
| Max drawdown | 15% | STOP ALL (manual intervention) |

### Position Limits
- Max concurrent positions: 2
- Max exposure: 100% of equity
- Default leverage: 1x

## Regime Detection

| Regime | ADX | Behavior |
|--------|-----|----------|
| TREND | > 25 | TrendFollow can trade |
| RANGE | < 20 | MeanReversion can trade |
| CHAOS | 20-25 | NO TRADING |

## LLM Veto

The LLM acts as a **filter only**, not a decision maker:
- Receives setups that passed all rules
- Can ALLOW or DENY (with confidence score)
- Rate limited to 6 calls/day
- Fallback: ALLOW (rules already filtered)
- CHAOS regime: automatic DENY

## Configuration

Edit `simple_bot/config/trading.yaml`:

```yaml
# Assets
universe:
  assets:
    - symbol: "BTC"
      enabled: true
    - symbol: "ETH"
      enabled: true

# Risk (CRITICAL)
risk:
  per_trade_pct: 0.5
  max_positions: 2

# Kill Switch (NON-NEGOTIABLE)
kill_switch:
  daily_loss_pct: 2.0
  weekly_loss_pct: 5.0
  max_drawdown_pct: 15.0

# LLM
llm:
  enabled: true
  max_calls_per_day: 6
```

## Running the Bot

```bash
# Start the conservative bot
python -m simple_bot.main_conservative

# With custom config
python -m simple_bot.main_conservative -c path/to/config.yaml

# Verbose logging
python -m simple_bot.main_conservative -v
```

## Dashboard

The frontend dashboard shows:
- `/market-state` - Current regime and indicators
- `/risk-monitor` - Kill switch status and drawdowns
- `/trade-history` - Closed trades with R-multiples
- `/llm-decisions` - LLM veto log and accuracy

## Database

New tables created by migration `005_conservative_refactor.sql`:
- `market_states` - OHLCV + indicators history
- `equity_curve` - Equity snapshots
- `kill_switch_log` - Kill switch events
- `llm_decisions` - LLM decision tracking
- `trade_setups` - Setup log
- `trade_intents` - Sized trade log

## Key Principles

1. **Trade only liquid assets** (BTC, ETH)
2. **One strategy at a time** (trend follow primary)
3. **Risk-based sizing** (not fixed percentage)
4. **Kill switch ALWAYS active**
5. **LLM as filter, not decision maker**
6. **No live parameter optimization** (offline learning only)
7. **Conservative position limits**

## Author

Francesco Carlesi
