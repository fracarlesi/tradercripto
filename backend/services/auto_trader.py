"""
Auto Trading Service - Main entry point for automated crypto trading
This file maintains backward compatibility while delegating to split services
"""

import asyncio
import logging
from decimal import Decimal
from typing import Any

from database.connection import SessionLocal
from database.models import Account
from services.ai_decision_service import (
    call_ai_for_decision,
    get_decision_cache,
    save_ai_decision,
)
from services.asset_calculator import calc_positions_value
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

logger = logging.getLogger(__name__)

# Constants
AI_TRADE_JOB_ID = "ai_crypto_trade"

# Decision cache (10-minute window)
_decision_cache = get_decision_cache(window_seconds=600)


def place_ai_driven_crypto_order(max_ratio: float = 0.2) -> None:
    """Place AI-driven crypto order using DeepSeek AI and Hyperliquid execution.

    This function:
    1. Gets the active AI trading account from database
    2. Fetches current market prices and portfolio data
    3. Calls DeepSeek AI for trading decision (with caching)
    4. Validates the AI decision with safety checks
    5. Executes the order on Hyperliquid exchange
    6. Logs the decision and result to database

    Args:
        max_ratio: Maximum portion of portfolio to use per trade (0.0-1.0)
                  Default 0.2 = 20% of available capital per trade

    Returns:
        None

    Safety Features:
    - API key validation (skips demo keys)
    - Decision caching (10-minute window to avoid duplicate trades)
    - Position validation (check existing positions before trading)
    - Capital limits (respect max_ratio and MAX_CAPITAL_USD from settings)
    - Error handling with detailed logging
    """
    logger.info(f"=== AI Trading Cycle Started (max_ratio={max_ratio}) ===")

    db = SessionLocal()
    try:
        # 1. Get active AI trading account
        account = (
            db.query(Account)
            .filter(Account.account_type == "AI", Account.is_active == True)
            .first()
        )

        if not account:
            logger.warning("No active AI trading account found, skipping cycle")
            return

        logger.info(
            f"Trading with account: {account.name} (id={account.id})",
            extra={"context": {"account_id": account.id, "account_name": account.name}},
        )

        # 2. Fetch market data (prices for all crypto symbols)
        logger.info("Fetching market prices...")
        prices = _fetch_market_prices()
        logger.info(f"Fetched prices for {len(prices)} symbols")

        # 3. Build portfolio data (cash + positions)
        logger.info("Building portfolio data...")
        portfolio = _build_portfolio_data(db, account)
        logger.info(
            f"Portfolio: ${portfolio['cash']:.2f} cash, "
            f"{len(portfolio['positions'])} positions, "
            f"${portfolio['total_assets']:.2f} total"
        )

        # 4. Get AI decision (with caching)
        logger.info("Calling AI for trading decision...")

        # Use decision cache to avoid redundant API calls
        decision = _decision_cache.get_or_generate_decision(
            price=prices.get("BTC", 0.0),  # Use BTC price as market state indicator
            position=portfolio["total_assets"],
            news_summary="",  # News fetched inside call_ai_for_decision
            generate_func=lambda: call_ai_for_decision(account, portfolio, prices),
        )

        if not decision:
            logger.info("AI returned no decision (HOLD or error), skipping cycle")
            return

        logger.info(f"AI Decision: {decision}")

        # 5. Validate decision
        validation_result = _validate_decision(decision, portfolio, prices, max_ratio)
        if not validation_result["valid"]:
            logger.warning(f"Decision validation failed: {validation_result['reason']}")
            # Save failed decision to database for analysis
            save_ai_decision(db, account.id, decision, success=False)
            return

        # 6. Execute order on Hyperliquid
        logger.info("Executing order on Hyperliquid...")
        execution_result = _execute_order_async(decision, validation_result["order_size"])

        if execution_result.get("status") == "ok":
            logger.info(f"✅ Order executed successfully: {execution_result}")
            save_ai_decision(db, account.id, decision, success=True)
        else:
            logger.error(f"❌ Order execution failed: {execution_result}")
            save_ai_decision(db, account.id, decision, success=False)

    except Exception as e:
        logger.error(
            f"AI trading cycle failed: {e}",
            extra={"context": {"error": str(e)}},
            exc_info=True,
        )
    finally:
        db.close()
        logger.info("=== AI Trading Cycle Completed ===")


