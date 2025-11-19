# trader_bitcoin Development Guidelines

Auto-generated from all feature plans. Last updated: 2025-11-14

## 🚀 TRADING SYSTEM ARCHITECTURE (WebSocket-Based Real-Time Momentum)

**CRITICAL**: Sistema completamente refactorato da Prophet forecasting → WebSocket streaming.

### Latest Changes (2025-11-14)

**Change 1: HTTP → WebSocket Migration (Candles)**
- **Problem**: HTTP polling 220 API calls/cycle → 6-8x oltre rate limit (1200 weight/min)
- **Solution**: WebSocket streaming candele 1h → **0 API calls**, zero rate limiting, 0.5s latency
- **See**: `backend/docs/WEBSOCKET_ARCHITECTURE.md`

**Change 2: WebSocket allMids Integration (Prices)**
- **Problem**: API calls per prezzi → ~400 weight/hour aggiuntivi
- **Solution**: WebSocket allMids subscription → **0 API calls** per prezzi, real-time updates
- **Impact**: Eliminati TUTTI i rate limit risk per market data
- **Files Modified**:
  - `websocket_candle_service.py`: Aggiunto allMids subscription + price cache
  - `price_cache.py`: Priority WebSocket → local cache fallback

### Core Architecture

```
┌─────────────────────────────────────────────┐
│ WebSocket Service (persistent, background)  │
│ - Connects to wss://api.hyperliquid.xyz/ws │
│ - Subscribes to ALL 220 symbols (1h)       │
│ - Stores in local cache (1 MB)             │
│ - Auto-reconnect on disconnect             │
└─────────────────────────────────────────────┘
              ↓ (cache populated)
┌─────────────────────────────────────────────┐
│ Auto Trader (every 3 min)                   │
│ 1. Read candles from cache (0 API calls)   │
│ 2. Calculate momentum → top 20 coins       │
│ 3. Technical analysis (20 coins)           │
│ 4. AI decision (DeepSeek)                  │
│ 5. Execute LONG/SHORT (20% capital)        │
└─────────────────────────────────────────────┘
```

### 🎯 Trading Strategy: Momentum Surfing

**Obiettivo**: Surfare le crescite delle crypto e uscire appena iniziano a scendere.

**Meccanica**:
1. **Identificazione momentum**: Scansiona 220+ coins ogni ora per trovare quelle in forte crescita (top 20)
2. **Entry rapido**: Apre posizione LONG/SHORT sulla coin con migliore momentum + segnali tecnici
3. **Exit rapido**: Stop loss a -2% per limitare perdite, take profit automatico su segnali di inversione

**Timeframe**: 1h candles (bilancia reattività vs noise)
- Abbastanza veloce per catturare rally intraday
- Abbastanza lento per evitare micro-fluttuazioni

**Holding period**: Tipicamente 1-6 ore (non swing trading)
- Sistema monitora ogni 3 minuti per possibili exit
- Stop loss -2% e take profit +5% proteggono capitale

### How It Works

1. **WebSocket Candle Service** (`websocket_candle_service.py`): Real-time data stream
   - Persistent connection to Hyperliquid WebSocket API
   - Receives 1h candle updates for 220+ symbols
   - Local cache: 24 candles per symbol (~1 MB memory)
   - State persistence: Saves/loads cache to disk
   - Auto-reconnect: Exponential backoff (1s → 60s)

2. **Real-Time Price Cache** (`price_cache.py` + WebSocket allMids): Real-time price updates
   - **WebSocket allMids subscription** (2025-11-14): Receives all symbol prices in real-time
   - **0 API calls** for price lookups (eliminates ~400 weight/hour)
   - Priority: WebSocket cache → local TTL cache (fallback)
   - Auto-updated every ~1 second via WebSocket stream
   - Memory: ~0.02 MB for 220 prices

3. **Momentum Calculation** (`hourly_momentum.py`): Reads from local cache
   - **0 API calls** (reads from WebSocket cache)
   - Calcola % change ultima ora per ogni coin
   - Filtra per volume minimo ($10k/h)
   - Ritorna **top 20 coins** con momentum score più alto
   - Duration: **0.5s** (was 15-30s with HTTP)

3. **Technical Analysis** (`technical_analysis_service.py`): SOLO sui top 20
   - Candele: **1h timeframe, 24 candles**
   - Indicatori: RSI, MACD, Pivot Points, Support/Resistance
   - Score composito 0-1 per ogni coin

4. **Multi-Agent AI Decision** (UNICA MODALITÀ DI FUNZIONAMENTO):
   - **Architettura**: Sistema multi-agent con orchestratore
   - **Agenti specializzati** (LONG e SHORT):
     - LONG Agent: Specializzato in posizioni long (buy)
     - SHORT Agent: Specializzato in posizioni short
   - **Orchestrator** (`orchestrator_service.py`):
     - Raccoglie proposte da entrambi gli agenti
     - Seleziona la MIGLIORE proposta per technical_score
     - Esegue SOLO UN trade per ciclo (non entrambi)
   - **Entry Point**: `place_multi_agent_order()` in `auto_trader.py`
   - **Scheduler**: `main.py` (APScheduler) usa `place_multi_agent_order`

