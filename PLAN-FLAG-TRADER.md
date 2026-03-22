# FLAG-Trader Implementation Plan

> Replacing XGBoost+EMA strategy with LLM (SmolLM2 135M) + PPO/RL for Hyperliquid crypto trading.
> Based on paper: FLAG-Trader (arxiv 2502.11433, Harvard/NVIDIA, Feb 2025)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    FLAG-Trader Bot                        │
│                                                          │
│  ┌──────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │ Data      │───▶│ Prompt       │───▶│ SmolLM2 135M  │  │
│  │ Collector │    │ Builder      │    │ (Policy Net)  │  │
│  │ (candles, │    │ (state→text) │    │               │  │
│  │  volume,  │    └──────────────┘    │ Frozen base   │  │
│  │  funding) │                        │ + Trainable   │  │
│  └──────────┘                        │   top layers  │  │
│                                       │ + Policy head │  │
│                                       │ + Value head  │  │
│                                       └───────┬───────┘  │
│                                               │          │
│                                    ┌──────────▼────────┐ │
│                                    │ Action: BUY/SELL/ │ │
│                                    │         HOLD      │ │
│                                    └──────────┬────────┘ │
│                                               │          │
│  ┌──────────────┐  ┌──────────┐  ┌───────────▼───────┐  │
│  │ Risk Manager │◀─│Kill Switch│◀─│ Execution Engine  │  │
│  │ (KEEP)       │  │(KEEP)    │  │ (KEEP)            │  │
│  └──────────────┘  └──────────┘  └───────────────────┘  │
│                                                          │
│  ┌──────────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ Notifications│  │ Capital  │  │ Performance       │  │
│  │ ntfy (KEEP)  │  │ Ladder   │  │ Monitor (KEEP)    │  │
│  └──────────────┘  │ (KEEP)   │  └───────────────────┘  │
│                     └──────────┘                         │
└─────────────────────────────────────────────────────────┘

Training (offline):
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│Historical│───▶│  Gym     │───▶│  PPO     │───▶│ Updated  │
│ Candles  │    │  Env     │    │ Training │    │  Model   │
└──────────┘    └──────────┘    └──────────┘    └──────────┘
                     │               ▲
                     │  Reward =     │
                     │  ΔSharpe      │
                     └───────────────┘
