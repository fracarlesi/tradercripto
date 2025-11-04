# trader_bitcoin Development Guidelines

Auto-generated from all feature plans. Last updated: 2025-11-03

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
# Get real balance from Hyperliquid
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service
user_state = await hyperliquid_trading_service.get_user_state_async()
real_balance = float(user_state['marginSummary']['accountValue'])

# Update database with real balance
account.current_cash = real_balance
db.commit()
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

## 🔍 EXCEPTION HANDLING & LOGGING - IDENTIFIED IMPROVEMENTS

**Status**: Identified during 2025-11-04 refactoring analysis

**Issue**: 42 files contain `except Exception as e` patterns that may lack proper logging with stack traces.

**Files Requiring Review**:
- api/*_routes.py (10 files)
- services/*.py (20+ files)
- scripts/maintenance/*.py (8 files)
- scripts/testing/*.py (4 files)

**Best Practices to Apply**:
1. **Always include exc_info=True** in error logs:
   ```python
   # ❌ WRONG - No stack trace
   except Exception as e:
       logger.error(f"Error: {e}")

   # ✅ CORRECT - Full stack trace
   except Exception as e:
       logger.error(f"Error: {e}", exc_info=True)
   ```

2. **Catch specific exceptions** instead of bare `Exception`:
   ```python
   # ❌ WRONG - Too broad
   except Exception as e:
       pass

   # ✅ CORRECT - Specific exceptions
   except (ValueError, KeyError, TypeError) as e:
       logger.error(f"Data error: {e}", exc_info=True)
   except HyperliquidAPIError as e:
       logger.error(f"API error: {e}", exc_info=True)
   ```

3. **Fail fast** - Don't silently catch and continue:
   ```python
   # ❌ WRONG - Silent failure
   except Exception:
       pass  # Hides bugs

   # ✅ CORRECT - Log and re-raise or return error
   except Exception as e:
       logger.error(f"Critical error: {e}", exc_info=True)
       raise  # Or return error response
   ```

**TODO**: Systematic review of identified 42 files to apply best practices (future task)

## 📚 MCP SERVER DOCUMENTATION

**See `.claudemcp.md`** for complete MCP server configuration and usage guidelines.

Quick reference for 4 active MCP servers:
1. **claude-context**: Semantic code search (re-index before each use!)
2. **context7**: Library documentation lookup
3. **playwright**: Browser automation and testing
4. **perplexity-ask**: Web research and best practices

**Critical Rule**: ALWAYS re-index claude-context with `force=true` and wait 15 seconds before searching.

<!-- MANUAL ADDITIONS END -->
