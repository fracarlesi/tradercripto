"""
Auto Trading Service - Main entry point for automated crypto trading
This file maintains backward compatibility while delegating to split services
"""

import asyncio
import logging
import math
from decimal import Decimal
from typing import Any

from database.connection import SessionLocal
from database.models import Account
from services.ai_decision_service import (
    call_ai_for_decision,
    call_ai_for_agent_decision,
    get_decision_cache,
    save_ai_decision,
)
from services.orchestrator_service import (
    orchestrator_service,
    AgentProposal,
)
from services.asset_calculator import calc_positions_value
from services.learning import save_decision_snapshot
from services.technical_analysis_service import calculate_technical_factors
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service
from services.market_data.ema_alignment import get_ema_alignment_for_symbols, get_ema_alignment_score_for_decision
from services.market_data.risk_reward_calculator import validate_trade_risk_reward

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

    # CRITICAL SAFETY CHECK: Verify WebSocket health before trading
    # If WebSocket is disconnected or cache is empty, trading MUST be suspended
    from services.market_data.websocket_candle_service import get_websocket_candle_service

    ws_service = get_websocket_candle_service()
    cache_stats = ws_service.get_cache_stats()

    if not cache_stats["connected"]:
        logger.error("🚫 TRADING SUSPENDED: WebSocket not connected - cannot make informed decisions")
        logger.error(f"WebSocket cache: {cache_stats['symbols_cached']} symbols, {cache_stats['total_candles']} candles")
        logger.error("System will retry next cycle (3 minutes)")
        return

    if cache_stats["symbols_cached"] < 100:
        # Less than ~45% symbols cached = insufficient data for momentum analysis
        logger.warning(f"🚫 TRADING SUSPENDED: Insufficient cache data ({cache_stats['symbols_cached']}/221 symbols)")
        logger.warning("WebSocket may be warming up or reconnecting - system will retry next cycle")
        return

    logger.info(f"✅ WebSocket health check passed: {cache_stats['symbols_cached']}/221 symbols cached")

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

        # 3.5. Get ALL subscribed symbols from WebSocket (221 coins)
        logger.info("=" * 60)
        logger.info("STEP 1: Fetching ALL coins from WebSocket cache")
        logger.info("=" * 60)

        from services.market_data.websocket_candle_service import get_websocket_candle_service

        # Get ALL symbols subscribed to WebSocket (no pre-filtering!)
        ws_service = get_websocket_candle_service()
        all_symbols = list(ws_service.subscribed_symbols)
        logger.info(f"✅ Found {len(all_symbols)} coins in WebSocket cache - analyzing ALL")

        # 3.6. Calculate technical factors for ALL coins (WebSocket cache = ZERO API calls!)
        logger.info("=" * 60)
        logger.info(f"STEP 2: Technical analysis on ALL {len(all_symbols)} coins (WebSocket cache)")
        logger.info("=" * 60)

        technical_factors = calculate_technical_factors(all_symbols)
        logger.info(
            f"✅ Technical analysis complete: {len(technical_factors.get('recommendations', []))} symbols with full data"
        )

        # Apply dynamic adjustments from hourly retrospective learning
        from services.learning.weight_adjustments import (
            check_and_apply_adjustments,
            log_adjustment_status
        )

        log_adjustment_status()  # Log current active adjustments

        # Apply adjustments to each symbol's technical data
        recommendations = technical_factors.get('recommendations', [])
        adjusted_recommendations = []

        for rec in recommendations:
            symbol = rec['symbol']
            technical_data = {
                'score': rec['score'],
                'momentum': rec['momentum'],
                'support': rec['support']
            }

            # Apply any active adjustments (threshold override, score boost, etc.)
            adjusted_data = check_and_apply_adjustments(symbol, technical_data)

            # Update recommendation with adjusted values
            rec_copy = rec.copy()
            rec_copy['score'] = adjusted_data.get('score', rec['score'])
            rec_copy['threshold_override'] = adjusted_data.get('threshold_override')
            adjusted_recommendations.append(rec_copy)

        # Replace original recommendations with adjusted ones
        technical_factors['recommendations'] = adjusted_recommendations

        # 3.7. Calculate EMA alignment for top 20 symbols (trend confirmation filter)
        logger.info("=" * 60)
        logger.info("STEP 3: Calculating EMA Alignment (25/50/100) for top symbols")
        logger.info("=" * 60)

        # Get top 20 symbols by technical score for EMA calculation
        top_symbols = [r['symbol'] for r in sorted(
            adjusted_recommendations,
            key=lambda x: x['score'],
            reverse=True
        )[:20]]

        ema_alignment_data = {}
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                ema_alignment_data = loop.run_until_complete(
                    get_ema_alignment_for_symbols(top_symbols)
                )
            finally:
                loop.close()

            # Add EMA data to recommendations
            for rec in adjusted_recommendations:
                symbol = rec['symbol']
                if symbol in ema_alignment_data:
                    ema = ema_alignment_data[symbol]
                    rec['ema_alignment'] = ema.alignment
                    rec['ema_25'] = ema.ema_25
                    rec['ema_50'] = ema.ema_50
                    rec['ema_100'] = ema.ema_100
                    rec['ema_score'] = ema.alignment_score
                    rec['trend_strength'] = ema.trend_strength

            aligned_count = sum(1 for e in ema_alignment_data.values()
                              if e.alignment in ["BULLISH", "BEARISH"])
            logger.info(
                f"EMA alignment: {len(ema_alignment_data)} symbols analyzed, "
                f"{aligned_count} with confirmed trend"
            )
        except Exception as e:
            logger.error(f"EMA calculation failed: {e}", exc_info=True)

        # Add technical factors to portfolio data for AI
        # (New tokens are now captured by hourly momentum - no separate detection needed)
        portfolio["technical_factors"] = technical_factors
        portfolio["ema_alignment"] = {
            symbol: {
                "alignment": ema.alignment,
                "score": ema.alignment_score,
                "trend_strength": ema.trend_strength
            }
            for symbol, ema in ema_alignment_data.items()
        }

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

            # 5b. Check if adding to existing position (momentum stacking)
            # Allow stacking ONLY if momentum is accelerating (top 10 coins)
            symbol = decision.get("symbol", "")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                user_state = loop.run_until_complete(
                    hyperliquid_trading_service.get_user_state_async()
                )
            finally:
                loop.close()

            existing_position = None
            for pos in user_state.get('assetPositions', []):
                if pos['position']['coin'] == symbol:
                    existing_position = pos
                    break

            if existing_position:
                # Already in position on this symbol
                # Allow stacking ONLY if coin is in top 10 momentum (strong acceleration)
                from services.market_data.hourly_momentum import get_hourly_momentum_scores
                try:
                    momentum_scores = get_hourly_momentum_scores(top_n=10)
                    top_10_symbols = [m['symbol'] for m in momentum_scores]

                    if symbol not in top_10_symbols:
                        logger.warning(
                            f"⚠️ Already in position on {symbol} (not in top 10 momentum). "
                            f"Blocking position stacking. Consider rotation instead."
                        )
                        save_ai_decision(db, account, decision, portfolio, executed=False)
                        return
                    else:
                        logger.info(
                            f"✅ {symbol} in top 10 momentum - allowing position increment "
                            f"(momentum acceleration detected)"
                        )
                except Exception as e:
                    logger.error(f"Failed to check momentum for stacking: {e}", exc_info=True)
                    # If momentum check fails, be conservative: don't stack
                    logger.warning(f"⚠️ Already in position on {symbol}, blocking stack (momentum check failed)")
                    save_ai_decision(db, account, decision, portfolio, executed=False)
                    return

        # 6. Validate decision
        validation_result = _validate_decision(decision, portfolio, prices, max_ratio)
        if not validation_result["valid"]:
            logger.warning(f"Decision validation failed: {validation_result['reason']}")
            # Save failed decision to database for analysis
            save_ai_decision(db, account, decision, portfolio, executed=False)
            return

        # 6b. Validate Risk/Reward ratio (minimum 1:2)
        operation = decision.get("operation", "").lower()
        if operation in ["buy", "short"]:
            symbol = decision.get("symbol", "")
            entry_price = prices.get(symbol, 0.0)

            # Get pivot points for R:R calculation
            recommendations = portfolio.get("technical_factors", {}).get("recommendations", [])
            symbol_rec = next((r for r in recommendations if r["symbol"] == symbol), None)

            if symbol_rec and entry_price > 0:
                # Build pivot points from technical factors or calculate
                from services.pivot_point_service import calculate_pivot_points_from_cache

                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        pivot_data = loop.run_until_complete(
                            calculate_pivot_points_from_cache(symbol)
                        )
                    finally:
                        loop.close()

                    if pivot_data:
                        direction = "LONG" if operation == "buy" else "SHORT"
                        rr_validation = validate_trade_risk_reward(
                            symbol=symbol,
                            direction=direction,
                            entry_price=entry_price,
                            pivot_points=pivot_data,
                            min_ratio=2.0,
                        )

                        if not rr_validation["valid"]:
                            logger.warning(
                                f"R:R REJECTED {symbol} {direction}: "
                                f"R:R {rr_validation['rr_ratio']:.1f}:1 < 2:1 minimum. "
                                f"Risk={rr_validation['risk_pct']:.1f}%, Reward={rr_validation['reward_pct']:.1f}%"
                            )
                            # Save failed decision for learning
                            decision["rr_rejection"] = rr_validation
                            save_ai_decision(db, account, decision, portfolio, executed=False)
                            return

                        logger.info(
                            f"R:R APPROVED {symbol} {direction}: "
                            f"R:R {rr_validation['rr_ratio']:.1f}:1 "
                            f"(SL=${rr_validation['stop_loss']:.2f}, TP=${rr_validation['take_profit']:.2f})"
                        )
                except Exception as e:
                    logger.error(f"R:R validation failed for {symbol}: {e}", exc_info=True)
                    # Continue without R:R check if calculation fails

        # 7. Execute order on Hyperliquid
        logger.info("Executing order on Hyperliquid...")
        leverage = validation_result.get("leverage", 1)
        execution_result = _execute_order_async(decision, validation_result["order_size"], leverage, account_id=account.id)

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