```

---

## 2. File Map — crypto_bot/ (What Changes)

### KEEP (infrastructure — do not touch)

| File | Purpose |
|------|---------|
| `api/__init__.py` | API package |
| `api/hyperliquid.py` | Hyperliquid SDK wrapper |
| `api/exceptions.py` | Custom exceptions |
| `api/rate_limiter.py` | API rate limiting |
| `config/__init__.py` | Config package |
| `config/loader.py` | YAML config loader |
| `config/trading.yaml` | Primary config (will add new section) |
| `config/trading_paper.yaml` | Paper trading config |
| `core/__init__.py` | Core package |
| `core/enums.py` | Direction, Regime enums |
| `core/models.py` | MarketState, Signal models |
| `services/__init__.py` | Services package |
| `services/base.py` | Base service class |
| `services/execution_engine.py` | Order execution + TP/SL |
| `services/risk_manager.py` | Position sizing, daily limits |
| `services/kill_switch.py` | Circuit breaker |
| `services/capital_ladder.py` | Capital scaling levels |
| `services/performance_monitor.py` | P&L tracking |
| `services/protections.py` | Protection manager |
| `services/message_bus.py` | Internal pub/sub |
| `services/telegram_service.py` | Telegram notifications |
| `services/whatsapp_service.py` | WhatsApp notifications |
| `services/trade_memory.py` | Trade history persistence |
| `services/market_snapshots.py` | Market data snapshots |
| `conftest.py` | Test configuration |

### DELETE (replaced by FLAG-Trader)

| File | Purpose | Why Delete |
|------|---------|------------|
| `strategies/base.py` | Strategy base class | LLM replaces strategy logic |
| `services/market_state.py` | EMA/RSI/ATR calculation | LLM reads raw candles |
| `services/ml_model.py` | XGBoost/LGB inference | Replaced by LLM |
| `services/ml_dataset.py` | ML feature engineering | LLM doesn't need features |
| `services/llm_veto.py` | DeepSeek API veto | LLM IS the decision maker |
| `services/outcome_tracker.py` | ML outcome tracking | New RL training replaces this |
| `services/counterfactual_logger.py` | What-if logging | Not needed with RL |
| `scripts/retrain_model.py` | ML retraining script | New RL training script |
| `scripts/compare_models.py` | ML model comparison | Not needed |
| `tests/test_ml_model.py` | ML model tests | New tests needed |
| `tests/test_momentum_fade.py` | Strategy-specific test | Strategy removed |
| `tests/test_regime_exit.py` | Strategy-specific test | Strategy removed |
| `tests/test_regime_hysteresis.py` | Strategy-specific test | Strategy removed |
| `tests/test_roi_graduated.py` | Strategy-specific test | Strategy removed |
| `tests/test_volume_breakout.py` | Strategy-specific test | Strategy removed |
| `tests/test_spread_filter.py` | Strategy-specific test | Strategy removed |
| `tests/test_cooldown.py` | Strategy-specific test | Strategy removed |

### MODIFY

| File | What Changes |
|------|-------------|
| `main.py` | Remove strategy/ML init, add LLM agent init. Main loop: collect data → prompt LLM → execute action |
| `config/trading.yaml` | Add `flag_trader:` section (model path, prompt template, training params). Remove `ml_model:`, `strategy:` sections |
| `core/models.py` | Add `LLMAction` model (action + reasoning + confidence) |

### CREATE (new files)

| File | Purpose |
|------|---------|
| `flag_trader/__init__.py` | FLAG-Trader package |
| `flag_trader/agent.py` | LLM trading agent — inference (prompt→action) |
| `flag_trader/prompt.py` | Prompt builder (market state → structured text) |
| `flag_trader/model.py` | Model loader (SmolLM2 + policy/value heads) |
| `flag_trader/environment.py` | Gymnasium env for Hyperliquid trading simulation |
| `flag_trader/reward.py` | Reward function (Sharpe ratio delta) |
| `flag_trader/trainer.py` | PPO training loop |
| `flag_trader/data_collector.py` | Download historical candles from Hyperliquid |
| `flag_trader/walk_forward.py` | Walk-forward validation |
| `tests/test_flag_trader.py` | Tests for FLAG-Trader components |
| `tests/test_environment.py` | Tests for Gym environment |

---

## 3. Dependencies

### Add to requirements.txt
```
torch>=2.1.0              # PyTorch (CPU for Phase 1)
transformers>=4.40.0       # HuggingFace model loading
accelerate>=0.28.0         # Model loading utilities
gymnasium>=0.29.0          # RL environment
trl>=0.8.0                 # PPO trainer for LLMs
peft>=0.10.0               # LoRA/parameter-efficient fine-tuning
datasets>=2.18.0           # Data handling
```

### Remove from requirements.txt
```
xgboost                    # No longer needed
lightgbm                   # No longer needed
scikit-learn               # No longer needed (unless used elsewhere)
```

### Keep
```
hyperliquid-python-sdk     # Exchange connection
pydantic                   # Data validation
pyyaml                     # Config
aiohttp / httpx            # HTTP client
python-dotenv              # Env vars
pytest                     # Testing
```

---

## 4. Gymnasium Environment (`flag_trader/environment.py`)

```python
class HyperliquidTradingEnv(gymnasium.Env):
    """
    Simulates trading on Hyperliquid historical candles.

    State: Last N candles (OHLCV) + portfolio state (cash, position, pnl)
    Action: {-1: Sell, 0: Hold, 1: Buy}
    Reward: Delta of Sharpe ratio (SR_t - SR_{t-1})

    Episode = 1 trading day or configurable window
    """

    # State space: text representation of market (handled by prompt builder)
    # Action space: Discrete(3) — Sell, Hold, Buy
    # Reward: Sharpe ratio delta (as per FLAG-Trader paper eq. 1)

    # Key methods:
    #   reset() → load next candle window, reset portfolio
    #   step(action) → execute trade, compute reward, return next state
    #   _compute_sharpe_delta() → SR_t - SR_{t-1}
