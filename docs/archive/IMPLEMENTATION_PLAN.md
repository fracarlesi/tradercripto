# HLQuantBot - Implementation Plan
## From Analysis to Production-Ready Trading Bot

**Goal**: Implementare protections, metrics, backtesting e safety nets per rendere HLQuantBot profittevole e robusto.

**Timeline**: 2 settimane (10 giorni lavorativi)

---

## Phase 1: Critical Safety Nets (P0) - Days 1-5

### Task 1.1: Cooldown System dopo Drawdown
**Obiettivo**: Fermare il bot automaticamente dopo loss streaks per evitare disastri

**Files da modificare**:
- `simple_bot/services/risk_manager.py`
- `simple_bot/core/models.py` (aggiungere CooldownState)
- `simple_bot/database/schema.py` (tabella cooldowns)

**Implementation**:
```python
# risk_manager.py
class CooldownState(BaseModel):
    active: bool
    reason: str
    triggered_at: datetime
    cooldown_until: datetime

async def check_cooldown_required(self) -> Tuple[bool, Optional[CooldownState]]:
    """
    Trigger cooldown se:
    1. 3+ stoploss consecutivi in 1h
    2. Daily drawdown > 5%
    3. 5+ losing trades in 24h con win rate < 20%
    """
    # Implementation here
```

**Success Criteria**:
- [ ] Test: 3 stoploss consecutivi → cooldown attivo per 6h
- [ ] Test: Daily DD 6% → cooldown attivo per 12h
- [ ] Dashboard mostra stato cooldown real-time
- [ ] Telegram alert quando cooldown triggered

**Estimated Time**: 4 ore

---

### Task 1.2: Performance Metrics Real-Time
**Obiettivo**: Calcolare Sharpe Ratio, Max Drawdown, Profit Factor in tempo reale

**Files da modificare**:
- `simple_bot/core/models.py` (PerformanceMetrics model)
- `simple_bot/services/risk_manager.py` (calcolo metrics)
- `simple_bot/dashboard/routes.py` (endpoint API)
- `simple_bot/dashboard/templates/index.html` (visualizzazione)

**Implementation**:
```python
# models.py
@dataclass
class PerformanceMetrics:
    timestamp: datetime
    equity: Decimal
    sharpe_ratio: Decimal          # Annualized
    sortino_ratio: Decimal         # Downside deviation only
    max_drawdown_pct: Decimal      # Peak-to-trough
    max_drawdown_duration_hours: int
    profit_factor: Decimal         # Gross profit / Gross loss
    win_rate: Decimal
    avg_win_loss_ratio: Decimal
    expectancy: Decimal
    sqn: Optional[Decimal]         # System Quality Number

    @classmethod
    async def calculate_from_trades(cls, trades: List[Trade]) -> "PerformanceMetrics":
        # Implementation here
```

**Success Criteria**:
- [ ] Sharpe Ratio calcolato correttamente (testato su dati storici)
- [ ] Max Drawdown tracking funzionante
- [ ] Dashboard mostra tutte le metriche
- [ ] API endpoint `/api/performance` ritorna JSON
- [ ] Metriche aggiornate ogni 5 minuti

**Estimated Time**: 6 ore

---

### Task 1.3: ROI Graduato Time-Based
**Obiettivo**: Exit progressivo invece di TP fisso per catturare più profitti

**Files da modificare**:
- `simple_bot/config/trading.yaml` (config ROI)
- `simple_bot/services/execution_engine.py` (logica ROI)
- `simple_bot/strategies/base_strategy.py` (integrate ROI check)

**Implementation**:
```yaml
# trading.yaml
stops:
  minimal_roi:
    "0": 0.03      # 3% immediatamente
    "30": 0.02     # 2% dopo 30 min
    "60": 0.015    # 1.5% dopo 1h
    "120": 0.01    # 1% dopo 2h
    "240": 0.005   # 0.5% dopo 4h
    "480": 0.0     # Break-even dopo 8h
```

