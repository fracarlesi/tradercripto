# Trading Decision Flow - Documentazione Tecnica Completa

Questo documento descrive step-by-step il processo completo di decisione di trading, dalla raccolta dati all'esecuzione dell'ordine su Hyperliquid.

**Ultimo aggiornamento**: 2025-11-21

> **NOTA IMPORTANTE**: Questo documento descrive il FLUSSO LOGICO del sistema.
> Il codice mostrato è spesso PSEUDO-CODICE semplificato per chiarezza.
> Per il codice esatto, fare riferimento ai file sorgente indicati.

---

## Indice

1. [Overview Architetturale](#1-overview-architetturale)
2. [Trigger e Scheduling](#2-trigger-e-scheduling)
3. [Step 1: Health Check WebSocket](#step-1-health-check-websocket)
4. [Step 2: Fetch Market Prices](#step-2-fetch-market-prices)
5. [Step 3: Build Portfolio Data](#step-3-build-portfolio-data)
6. [Step 4: Technical Analysis](#step-4-technical-analysis)
7. [Step 5: Pivot Points Calculation](#step-5-pivot-points-calculation)
8. [Step 6: LONG Agent Decision](#step-6-long-agent-decision)
9. [Step 7: SHORT Agent Decision](#step-7-short-agent-decision)
10. [Step 8: Orchestrator Resolution](#step-8-orchestrator-resolution)
11. [Step 9: Validation](#step-9-validation)
12. [Step 10: Order Execution](#step-10-order-execution)
13. [Step 11: Post-Trade Sync](#step-11-post-trade-sync)
14. [Appendice: Struttura Dati Completa](#appendice-struttura-dati-completa)

---

## 1. Overview Architetturale

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         APScheduler (main.py)                               │
│                    Job: ai_crypto_trade (ogni 3 minuti)                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    place_multi_agent_order()                                │
│                    File: backend/services/auto_trader.py:528                │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌───────────────┐         ┌─────────────────┐         ┌───────────────┐
│ WebSocket     │         │ Hyperliquid API │         │ Database      │
│ Cache (local) │         │ (real-time)     │         │ (PostgreSQL)  │
├───────────────┤         ├─────────────────┤         ├───────────────┤
│ - Candles 1h  │         │ - User State    │         │ - Account     │
│ - allMids     │         │ - Positions     │         │ - Positions   │
│ (0 API calls) │         │ - Balance       │         │ - Orders      │
└───────────────┘         └─────────────────┘         └───────────────┘
        │                           │                           │
        └───────────────────────────┼───────────────────────────┘
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Technical Analysis                                   │
│         File: backend/services/technical_analysis_service.py                │
│         Input: 221 symbols × 70 candles = ~15,000 data points              │
│         Output: Momentum + Support scores (0-1) per symbol                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
        ┌───────────────────────┐       ┌───────────────────────┐
        │     LONG Agent        │       │     SHORT Agent       │
        │  (DeepSeek API call)  │       │  (DeepSeek API call)  │
        │   ~30-60 sec          │       │   ~30-60 sec          │
        └───────────────────────┘       └───────────────────────┘
                    │                               │
                    └───────────────┬───────────────┘
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Orchestrator Service                                 │
│             File: backend/services/orchestrator_service.py                  │
│             Logic: Seleziona BEST proposal by technical_score               │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Order Execution                                      │
│         File: backend/services/trading/hyperliquid_trading_service.py       │
│         Method: place_market_order_async()                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Trigger e Scheduling

### Job Configuration

| Job ID | Intervallo | File | Funzione | Start Delay |
|--------|------------|------|----------|-------------|
| `ai_crypto_trade` | 180s (3 min) | `main.py:169` | `place_multi_agent_order()` | 0s |
| `stop_loss_check` | 300s (5 min) | `main.py:159` | `check_stop_loss_async()` | 0s |
| `take_profit_check` | 300s (5 min) | `main.py:179` | `check_take_profit_async()` | 150s |
| `hyperliquid_sync` | 30s | `main.py:151` | `periodic_sync_job()` | 45s |

### Codice di Scheduling (main.py:169-175)

```python
# Add MULTI-AGENT ORCHESTRATED trading job (every 3 minutes)
scheduler_service.add_sync_job(
    job_func=lambda: place_multi_agent_order(max_ratio=0.2),
    interval_seconds=180,  # 3 minutes - Fast momentum surfing
    job_id="ai_crypto_trade"
)
```

---

## Step 1: Health Check WebSocket

### File e Funzione
- **File**: `backend/services/auto_trader.py:544-558`
- **Funzione**: Inline check in `place_multi_agent_order()`
- **Dipendenza**: `backend/services/market_data/websocket_candle_service.py`

### Quando viene eseguito
All'inizio di ogni ciclo di trading (ogni 3 minuti)

### Codice

```python
# CRITICAL SAFETY CHECK: Verify WebSocket health before trading
from services.market_data.websocket_candle_service import get_websocket_candle_service

ws_service = get_websocket_candle_service()
cache_stats = ws_service.get_cache_stats()

if not cache_stats["connected"]:
    logger.error("🚫 TRADING SUSPENDED: WebSocket not connected")
    return

if cache_stats["symbols_cached"] < 100:
    logger.warning(f"🚫 TRADING SUSPENDED: Insufficient cache ({cache_stats['symbols_cached']}/221 symbols)")
    return
```

### Esempio Output `cache_stats`

```json
{
  "connected": true,
  "symbols_cached": 221,
  "total_candles": 15470,
  "oldest_candle_age_hours": 69.5,
  "newest_candle_age_seconds": 45,
  "price_cache_size": 221,
  "price_cache_age_seconds": 1.2
}
```

### Blocking Conditions
- `connected == false` → Trading sospeso
- `symbols_cached < 100` → Trading sospeso

---

## Step 2: Fetch Market Prices

### File e Funzione
- **File**: `backend/services/auto_trader.py:759-785`
- **Funzione**: `_fetch_market_prices()`
- **Dipendenza**: `backend/services/market_data/hyperliquid_market_data.py`

### Quando viene eseguito
Dopo health check, prima di costruire portfolio

### Codice

```python
def _fetch_market_prices() -> dict[str, float]:
    """Fetch current market prices for ALL crypto symbols in ONE API call."""
    try:
        from services.market_data.hyperliquid_market_data import get_all_prices_from_hyperliquid

        # Get ALL prices in ONE efficient API call using all_mids() endpoint
        prices = get_all_prices_from_hyperliquid()

        logger.info(f"Fetched {len(prices)} prices from Hyperliquid in ONE API call")

        return prices
    except Exception as e:
        logger.error(f"Failed to fetch market prices: {e}", exc_info=True)
        return {"BTC": 100000.0, "ETH": 4000.0, "SOL": 200.0}  # Fallback
```

### Esempio Output `prices`

```json
{
  "BTC": 96847.50,
  "ETH": 3642.25,
  "SOL": 241.18,
  "DOGE": 0.3845,
  "XRP": 1.4523,
  "AVAX": 42.67,
  "ARB": 1.0234,
  "OP": 2.1567,
  "LINK": 18.92,
  "MATIC": 0.5234,
  "... (221 total symbols)": "..."
}
```

### API Call
- **Endpoint**: Hyperliquid `all_mids()` via SDK
- **API Weight**: 1 call (molto efficiente)
- **Latency**: ~100-200ms

---

## Step 3: Build Portfolio Data

### File e Funzione
- **File**: `backend/services/auto_trader.py:787-850`
- **Funzione**: `_build_portfolio_data(db, account)`
- **Dipendenza**: `backend/services/trading/hyperliquid_trading_service.py`

### Quando viene eseguito
Dopo fetch prices

### Codice

```python
def _build_portfolio_data(db, account) -> dict:
    """Build portfolio data from Hyperliquid (source of truth)."""

    # Get real balance from Hyperliquid API
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        user_state = loop.run_until_complete(
            hyperliquid_trading_service.get_user_state_async()
        )
    finally:
        loop.close()

    margin = user_state['marginSummary']
    account_value = float(margin.get('accountValue'))
    total_margin_used = float(margin.get('totalMarginUsed'))
    withdrawable = float(margin.get('withdrawable'))

    # Get positions from Hyperliquid
    hl_positions = user_state.get('assetPositions', [])

    positions = []
    for pos in hl_positions:
        pos_data = pos.get('position', {})
        positions.append({
            "symbol": pos_data.get('coin'),
            "quantity": float(pos_data.get('szi', 0)),
            "avg_cost": float(pos_data.get('entryPx', 0)),
            "side": "LONG" if float(pos_data.get('szi', 0)) > 0 else "SHORT",
            "unrealized_pnl": float(pos_data.get('unrealizedPnl', 0)),
            "market_value": float(pos_data.get('positionValue', 0))
        })

    return {
        "cash": withdrawable,
        "total_assets": account_value,
        "margin_used": total_margin_used,
        "positions": positions,
        "strategy_weights": account.strategy_weights or DEFAULT_WEIGHTS
    }
```

### Esempio Output `portfolio`

```json
{
  "cash": 45.23,
  "total_assets": 187.56,
  "margin_used": 142.33,
  "positions": [
    {
      "symbol": "HYPE",
      "quantity": 3.5,
      "avg_cost": 28.45,
      "side": "LONG",
      "unrealized_pnl": 12.34,
      "market_value": 99.58,
      "profit_pct": 5.2
    },
    {
      "symbol": "SOL",
      "quantity": -0.15,
      "avg_cost": 245.00,
      "side": "SHORT",
      "unrealized_pnl": -3.21,
      "market_value": 36.18,
      "profit_pct": -2.1
    }
  ],
  "strategy_weights": {
    "pivot_points": 0.8,
    "prophet": 0.5,
    "technical_analysis": 0.7,
    "whale_alerts": 0.4,
    "sentiment": 0.3,
    "news": 0.2
  }
}
```

### Source of Truth
- **Balance**: Hyperliquid API (`marginSummary`)
- **Positions**: Hyperliquid API (`assetPositions`)
- **Strategy Weights**: Database (`Account.strategy_weights`)

---

## Step 4: Technical Analysis

### File e Funzione
- **File**: `backend/services/technical_analysis_service.py:148-221`
- **Funzione**: `calculate_technical_factors(symbols)`
- **Dipendenza**: `backend/services/market_data/websocket_candle_service.py`

### Quando viene eseguito
Dopo build portfolio, prima di chiamare gli agenti AI

### Codice

```python
def calculate_technical_factors(symbols: list[str]) -> dict[str, Any]:
    """Calculate momentum and support factors for given symbols."""

    # Fetch historical data from WebSocket cache (70 candles for RSI/MACD)
    history = fetch_historical_data(symbols, period="1h", count=70)

    # Calculate momentum factor (RSI, MACD, price change)
    momentum_df = compute_momentum(history)

    # Calculate support factor (volume, price stability)
    support_df = compute_support(history)

    # Combine results
    result = {
        "momentum": {},
        "support": {},
        "recommendations": []
    }

    # Generate combined recommendations (average of momentum + support)
    for symbol in symbols:
        if symbol in result["momentum"] and symbol in result["support"]:
            combined_score = (
                result["momentum"][symbol]["score"] * 0.6 +
                result["support"][symbol]["score"] * 0.4
            )
            recommendations.append({
                "symbol": symbol,
                "score": combined_score,
                "momentum": result["momentum"][symbol]["score"],
                "support": result["support"][symbol]["score"]
            })

    # Sort by score descending
    recommendations.sort(key=lambda x: x["score"], reverse=True)

    return result
```

### Esempio Output `technical_factors`

```json
{
  "momentum": {
    "HYPE": {"score": 0.92, "raw": 2.34, "rank": 1},
    "VIRTUAL": {"score": 0.89, "raw": 2.12, "rank": 2},
    "AI16Z": {"score": 0.87, "raw": 1.98, "rank": 3},
    "SOL": {"score": 0.75, "raw": 1.23, "rank": 15},
    "BTC": {"score": 0.68, "raw": 0.89, "rank": 28}
  },
  "support": {
    "HYPE": {"score": 0.85, "raw": 0.72, "rank": 2},
    "VIRTUAL": {"score": 0.78, "raw": 0.65, "rank": 5},
    "AI16Z": {"score": 0.82, "raw": 0.68, "rank": 3},
    "SOL": {"score": 0.88, "raw": 0.76, "rank": 1},
    "BTC": {"score": 0.90, "raw": 0.82, "rank": 1}
  },
  "recommendations": [
    {"symbol": "HYPE", "score": 0.892, "momentum": 0.92, "support": 0.85},
    {"symbol": "VIRTUAL", "score": 0.846, "momentum": 0.89, "support": 0.78},
    {"symbol": "AI16Z", "score": 0.858, "momentum": 0.87, "support": 0.82},
    {"symbol": "SOL", "score": 0.802, "momentum": 0.75, "support": 0.88},
    {"symbol": "BTC", "score": 0.768, "momentum": 0.68, "support": 0.90}
  ]
}
```

### Score Interpretation
- `score >= 0.85` → **STRONG BUY** signal
- `score >= 0.70` → **BUY** signal
- `score >= 0.50` → **HOLD**
- `score < 0.50` → **SELL/AVOID**
- `score < 0.30` → **SHORT** opportunity

### Data Source
- **70 candles 1h** dal WebSocket cache (local, 0 API calls)
- ~15,000 data points processati in ~0.5 secondi

---

## Step 5: Pivot Points Calculation

### File e Funzione
- **File**: `backend/services/market_data/pivot_calculator.py:57-150`
- **Funzione**: `calculate_pivot_points(symbol, current_price)`
- **Dipendenza**: WebSocket cache per candles giornaliere

### Quando viene eseguito
Solo per i top symbols (score > 0.7), durante la costruzione del prompt AI

### Codice

```python
async def calculate_pivot_points(self, symbol: str, current_price: float) -> Dict:
    """Calcola pivot points per un simbolo."""

    # Fetch previous day candle
    prev_candle = await self._fetch_previous_candle(symbol, "1d")

    high = prev_candle["high"]
    low = prev_candle["low"]
    close = prev_candle["close"]

    # Standard Pivot Point calculation
    PP = (high + low + close) / 3

    # Resistance levels
    R1 = (2 * PP) - low
    R2 = PP + (high - low)
    R3 = high + 2 * (PP - low)

    # Support levels
    S1 = (2 * PP) - high
    S2 = PP - (high - low)
    S3 = low - 2 * (high - PP)

    # Calculate distances
    distances = {
        "to_pp": current_price - PP,
        "to_r1": current_price - R1,
        "to_s1": current_price - S1,
    }

    # Determine signal
    if current_price < S1:
        signal = "long_opportunity"  # Oversold, bounce expected
    elif current_price > R1:
        signal = "short_opportunity"  # Overbought, pullback expected
    elif current_price > PP:
        signal = "bullish_zone"
    elif current_price < PP:
        signal = "bearish_zone"
    else:
        signal = "neutral"

    return {
        "symbol": symbol,
        "PP": PP, "R1": R1, "R2": R2, "R3": R3,
        "S1": S1, "S2": S2, "S3": S3,
        "current_price": current_price,
        "distances": distances,
        "signal": signal
    }
```

### Esempio Output `pivot_points`

```json
{
  "symbol": "HYPE",
  "current_price": 29.45,
  "PP": 28.67,
  "R1": 30.12,
  "R2": 31.58,
  "R3": 33.03,
  "S1": 27.21,
  "S2": 25.76,
  "S3": 24.30,
  "distances": {
    "to_pp": 0.78,
    "to_r1": -0.67,
    "to_s1": 2.24
  },
  "distances_pct": {
    "to_pp": 2.72,
    "to_r1": -2.27,
    "to_s1": 8.23,
    "to_r2": -6.74,
    "to_s2": 14.33
  },
  "signal": "bullish_zone",
  "interpretation": "Price above PP, trending bullish. R1 nearby - watch for breakout or rejection."
}
```

### Signal Types
| Signal | Meaning | Trading Action |
|--------|---------|----------------|
| `long_opportunity` | Price below S1 | BUY (oversold bounce) |
| `short_opportunity` | Price above R1 | SHORT (overbought pullback) |
| `bullish_zone` | Price between PP and R1 | Consider LONG |
| `bearish_zone` | Price between S1 and PP | Consider SHORT |
| `neutral` | Price near PP | HOLD |

---

## Step 6: LONG Agent Decision

### File e Funzione
- **File**: `backend/services/ai_decision_service.py:1313-1520`
- **Funzione**: `call_ai_for_agent_decision(account, portfolio, prices, "LONG")`
- **Dipendenza**: DeepSeek API

### Quando viene eseguito
Dopo technical analysis, in parallelo con SHORT agent

### Prompt Structure (ai_decision_service.py:1397-1440)

```python
prompt = f"""You are a LONG specialist crypto trading AI.

=== PORTFOLIO STATE ===
Total Assets: ${portfolio.get("total_assets", 0):.2f}
Available Cash: ${portfolio.get("cash", 0):.2f}
Current Positions:
{positions_toon}

{recent_trades_prompt}

=== TOP OPPORTUNITIES (By Technical Score) ===
Only symbols with score > 0.7 (LONG) or < 0.3 (SHORT) are shown:

{technical_section}

=== KEY INDICATOR FOCUS (3 ONLY) ===
1. **Technical Score** (0-1): >0.9 = STRONG BUY, >0.7 = moderate BUY
2. **Momentum** (0-1): >0.8 = strong uptrend
3. **Pivot Position**: Near S1 = BUY bounce, Near R1 = SELL/SHORT

{pivot_section}

=== DECISION RULES (MANDATORY) ===
1. DO NOT trade symbols you traded in last 30 minutes
2. DO NOT reverse a position (LONG->SHORT) in less than 60 minutes
3. If score < 0.7 for LONG, choose HOLD
4. Max 1 trade per cycle - choose the BEST opportunity only

=== LONG AGENT SPECIALIZATION ===
You ONLY recommend BUY (long) positions.
Look for: Momentum > 0.8, Price near S1 (support bounce), Score > 0.75

Respond with ONLY JSON:
{{
  "operation": "buy" or "hold",
  "symbol": "SYMBOL",
  "target_portion_of_balance": 0.5,
  "leverage": 1,
  "reason": "Brief 1-sentence explanation"
}}"""
```

### Esempio Input al LONG Agent

```
=== PORTFOLIO STATE ===
Total Assets: $187.56
Available Cash: $45.23
Current Positions:
positions[2]:
  [0] symbol="HYPE" quantity=3.5 avg_cost=28.45 profit_pct=5.2%
  [1] symbol="SOL" quantity=-0.15 avg_cost=245.0 profit_pct=-2.1%

=== RECENT TRADES (Last 24h) ===
- 2h ago: SOLD VIRTUAL at $2.34 (+3.2% profit)
- 5h ago: BOUGHT HYPE at $28.45 (still open)

=== TOP OPPORTUNITIES (By Technical Score) ===
Rank | Symbol  | Score | Momentum | Support | Signal
-----|---------|-------|----------|---------|--------
1    | HYPE    | 0.892 | 0.92     | 0.85    | STRONG BUY
2    | VIRTUAL | 0.846 | 0.89     | 0.78    | BUY
3    | AI16Z   | 0.858 | 0.87     | 0.82    | BUY
4    | FARTCOIN| 0.812 | 0.83     | 0.78    | BUY

=== PIVOT POINTS (Top 3 Symbols) ===
HYPE: Price $29.45 | PP=$28.67 | R1=$30.12 | S1=$27.21 | Signal: bullish_zone
VIRTUAL: Price $2.45 | PP=$2.38 | R1=$2.52 | S1=$2.24 | Signal: bullish_zone
AI16Z: Price $0.89 | PP=$0.85 | R1=$0.93 | S1=$0.77 | Signal: bullish_zone
```

### Esempio Output LONG Agent

```json
{
  "operation": "buy",
  "symbol": "VIRTUAL",
  "target_portion_of_balance": 0.5,
  "leverage": 2,
  "reason": "VIRTUAL has strong momentum (0.89) and score (0.846), price in bullish zone near PP with R1 as target. Not in recent trades."
}
```

### API Call Details
- **Endpoint**: `{account.base_url}/chat/completions`
- **Model**: `account.model` (es. "deepseek-chat")
- **Temperature**: 0.7
- **Timeout**: 300s (5 min)
- **Tokens**: ~2000-4000 input, ~100-200 output

---

## Step 7: SHORT Agent Decision

### File e Funzione
- **File**: `backend/services/ai_decision_service.py:1313-1520`
- **Funzione**: `call_ai_for_agent_decision(account, portfolio, prices, "SHORT")`
- **Dipendenza**: DeepSeek API

### Quando viene eseguito
In parallelo con LONG agent (dopo technical analysis)

### SHORT Agent Specialization Prompt

```python
=== SHORT AGENT SPECIALIZATION ===
You ONLY recommend SHORT positions.
Look for: Momentum < 0.3, Price near R1 (resistance rejection), Score < 0.35

Respond with ONLY JSON:
{{
  "operation": "short" or "hold",
  "symbol": "SYMBOL",
  "target_portion_of_balance": 0.5,
  "leverage": 1,
  "reason": "Brief 1-sentence explanation"
}}
```

### Esempio Output SHORT Agent

```json
{
  "operation": "hold",
  "symbol": "",
  "target_portion_of_balance": 0,
  "leverage": 1,
  "reason": "No clear SHORT opportunities. All top symbols have momentum > 0.7 indicating uptrend. Waiting for reversal signals."
}
```

---

## Step 8: Orchestrator Resolution

### File e Funzione
- **File**: `backend/services/orchestrator_service.py:59-166`
- **Funzione**: `resolve_proposals(long_proposal, short_proposal, current_positions)`
- **Classe**: `OrchestratorService`

### Quando viene eseguito
Dopo che ENTRAMBI gli agenti hanno risposto

### Codice

```python
def resolve_proposals(
    self,
    long_proposal: Optional[AgentProposal],
    short_proposal: Optional[AgentProposal],
    current_positions: List[Dict[str, Any]]
) -> List[OrchestratorDecision]:
    """
    Resolve proposals from both agents and return the SINGLE best decision.

    Selection Strategy (momentum surfing optimization):
    - Only execute ONE trade per cycle (highest technical score wins)
    - Avoids conflicting market bets that hedge momentum strategy

    Blocking Rules:
    - Cannot open opposite position on same asset
    """
    candidates = []

    # Build current positions map
    positions_map = {}
    for pos in current_positions:
        symbol = pos.get("coin") or pos.get("symbol")
        size = float(pos.get("szi", 0))
        if size != 0:
            positions_map[symbol] = "LONG" if size > 0 else "SHORT"

    # Process LONG proposal
    if long_proposal and long_proposal.operation != "hold":
        # Check for existing opposite position
        if long_proposal.symbol in positions_map:
            existing = positions_map[long_proposal.symbol]
            if existing == "SHORT" and long_proposal.operation == "buy":
                logger.warning(f"LONG agent blocked: Cannot BUY {long_proposal.symbol} while SHORT exists")
            else:
                candidates.append(("LONG", long_proposal))
        else:
            candidates.append(("LONG", long_proposal))

    # Process SHORT proposal (similar logic)
    # ...

    # Select BEST candidate by technical score
    if len(candidates) == 1:
        winner_type, winner = candidates[0]
    else:
        # Compare by technical score
        if long_prop.technical_score >= short_prop.technical_score:
            winner_type, winner = "LONG", long_prop
        else:
            winner_type, winner = "SHORT", short_prop

    # Create single decision
    decision = OrchestratorDecision(
        execute=True,
        agent_type=winner_type,
        operation=winner.operation,
        symbol=winner.symbol,
        target_portion=winner.target_portion,
        leverage=winner.leverage,
        reasoning=winner.reasoning,
    )

    return [decision]
```

### Esempio Input Orchestrator

```python
long_proposal = AgentProposal(
    agent_type="LONG",
    operation="buy",
    symbol="VIRTUAL",
    confidence=0.5,
    target_portion=0.5,
    leverage=2,
    reasoning="Strong momentum...",
    technical_score=0.846  # From recommendations
)

short_proposal = None  # SHORT agent returned "hold"

current_positions = [
    {"coin": "HYPE", "szi": 3.5},   # LONG position
    {"coin": "SOL", "szi": -0.15}   # SHORT position
]
```

### Esempio Output Orchestrator

```python
[OrchestratorDecision(
    execute=True,
    agent_type="LONG",
    operation="buy",
    symbol="VIRTUAL",
    target_portion=0.5,
    leverage=2,
    reasoning="Strong momentum...",
    conflict_resolution=None  # No conflict (only 1 proposal)
)]
```

### Blocking Rules
1. **Opposite Position**: Cannot BUY if SHORT exists on same symbol (and vice versa)
2. **Technical Score**: When both agents propose, higher score wins
3. **Single Trade**: Only 1 trade per cycle (never 2)

---

## Step 9: Validation

### File e Funzione
- **File**: `backend/services/auto_trader.py:400-480`
- **Funzione**: `_validate_decision(decision, portfolio, prices, max_ratio)`

### Quando viene eseguito
Prima dell'esecuzione, dopo orchestrator resolution

### Validations Performed

```python
def _validate_decision(decision, portfolio, prices, max_ratio):
    """Validate AI decision before execution."""

    operation = decision.get("operation", "").lower()
    symbol = decision.get("symbol", "")
    target_portion = decision.get("target_portion_of_balance", 0)
    leverage = decision.get("leverage", 1)

    # 1. Operation validation
    if operation not in ["buy", "sell", "short", "hold"]:
        return {"valid": False, "reason": f"Invalid operation: {operation}"}

    # 2. Symbol validation
    if not symbol or symbol not in prices:
        return {"valid": False, "reason": f"Invalid symbol: {symbol}"}

    # 3. Portion validation
    if target_portion <= 0 or target_portion > 1:
        return {"valid": False, "reason": f"Invalid portion: {target_portion}"}

    # 4. Max ratio check
    if target_portion > max_ratio:
        target_portion = max_ratio  # Cap at max_ratio

    # 5. Minimum order size ($10 Hyperliquid requirement)
    price = prices[symbol]
    available_cash = portfolio["cash"]
    order_value = available_cash * target_portion

    MIN_ORDER_SIZE = 10.0
    if order_value < MIN_ORDER_SIZE:
        if available_cash >= MIN_ORDER_SIZE:
            order_value = MIN_ORDER_SIZE
        else:
            return {"valid": False, "reason": f"Insufficient cash for min order ${MIN_ORDER_SIZE}"}

    # 6. Calculate order size (quantity)
    order_size = order_value / price

    # 7. Leverage validation (1-10x)
    if leverage < 1 or leverage > 10:
        leverage = max(1, min(10, leverage))

    return {
        "valid": True,
        "order_size": order_size,
        "order_value": order_value,
        "leverage": leverage
    }
```

### Validation Checks Summary

| Check | Condition | Action |
|-------|-----------|--------|
| Operation | Must be buy/sell/short/hold | Reject if invalid |
| Symbol | Must exist in prices dict | Reject if not found |
| Portion | 0 < portion <= 1 | Reject if out of range |
| Max Ratio | portion > max_ratio (0.2) | Cap at max_ratio |
| Min Order | order_value >= $10 | Adjust or reject |
| Leverage | 1 <= leverage <= 10 | Clamp to range |

---

## Step 10: Order Execution

### File e Funzione
- **File**: `backend/services/trading/hyperliquid_trading_service.py:236-301`
- **Funzione**: `place_market_order_async(symbol, is_buy, size, reduce_only, leverage)`
- **Dipendenza**: `hyperliquid-python-sdk`

### Quando viene eseguito
Dopo validation success

### Codice

```python
async def place_market_order_async(
    self, symbol: str, is_buy: bool, size: float,
    reduce_only: bool = False, leverage: int = 1
) -> dict[str, Any]:
    """Place a market order on Hyperliquid (async)."""

    # Update leverage BEFORE opening position
    if not reduce_only and leverage > 1:
        await self.update_leverage_async(symbol=symbol, leverage=leverage, is_cross=True)

    def _place_order():
        """Synchronous order placement via SDK."""
        order_result = self._exchange.market_open(
            name=symbol, is_buy=is_buy, sz=size
        )
        return order_result

    logger.info(
        f"Placing {'BUY' if is_buy else 'SELL'} market order: {size} {symbol} (leverage: {leverage}x)"
    )

    result = await run_in_thread(_place_order)

    return result
```

### Esempio Execution Flow

```python
# Input from validated decision
symbol = "VIRTUAL"
is_buy = True  # operation == "buy"
size = 10.23   # order_size in base asset
leverage = 2

# Step 1: Update leverage
await hyperliquid_trading_service.update_leverage_async(
    symbol="VIRTUAL", leverage=2, is_cross=True
)
# Output: {"status": "ok"}

# Step 2: Place order
result = await hyperliquid_trading_service.place_market_order_async(
    symbol="VIRTUAL",
    is_buy=True,
    size=10.23,
    reduce_only=False,
    leverage=2
)
```

### Esempio Output `execution_result`

```json
{
  "status": "ok",
  "response": {
    "type": "order",
    "data": {
      "statuses": [
        {
          "resting": {
            "oid": 12345678
          }
        }
      ]
    }
  }
}
```

### Error Response Example

```json
{
  "status": "ok",
  "response": {
    "type": "order",
    "data": {
      "statuses": [
        {
          "error": "Insufficient margin"
        }
      ]
    }
  }
}
```

### Execution Success Check

```python
is_executed = False
if execution_result.get("status") == "ok":
    response = execution_result.get("response", {})
    if response.get("type") == "order":
        statuses = response.get("data", {}).get("statuses", [])
        has_errors = any(s.get("error") for s in statuses)
        if not has_errors:
            is_executed = True
```

---

## Step 11: Post-Trade Sync

### File e Funzioni
- **File**: `backend/services/trading/hyperliquid_sync_service.py:436-505`
- **Funzione**: `sync_account(account_id)`
- **Job**: `hyperliquid_sync` (ogni 30 secondi)

### Quando viene eseguito
- Automaticamente ogni 30 secondi (scheduled job)
- Manualmente dopo ogni trade execution

### Sync Operations

```python
async def sync_account(self, account_id: int) -> Dict:
    """Full account sync from Hyperliquid."""

    async with get_async_session_factory()() as db:
        # 1. Sync positions (clear-recreate strategy)
        positions_synced = await self.sync_positions(db=db, account=account)

        # 2. Get fills from Hyperliquid
        fills = await hyperliquid_trading_service.get_user_fills_async(limit=100)

        # 3. Sync orders from fills
        orders_synced = await self.sync_orders_from_fills(db=db, account=account, fills=fills)

        # 4. Sync trades from fills
        trades_synced = await self.sync_trades_from_fills(db=db, account=account, fills=fills)

        await db.commit()

        return {
            "success": True,
            "positions_synced": positions_synced,
            "orders_synced": orders_synced,
            "trades_synced": trades_synced
        }
```

### Position Sync (Clear-Recreate)

```python
async def sync_positions(self, db: AsyncSession, account: Account) -> int:
    """Sync positions using clear-recreate strategy."""

    # Get positions from Hyperliquid
    user_state = await hyperliquid_trading_service.get_user_state_async()
    hyperliquid_positions = user_state.get("assetPositions", [])

    # Clear all existing positions
    await db.execute(
        text("DELETE FROM positions WHERE account_id = :account_id"),
        {"account_id": account.id}
    )

    # Create fresh positions from Hyperliquid
    positions_to_create = []
    for hl_pos in hyperliquid_positions:
        pos_data = hl_pos.get("position", {})
        symbol = pos_data.get("coin")
        size = Decimal(str(pos_data.get("szi", "0")))

        if size != 0 and symbol:
            position = Position(
                account_id=account.id,
                symbol=symbol,
                quantity=abs(size),
                average_cost=Decimal(str(pos_data.get("entryPx", "0"))),
                leverage=Decimal(str(pos_data.get("leverage", {}).get("value", 1))),
            )
            positions_to_create.append(position)

    # Bulk insert
    if positions_to_create:
        db.add_all(positions_to_create)

    return len(positions_to_create)
```

---

## Appendice: Struttura Dati Completa

### A1. MarketDataSnapshot (JSON completo passato a DeepSeek)

```json
{
  "metadata": {
    "timestamp": "2025-11-21T18:30:00Z",
    "symbols_analyzed": 221,
    "data_sources": ["websocket_cache", "hyperliquid_api"]
  },
  "portfolio": {
    "total_assets": 187.56,
    "available_cash": 45.23,
    "positions_value": 142.33,
    "unrealized_pnl": 9.13,
    "positions": [
      {
        "symbol": "HYPE",
        "side": "LONG",
        "quantity": 3.5,
        "entry_price": 28.45,
        "current_price": 29.45,
        "unrealized_pnl": 3.50,
        "unrealized_pnl_pct": 3.51,
        "market_value": 103.08
      }
    ],
    "strategy_weights": {
      "pivot_points": 0.8,
      "prophet": 0.5,
      "technical_analysis": 0.7,
      "whale_alerts": 0.4,
      "sentiment": 0.3,
      "news": 0.2
    }
  },
  "symbols": {
    "HYPE": {
      "price": 29.45,
      "technical_analysis": {
        "score": 0.892,
        "momentum": 0.92,
        "support": 0.85,
        "signal": "STRONG_BUY",
        "rank": 1
      },
      "pivot_points": {
        "PP": 28.67,
        "R1": 30.12,
        "R2": 31.58,
        "S1": 27.21,
        "S2": 25.76,
        "current_zone": "bullish",
        "signal": "bullish_zone",
        "distance_to_support_pct": 7.61,
        "distance_to_resistance_pct": 2.27
      }
    }
  },
  "global_indicators": {
    "sentiment": {
      "value": 72,
      "label": "Greed",
      "signal": "BULLISH"
    },
    "whale_alerts": [],
    "news": []
  }
}
```

### A2. AgentProposal Structure

```python
@dataclass
class AgentProposal:
    agent_type: str       # "LONG" or "SHORT"
    operation: str        # "buy", "short", "sell", "hold"
    symbol: str           # "BTC", "ETH", etc.
    confidence: float     # 0.0-1.0
    target_portion: float # 0.0-1.0 (portion of available cash)
    leverage: int         # 1-10
    reasoning: str        # AI explanation
    technical_score: float # 0.0-1.0 (from recommendations)
```

### A3. OrchestratorDecision Structure

```python
@dataclass
class OrchestratorDecision:
    execute: bool              # True if should execute
    agent_type: str            # "LONG" or "SHORT"
    operation: str             # "buy", "short", "sell"
    symbol: str                # Target symbol
    target_portion: float      # Final portion after orchestration
    leverage: int              # Final leverage
    reasoning: str             # Combined reasoning
    conflict_resolution: str   # How conflict was resolved (if any)
```

### A4. DeepSeek Response Format

```json
{
  "operation": "buy",
  "symbol": "VIRTUAL",
  "target_portion_of_balance": 0.5,
  "leverage": 2,
  "reason": "VIRTUAL has strong momentum (0.89) with score 0.846. Price in bullish zone above PP ($2.38), targeting R1 ($2.52) for +5% gain. Good R:R ratio with S1 ($2.24) as stop loss."
}
```

---

## Timeline di un Ciclo Completo

| Tempo | Step | Durata | File |
|-------|------|--------|------|
| T+0s | Health Check | ~10ms | auto_trader.py:544 |
| T+0.01s | Fetch Prices | ~150ms | auto_trader.py:759 |
| T+0.2s | Build Portfolio | ~200ms | auto_trader.py:787 |
| T+0.4s | Technical Analysis | ~500ms | technical_analysis_service.py:148 |
| T+1s | Pivot Points | ~300ms | pivot_calculator.py:57 |
| T+1.3s | LONG Agent Call | ~30-60s | ai_decision_service.py:1313 |
| T+1.3s | SHORT Agent Call | ~30-60s | ai_decision_service.py:1313 |
| T+60s | Orchestrator | ~10ms | orchestrator_service.py:59 |
| T+60s | Validation | ~5ms | auto_trader.py:400 |
| T+60s | Execution | ~500ms | hyperliquid_trading_service.py:236 |
| T+61s | Save Decision | ~50ms | ai_decision_service.py:save_ai_decision |
| **Total** | | **~60-90s** | |

---

## Riferimenti File Principali

| Componente | File | Linee Chiave |
|------------|------|--------------|
| Entry Point | `backend/services/auto_trader.py` | 528-756 |
| Orchestrator | `backend/services/orchestrator_service.py` | 18-182 |
| AI Decision | `backend/services/ai_decision_service.py` | 1313-1520 |
| Technical Analysis | `backend/services/technical_analysis_service.py` | 148-221 |
| Pivot Calculator | `backend/services/market_data/pivot_calculator.py` | 57-150 |
| WebSocket Service | `backend/services/market_data/websocket_candle_service.py` | 46-547 |
| Hyperliquid Trading | `backend/services/trading/hyperliquid_trading_service.py` | 236-301 |
| Hyperliquid Sync | `backend/services/trading/hyperliquid_sync_service.py` | 187-505 |
| Scheduler | `backend/main.py` | 138-200 |
| DeepSeek Client | `backend/services/ai/deepseek_client.py` | 107-645 |