```

### Data format
- Candles: [timestamp, open, high, low, close, volume] per asset
- Fetch via Hyperliquid API: `info.candles_snapshot()`
- Store locally as Parquet files in `data/candles/`

---

## 5. Prompt Template (`flag_trader/prompt.py`)

Based on FLAG-Trader paper Figure 3:

```
Task: You are a cryptocurrency trading agent. Your goal is to maximize
long-term risk-adjusted returns. Choose optimal buy, sell, or hold
decisions based on market conditions and risk assessment.

Legible Actions: Choose from {Buy, Sell, Hold}

Current State:
{
  "historical_prices": [last 20 candles OHLCV],
  "account_status": {
    "cash_balance": <float>,
    "asset_position": <float>,
    "total_account_value": <float>
  },
  "previous_decision_metrics": {
    "recent_rewards": [last 10 rewards],
    "net_values": [last 10 portfolio values],
    "actions": [last 10 actions]
  }
}

Output Action: Format your answer as JSON: {"Action": "Buy"}, {"Action": "Sell"}, {"Action": "Hold"}
```

---

## 6. Model Architecture (`flag_trader/model.py`)

Based on FLAG-Trader paper Section 4.2:

```
SmolLM2-135M (HuggingFace: HuggingFaceTB/SmolLM2-135M-Instruct)
│
├── Frozen base layers (θ_frozen) — preserve general knowledge
│   └── Layers 0 to N
│
├── Trainable top layers (θ_train) — adapt to trading
│   └── Layers N+1 to N+M
│
├── Policy head (θ_P) — outputs action probabilities
│   └── MLP: hidden → 3 (Buy/Sell/Hold logits)
│
└── Value head (θ_V) — estimates expected return
    └── MLP: hidden → 1 (state value)
```

### Freeze strategy
- Freeze bottom 80% of layers
- Train top 20% + policy head + value head
- This preserves language understanding while adapting to trading

---

## 7. Training Pipeline (`flag_trader/trainer.py`)

### PPO Training (Algorithm 1 from paper)

```
1. Load historical candles (6+ months, all assets)
2. Initialize SmolLM2-135M with frozen base + trainable top
3. Add policy head (→ Buy/Sell/Hold) and value head (→ expected return)
4. For each episode:
   a. Reset environment with random candle window
   b. Build prompt from current state: lang(s_t)
   c. Forward pass through LLM → action logits
   d. Sample action from policy distribution
   e. Execute in environment → reward (Sharpe delta)
   f. Store (state, action, reward, next_state) in replay buffer
   g. Every τ steps: update policy head, value head, trainable layers via PPO
5. Save best model checkpoint based on validation Sharpe
```

### Training config
```yaml
flag_trader:
  model_name: "HuggingFaceTB/SmolLM2-135M-Instruct"
  freeze_layers_pct: 0.8          # Freeze bottom 80%
  learning_rate: 1e-5
  ppo_epochs: 4
  batch_size: 32
  gamma: 0.99                     # Discount factor
  clip_range: 0.2                 # PPO clipping
  max_grad_norm: 0.5
  num_episodes: 10000
  episode_length: 100             # Candles per episode
  update_frequency: 10            # Update every 10 steps
  candle_history: 20              # Last 20 candles in prompt
  validation_split: 0.2           # 20% data for validation
```

---

## 8. Walk-Forward Validation (`flag_trader/walk_forward.py`)

```
Total data: 8 months of candles
│
├── Window 1: Train [month 1-4] → Validate [month 5] → Test [month 6]
├── Window 2: Train [month 2-5] → Validate [month 6] → Test [month 7]
├── Window 3: Train [month 3-6] → Validate [month 7] → Test [month 8]
│
Pass criteria (ALL must be met):
  - OOS Sharpe ratio > 1.0
  - OOS Profit Factor > 1.2
  - OOS Max Drawdown < 20%
  - Minimum 50 trades per window
  - At least 2/3 windows profitable