5. **Execution** (`auto_trader.py`): Ordine su Hyperliquid
   - Intervallo: **3 minuti**
   - Post-trade: Sync positions + assign trading strategy

### Multi-Agent System Architecture (CRITICAL)

**IMPORTANTE**: Il sistema usa ESCLUSIVAMENTE `place_multi_agent_order`, NON `place_ai_driven_crypto_order`.

```
┌─────────────────────────────────────────────────┐
│ APScheduler (every 3 min)                       │
│ main.py → place_multi_agent_order()             │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│ Specialized Agents (parallel analysis)          │
│ ┌─────────────────┐  ┌─────────────────┐        │
│ │ LONG Agent      │  │ SHORT Agent     │        │
│ │ (buy proposals) │  │ (short props)   │        │
│ └─────────────────┘  └─────────────────┘        │
└─────────────────────────────────────────────────┘
                    ↓ (AgentProposal with technical_score)
┌─────────────────────────────────────────────────┐
│ Orchestrator (orchestrator_service.py)          │
│ - Collects proposals from LONG and SHORT agents │
│ - Selects BEST proposal by technical_score      │
│ - Returns SINGLE decision (only 1 trade/cycle)  │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│ Execution on Hyperliquid                        │
└─────────────────────────────────────────────────┘
```

**Key Files**:
- `backend/services/auto_trader.py`: `place_multi_agent_order()` implementation
- `backend/services/orchestrator_service.py`: Orchestrator logic (LONG/SHORT agent selection)
- `backend/services/ai/deepseek_client.py`: Agent prompts
- `backend/main.py`: APScheduler job configuration

### Performance Evolution

| Metric | Daily Prophet | HTTP Polling | WebSocket (Current) |
|--------|--------------|--------------|---------------------|
| **Timeframe** | 1d (71 candles) | 1h (24 candles) | 1h (24 candles) |
| **Analysis time** | ~60s | ~15-30s | **0.5s** |
| **API calls/cycle** | 220+ | 220 | **0** |
| **Rate limit usage** | High | **6-8x OVER** | **0** |
| **Cycle frequency** | 10min | 3min | 3min |
| **Risk** | Missed rallies | 429 errors | **None** |

### Files Changed (WebSocket Migration)

- **NEW**: `backend/services/market_data/websocket_candle_service.py` (460 lines)
- **NEW**: `backend/scripts/testing/test_websocket_momentum.py` (test script)
- **NEW**: `backend/docs/WEBSOCKET_ARCHITECTURE.md` (complete documentation)
- **MODIFIED**: `backend/services/market_data/hourly_momentum.py` (cache reads instead of API)
- **MODIFIED**: `backend/services/startup.py` (WebSocket initialization)

## 📋 CLAUDE.md FILE ORGANIZATION RULES (META)

**CRITICAL**: This section documents how to organize THIS file itself.

### What BELONGS in CLAUDE.md (100-150 lines ideal, readable in 5-10 minutes):
- ✅ Project overview and goals
- ✅ Current status summary
- ✅ Key architectural concepts (brief)
- ✅ Essential onboarding info and gotchas
- ✅ Day-to-day development rules (coding style, testing, debug workflows)
- ✅ **References/pointers** to external detailed docs

### What DOES NOT BELONG in CLAUDE.md:
- ❌ Extensive technical details (>100 lines on single topic)
- ❌ Long-form strategic planning and roadmaps
- ❌ Detailed implementation checklists (>100 lines)
- ❌ Monitoring setup details, CI/CD pipeline configs
- ❌ Exhaustive reference documentation
- ❌ **Temporary .md files in project root** (use `backend/docs/` or delete after conversation)

### Correct Organization Pattern:

| Content Type | CLAUDE.md | Separate Doc |
|--------------|-----------|--------------|
| Project summary/goals | Yes | No |
| Current status | Yes (concise) | Yes (detailed) |
| Coding style rules | Pointer/summary | Yes (details) |
| Strategic roadmap | Brief summary + pointer | Yes (`docs/DEPLOYMENT_ROADMAP.md`) |
| Implementation checklists (>100 lines) | Pointer only | Yes |
| Deployment instructions | Pointer/summary | Yes (details) |
| Monitoring/CI/CD (>100 lines) | Pointer only | Yes |

### File Structure for Large Analysis:
When adding 600+ line content:
1. Create `backend/docs/[TOPIC]_ROADMAP.md` with full details
2. Add concise 5-10 line summary in CLAUDE.md
3. Link to detailed doc

**Example - CORRECT**:
```markdown
## 📚 DEPLOYMENT & INFRASTRUCTURE DOCUMENTATION

For comprehensive deployment analysis, see:
- **`backend/docs/DEPLOYMENT_ROADMAP.md`** - Complete analysis (600+ lines)

Quick summary: 6 critical gaps identified, prioritized roadmap provided.
```

**Example - WRONG**:
```markdown
## 🚀 DEPLOYMENT ROBUSTNESS ANALYSIS

[600+ lines of deployment details here...]
```

**Why this matters**:
- Claude Code works best with concise, actionable CLAUDE.md
- Long documents reduce signal-to-noise ratio
- Detailed analysis belongs in dedicated files
- Reference: https://www.eesel.ai/blog/claude-code-best-practices

## Active Technologies