def place_multi_agent_order(max_ratio: float = 0.2) -> None:
    """
    Place AI-driven crypto order using MULTI-AGENT ORCHESTRATOR system.

    This function:
    1. Gets the active AI trading account
    2. Fetches market data and builds portfolio
    3. Calls BOTH LONG and SHORT agents in parallel
    4. Uses orchestrator to resolve conflicts
    5. Executes approved trades

    Args:
        max_ratio: Maximum portion of portfolio per trade (0.0-1.0)
    """
    logger.info("=" * 60)
    logger.info("=== MULTI-AGENT ORCHESTRATED TRADING CYCLE ===")
    logger.info("=" * 60)

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

    logger.info(f"✅ WebSocket health OK: {cache_stats['symbols_cached']}/221 symbols cached")

    db = SessionLocal()
    try:
        # 1. Get active AI trading account
        account = (
            db.query(Account)
            .filter(Account.account_type == "AI", Account.is_active == True)
            .first()
        )

        if not account:
            logger.warning("No active AI trading account found")
            return

        logger.info(f"Trading with account: {account.name} (id={account.id})")

        # 2. Fetch market data
        prices = _fetch_market_prices()
        logger.info(f"Fetched prices for {len(prices)} symbols")

        # 3. Build portfolio data
        portfolio = _build_portfolio_data(db, account)
        logger.info(
            f"Portfolio: ${portfolio['cash']:.2f} cash, "
            f"{len(portfolio['positions'])} positions, "
            f"${portfolio['total_assets']:.2f} total"
        )

        # 4. Get technical factors for all symbols
        ws_service = get_websocket_candle_service()
        all_symbols = list(ws_service.subscribed_symbols)
        technical_factors = calculate_technical_factors(all_symbols)
        portfolio["technical_factors"] = technical_factors

        # 5. Get current positions from Hyperliquid (for orchestrator)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            user_state = loop.run_until_complete(
                hyperliquid_trading_service.get_user_state_async()
            )
            current_positions = user_state.get('assetPositions', [])
        finally:
            loop.close()

        # 6. Call BOTH agents
        logger.info("=" * 40)
        logger.info("STEP 1: Calling LONG and SHORT agents")
        logger.info("=" * 40)

        # Call LONG agent
        logger.info("[LONG] Calling LONG agent...")
        long_decision = asyncio.run(
            call_ai_for_agent_decision(account, portfolio, prices, "LONG")
        )

        # Call SHORT agent
        logger.info("[SHORT] Calling SHORT agent...")
        short_decision = asyncio.run(
            call_ai_for_agent_decision(account, portfolio, prices, "SHORT")
        )

        # 7. Convert decisions to proposals
        long_proposal = None
        short_proposal = None

        if long_decision and long_decision.get("operation") != "hold":
            # Find technical score for symbol
            symbol = long_decision.get("symbol", "BTC")
            recommendations = technical_factors.get("recommendations", [])
            symbol_data = next((r for r in recommendations if r["symbol"] == symbol), None)
            technical_score = symbol_data["score"] if symbol_data else 0.5

            long_proposal = AgentProposal(
                agent_type="LONG",
                operation=long_decision.get("operation", "hold"),
                symbol=symbol,
                confidence=float(long_decision.get("target_portion_of_balance", 0.5)),
                target_portion=float(long_decision.get("target_portion_of_balance", 0.5)),
                leverage=int(long_decision.get("leverage", 1)),
                reasoning=long_decision.get("reason", ""),
                technical_score=technical_score,
            )
            logger.info(f"[LONG] Proposal: {long_proposal.operation} {long_proposal.symbol} @ {long_proposal.target_portion:.0%}")

        if short_decision and short_decision.get("operation") != "hold":
            symbol = short_decision.get("symbol", "BTC")
            recommendations = technical_factors.get("recommendations", [])
            symbol_data = next((r for r in recommendations if r["symbol"] == symbol), None)
            technical_score = symbol_data["score"] if symbol_data else 0.5

            short_proposal = AgentProposal(
                agent_type="SHORT",
                operation=short_decision.get("operation", "hold"),
                symbol=symbol,
                confidence=float(short_decision.get("target_portion_of_balance", 0.5)),
                target_portion=float(short_decision.get("target_portion_of_balance", 0.5)),
                leverage=int(short_decision.get("leverage", 1)),
                reasoning=short_decision.get("reason", ""),
                technical_score=technical_score,
            )
            logger.info(f"[SHORT] Proposal: {short_proposal.operation} {short_proposal.symbol} @ {short_proposal.target_portion:.0%}")

        # 8. Orchestrator resolves conflicts
        logger.info("=" * 40)
        logger.info("STEP 2: Orchestrator resolving proposals")
        logger.info("=" * 40)

        decisions = orchestrator_service.resolve_proposals(
            long_proposal=long_proposal,
            short_proposal=short_proposal,
            current_positions=current_positions,
        )

        if not decisions:
            logger.info("Orchestrator: No trades to execute (all HOLD or blocked)")
            return

        # 9. Execute each approved decision
        logger.info("=" * 40)
        logger.info(f"STEP 3: Executing {len(decisions)} approved trades")
        logger.info("=" * 40)

        for decision in decisions:
            # Convert orchestrator decision to execution format
            trade_decision = {
                "operation": decision.operation,
                "symbol": decision.symbol,
                "target_portion_of_balance": decision.target_portion,
                "leverage": decision.leverage,
                "reason": decision.reasoning,
            }

            logger.info(
                f"Executing [{decision.agent_type}]: {decision.operation} "
                f"{decision.symbol} @ {decision.target_portion:.0%} capital, {decision.leverage}x"
            )

            # Validate decision
            validation_result = _validate_decision(trade_decision, portfolio, prices, max_ratio)
            if not validation_result["valid"]:
                logger.warning(f"Validation failed: {validation_result['reason']}")
                save_ai_decision(db, account, trade_decision, portfolio, executed=False)
                continue

            # Execute order
            leverage = validation_result.get("leverage", 1)
            execution_result = _execute_order_async(
                trade_decision,
                validation_result["order_size"],
                leverage,
                account_id=account.id
            )

            # Check result
            is_executed = False
            if execution_result.get("status") == "ok":
                response = execution_result.get("response", {})
                if response.get("type") == "order":
                    statuses = response.get("data", {}).get("statuses", [])
                    has_errors = any(s.get("error") for s in statuses)
                    if not has_errors:
                        is_executed = True

            if is_executed:
                logger.info(f"✅ [{decision.agent_type}] Order executed: {decision.symbol}")
                save_ai_decision(db, account, trade_decision, portfolio, executed=True)
            else:
                logger.error(f"❌ [{decision.agent_type}] Order failed: {execution_result}")
                save_ai_decision(db, account, trade_decision, portfolio, executed=False)

    except Exception as e:
        logger.error(f"Multi-agent trading cycle failed: {e}", exc_info=True)
    finally:
        db.close()
        logger.info("=== Multi-Agent Trading Cycle Completed ===")


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
    # Rule: Allow >50% allocation ONLY for exceptional opportunities
    # Exceptional = score >= 0.85 AND momentum >= 0.90
    # Strong = score >= 0.75 → 50% allocation
    if operation in ["buy", "short"] and target_portion > 0.5:
        # Get technical factors from portfolio if available
        technical_factors = portfolio.get("technical_factors", {})
        recommendations = technical_factors.get("recommendations", [])

        # Find technical score for this symbol
        symbol_data = next((r for r in recommendations if r["symbol"] == symbol), None)

        if symbol_data:
            score = symbol_data["score"]
            momentum = symbol_data["momentum"]

            # Rule: >50% allocation requires score >= 0.85 AND momentum >= 0.90
            if score < 0.85 or momentum < 0.90:
                logger.warning(
                    f"AI requested {target_portion:.1%} allocation on {symbol} but score={score:.3f}, "
                    f"momentum={momentum:.3f} (not exceptional enough). Capping to 50% for risk management."
                )
                target_portion = 0.5  # Cap to 50% for strong signals
                decision["target_portion_of_balance"] = target_portion  # Update decision
            else:
                logger.info(
                    f"⚡ EXCEPTIONAL OPPORTUNITY: {symbol} score={score:.3f}, momentum={momentum:.3f}. "
                    f"Allowing {target_portion:.1%} allocation."
                )
        else:
            # If no technical data available, default to safety (cap at 50%)
            logger.warning(
                f"No technical data for {symbol} - capping allocation to 50% for safety"
            )
            target_portion = 0.5
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


