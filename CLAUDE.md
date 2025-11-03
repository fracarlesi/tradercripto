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

<!-- MANUAL ADDITIONS END -->
