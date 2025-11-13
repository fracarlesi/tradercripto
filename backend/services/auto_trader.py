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
from services.learning import save_decision_snapshot
from services.technical_analysis_service import calculate_technical_factors
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
    - Capital limits (respect max_ratio per trade, default 20%)
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

        # 3.5. Pre-filter top 20 coins by hourly momentum (NEW: Fast momentum trading)
        logger.info("=" * 60)
        logger.info("STEP 1: Pre-filtering top 20 coins by hourly momentum")
        logger.info("=" * 60)

        from services.market_data.hourly_momentum import get_top_momentum_symbols

        # Get top 20 coins with highest hourly momentum
        top_momentum_symbols = asyncio.run(get_top_momentum_symbols(limit=20))
        logger.info(f"✅ Pre-filtered to {len(top_momentum_symbols)} coins with best hourly momentum")

        # 3.6. Calculate technical factors ONLY for top momentum coins (not all 220+)
        logger.info("=" * 60)
        logger.info("STEP 2: Technical analysis on top momentum coins")
        logger.info("=" * 60)

        technical_factors = calculate_technical_factors(top_momentum_symbols)
        logger.info(
            f"Technical analysis: {len(technical_factors.get('recommendations', []))} symbols analyzed"
        )

        # Add technical factors to portfolio data for AI
        # (New tokens are now captured by hourly momentum - no separate detection needed)
        portfolio["technical_factors"] = technical_factors

        # 4. Get AI decision (with caching)
        logger.info("Calling AI for trading decision...")

        # Use decision cache to avoid redundant API calls
        # NOTE: call_ai_for_decision is now async (for pivot points calculation)
        decision = _decision_cache.get_or_generate_decision(
            price=prices.get("BTC", 0.0),  # Use BTC price as market state indicator
            position=portfolio["total_assets"],
            news_summary="",  # News fetched inside call_ai_for_decision
            generate_func=lambda: asyncio.run(call_ai_for_decision(account, portfolio, prices)),
        )

        if not decision:
            logger.info("AI returned no decision (HOLD or error), skipping cycle")
            return

        logger.info(f"AI Decision: {decision}")

        # Save decision snapshot for counterfactual learning (even for HOLD decisions)
        try:
            # Build indicators snapshot with available data
            indicators_snapshot = {
                "technical_factors": technical_factors,
                "prices": prices,
                "portfolio_value": portfolio.get("total_assets", 0),
                "available_cash": portfolio.get("available_cash", 0),
            }

            # Get symbol and price for snapshot
            symbol = decision.get("symbol", "BTC")
            entry_price = prices.get(symbol, 0.0)

            # Map operation to decision format (BUY -> LONG, SELL -> HOLD, etc.)
            operation = decision.get("operation", "hold").lower()
            actual_decision = "LONG" if operation == "buy" else "SHORT" if operation == "short" else "HOLD"

            # Save snapshot asynchronously
            asyncio.run(
                save_decision_snapshot(
                    account_id=account.id,
                    symbol=symbol,
                    indicators_snapshot=indicators_snapshot,
                    deepseek_reasoning=decision.get("reason", "No reasoning provided"),
                    actual_decision=actual_decision,
                    actual_size_pct=decision.get("target_portion_of_balance", 0.0),
                    entry_price=entry_price,
                )
            )

            logger.info(
                f"✅ Decision snapshot saved: {symbol} {actual_decision} @ ${entry_price:.2f}"
            )

        except Exception as e:
            # Don't fail the trade if snapshot save fails
            logger.error(
                f"Failed to save decision snapshot: {e}",
                extra={"context": {"account_id": account.id, "error": str(e)}},
                exc_info=True,
            )

        # 5. Check margin safety BEFORE opening new positions (FIX 5)
        operation = decision.get("operation", "").lower()
        if operation in ["buy", "short"]:
            # Only check margin for new positions (not for sell/hold)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                margin_check = loop.run_until_complete(_check_margin_safety(margin_limit=0.70))
            finally:
                loop.close()

            if not margin_check["safe"]:
                logger.warning(
                    f"Margin safety check failed: {margin_check['reason']}. "
                    f"Blocking new position. Utilization: {margin_check['margin_utilization']:.1%}"
                )
                # Save failed decision to database for analysis
                save_ai_decision(db, account, decision, portfolio, executed=False)
                return

        # 6. Validate decision
        validation_result = _validate_decision(decision, portfolio, prices, max_ratio)
        if not validation_result["valid"]:
            logger.warning(f"Decision validation failed: {validation_result['reason']}")
            # Save failed decision to database for analysis
            save_ai_decision(db, account, decision, portfolio, executed=False)
            return

        # 7. Execute order on Hyperliquid
        logger.info("Executing order on Hyperliquid...")
        leverage = validation_result.get("leverage", 1)
        execution_result = _execute_order_async(decision, validation_result["order_size"], leverage)

        # Check if order was actually executed (not just HTTP success)
        is_executed = False
        if execution_result.get("status") == "ok":
            # Check for Hyperliquid errors in response
            response = execution_result.get("response", {})
            if response.get("type") == "order":
                statuses = response.get("data", {}).get("statuses", [])
                # If there are errors in statuses, order was rejected
                has_errors = any(s.get("error") for s in statuses)
                if has_errors:
                    error_msg = statuses[0].get("error", "Unknown error")
                    logger.warning(f"⚠️ Order rejected by Hyperliquid: {error_msg}")
                    is_executed = False
                else:
                    # No errors, order was accepted
                    is_executed = True
            elif execution_result.get("message") == "No action (HOLD)":
                # HOLD is considered "executed" (decision was applied)
                is_executed = True

        if is_executed:
            logger.info(f"✅ Order executed successfully: {execution_result}")
            save_ai_decision(db, account, decision, portfolio, executed=True)

            # 8. Assign trading strategy to newly opened position (only for BUY/SHORT)
            operation = decision.get("operation", "").lower()
            if operation in ["buy", "short"]:
                try:
                    from services.trading.strategy_tracker import assign_strategy_to_position

                    symbol = decision.get("symbol")
                    technical_factors = portfolio.get("technical_factors", {})

                    # CRITICAL: Sync positions from Hyperliquid BEFORE assigning strategy
                    # Position may not exist in DB yet (scheduled sync runs every 60s)
                    logger.info("Syncing positions from Hyperliquid to assign strategy...")
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        sync_result = loop.run_until_complete(
                            hyperliquid_trading_service.sync_account_to_database_async(db, account)
                        )
                        if sync_result.get("success"):
                            logger.info(f"Post-trade sync completed: {sync_result.get('positions', 0)} positions synced")
                        else:
                            logger.warning(f"Post-trade sync failed: {sync_result.get('error', 'Unknown error')}")
                    finally:
                        loop.close()

                    # Extract technical data for this symbol
                    recommendations = technical_factors.get("recommendations", [])
                    symbol_data = next((r for r in recommendations if r["symbol"] == symbol), None)

                    if symbol_data:
                        # Prepare technical_data dict for strategy classification
                        technical_data = {
                            "technical_score": symbol_data.get("score", 0.0),
                            "momentum": symbol_data.get("momentum", 0.0),
                            "support": symbol_data.get("support", 0.0),
                        }

                        # Assign strategy (this updates the Position record in DB)
                        strategy_type = assign_strategy_to_position(
                            db=db,
                            account_id=account.id,
                            symbol=symbol,
                            technical_data=technical_data,
                            sentiment=None,  # TODO: Add sentiment from Fear & Greed Index
                            prophet_trend=None,  # TODO: Add from Prophet forecast
                        )

                        if strategy_type:
                            logger.info(f"✅ Strategy {strategy_type} assigned to position {symbol}")
                        else:
                            logger.warning(f"⚠️ Failed to assign strategy to {symbol} (position not found)")
                    else:
                        logger.warning(f"⚠️ No technical data for {symbol}, cannot assign strategy")

                except Exception as e:
                    # Don't fail the trade if strategy assignment fails
                    logger.error(
                        f"Failed to assign strategy to position {symbol}: {e}",
                        extra={"context": {"account_id": account.id, "symbol": symbol}},
                        exc_info=True,
                    )
        else:
            logger.error(f"❌ Order execution failed or rejected: {execution_result}")
            save_ai_decision(db, account, decision, portfolio, executed=False)

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
    """Fetch current market prices for ALL crypto symbols in ONE API call.

    Uses Hyperliquid's all_mids() endpoint - the most efficient method.
    Returns 468+ prices in a single call instead of 63+ separate calls.

    Returns:
        Dict mapping symbol (e.g. "BTC") to price
    """
    try:
        from services.market_data.hyperliquid_market_data import get_all_prices_from_hyperliquid

        # Get ALL prices in ONE efficient API call using all_mids() endpoint
        prices = get_all_prices_from_hyperliquid()

        logger.info(f"Fetched {len(prices)} prices from Hyperliquid in ONE API call (all_mids endpoint)")

        if not prices:
            logger.warning("No prices received from Hyperliquid, using fallback")
            return {"BTC": 100000.0, "ETH": 4000.0, "SOL": 200.0}

        return prices

    except Exception as e:
        logger.error(f"Failed to fetch market prices: {e}", exc_info=True)
        # Return fallback prices
        return {"BTC": 100000.0, "ETH": 4000.0, "SOL": 200.0}