def _execute_order_async(decision: dict[str, Any], order_size: float, leverage: int = 1, account_id: int | None = None) -> dict[str, Any]:
    """Execute order on Hyperliquid (wrapper for async call).

    Args:
        decision: AI decision with operation and symbol
        order_size: Calculated order size in base currency units
        leverage: Leverage multiplier (1-10x)
        account_id: Account ID for saving trade metadata (optional)

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
                # Use ceil to ensure final value >= $10 after rounding
                # Example: 0.0001036 BTC @ 5 decimals → ceil(0.0001036 * 10^5) / 10^5 = 0.00011
                # This ensures: 0.00011 * $96,500 = $10.615 >= $10 ✅
                rounded_size = math.ceil(order_size * 10**sz_decimals) / 10**sz_decimals
                logger.info(f"Rounded order size UP from {order_size} to {rounded_size} ({sz_decimals} decimals)")
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

            # Save leverage/strategy metadata BEFORE order (for NEW positions only)
            # This ensures metadata persists even after position closes
            if account_id and not reduce_only and operation in ["buy", "short"]:
                try:
                    from decimal import Decimal
                    from database.connection import SessionLocal
                    from database.models import TradeMetadata

                    db = SessionLocal()
                    try:
                        # Determine strategy based on operation
                        strategy = "LONG" if operation == "buy" else "SHORT"

                        metadata = TradeMetadata(
                            account_id=account_id,
                            symbol=symbol,
                            leverage=Decimal(str(leverage)),
                            strategy=strategy
                        )
                        db.add(metadata)
                        db.commit()
                        logger.info(f"💾 Saved trade metadata: {symbol} leverage={leverage}x strategy={strategy}")
                    finally:
                        db.close()
                except Exception as e:
                    logger.error(f"Failed to save trade metadata: {e}", exc_info=True)
                    # Don't fail the order if metadata save fails

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


async def check_stop_loss_async() -> None:
    """Check all open positions using AI Stop Loss agent for intelligent exit decisions.

    This function runs every 60 seconds. Instead of fixed thresholds, it uses
    DeepSeek AI to analyze market conditions and make intelligent stop-loss decisions.

    The AI agent considers:
    - Current P&L and trend
    - Technical indicators (momentum, support levels)
    - Whether this is a temporary dip or trend reversal
    - Risk of larger losses if holding

    Returns:
        None
    """
    from database.connection import async_session_factory
    from database.models import Account
    from services.agents.exit_agents import call_exit_agent
    from services.technical_analysis_service import get_technical_analysis_structured
    from sqlalchemy import select

    try:
        # Fetch current positions from Hyperliquid
        user_state = await hyperliquid_trading_service.get_user_state_async()
        positions = user_state.get('assetPositions', [])

        if not positions:
            logger.debug("No open positions to check for stop-loss")
            return

        # Get active account for AI calls
        async with async_session_factory() as db:
            result = await db.execute(
                select(Account).where(Account.is_active == True)
            )
            account = result.scalar_one_or_none()

        if not account:
            logger.warning("No active account found for stop-loss agent")
            return

        # Extract symbols from positions for technical analysis
        symbols = [pos['position']['coin'] for pos in positions if pos.get('position', {}).get('coin')]

        # Get technical analysis for context (sync function)
        try:
            technical_data = get_technical_analysis_structured(symbols)
            # Convert to format expected by exit_agents
            technical_factors = {'recommendations': [
                {'symbol': sym, **data} for sym, data in technical_data.items()
            ]}
        except Exception as e:
            logger.warning(f"Failed to get technical analysis: {e}")
            technical_factors = {'recommendations': []}

        # Filter valid positions (non-zero size)
        valid_positions = [
            pos for pos in positions
            if float(pos['position'].get('szi', 0)) != 0
        ]

        if not valid_positions:
            logger.debug("No valid positions to check for stop-loss")
            return

        logger.info(f"AI Stop Loss Agent checking {len(valid_positions)} positions in PARALLEL")

        # Create tasks for all positions (parallel execution)
        async def check_single_position(pos):
            """Check single position and return (position_data, decision)."""
            position_data = pos['position']
            try:
                decision = await call_exit_agent(
                    account=account,
                    position_data=position_data,
                    technical_factors=technical_factors,
                    agent_type="STOP_LOSS"
                )
                return (position_data, decision)
            except Exception as e:
                logger.error(f"Error in AI stop-loss for {position_data.get('coin')}: {e}", exc_info=True)
                return (position_data, None)

        # Execute ALL AI calls in parallel using asyncio.gather()
        import asyncio
        tasks = [check_single_position(pos) for pos in valid_positions]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Parallel task failed: {result}", exc_info=True)
                continue

            position_data, decision = result
            coin = position_data['coin']
            szi = float(position_data.get('szi', 0))

            if decision and decision.should_exit and decision.confidence >= 0.6:
                logger.warning(
                    f"🛑 AI STOP-LOSS for {coin}: "
                    f"P&L={decision.pnl_pct:.2%}, Confidence={decision.confidence:.0%}\n"
                    f"   Reasoning: {decision.reasoning}"
                )

                # Close position
                await _close_position_async(
                    coin=coin,
                    size=abs(szi),
                    is_long=(szi > 0),
                    reason="ai_stop_loss"
                )

                logger.info(f"✅ AI Stop-loss executed: Closed {coin} position")

            elif decision:
                logger.debug(
                    f"{coin}: AI recommends HOLD (P&L={decision.pnl_pct:.2%}, "
                    f"Confidence={decision.confidence:.0%})"
                )

    except Exception as e:
        logger.error(f"AI Stop-loss check failed: {e}", exc_info=True)


async def check_take_profit_async() -> None:
    """Check all open positions using AI Take Profit agent for intelligent exit decisions.

    This function runs every 60 seconds. Instead of fixed thresholds, it uses
    DeepSeek AI to analyze market conditions and make intelligent take-profit decisions.

    The AI agent considers:
    - Current P&L and momentum trend
    - Technical indicators (overbought/oversold)
    - Whether momentum is slowing
    - Optimal time to lock in profits

    Returns:
        None
    """
    from database.connection import async_session_factory
    from database.models import Account
    from services.agents.exit_agents import call_exit_agent
    from services.technical_analysis_service import get_technical_analysis_structured
    from sqlalchemy import select

    try:
        # Fetch current positions from Hyperliquid
        user_state = await hyperliquid_trading_service.get_user_state_async()
        positions = user_state.get('assetPositions', [])

        if not positions:
            logger.debug("No open positions to check for take-profit")
            return

        # Get active account for AI calls
        async with async_session_factory() as db:
            result = await db.execute(
                select(Account).where(Account.is_active == True)
            )
            account = result.scalar_one_or_none()

        if not account:
            logger.warning("No active account found for take-profit agent")
            return

        # Extract symbols from positions for technical analysis
        symbols = [pos['position']['coin'] for pos in positions if pos.get('position', {}).get('coin')]

        # Get technical analysis for context (sync function)
        try:
            technical_data = get_technical_analysis_structured(symbols)
            # Convert to format expected by exit_agents
            technical_factors = {'recommendations': [
                {'symbol': sym, **data} for sym, data in technical_data.items()
            ]}
        except Exception as e:
            logger.warning(f"Failed to get technical analysis: {e}")
            technical_factors = {'recommendations': []}

        # Filter valid positions (non-zero size)
        valid_positions = [
            pos for pos in positions
            if float(pos['position'].get('szi', 0)) != 0
        ]

        if not valid_positions:
            logger.debug("No valid positions to check for take-profit")
            return

        logger.info(f"AI Take Profit Agent checking {len(valid_positions)} positions with semaphore (max 3 concurrent)")

        # Use semaphore to limit concurrent API calls (max 3 at a time)
        # This prevents blocking the event loop for too long while still being efficient
        import asyncio
        semaphore = asyncio.Semaphore(3)

        async def check_position_with_semaphore(pos):
            """Check single position with rate limiting via semaphore."""
            async with semaphore:
                position_data = pos['position']
                coin = position_data['coin']
                szi = float(position_data.get('szi', 0))
                try:
                    decision = await call_exit_agent(
                        account=account,
                        position_data=position_data,
                        technical_factors=technical_factors,
                        agent_type="TAKE_PROFIT"
                    )
                    return (position_data, decision, szi)
                except Exception as e:
                    logger.error(f"Error in AI take-profit for {coin}: {e}", exc_info=True)
                    return (position_data, None, szi)

        # Execute with limited concurrency
        tasks = [check_position_with_semaphore(pos) for pos in valid_positions]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Task failed: {result}", exc_info=True)
                continue

            position_data, decision, szi = result
            coin = position_data['coin']

            if decision and decision.should_exit and decision.confidence >= 0.6:
                logger.warning(
                    f"💰 AI TAKE-PROFIT for {coin}: "
                    f"P&L={decision.pnl_pct:.2%}, Confidence={decision.confidence:.0%}\n"
                    f"   Reasoning: {decision.reasoning}"
                )

                # Close position to lock in profit
                await _close_position_async(
                    coin=coin,
                    size=abs(szi),
                    is_long=(szi > 0),
                    reason="ai_take_profit"
                )

                logger.info(f"✅ AI Take-profit executed: Closed {coin} with {decision.pnl_pct:.2%} profit")

            elif decision:
                logger.debug(
                    f"{coin}: AI recommends HOLD (P&L={decision.pnl_pct:.2%}, "
                    f"Confidence={decision.confidence:.0%})"
                )

    except Exception as e:
        logger.error(f"AI Take-profit check failed: {e}", exc_info=True)


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