```python
# execution_engine.py
async def should_exit_on_roi(self, trade: Trade) -> bool:
    """Check ROI target based on time in trade"""
    time_in_trade_min = (datetime.now(timezone.utc) - trade.entry_time).total_seconds() / 60

    roi_config = self.config["stops"]["minimal_roi"]

    # Find current ROI target
    target_roi = Decimal("0")
    for time_threshold, roi_value in sorted(roi_config.items(), key=lambda x: int(x[0])):
        if time_in_trade_min >= int(time_threshold):
            target_roi = Decimal(str(roi_value))

    current_roi = (trade.current_price - trade.entry_price) / trade.entry_price
    return current_roi >= target_roi
```

**Success Criteria**:
- [ ] Test: Trade a +3% dopo 5min → exit
- [ ] Test: Trade a +1.5% dopo 90min → exit
- [ ] Test: Trade a +0.5% dopo 5h → hold (target non raggiunto)
- [ ] Backtest mostra migliore profit capture vs TP fisso

**Estimated Time**: 3 ore

---

### Task 1.4: Protection System Modulare
**Obiettivo**: Sistema automatico che blocca trading in condizioni avverse

**Files da creare**:
- `simple_bot/services/protections.py` (nuovo)

**Files da modificare**:
- `simple_bot/services/risk_manager.py` (integrate protections)
- `simple_bot/config/trading.yaml` (config protections)
- `simple_bot/database/schema.py` (tabella protection_log)

**Implementation**:
```python
# protections.py
class ProtectionManager:
    """
    Protections:
    1. StoplossGuard: Blocca dopo X stoploss in Y tempo
    2. MaxDrawdown: Blocca se DD > threshold
    3. CooldownPeriod: Minuti tra trades
    4. LowPerformance: Blocca se win rate < 30% su 20 trades
    """

    async def check_all_protections(self) -> Tuple[bool, Optional[str]]:
        """Returns (can_trade, block_reason)"""

        for protection in self.protections:
            can_trade, reason = await protection.check()
            if not can_trade:
                await self.log_protection_trigger(protection.name, reason)
                return False, reason

        return True, None
```

```yaml
# trading.yaml
protections:
  - name: "StoplossGuard"
    lookback_period_min: 60
    stoploss_limit: 3
    stop_duration_min: 360  # 6h

  - name: "MaxDrawdown"
    lookback_period_min: 1440  # 24h
    max_drawdown_pct: 5.0
    stop_duration_min: 720  # 12h

  - name: "CooldownPeriod"
    cooldown_between_trades_min: 5

  - name: "LowPerformance"
    min_trades: 20
    min_win_rate: 0.30
    stop_duration_min: 1440  # 24h
```

**Success Criteria**:
- [ ] Test: 3 SL in 1h → trading bloccato per 6h
- [ ] Test: 6% DD → trading bloccato per 12h
- [ ] Test: Win rate 25% su 20 trades → trading bloccato
- [ ] Dashboard mostra protections attive
- [ ] Telegram alert quando protection triggered

**Estimated Time**: 5 ore

---

### Task 1.5: Disabilita Short + Aumenta Leverage (Quick Win)
**Obiettivo**: Fix immediato per problemi attuali (short falliscono, equity bassa)

**Files da modificare**:
- `simple_bot/config/trading.yaml`
- `simple_bot/strategies/trend_follow.py`

**Implementation**:
```yaml
# trading.yaml
risk:
  leverage: 5                    # Da 1 a 5
  per_trade_pct: 2.0             # Da 1% a 2%
  max_positions: 3

strategies:
  trend_follow:
    enabled: true
    allow_short: false           # NUOVO: disabilita short
    regime_required: "trend"
```

**Success Criteria**:
- [ ] Config leverage=5 applicato agli ordini
- [ ] Nessun short aperto in live trading
- [ ] Trade size aumentato (da $0.86 a ~$4)
- [ ] Backtest su ultimo mese mostra miglioramento

**Estimated Time**: 1 ora

---

## Phase 2: Advanced Features (P1) - Days 6-10

### Task 2.1: Walk-Forward Backtesting Engine
**Obiettivo**: Backtesting robusto che evita overfitting

**Files da creare**:
- `simple_bot/backtesting/walk_forward.py` (nuovo)
- `simple_bot/backtesting/backtest_engine.py` (nuovo)
- `simple_bot/backtesting/optimizer.py` (grid search)