def _build_portfolio_data(db, account: Account) -> dict[str, Any]:
    """Build portfolio data dictionary from account and positions.

    Fetches real-time data from Hyperliquid to avoid stale database values.

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
    from services.trading.hyperliquid_trading_service import hyperliquid_trading_service
    import asyncio

    # Fetch real-time data from Hyperliquid (NO REDUNDANCY!)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        user_state = loop.run_until_complete(hyperliquid_trading_service.get_user_state_async())
        margin = user_state.get('marginSummary', {})
        hl_positions = user_state.get('assetPositions', [])

        account_value = float(margin.get('accountValue', '0'))
        total_margin_used = float(margin.get('totalMarginUsed', '0'))

        # Calculate position value from Hyperliquid
        positions_value = 0
        for p in hl_positions:
            pos = p.get('position', {})
            size = float(pos.get('szi', '0'))
            entry_px = float(pos.get('entryPx', '0'))
            positions_value += size * entry_px

        cash_available = account_value - positions_value

    except Exception as e:
        logger.error(f"Failed to fetch real-time data from Hyperliquid: {e}", exc_info=True)
        # Balance data from Hyperliquid API, not from DB
        # If Hyperliquid API fails, we cannot proceed - no fallback to stale DB data
        raise RuntimeError(f"Cannot fetch balance from Hyperliquid: {e}") from e
    finally:
        loop.close()

    # Get current positions from DB
    db_positions = db.query(Position).filter(Position.account_id == account.id).all()

    # Create a set of symbols that exist in Hyperliquid (to filter out stale DB entries)
    hl_symbols = set()
    for p in hl_positions:
        pos = p.get('position', {})
        coin = pos.get('coin', '')
        if coin:
            hl_symbols.add(coin)

    # Only include positions that exist in Hyperliquid (avoid stale DB data)
    active_positions = []
    for pos in db_positions:
        if pos.symbol in hl_symbols:
            active_positions.append({
                "symbol": pos.symbol,
                "quantity": float(pos.quantity or 0),
                "avg_cost": float(pos.average_cost or 0),
            })
        else:
            logger.warning(f"Position {pos.symbol} exists in DB but not in Hyperliquid - filtering out stale data")

    portfolio = {
        "cash": cash_available,  # Real-time from Hyperliquid
        "frozen_cash": total_margin_used,  # Real-time from Hyperliquid
        "total_assets": account_value,  # Real-time from Hyperliquid
        "positions": active_positions,  # Only positions that exist in Hyperliquid
    }

    return portfolio


async def _check_margin_safety(margin_limit: float = 0.70) -> dict[str, Any]:
    """Check if margin utilization is safe before opening new positions (async).

    This prevents over-leveraging and reduces liquidation risk.
    Balanced approach: blocks new positions when margin usage > 70%.

    Args:
        margin_limit: Maximum allowed margin utilization (default: 0.70 = 70%)

    Returns:
        Dict with:
        {
            "safe": True/False,
            "reason": "explanation if unsafe",
            "margin_utilization": current utilization percentage,
            "account_value": total account value,
            "margin_used": total margin used
        }

    Example:
        Account value: $22
        Total margin used: $18
        Margin utilization: 81.8%
        Result: {"safe": False, "reason": "Margin utilization 81.8% > 70% limit"}
    """
    try:
        # Fetch current margin state from Hyperliquid
        user_state = await hyperliquid_trading_service.get_user_state_async()
        margin_summary = user_state.get('marginSummary', {})

        account_value = float(margin_summary.get('accountValue', '0'))
        total_margin_used = float(margin_summary.get('totalMarginUsed', '0'))

        # Calculate margin utilization
        if account_value > 0:
            margin_utilization = total_margin_used / account_value
        else:
            # No account value = cannot open positions
            return {
                "safe": False,
                "reason": "Account value is 0",
                "margin_utilization": 0.0,
                "account_value": 0.0,
                "margin_used": 0.0
            }

        # Check if margin utilization exceeds limit
        if margin_utilization > margin_limit:
            logger.warning(
                f"⚠️ Margin utilization too high: {margin_utilization:.1%} > {margin_limit:.1%} limit. "
                f"Account=${account_value:.2f}, Margin Used=${total_margin_used:.2f}"
            )
            return {
                "safe": False,
                "reason": f"Margin utilization {margin_utilization:.1%} exceeds {margin_limit:.1%} limit",
                "margin_utilization": margin_utilization,
                "account_value": account_value,
                "margin_used": total_margin_used
            }

        logger.info(
            f"✅ Margin check passed: {margin_utilization:.1%} < {margin_limit:.1%} limit. "
            f"Account=${account_value:.2f}, Margin Used=${total_margin_used:.2f}"
        )

        return {
            "safe": True,
            "reason": "Margin utilization within safe limits",
            "margin_utilization": margin_utilization,
            "account_value": account_value,
            "margin_used": total_margin_used
        }

    except Exception as e:
        logger.error(f"Margin safety check failed: {e}", exc_info=True)
        # Fail safe: if check fails, block new positions
        return {
            "safe": False,
            "reason": f"Margin check error: {str(e)}",
            "margin_utilization": 0.0,
            "account_value": 0.0,
            "margin_used": 0.0
        }


def _validate_decision(
    decision: dict[str, Any], portfolio: dict[str, Any], prices: dict[str, float], max_ratio: float
) -> dict[str, Any]:
    """Validate AI decision with safety checks.

    Args:
        decision: AI decision dict with operation, symbol, target_portion_of_balance, leverage
        portfolio: Current portfolio data
        prices: Current market prices
        max_ratio: Maximum allowed ratio per trade

    Returns:
        Dict with:
        {
            "valid": True/False,
            "reason": "explanation if invalid",
            "order_size": calculated size in base currency units,
            "leverage": validated leverage value
        }
    """
    operation = decision.get("operation", "").lower()
    symbol = decision.get("symbol", "")
    target_portion = float(decision.get("target_portion_of_balance", 0))
    leverage = int(decision.get("leverage", 1))  # Default to 1x (no leverage)

    # 1. Check operation is valid (now includes "short")
    if operation not in ["buy", "sell", "short", "hold"]:
        return {"valid": False, "reason": f"Invalid operation: {operation}"}

    # 2. HOLD requires no validation
    if operation == "hold":
        return {"valid": True, "reason": "Hold decision", "order_size": 0, "leverage": 1}

    # 3. Check symbol is in our price data
    if symbol not in prices:
        return {"valid": False, "reason": f"Symbol {symbol} not in market data"}

    # 4. Check target_portion is reasonable (basic sanity check only)
    # Trust AI decisions - no artificial limits!
    if target_portion <= 0:
        return {
            "valid": False,
            "reason": f"target_portion {target_portion} must be positive",
        }

    if target_portion > 1.0:
        return {
            "valid": False,
            "reason": f"target_portion {target_portion} exceeds 100%",
        }

    # 4b. INTELLIGENT CAPITAL ALLOCATION VALIDATION (only for BUY/SHORT operations)
    # Rule: Allow >25% allocation ONLY for exceptional opportunities
    # Exceptional = score >= 0.85 AND momentum >= 0.90
    if operation in ["buy", "short"] and target_portion > 0.25:
        # Get technical factors from portfolio if available
        technical_factors = portfolio.get("technical_factors", {})
        recommendations = technical_factors.get("recommendations", [])

        # Find technical score for this symbol
        symbol_data = next((r for r in recommendations if r["symbol"] == symbol), None)

        if symbol_data:
            score = symbol_data["score"]
            momentum = symbol_data["momentum"]

            # Rule: >25% allocation requires score >= 0.85 AND momentum >= 0.90
            if score < 0.85 or momentum < 0.90:
                logger.warning(
                    f"AI requested {target_portion:.1%} allocation on {symbol} but score={score:.3f}, "
                    f"momentum={momentum:.3f} (not exceptional enough). Capping to 25% for diversification."
                )
                target_portion = 0.25  # Cap to 25% for diversification
                decision["target_portion_of_balance"] = target_portion  # Update decision
            else:
                logger.info(
                    f"⚡ EXCEPTIONAL OPPORTUNITY: {symbol} score={score:.3f}, momentum={momentum:.3f}. "
                    f"Allowing {target_portion:.1%} allocation."
                )
        else:
            # If no technical data available, default to safety (cap at 25%)
            logger.warning(
                f"No technical data for {symbol} - capping allocation to 25% for safety"
            )
            target_portion = 0.25
            decision["target_portion_of_balance"] = target_portion

    # 5. Validate leverage (1-10x allowed)
    if leverage < 1 or leverage > 10:
        return {"valid": False, "reason": f"Leverage {leverage} out of range (1-10)"}

    # 6. Calculate order size
    if operation == "buy":
        # Buy: use portion of available cash
        cash_available = portfolio["cash"]
        order_value_usd = cash_available * target_portion
        price = prices[symbol]

        # Hyperliquid minimum is $10 per order
        # If AI suggests less but we have enough, bump to $10
        MIN_ORDER_USD = 10.0
        if order_value_usd < MIN_ORDER_USD and cash_available >= MIN_ORDER_USD:
            order_value_usd = MIN_ORDER_USD
            logger.info(f"Bumped order value from ${cash_available * target_portion:.2f} to ${MIN_ORDER_USD:.2f} (Hyperliquid minimum)")

        order_size = order_value_usd / price

        return {"valid": True, "reason": "Buy validation passed", "order_size": order_size, "leverage": leverage}

    elif operation == "short":
        # Short: open short position (similar to buy, but is_buy=False)
        cash_available = portfolio["cash"]
        order_value_usd = cash_available * target_portion
        price = prices[symbol]

        # Hyperliquid minimum is $10 per order
        MIN_ORDER_USD = 10.0
        if order_value_usd < MIN_ORDER_USD and cash_available >= MIN_ORDER_USD:
            order_value_usd = MIN_ORDER_USD
            logger.info(f"Bumped SHORT order value from ${cash_available * target_portion:.2f} to ${MIN_ORDER_USD:.2f} (Hyperliquid minimum)")

        order_size = order_value_usd / price

        return {"valid": True, "reason": "Short validation passed", "order_size": order_size, "leverage": leverage}

    elif operation == "sell":
        # Sell: check we have the position
        position = next((p for p in portfolio["positions"] if p["symbol"] == symbol), None)

        if not position or position["quantity"] <= 0:
            return {"valid": False, "reason": f"No position in {symbol} to sell"}

        order_size = position["quantity"] * target_portion

        # Let Hyperliquid decide if order size is acceptable
        # No artificial minimum imposed by us

        return {"valid": True, "reason": "Sell validation passed", "order_size": order_size, "leverage": 1}

    return {"valid": False, "reason": "Unknown validation error"}


def _execute_order_async(decision: dict[str, Any], order_size: float, leverage: int = 1) -> dict[str, Any]:
    """Execute order on Hyperliquid (wrapper for async call).

    Args:
        decision: AI decision with operation and symbol
        order_size: Calculated order size in base currency units
        leverage: Leverage multiplier (1-10x)

    Returns:
        Order execution result dict
    """
    operation = decision.get("operation", "").lower()
    symbol = decision.get("symbol", "")

    if operation == "hold":
        return {"status": "ok", "message": "No action (HOLD)"}

    # Round order size to proper decimals for Hyperliquid
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Get asset metadata to determine proper decimal places
            meta = loop.run_until_complete(hyperliquid_trading_service.get_meta_async())
            asset_info = next((a for a in meta['universe'] if a['name'] == symbol), None)

            if asset_info:
                sz_decimals = asset_info.get('szDecimals', 8)
                rounded_size = round(order_size, sz_decimals)
                logger.info(f"Rounded order size from {order_size} to {rounded_size} ({sz_decimals} decimals)")
                order_size = rounded_size
            else:
                logger.warning(f"Asset {symbol} not found in meta, using raw size")

            # Execute order with properly rounded size
            # Determine order direction and reduce_only flag:
            # - BUY: is_buy=True, reduce_only=False (opens LONG position)
            # - SHORT: is_buy=False, reduce_only=False (opens SHORT position)
            # - SELL: reduce_only=True (closes existing position, direction depends on current position)

            if operation == "buy":
                is_buy = True
                reduce_only = False
            elif operation == "short":
                is_buy = False
                reduce_only = False
            elif operation == "sell":
                # For sell, we MUST check current position direction from Hyperliquid
                # LONG (szi > 0) → sell with is_buy=False (close by selling)
                # SHORT (szi < 0) → sell with is_buy=True (close by buying)
                # Fetch position from Hyperliquid to determine direction
                user_state = loop.run_until_complete(hyperliquid_trading_service.get_user_state_async())
                hl_positions = user_state.get('assetPositions', [])

                # Find position for this symbol
                current_position = next(
                    (p for p in hl_positions if p['position']['coin'] == symbol),
                    None
                )

                if not current_position:
                    logger.error(f"Cannot sell {symbol}: no position found in Hyperliquid")
                    return {"status": "error", "message": f"No position found for {symbol}"}

                # Get signed size (szi): positive = LONG, negative = SHORT
                szi = float(current_position['position']['szi'])

                if szi > 0:
                    # LONG position → sell by is_buy=False
                    is_buy = False
                    logger.info(f"Closing LONG position: {symbol} szi={szi} → SELL (is_buy=False)")
                elif szi < 0:
                    # SHORT position → sell by is_buy=True (buy to close short)
                    is_buy = True
                    logger.info(f"Closing SHORT position: {symbol} szi={szi} → BUY (is_buy=True)")
                else:
                    logger.error(f"Position {symbol} has zero size (szi={szi})")
                    return {"status": "error", "message": f"Position {symbol} has zero size"}

                reduce_only = True
            else:
                logger.error(f"Unknown operation: {operation}")
                return {"status": "error", "message": f"Unknown operation: {operation}"}

            logger.info(f"Executing {operation.upper()} order: {symbol} size={order_size} leverage={leverage}x is_buy={is_buy} reduce_only={reduce_only}")

            result = loop.run_until_complete(
                hyperliquid_trading_service.place_market_order_async(
                    symbol=symbol,
                    is_buy=is_buy,
                    size=order_size,
                    reduce_only=reduce_only,
                    leverage=leverage
                )
            )
            return result
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Order execution failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


async def check_stop_loss_async(stop_loss_threshold: float = -0.02) -> None:
    """Check all open positions and close if loss exceeds threshold (async).

    This function runs every 60 seconds to protect against losses.
    Conservative approach: closes position if unrealized loss > 2%.

    Args:
        stop_loss_threshold: Loss threshold as negative decimal (default: -0.02 = -2%)

    Returns:
        None

    Example:
        Position value: $100
        Unrealized P&L: -$12
        P&L %: -12%
        Action: Close position (exceeds -10% threshold)
    """
    try:
        # Fetch current positions from Hyperliquid
        user_state = await hyperliquid_trading_service.get_user_state_async()
        positions = user_state.get('assetPositions', [])

        if not positions:
            logger.debug("No open positions to check for stop-loss")
            return

        logger.info(f"Checking {len(positions)} positions for stop-loss (threshold: {stop_loss_threshold:.1%})")

        for pos in positions:
            try:
                coin = pos['position']['coin']
                szi = float(pos['position']['szi'])  # Signed size (negative = short)
                entry_px = float(pos['position']['entryPx'])
                position_value = float(pos['position']['positionValue'])
                unrealized_pnl = float(pos['position']['unrealizedPnl'])

                # Calculate P&L percentage
                if position_value > 0:
                    pnl_pct = unrealized_pnl / position_value
                else:
                    continue  # Skip if position value is 0

                logger.debug(
                    f"{coin}: Size={szi}, Entry=${entry_px:.2f}, "
                    f"Value=${position_value:.2f}, P&L=${unrealized_pnl:.2f} ({pnl_pct:.2%})"
                )

                # Check if loss exceeds threshold
                if pnl_pct < stop_loss_threshold:
                    logger.warning(
                        f"🛑 STOP-LOSS TRIGGERED for {coin}: "
                        f"P&L={pnl_pct:.2%} < threshold={stop_loss_threshold:.2%}"
                    )

                    # Close position immediately
                    await _close_position_async(
                        coin=coin,
                        size=abs(szi),
                        is_long=(szi > 0),
                        reason="stop_loss"
                    )

                    logger.info(f"✅ Stop-loss executed: Closed {coin} position")

            except Exception as e:
                logger.error(f"Error checking stop-loss for position: {e}", exc_info=True)
                continue

    except Exception as e:
        logger.error(f"Stop-loss check failed: {e}", exc_info=True)


async def check_take_profit_async(take_profit_threshold: float = 0.10) -> None:
    """Check all open positions and close if profit exceeds threshold (async).

    This function runs every 30 seconds to lock in profits quickly.
    Balanced approach: closes position if unrealized profit > 10%.

    Args:
        take_profit_threshold: Profit threshold as positive decimal (default: 0.10 = +10%)

    Returns:
        None

    Example:
        Position value: $100
        Unrealized P&L: +$11
        P&L %: +11%
        Action: Close position (exceeds +10% threshold)
    """
    try:
        # Fetch current positions from Hyperliquid
        user_state = await hyperliquid_trading_service.get_user_state_async()
        positions = user_state.get('assetPositions', [])

        if not positions:
            logger.debug("No open positions to check for take-profit")
            return

        logger.info(f"Checking {len(positions)} positions for take-profit (threshold: +{take_profit_threshold:.1%})")

        for pos in positions:
            try:
                coin = pos['position']['coin']
                szi = float(pos['position']['szi'])  # Signed size (negative = short)
                entry_px = float(pos['position']['entryPx'])
                position_value = float(pos['position']['positionValue'])
                unrealized_pnl = float(pos['position']['unrealizedPnl'])

                # Calculate P&L percentage
                if position_value > 0:
                    pnl_pct = unrealized_pnl / position_value
                else:
                    continue  # Skip if position value is 0

                logger.debug(
                    f"{coin}: Size={szi}, Entry=${entry_px:.2f}, "
                    f"Value=${position_value:.2f}, P&L=${unrealized_pnl:.2f} ({pnl_pct:.2%})"
                )

                # Check if profit exceeds threshold
                if pnl_pct > take_profit_threshold:
                    logger.warning(
                        f"💰 TAKE-PROFIT TRIGGERED for {coin}: "
                        f"P&L={pnl_pct:.2%} > threshold=+{take_profit_threshold:.2%}"
                    )

                    # Close position immediately to lock in profit
                    await _close_position_async(
                        coin=coin,
                        size=abs(szi),
                        is_long=(szi > 0),
                        reason="take_profit"
                    )

                    logger.info(f"✅ Take-profit executed: Closed {coin} position with +{pnl_pct:.2%} profit")

            except Exception as e:
                logger.error(f"Error checking take-profit for position: {e}", exc_info=True)
                continue

    except Exception as e:
        logger.error(f"Take-profit check failed: {e}", exc_info=True)


async def _close_position_async(coin: str, size: float, is_long: bool, reason: str) -> dict[str, Any]:
    """Close a position on Hyperliquid (async helper).

    Args:
        coin: Symbol to close (e.g., "BTC")
        size: Position size to close (absolute value)
        is_long: True if closing LONG position, False if closing SHORT
        reason: Reason for closing ("stop_loss", "take_profit", etc.)

    Returns:
        Order execution result dict
    """
    try:
        # For LONG position: sell (is_buy=False)
        # For SHORT position: buy to cover (is_buy=True)
        is_buy = not is_long

        logger.info(
            f"Closing {coin} position: size={size}, "
            f"type={'LONG' if is_long else 'SHORT'}, reason={reason}"
        )

        result = await hyperliquid_trading_service.place_market_order_async(
            symbol=coin,
            is_buy=is_buy,
            size=size,
            reduce_only=True,  # Only close existing position
            leverage=1  # Leverage irrelevant when closing
        )

        return result

    except Exception as e:
        logger.error(f"Failed to close {coin} position: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