```

---

## 9. Inference Pipeline (Live Trading)

```
Every 5 minutes:
  1. Fetch last 20 candles (15min) for target asset
  2. Build prompt with current portfolio state
  3. Forward pass through trained SmolLM2 → action
  4. If action != Hold:
     a. Risk manager validates (position size, daily limits)
     b. Kill switch check
     c. Execution engine places order with TP/SL
  5. Log: prompt, action, reasoning, market state
```

### Integration with existing bot
- `main.py` loop: replace strategy scan with LLM inference
- Execution engine unchanged (TP/SL, partial fills)
- Risk manager unchanged (position sizing, daily loss limit)
- Kill switch unchanged (circuit breaker)
- Notifications unchanged (ntfy alerts on trades)

---

## 10. Implementation Timeline

### Phase 1: Foundation (Days 1-3)
- [ ] Clean codebase: delete old ML/strategy files
- [ ] Create `flag_trader/` package structure
- [ ] Implement `environment.py` (Gym env)
- [ ] Implement `prompt.py` (prompt builder)
- [ ] Implement `data_collector.py` (fetch historical candles)
- [ ] Write tests for env and prompt

### Phase 2: Training (Days 4-7)
- [ ] Implement `model.py` (SmolLM2 + heads)
- [ ] Implement `reward.py` (Sharpe delta)
- [ ] Implement `trainer.py` (PPO loop)
- [ ] Download 6+ months of candle data
- [ ] Run first training on VPS (CPU — will be slow but functional)
- [ ] Write tests for training components

### Phase 3: Validation (Days 8-10)
- [ ] Implement `walk_forward.py`
- [ ] Run walk-forward validation (3 windows)
- [ ] Analyze results: does it beat buy-and-hold? Does it beat old bot?
- [ ] Compare OOS metrics against pass criteria

### Phase 4: Integration (Days 11-13)
- [ ] Implement `agent.py` (live inference)
- [ ] Modify `main.py` for FLAG-Trader loop
- [ ] Update `config/trading.yaml`
- [ ] Deploy to VPS in dry_run mode
- [ ] Run paper trading for 1 week

### Phase 5: Go Live (Day 14+)
- [ ] If paper trading metrics pass → switch to live
- [ ] Capital ladder: start at Level 0 ($100)
- [ ] Monitor and log all LLM decisions
- [ ] Plan Phase 2 (GPU upgrade) if results are positive

---

## 11. Key Decisions (from FLAG-Trader paper)

| Decision | Choice | Rationale (paper reference) |
|----------|--------|----------------------------|
| Model size | 135M (SmolLM2) | Paper showed 135M beats GPT-4 on trading (Table 1-2) |
| RL algorithm | PPO | Paper uses PPO with replay buffer (Algorithm 1) |
| Reward | Sharpe delta | Not raw P&L — teaches consistent returns (Eq. 1) |
| Freeze strategy | Bottom 80% frozen | Preserves language ability, adapts top layers (Sec 4.2) |
| Action space | Discrete: Buy/Sell/Hold | All-in/all-out per paper design (Sec 3) |
| Training data | 6+ months candles | Need enough for walk-forward windows |
| Prompt format | Structured JSON | Paper Figure 3 format |

---

## 12. Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Training too slow on CPU | Phase 1 is validation only. Upgrade to GEX44 (€104/mo) for Phase 2 |
| Overfitting historical data | Walk-forward validation with strict pass criteria |
| LLM worse than current bot | Paper trading first. Current bot P&L is +$1.71 — low bar to beat |
| High memory usage on VPS | SmolLM2 135M needs ~500MB RAM. VPS has 2.5GB free |
| API rate limits during data fetch | Batch download, cache locally as Parquet |

---

## 13. IB Bot Impact

- **NONE** — ib_bot/ is completely isolated (zero shared imports)
- ib_bot continues running independently on paper trading
- No files in ib_bot/ will be modified
- Backtesting framework in ib_bot/ (simulator, walk-forward) may be referenced for patterns

---

## 14. How to Resume This Work

If opening a new terminal:
1. Read this file: `PLAN-FLAG-TRADER.md`
2. Read memory: check `flag_trader_decision.md` in claude memory
3. Check git status for what's been done
4. Follow the Phase checklist above
5. The current bot remains deployed and running until Phase 4 is validated