**Files da modificare**:
- `simple_bot/strategies/base_strategy.py` (supporto backtest mode)

**Implementation**:
```python
# walk_forward.py
class WalkForwardBacktest:
    def __init__(
        self,
        strategy: Strategy,
        train_window_days: int = 90,
        test_window_days: int = 30,
        optimization_metric: str = "sharpe_ratio"
    ):
        self.strategy = strategy
        self.train_window = timedelta(days=train_window_days)
        self.test_window = timedelta(days=test_window_days)

    async def run(
        self,
        start_date: datetime,
        end_date: datetime,
        param_space: dict
    ) -> WalkForwardResult:
        """
        Rolling window backtest:
        1. Train su 3 mesi → optimize params
        2. Test su 1 mese con optimized params
        3. Roll forward, repeat
        """
        results = []
        current_date = start_date

        while current_date + self.train_window + self.test_window <= end_date:
            # Phase 1: Optimize on train window
            train_data = await self.fetch_market_data(
                current_date,
                current_date + self.train_window
            )

            best_params = await self.optimize_parameters(
                train_data,
                param_space
            )

            # Phase 2: Test on test window
            test_data = await self.fetch_market_data(
                current_date + self.train_window,
                current_date + self.train_window + self.test_window
            )

            period_result = await self.backtest_period(
                test_data,
                best_params
            )

            results.append(period_result)

            # Roll forward
            current_date += self.test_window

        return WalkForwardResult(
            periods=results,
            overall_sharpe=self.calculate_overall_sharpe(results),
            overall_max_dd=self.calculate_overall_max_dd(results),
            profit_factor=self.calculate_profit_factor(results)
        )
```

**Success Criteria**:
- [ ] Script `python -m simple_bot.backtesting.walk_forward` funzionante
- [ ] Output HTML con grafici equity curve per ogni period
- [ ] Confronto train vs test performance (detect overfitting)
- [ ] Grid search trova parametri ottimali per ogni window
- [ ] Metrics salvate in database per tracking

**Estimated Time**: 8 ore

---

### Task 2.2: Liquidation Price Monitoring Preciso
**Obiettivo**: Calcolo preciso liq price Hyperliquid + monitoring real-time

**Files da modificare**:
- `simple_bot/services/risk_manager.py`
- `simple_bot/dashboard/templates/positions.html`

**Implementation**:
```python
# risk_manager.py
def calculate_liquidation_price_hyperliquid(
    self,
    symbol: str,
    open_rate: Decimal,
    is_short: bool,
    amount: Decimal,
    stake_amount: Decimal,
    leverage: int,
    wallet_balance: Decimal
) -> Decimal:
    """
    Formula precisa Hyperliquid:
    Basata su implementazione Freqtrade testata
    """
    position_value = open_rate * amount

    # Hyperliquid specifics
    max_leverage = self.get_max_leverage(symbol)  # Es. 50x per BTC
    maintenance_margin_rate = Decimal("1") / Decimal(max_leverage) / Decimal("2")
    maintenance_margin = position_value * maintenance_margin_rate

    # Margin available for loss absorption
    if self.margin_mode == "isolated":
        margin_available = stake_amount - maintenance_margin
    else:  # cross
        margin_available = wallet_balance - maintenance_margin

    # Liquidation price
    maintenance_leverage = max_leverage * 2
    ll = Decimal("1") / Decimal(maintenance_leverage)
    side = Decimal("-1") if is_short else Decimal("1")

    liq_price = open_rate - side * margin_available / amount / (Decimal("1") - ll * side)

    return liq_price

async def check_liquidation_risk(self, position: Position) -> LiquidationRisk:
    """
    Monitor distanza da liq price:
    - GREEN: >20% from liq
    - YELLOW: 10-20% from liq
    - RED: <10% from liq
    """
    liq_price = self.calculate_liquidation_price_hyperliquid(...)
    current_price = await self.exchange.get_current_price(position.symbol)

    distance_pct = abs(current_price - liq_price) / current_price

    if distance_pct < Decimal("0.10"):
        return LiquidationRisk.HIGH
    elif distance_pct < Decimal("0.20"):
        return LiquidationRisk.MEDIUM
    else:
        return LiquidationRisk.LOW
```