- Python 3.11+ (currently 3.13 in Dockerfile) + FastAPI, SQLAlchemy 2.0+ (async), hyperliquid-python-sdk >=0.20.0, APScheduler, uvicorn (001-production-refactor)

## Project Structure

```text
src/
tests/
```

## Commands

cd src [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] pytest [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] ruff check .

## Code Style

Python 3.11+ (currently 3.13 in Dockerfile): Follow standard conventions

## Recent Changes

- 001-production-refactor: Added Python 3.11+ (currently 3.13 in Dockerfile) + FastAPI, SQLAlchemy 2.0+ (async), hyperliquid-python-sdk >=0.20.0, APScheduler, uvicorn

<!-- MANUAL ADDITIONS START -->

## 📝 FILE MANAGEMENT RULES

**CRITICAL**: NEVER create temporary .md files in project root during conversations.

**Rules**:
1. **Permanent documentation**: Create in `backend/docs/` with clear naming
2. **Temporary explanations**: Provide inline in chat response, NO file creation
3. **Analysis reports**: If >200 lines, create in `backend/docs/` OR just explain in chat
4. **Root directory**: ONLY for permanent project files (README.md, CLAUDE.md, docker-compose.yml)

**Examples**:
- ❌ `RISPOSTA_DOMANDE_API.md` in root (temporary explanation)
- ✅ `backend/docs/RATE_LIMIT_ANALYSIS_HOURLY_MOMENTUM.md` (permanent reference)
- ✅ Explain in chat without file creation

**Cleanup**: If temporary .md files exist in root, delete them at end of conversation.

## 🤝 USER DECISION-MAKING PROTOCOL

**CRITICAL RULE**: NEVER make implementation decisions without explicit user approval.

**When implementing fixes or features:**
1. **STOP** before making any code changes
2. **EXPLAIN** in detail what you plan to do:
   - Which files will be modified
   - What changes will be made
   - Why each change is necessary
   - What risks/trade-offs exist
3. **WAIT** for explicit user approval ("yes", "ok", "procedi", etc.)
4. **ONLY THEN** proceed with implementation

**Example - CORRECT workflow**:
```
Assistant: "I plan to modify auto_trader.py to add leverage support.
This will:
- Add leverage parameter (1-10x)
- Modify order execution to pass leverage to Hyperliquid
- Update validation to check leverage limits
Risks: Higher leverage = higher losses possible
Do you approve?"

User: "yes"
```

## 🔍 CODEBASE EXPLORATION RULES

### ALWAYS Use claude-context MCP for Code Exploration

**IMPORTANT**: This project uses the claude-context MCP server for semantic code search.

**Rules**:
1. **ALWAYS re-index BEFORE using search_code** - Index does NOT auto-update!
2. **ALWAYS use claude-context FIRST when user asks ANY question about codebase**
3. **Use Grep/Read/Glob ONLY AFTER claude-context**, or when user provides explicit file path
4. **NEVER use Grep/Glob as first step** when exploring code

**CRITICAL WORKFLOW** - Do this EVERY TIME before searching:
```python
# Step 1: ALWAYS re-index first (takes ~10-15 seconds for 153 files)
mcp__claude-context__index_codebase(
    path="/Users/francescocarlesi/Downloads/Progetti Python/trader_bitcoin",
    splitter="ast",
    force=true  # Force ensures fresh index
)

# Step 2: WAIT 15 seconds for indexing to complete
# Use Bash sleep command to wait
Bash(command="sleep 15", description="Wait for indexing to complete")

# Step 3: Then search
mcp__claude-context__search_code(
    path="/Users/francescocarlesi/Downloads/Progetti Python/trader_bitcoin",
    query="your semantic query here",
    limit=5
)
```

**IMPORTANT**: The 15-second wait is MANDATORY. Searching before indexing completes will return incomplete/outdated results.

**Codebase Stats**: 153 files, 1229 chunks, AST splitter, OpenAI embeddings

### Index Maintenance and Updates

**CRITICAL**: claude-context does **NOT** automatically re-index when files change.

**Re-indexing**: Always automatic via workflow above (force=true), takes ~10-15 seconds, MUST wait before searching.

## 🚨 CRITICAL TESTING RULES

### ALWAYS Use Real Data - NEVER Invent Data

**IMPORTANT**: This is a REAL TRADING system connected to live Hyperliquid exchange.

**Rules**:
1. **NEVER invent or fabricate test data** (account balances, prices, positions, etc.)
2. **ALWAYS fetch real data** from Hyperliquid API via `hyperliquid_trading_service`
3. **ALWAYS sync database** with real on-chain state before testing
4. **Inventing data causes cascading problems** in subsequent operations

**Why**: Fake data leads to:
- Validation failures
- Order rejection
- Incorrect balance tracking
- Misleading test results
- Production bugs

**How to get real data**:
```python
# Get real balance from Hyperliquid (ALWAYS use API, not database)
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service
user_state = await hyperliquid_trading_service.get_user_state_async()
real_balance = float(user_state['marginSummary']['accountValue'])
total_margin = float(user_state['marginSummary']['totalMarginUsed'])
withdrawable = float(user_state['marginSummary']['withdrawable'])

# NOTE: Balance is NOT stored in database - always fetch from Hyperliquid API
# Database Account table only stores metadata (name, AI config, active status)
```