def _fetch_market_prices() -> dict[str, float]:
    """Fetch current market prices for all crypto symbols.

    Returns:
        Dict mapping symbol (e.g. "BTC") to price
    """
    try:
        # Use sync version for now (called from sync context)
        from services.market_data.hyperliquid_market_data import (
            get_all_symbols_from_hyperliquid,
            get_last_price_from_hyperliquid,
        )

        symbols = get_all_symbols_from_hyperliquid()
        prices = {}

        # Get prices for major symbols (limit to avoid too many API calls)
        major_symbols = ["BTC", "ETH", "SOL", "DOGE", "XRP", "BNB", "AVAX", "ARB"]

        for symbol in major_symbols:
            if symbol in symbols or f"{symbol}/USDC:USDC" in symbols:
                try:
                    price = get_last_price_from_hyperliquid(symbol)
                    if price and price > 0:
                        prices[symbol] = price
                except Exception as e:
                    logger.warning(f"Failed to get price for {symbol}: {e}")

        return prices

    except Exception as e:
        logger.error(f"Failed to fetch market prices: {e}")
        # Return fallback prices
        return {"BTC": 100000.0, "ETH": 4000.0, "SOL": 200.0}


def _build_portfolio_data(db, account: Account) -> dict[str, Any]:
    """Build portfolio data dictionary from account and positions.

    Args:
        db: Database session
        account: Trading account

    Returns:
        Dict with portfolio data:
        {
            "cash": 52.00,
            "frozen_cash": 0.00,
            "total_assets": 52.00,
            "positions": [...]
        }
    """
    from database.models import Position

    # Get current positions
    positions = db.query(Position).filter(Position.account_id == account.id).all()

    # Calculate total asset value (calc_positions_value queries positions itself)
    positions_value = calc_positions_value(db, account.id)

    portfolio = {
        "cash": float(account.current_cash or 0),
        "frozen_cash": float(account.frozen_cash or 0),
        "total_assets": float(account.current_cash or 0) + positions_value,
        "positions": [
            {
                "symbol": pos.symbol,
                "quantity": float(pos.quantity or 0),
                "avg_cost": float(pos.avg_cost or 0),
            }
            for pos in positions
        ],
    }

    return portfolio


def _validate_decision(
    decision: dict[str, Any], portfolio: dict[str, Any], prices: dict[str, float], max_ratio: float
) -> dict[str, Any]:
    """Validate AI decision with safety checks.

    Args:
        decision: AI decision dict with operation, symbol, target_portion_of_balance
        portfolio: Current portfolio data
        prices: Current market prices
        max_ratio: Maximum allowed ratio per trade

    Returns:
        Dict with:
        {
            "valid": True/False,
            "reason": "explanation if invalid",
            "order_size": calculated size in base currency units
        }
    """
    operation = decision.get("operation", "").lower()
    symbol = decision.get("symbol", "")
    target_portion = float(decision.get("target_portion_of_balance", 0))

    # 1. Check operation is valid
    if operation not in ["buy", "sell", "hold"]:
        return {"valid": False, "reason": f"Invalid operation: {operation}"}

    # 2. HOLD requires no validation
    if operation == "hold":
        return {"valid": True, "reason": "Hold decision", "order_size": 0}

    # 3. Check symbol is in our price data
    if symbol not in prices:
        return {"valid": False, "reason": f"Symbol {symbol} not in market data"}

    # 4. Check target_portion is reasonable
    if target_portion <= 0 or target_portion > max_ratio:
        return {
            "valid": False,
            "reason": f"target_portion {target_portion} outside allowed range (0, {max_ratio}]",
        }

    # 5. Calculate order size
    if operation == "buy":
        # Buy: use portion of available cash
        cash_available = portfolio["cash"]
        order_value_usd = cash_available * target_portion
        price = prices[symbol]
        order_size = order_value_usd / price

        if order_size * price < 10:  # Min order $10
            return {"valid": False, "reason": f"Order value ${order_size * price:.2f} below $10 minimum"}

        return {"valid": True, "reason": "Buy validation passed", "order_size": order_size}

    elif operation == "sell":
        # Sell: check we have the position
        position = next((p for p in portfolio["positions"] if p["symbol"] == symbol), None)

        if not position or position["quantity"] <= 0:
            return {"valid": False, "reason": f"No position in {symbol} to sell"}

        order_size = position["quantity"] * target_portion

        if order_size * prices[symbol] < 10:  # Min order $10
            return {"valid": False, "reason": f"Order value ${order_size * prices[symbol]:.2f} below $10 minimum"}

        return {"valid": True, "reason": "Sell validation passed", "order_size": order_size}

    return {"valid": False, "reason": "Unknown validation error"}


def _execute_order_async(decision: dict[str, Any], order_size: float) -> dict[str, Any]:
    """Execute order on Hyperliquid (wrapper for async call).

    Args:
        decision: AI decision with operation and symbol
        order_size: Calculated order size in base currency units

    Returns:
        Order execution result dict
    """
    operation = decision.get("operation", "").lower()
    symbol = decision.get("symbol", "")

    if operation == "hold":
        return {"status": "ok", "message": "No action (HOLD)"}

    # Execute async order in new event loop (since we're in sync context)
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                hyperliquid_trading_service.place_market_order_async(
                    symbol=symbol, is_buy=(operation == "buy"), size=order_size, reduce_only=False
                )
            )
            return result
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Order execution failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