**Success Criteria**:
- [ ] Liq price calcolato matches Hyperliquid UI (test su 5 posizioni)
- [ ] Dashboard mostra liq price + distance % per ogni position
- [ ] Alert Telegram se position entra in RED zone (<10%)
- [ ] Emergency exit se liq distance < 5%

**Estimated Time**: 4 ore

---

### Task 2.3: Paper Trading Mode
**Obiettivo**: Testare strategie senza rischiare capitale reale

**Files da modificare**:
- `simple_bot/config/trading.yaml`
- `simple_bot/services/execution_engine.py`
- `simple_bot/exchange/hyperliquid_client.py` (mock mode)

**Implementation**:
```yaml
# trading.yaml
paper_trading:
  enabled: true              # false in production
  initial_balance: 1000.0
  simulate_slippage: true
  slippage_pct: 0.05         # 0.05% slippage medio
  simulate_fees: true
  log_simulated_orders: true
```

```python
# execution_engine.py
class ExecutionEngine:
    def __init__(self, config: dict):
        self.paper_trading = config.get("paper_trading", {}).get("enabled", False)
        if self.paper_trading:
            self.paper_balance = Decimal(str(config["paper_trading"]["initial_balance"]))
            self.paper_positions = {}

    async def execute_order(self, signal: Signal) -> Order:
        if self.paper_trading:
            return await self._simulate_order(signal)
        else:
            return await self._execute_real_order(signal)

    async def _simulate_order(self, signal: Signal) -> Order:
        """
        Simula order execution con:
        - Slippage realistico
        - Fees
        - Partial fills possibili
        - Fill delay
        """
        # Implementation
```

**Success Criteria**:
- [ ] Paper trading mode simula ordini senza toccare exchange
- [ ] Balance tracking corretto (fees + slippage)
- [ ] Dashboard mostra "PAPER TRADING" badge prominente
- [ ] Test: 24h paper trading → confronto con backtest

**Estimated Time**: 5 ore

---

### Task 2.4: Integration & Testing
**Obiettivo**: Integrare tutti i componenti e testare end-to-end

**Files da creare**:
- `simple_bot/tests/test_protections.py`
- `simple_bot/tests/test_performance_metrics.py`
- `simple_bot/tests/test_cooldown.py`
- `simple_bot/tests/test_roi_graduated.py`

**Tests da implementare**:
```python
# test_protections.py
async def test_stoploss_guard_triggers():
    """3 stoploss in 1h → trading bloccato"""

async def test_max_drawdown_triggers():
    """6% DD in 24h → trading bloccato"""

async def test_cooldown_period_enforced():
    """Trade ogni 5min max"""

# test_performance_metrics.py
async def test_sharpe_ratio_calculation():
    """Sharpe ratio corretto su dati storici"""

async def test_max_drawdown_tracking():
    """Max DD identificato correttamente"""

# test_roi_graduated.py
async def test_roi_early_exit():
    """3% profit dopo 10min → exit"""

async def test_roi_late_exit():
    """1% profit dopo 3h → hold"""
```

**Success Criteria**:
- [ ] 100% test pass
- [ ] Coverage >80% su codice nuovo
- [ ] Integration test: bot runs 24h in paper trading
- [ ] No memory leaks (profiling)
- [ ] Dashboard responsive <200ms

**Estimated Time**: 6 ore

---

### Task 2.5: Dashboard Enhancements
**Obiettivo**: UI per nuove features

**Files da modificare**:
- `simple_bot/dashboard/templates/index.html`
- `simple_bot/dashboard/static/css/custom.css`
- `simple_bot/dashboard/routes.py`

**Features da aggiungere**:
1. **Performance Metrics Card**
   - Sharpe Ratio
   - Max Drawdown (current + all-time)
   - Profit Factor
   - Win Rate
   - Expectancy

2. **Protections Status Panel**
   - Lista protections attive
   - Countdown timer se in cooldown
   - Storia trigger (ultimi 7 giorni)