**Trading Constraints**:
- Min order size: `$10` (Hyperliquid requirement)
- Max ratio per trade: `20%` (default, configurable)

## 🧹 Code Quality & Maintenance Rules

### ALWAYS Clean Up Obsolete Code - NEVER Leave Hardcoded Workarounds

**IMPORTANT**: When refactoring or changing architecture:

**Rules**:
1. **NEVER leave obsolete code paths** that are no longer used
2. **NEVER use hardcoded fallbacks** to patch underlying issues (e.g., `or '0'` to handle None values)
3. **ALWAYS remove deprecated fields/functions** completely or mark them clearly
4. **ALWAYS fix root causes** instead of adding defensive checks everywhere
5. **Track ALL places** where obsolete patterns are used and update them systematically

**Why**:
- Hardcoded fallbacks hide bugs instead of fixing them
- Obsolete code confuses future developers
- Technical debt accumulates and becomes unmaintainable
- Band-aids on band-aids make debugging impossible

**Example - WRONG**:
```python
# Bad: Using 'or' to handle None from deprecated DB field
account_value = float(margin.get('accountValue') or '0')  # ❌ Hides the real problem
```

**Example - CORRECT**:
```python
# Good: Remove the obsolete field completely, fix the API response
# 1. Remove deprecated DB column initial_capital, current_cash, frozen_cash
# 2. Always fetch from Hyperliquid API (single source of truth)
# 3. Handle None values at the API layer, not with fallbacks
user_state = await hyperliquid_trading_service.get_user_state_async()
if not user_state or 'marginSummary' not in user_state:
    raise ValueError("Failed to fetch data from Hyperliquid")  # ✅ Fail fast
account_value = float(user_state['marginSummary']['accountValue'])
```

**Refactoring Checklist**:
- [ ] Remove all obsolete database columns (via migration)
- [ ] Remove all code reading obsolete columns
- [ ] Update all functions that create/update obsolete data
- [ ] Search codebase for hardcoded fallbacks (grep for `or '0'`, `or 0`, etc.)
- [ ] Fix root causes instead of adding defensive checks
- [ ] Update tests to not use obsolete patterns
- [ ] Document why fields were removed (deprecation comments)

## 🚫 NEVER USE FALLBACK VALUES

**CRITICAL RULE**: NEVER use fallback values to mask missing or invalid data.