3. **Liquidation Risk Indicator**
   - Traffic light per ogni position (🟢🟡🔴)
   - Distance % from liq price
   - Emergency exit button

4. **Backtesting Results Tab**
   - Upload backtest results
   - Equity curve charts
   - Parameter comparison table

**Success Criteria**:
- [ ] Dashboard mobile-friendly
- [ ] Real-time updates via HTMX
- [ ] Charts responsive (Chart.js)
- [ ] Export reports to PDF

**Estimated Time**: 6 ore

---

## Phase 3: Deployment & Monitoring - Day 11

### Task 3.1: Deploy to Hetzner VPS
**Obiettivo**: Deploy nuovo codice su production

**Steps**:
1. Backup database attuale
2. Deploy via `./deploy.sh`
3. Run migrations
4. Verify services health
5. Enable paper trading per 24h
6. Switch to live dopo validation

**Success Criteria**:
- [ ] Deploy senza downtime
- [ ] Tutte le migrations applicate
- [ ] Dashboard accessibile
- [ ] Logs clean (no errors)

**Estimated Time**: 2 ore

---

### Task 3.2: Monitoring & Alerts Setup
**Obiettivo**: Telegram alerts per eventi critici

**Files da modificare**:
- `simple_bot/services/telegram_service.py`

**Alerts da configurare**:
- ✅ Trade opened/closed
- ✅ Cooldown triggered
- ✅ Protection triggered
- ✅ Liquidation risk HIGH
- ✅ Daily summary (equity, P&L, metrics)
- ✅ Error alerts

**Success Criteria**:
- [ ] Telegram bot funzionante
- [ ] Alerts ricevuti in <5 secondi
- [ ] Daily summary alle 00:00 UTC

**Estimated Time**: 2 ore

---

## Success Metrics (Overall)

Dopo implementazione completa, il bot deve:

1. **Safety**
   - [ ] Zero liquidazioni in 30 giorni
   - [ ] Cooldown triggered <3 volte/settimana
   - [ ] Max drawdown <10% in qualsiasi 7-day period

2. **Performance**
   - [ ] Sharpe Ratio >1.0 (annualized)
   - [ ] Win Rate >45%
   - [ ] Profit Factor >1.5
   - [ ] Monthly return target: 5-10%

3. **Reliability**
   - [ ] Uptime >99.5%
   - [ ] Zero missed fills (execution <2s)
   - [ ] Database sync 100% accuracy

4. **Testing**
   - [ ] 30-day paper trading successful
   - [ ] Backtest walk-forward profitable su 6 mesi
   - [ ] All protections tested in simulation

---

## Rollback Plan

Se performance peggiora dopo deploy:

1. **Immediate**: Switch back to paper trading
2. **Investigate**: Check logs, metrics, recent trades
3. **Fix**: Adjust parameters via config (no redeploy needed)
4. **Validate**: 24h paper trading
5. **Resume**: Switch to live dopo validation

---

## Resource Requirements

- **Development Time**: ~45 ore (2 settimane part-time)
- **Testing Time**: ~15 ore
- **VPS Resources**: Attuale Hetzner sufficiente
- **Database**: PostgreSQL attuale sufficiente
- **External APIs**: DeepSeek (già configurato)

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Breaking changes in production | Paper trading mode per 24h prima di live |
| Database migration failures | Backup prima di ogni migration |
| Performance degradation | Rollback immediato + config tuning |
| Liquidation durante testing | Start con equity minima ($100) |
| Bugs in protections | Extensive unit tests + simulation |

---

## Post-Implementation

Dopo completamento:

1. **Week 3**: Monitor performance daily
2. **Week 4**: Tune parameters basato su live data
3. **Month 2**: Gradual capital scaling (se metrics OK)
4. **Month 3**: Consider multi-strategy se Sharpe >1.5

---

## Notes for Ralph Loop

**Checkpoint dopo ogni task**:
- Run tests
- Verify no regressions
- Update this document with progress
- Git commit con message descrittivo

**If stuck**:
- Document blocker in BLOCKERS.md
- Try alternative approach
- Ask for human input if needed

**Quality gates**:
- All tests pass
- Type hints correct (pyright)
- Code formatted (black + ruff)
- No security issues (bandit)