**Why fallbacks are dangerous**:
- Hide bugs and data inconsistencies
- Show incorrect information to users (e.g., wrong prices, wrong P&L)
- Make debugging impossible (you don't know what data is real vs fake)
- Violate single-source-of-truth principle

**Examples of FORBIDDEN fallbacks**:
```python
# ❌ WRONG - Fallback hides missing price data
mark_px = pos.get('markPx', entry_px)  # Shows entry price as current price!

# ❌ WRONG - Fallback hides None values
account_value = margin.get('accountValue') or '0'

# ❌ WRONG - Fallback estimates instead of getting real data
market_value = quantity * avg_cost if not current_price else quantity * current_price
```

**CORRECT approach - Fail fast or show None**:
```python
# ✅ CORRECT - Get real current price, no fallback
current_price = all_mids.get(coin)
if current_price is None:
    logger.warning(f"No current price for {coin}")
    # Show None to user - don't make up data!
    last_price = None
    market_value = None
else:
    last_price = float(current_price)
    market_value = quantity * last_price

# ✅ CORRECT - Validate required data exists
if not user_state or 'marginSummary' not in user_state:
    raise ValueError("Failed to fetch data from Hyperliquid")  # Fail fast!
```

**When you see a fallback**:
1. Ask: "Why is the primary value missing?"
2. Fix the root cause (fetch from correct source)
3. If data truly unavailable, show None/null to user
4. Never invent or estimate values

## 🗑️ AGGRESSIVE CODE CLEANUP - DELETE OBSOLETE CODE IMMEDIATELY

**CRITICAL RULE**: When you identify obsolete code, files, or documentation - **DELETE IT IMMEDIATELY**.

**Why**:
- User does NOT want to keep historical code in the codebase
- Git commits are sufficient for history tracking
- Obsolete code confuses developers and adds cognitive load
- Clean codebase = faster navigation and better understanding

**What to delete**:
1. **Obsolete files** (old implementations, deprecated scripts)
2. **Obsolete functions/classes** (replaced by new architecture)
3. **Obsolete comments/documentation** (explaining removed features)
4. **Obsolete database columns** (via migration, immediately)
5. **Obsolete imports** (unused dependencies)
6. **Obsolete test files** (testing removed features)

**When to delete**:
- ✅ **IMMEDIATELY** when you identify obsolete code during refactoring
- ✅ **BEFORE committing** new changes (clean up first)
- ✅ **DURING code review** if you spot unused code
- ❌ **NEVER** comment out code "just in case" - delete it!

**Examples**:

```python
# ❌ WRONG - Commenting out obsolete code
# def old_function():
#     # Old implementation, replaced by new_function()
#     pass

# ✅ CORRECT - Just delete it
# (nothing - the function is gone)
```

```python
# ❌ WRONG - Keeping obsolete fields with comments
class Account:
    # DEPRECATED: Use Hyperliquid API instead
    initial_capital = Column(Numeric)
    current_cash = Column(Numeric)

# ✅ CORRECT - Delete the fields entirely
class Account:
    # (fields removed - fetch from Hyperliquid API)
```

**Workflow**:
1. Identify obsolete code (grep, search, manual inspection)
2. Verify it's truly unused (check references)
3. **DELETE** the code (file, function, field, etc.)
4. Run tests to ensure nothing breaks
5. Commit with clear message: "Remove obsolete X"

**User's preference**:
> "Non mi interessa tenere traccia di storico, tanto ci sono i commit"
> Translation: "I don't care about keeping history, commits are enough"

**Action**: When refactoring or identifying obsolete code, **delete it immediately** - do NOT ask for permission.

## 🚫 NEVER DISABLE SCHEDULED AGENTS FOR DEBUGGING

**CRITICAL RULE**: NEVER comment out or disable scheduled agents in `main.py`.

**Why this is critical**:
- Disabled agents mean no trading, no stop-loss, no take-profit
- System appears to work but positions sit idle without management
- Can result in significant financial losses from unmonitored positions

**What happened (2025-11-20)**:
- 3 agents were disabled "for debugging" and never re-enabled
- `stop_loss_check`, `ai_crypto_trade`, `take_profit_check`
- System stopped trading and managing positions for days

**If you need to debug agents**:
1. **Use environment variables** to conditionally disable (e.g., `DEBUG_DISABLE_TRADING=true`)
2. **Add logging** instead of commenting out code
3. **Use separate test account** for debugging
4. **NEVER commit disabled agents** to main branch
5. **If you must disable**: Add TODO comment with date and reason, re-enable same session

**Pre-commit checklist** (grep for these patterns):
```bash
grep -n "DISABLED" backend/main.py  # Should return 0 results
grep -n "# scheduler" backend/main.py  # Check for commented schedulers
```

**Active agents that MUST be running** (in `main.py`):
- `stop_loss_check` (5 min)
- `ai_crypto_trade` (3 min)
- `take_profit_check` (5 min)
- `periodic_sync_job` (30 sec)
- `daily_ai_usage_reset` (daily)

## 🚨 MANDATORY ERROR HANDLING PATTERNS

**CRITICAL RULE**: ALWAYS use `exc_info=True` when logging exceptions.

**Status**: 42 files identified (2025-11-04 analysis) requiring remediation.

### Pattern Checklist (verify EVERY exception handler)

✅ **CORRECT - Full stack trace**:
```python
try:
    await hyperliquid_trading_service.get_user_state_async()
except HyperliquidAPIError as e:  # Specific exception
    logger.error(
        "Hyperliquid API call failed",
        extra={"context": {"account_id": account_id}},
        exc_info=True  # ← MANDATORY for stack trace
    )
    raise  # Re-raise or return error response
```

❌ **WRONG - No stack trace**:
```python
except Exception as e:
    logger.error(f"Error: {e}")  # Missing exc_info=True!
```

❌ **WRONG - Silent failure**:
```python
except Exception:
    pass  # Hides bugs - NEVER do this!
```

❌ **WRONG - Too generic**:
```python
except Exception as e:  # Catch specific exceptions instead
    logger.error(f"Error: {e}", exc_info=True)
```

### Best Practice Reference

Real-world example from codebase (`backend/api/ws_async.py:720-757`):
```python
except Exception as e:
    logger.error(
        "WebSocket message handling error",
        extra={
            "context": {
                "message_type": kind,
                "account_id": account_id,
                "error": str(e),
            }
        },
        exc_info=True  # ✅ Perfect!
    )
```

### Action Items for Code Cleanup

**IDENTIFIED**: 42 files need remediation.

**Process** (apply when touching any file):
1. Review ALL exception handlers in the file
2. Add `exc_info=True` to ALL logger.error/logger.exception calls
3. Replace `except Exception` with specific exception types where possible
4. Remove silent `pass` blocks - log or re-raise instead

**Files Requiring Update** (priority order):
- `api/*_routes.py` (10 files) - User-facing, needs best error messages
- `services/*.py` (20+ files) - Core business logic
- `scripts/maintenance/*.py` (8 files) - Lower priority
- `scripts/testing/*.py` (4 files) - Lower priority

## 🎯 SOURCE OF TRUTH HIERARCHY

**CRITICAL**: This project has MULTIPLE data sources. Follow this hierarchy to avoid inconsistencies.

### 1. Real-time Trading Data → Hyperliquid API (ALWAYS)
- Account balance (accountValue, totalMarginUsed, withdrawable)
- Position sizes, entry prices, PNL, leverage
- Order status, fills
- Current market prices (mids)

**Rule**: NEVER read balance/positions from database for real-time display. DB is for metadata only.

### 2. Historical Snapshots → PortfolioSnapshot table
- Used ONLY for portfolio charts (5-minute intervals)
- Source: Captured FROM Hyperliquid API via scheduled job
- Never modify manually - read-only for charting

### 3. Account Metadata → Account table
- AI model config (model, base_url, api_key)
- Account name, type, active status
- User relationships
- NO balance data stored here (deprecated fields removed)

### 4. Trade History → Database tables (Order, Trade, Position)
- Synced FROM Hyperliquid via `hyperliquid_sync_service`
- Used for historical analysis, NOT real-time display
- Sync lag: up to 60 seconds (configured interval)

### Decision Matrix: "Where do I get this data?"

| Data Type | Source | Method | Cache OK? | File Reference |
|-----------|--------|--------|-----------|----------------|
| Current balance | Hyperliquid API | `get_user_state_async()` | No (real-time) | `backend/services/trading/hyperliquid_trading_service.py:68-89` |
| Current price | Hyperliquid API | `get_all_mids_async()` | Yes (2 min TTL) | `backend/services/market_data/price_cache.py` |
| Position size | Hyperliquid API | `get_user_state_async()` | No (real-time) | `backend/api/ws_async.py:215-289` |
| Historical chart | DB (PortfolioSnapshot) | `get_snapshots_for_chart()` | Yes (immutable) | `backend/services/portfolio_snapshot_service.py` |
| Trade history | DB (Trade) | `get_trades_by_account()` | Yes (historical) | `backend/repositories/trade_repo.py` |
| Account config | DB (Account) | `get_account_by_id()` | Yes (rarely changes) | `backend/repositories/account_repo.py` |

### Anti-Pattern Examples (DO NOT DO THIS):

❌ **Reading balance from DB**:
```python
# WRONG - DB balance fields are DEPRECATED and REMOVED
account_value = account.current_cash + account.frozen_cash  # Fields don't exist!
```

✅ **Reading balance from Hyperliquid**:
```python
# CORRECT - Always from API
user_state = await hyperliquid_trading_service.get_user_state_async()
account_value = float(user_state['marginSummary']['accountValue'])
```

❌ **Using fallback/stale prices**:
```python
# WRONG - Shows wrong price to user
last_price = pos.get('markPx', entry_px)  # Fallback to entry price!
```

✅ **Fetch current price or show None**:
```python
# CORRECT - Real current price or explicit None
all_mids = await hyperliquid_trading_service.get_all_mids_async()
current_price = all_mids.get(coin)
if current_price is None:
    logger.warning(f"No current price for {coin}")
    last_price = None  # Don't invent data!
```

## 🐛 DEBUG WORKFLOW

**When things go wrong, follow this systematic approach:**

### Step 1: Identify Error Location (use structured logs)

```bash
# Filter logs by request_id (from X-Request-ID header)
grep "request_id.*abc123" logs.json | jq '.exception.stack_trace'

# Filter by operation type
grep "operation.*place_order" logs.json | jq '.exception'

# Filter by account_id
grep "account_id.*1" logs.json | jq 'select(.level=="ERROR")'
```

**Log locations** (depending on deployment):
- Development: stdout (visible in terminal)
- Production: JSON logs in structured format

### Step 2: Check Service Health

```bash
# Overall health check
curl http://localhost:8000/api/health

# Detailed readiness check (tests DB + Hyperliquid API)
curl http://localhost:8000/api/readiness
```

**Expected response (healthy)**:
```json
{
  "ready": true,
  "checks": {
    "database": "ok",
    "hyperliquid_api": "ok",
    "environment": "ok"
  },
  "message": "System ready"
}
```

### Step 3: Verify Source of Truth

**When debugging data inconsistencies, ALWAYS check Hyperliquid first:**

```bash
# 1. Check real balance from Hyperliquid (source of truth)
cd backend/
python3 -c "
import asyncio
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

async def check():
    state = await hyperliquid_trading_service.get_user_state_async()
    print(f'Real balance: \${state[\"marginSummary\"][\"accountValue\"]}')
    print(f'Margin used: \${state[\"marginSummary\"][\"totalMarginUsed\"]}')
    print(f'Withdrawable: \${state[\"marginSummary\"][\"withdrawable\"]}')

asyncio.run(check())
"
```

```bash
# 2. Check DB snapshots (should match Hyperliquid after sync)
sqlite3 backend/data.db "
SELECT
    datetime(snapshot_time) as time,
    total_assets,
    withdrawable,
    total_margin_used
FROM portfolio_snapshots
ORDER BY snapshot_time DESC
LIMIT 5;
"
```

```bash
# 3. If mismatch detected → force sync
curl -X POST http://localhost:8000/api/sync/account/1
```

### Step 4: Common Issues & Solutions

| Symptom | Root Cause | Solution | File Reference |
|---------|------------|----------|----------------|
| Balance shows $0 | Fallback value used | Remove `or '0'` pattern | Search codebase for `or '0'` |
| Positions missing | Not synced from Hyperliquid | Run manual sync | `backend/api/sync_routes.py` |
| Prices stale | Price cache expired | Check cleanup job running | `backend/services/startup.py:35-41` |
| Chart empty | No snapshots yet | Wait 5 min or run snapshot job | `backend/services/portfolio_snapshot_service.py` |
| API timeouts | Hyperliquid API slow/down | Check circuit breaker | `backend/services/trading/hyperliquid_sync_service.py` |
| WebSocket disconnects | Client network issue | Check connection manager | `backend/api/ws_async.py:720-757` |

### Step 5: Check Background Jobs

```bash
# Verify scheduled jobs are running
ps aux | grep uvicorn  # Main app process

# Check job execution logs
grep "portfolio_snapshot_capture\|hyperliquid_sync\|price_cache_cleanup" logs.json
```

**Expected job frequency**:
- `price_cache_cleanup`: Every 2 minutes (startup.py)
- `hyperliquid_sync`: Every 30 seconds (main.py)
- `portfolio_snapshot_capture`: Every 5 minutes (startup.py)
- `ai_crypto_trade`: Every 3 minutes (main.py)
- `stop_loss_check`: Every 5 minutes (main.py)
- `take_profit_check`: Every 5 minutes (main.py)
- `hourly_market_retrospective`: Every 1 hour (startup.py)

## 📂 QUICK REFERENCE - Critical Files & Documentation

**When debugging, start with these resources:**

### 📚 Documentation (START HERE)
- **[docs/README.md](docs/README.md)** - Complete documentation index
- **[docs/architecture/SYSTEM_ORCHESTRATION.md](docs/architecture/SYSTEM_ORCHESTRATION.md)** - How the system works operationally
- **[docs/operations/SCHEDULED_JOBS.md](docs/operations/SCHEDULED_JOBS.md)** - All background jobs, intervals, dependencies
- **[docs/operations/MONITORING.md](docs/operations/MONITORING.md)** - Debug workflows, troubleshooting

### 🔴 Core Trading Logic (MOST CRITICAL)
- `backend/services/trading/hyperliquid_trading_service.py:20-90` - Hyperliquid SDK wrapper (async)
- `backend/services/trading/hyperliquid_sync_service.py` - Sync logic (balance, positions, trades)
- `backend/services/auto_trader.py` - AI decision execution
- `backend/services/trading_commands.py:54-79` - Order execution & post-trade sync

### 🟡 API Endpoints (USER-FACING)
- `backend/api/accounts_async.py` - Account management
- `backend/api/ws_async.py:215-289` - WebSocket real-time data (**uses Hyperliquid directly!**)
- `backend/api/health_routes.py:128-202` - Health/readiness checks
- `backend/api/sync_routes.py` - Manual sync triggers

### 🟢 Data Models & Database
- `backend/database/models.py:78-138` - Account model (**metadata only, NO balance**)
- `backend/database/connection.py:76-117` - Async session factory + `get_db()` dependency
- `backend/repositories/account_repo.py` - Account CRUD operations
- `backend/repositories/position_repo.py` - Position CRUD operations

### 🔵 Configuration & Logging
- `backend/config/settings.py:10-97` - Pydantic settings (env vars)
- `backend/config/logging.py:13-72` - Structured JSON logger (with exc_info support!)

### 🟣 Testing
- `backend/tests/integration/test_api_integration.py:130-205` - API endpoint tests
- `backend/tests/unit/test_hyperliquid_sync_service.py` - Sync service tests
- `backend/tests/integration/test_sync_integration.py:459-513` - Transaction rollback tests

### ⚪ Startup & Scheduling
- `backend/main.py:12-84` - FastAPI lifespan (startup/shutdown sequence)
- `backend/services/startup.py:12-71` - **Service initialization order (CRITICAL!)**
- `backend/services/infrastructure/scheduler.py:23-38` - APScheduler wrapper

### 🔧 SERVICE INITIALIZATION ORDER (CRITICAL - DO NOT CHANGE!)

**Two-phase initialization**: `startup.py` (task_scheduler) + `main.py` (APScheduler)

**Phase 1: `backend/services/startup.py` (task_scheduler)**
```python
def initialize_services():
    # 1. MUST be first - other services depend on scheduler
    start_scheduler()

    # 2. Schedule AI trading (uses place_multi_agent_order)
    schedule_auto_trading(interval_seconds=180)

    # 3. Independent interval tasks (order doesn't matter)
    task_scheduler.add_interval_task(clear_expired_prices)          # Every 2 min
    task_scheduler.add_interval_task(capture_snapshots_wrapper)     # Every 5 min
    task_scheduler.add_interval_task(analyze_hourly_market_sync)    # Every 1 hour
```

**Phase 2: `backend/main.py` (APScheduler)**
```python
# Trading-critical jobs in main.py lifespan
scheduler_service.add_sync_job(periodic_sync_job, interval_seconds=30)      # hyperliquid_sync
scheduler_service.add_sync_job(check_stop_loss_async, interval_seconds=300) # stop_loss_check
scheduler_service.add_sync_job(check_take_profit_async, interval_seconds=300) # take_profit_check
```

**Why this split matters**:
- task_scheduler: Threading-based (works for learning/analysis jobs)
- APScheduler: Async-friendly (works for trading/sync jobs)
- Both run independently without conflicts

## ⏰ SCHEDULED JOBS & API RATE LIMITING

**Context**: Hyperliquid API has rate limits (~10-20 requests/sec). Too many concurrent calls trigger `429 Too Many Requests` errors.

### Active Scheduled Jobs

| Job | Interval | File | Purpose | API Calls |
|-----|----------|------|---------|-----------|
| AI Trading | 180s (3min) | `main.py` | DeepSeek LONG/SHORT agent decisions + execution | 2-3/cycle |
| Price Cache Cleanup | 120s (2min) | `startup.py` | Clear expired price cache | 0 (local) |
| Portfolio Snapshot | 300s (5min) | `startup.py` | Historical chart data | 1/cycle |
| Hourly Retrospective | 3600s (1h) | `startup.py` | Market analysis + dynamic corrections | 0.1/cycle |
| **Hyperliquid Sync** | **30s** | `main.py` | **Sync balance/positions/orders** | **1/cycle** |
| **Stop Loss Check** | **300s (5min)** | `main.py` | **Check -5% loss threshold** | **1/cycle** |
| **Take Profit Check** | **300s (5min)** | `main.py` | **Check take profit targets** | **1/cycle** |

**Total API Load**: ~3 calls/min = ~180 calls/hour

###**Optimization History (2025-11-09)**

**Removed duplicates**:
- ❌ `sync_all_active_accounts` (60s) - Duplicated `periodic_sync_job` in main.py
- ❌ `setup_market_tasks()` - Empty placeholder function

**Reduced frequency**:
- Stop loss check: 30s → 60s (less critical, still catches -5% losses quickly)

**Result**: **-40% API calls** → lower risk of rate limiting

### Rate Limiting Symptoms

```
ccxt.base.errors.RateLimitExceeded: hyperliquid POST https://api.hyperliquid.xyz/info 429 Too Many Requests
```

**What happens**: Orders delayed 2-5 minutes until rate limit clears (system retries automatically).

**When it happens**: When multiple jobs execute simultaneously (e.g., sync + stop-loss + AI trading + snapshot at same moment).

**Solution**: Already optimized. If still occurs, consider increasing stop-loss interval to 90-120s.

## 📚 MCP SERVER DOCUMENTATION

**See `.claudemcp.md`** for complete MCP server configuration and usage guidelines.

Quick reference for 4 active MCP servers:
1. **claude-context**: Semantic code search (re-index before each use!)
2. **context7**: Library documentation lookup
3. **playwright**: Browser automation and testing
4. **perplexity-ask**: Web research and best practices

**Critical Rule**: ALWAYS re-index claude-context with `force=true` and wait 15 seconds before searching.

## 🧠 LEARNING SYSTEM

**Status**: Hourly Market Retrospective (replaced Counterfactual Learning)

The original counterfactual learning system (24h feedback loop) was too slow for real-time trading optimization. It has been replaced by the **Hourly Market Retrospective** which provides faster feedback with dynamic corrections.

### Current: Hourly Market Retrospective

- **Interval**: Every 1 hour (`startup.py`)
- **Function**: `analyze_hourly_market_sync()` from `backend/services/learning/hourly_retrospective.py`
- **Purpose**: Analyzes market conditions and trading decisions from the past hour
- **Output**: Dynamic weight adjustments and pattern recognition

### Disabled: Original Counterfactual Learning

The original system (`decision_snapshot_service.py`, `deepseek_self_analysis_service.py`) is disabled.
- Too slow: Required 24h wait for exit price
- Decision snapshots table still exists but not actively used
- Code remains for potential future use but not scheduled


## 📚 DEPLOYMENT & INFRASTRUCTURE DOCUMENTATION

For comprehensive deployment analysis, infrastructure roadmap, and implementation checklists, see:
- **`backend/docs/DEPLOYMENT_ROADMAP.md`** - Complete deployment robustness analysis (600+ lines)
  - Current strengths and critical gaps
  - Prioritized implementation roadmap (Priority 1-3)
  - Prometheus/Grafana monitoring setup
  - CI/CD pipeline configuration
  - Database backup automation
  - Version tagging strategy
  - Secrets management

**Quick Summary of Critical Issues:**
1. ⚠️ CRITICAL: No database backups before migrations
2. ⚠️ CRITICAL: No version tagging (Docker images use `latest` only)
3. ⚠️ HIGH: No monitoring/alerting (Prometheus + Grafana needed)
4. ⚠️ HIGH: Secrets in plain text (migrate to Docker secrets)
5. ⚠️ MEDIUM: Manual local/production mutual exclusion

See full document for implementation details and code examples.

## 🌐 PRODUCTION VPS DEPLOYMENT

**VPS IP**: 46.224.45.196

**Deployment Command**:
```bash
./deploy_to_hetzner.sh 46.224.45.196
```

### 🚨 CRITICAL: Local vs Production Mutual Exclusion

**NEVER run local and production trading simultaneously!**

Both local development and production VPS connect to the same Hyperliquid account. Running both at the same time will cause:
- Double trading on the same account
- Conflicting AI decisions
- Race conditions in order execution
- Database inconsistencies

**Rules**:
1. **Before starting local development**: Stop production VPS
   ```bash
   ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml stop'
   ```

2. **Before deploying to production**: Stop local backend
   ```bash
   # Kill local uvicorn process
   pkill -f "uvicorn main:app"
   ```

3. **Check production status**:
   ```bash
   ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml ps'
   ```

4. **Start/Stop commands**:
   ```bash
   # Stop production
   ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml stop'

   # Start production
   ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml start'

   # View production logs
   ssh root@46.224.45.196 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs -f'
   ```

**Why this matters**:
- Same Hyperliquid private key shared between local and production
- Both will try to place orders simultaneously
- Both will sync from same Hyperliquid account
- Results in unpredictable behavior and potential losses

<!-- MANUAL ADDITIONS END -->
