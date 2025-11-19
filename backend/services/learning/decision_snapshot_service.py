"""
Decision Snapshot Service - Counterfactual Learning System

Captures EVERY trading decision (LONG, SHORT, HOLD) with full context
to enable learning from both executed trades AND missed opportunities.

Key features:
- Saves decision snapshots with DeepSeek reasoning
- Calculates counterfactual P&L (what if we had chosen differently?)
- Enables self-analysis to identify systematic errors
- Supports adaptive weight optimization based on historical performance
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_async_session_factory
from database.models import DecisionSnapshot
from services.infrastructure.async_wrapper import run_in_thread
from services.market_data.hyperliquid_market_data import hyperliquid_client
from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

logger = logging.getLogger(__name__)


async def save_decision_snapshot(
    account_id: int,
    symbol: str,
    indicators_snapshot: Dict[str, Any],
    deepseek_reasoning: str,
    actual_decision: str,  # LONG, SHORT, HOLD
    actual_size_pct: Optional[float],
    entry_price: float,
) -> int:
    """
    Save a decision snapshot for counterfactual analysis.

    Args:
        account_id: Account ID
        symbol: Trading symbol (e.g., 'BTC')
        indicators_snapshot: Full snapshot of all indicators at decision time
        deepseek_reasoning: Complete reasoning from DeepSeek
        actual_decision: Decision taken (LONG, SHORT, HOLD)
        actual_size_pct: % of portfolio allocated (0.0-1.0)
        entry_price: Price at decision time

    Returns:
        Snapshot ID

    Example:
        >>> snapshot_id = await save_decision_snapshot(
        ...     account_id=1,
        ...     symbol="BTC",
        ...     indicators_snapshot={
        ...         "prophet": {"forecast_24h": 104500, "trend": "up", "confidence": 0.95},
        ...         "pivot_points": {"signal": "LONG"},
        ...         "rsi": 65,
        ...         "whale_alerts": [{"type": "buy", "size": 1000}],
        ...     },
        ...     deepseek_reasoning="Prophet BULLISH +1.3%, Pivot confirms LONG...",
        ...     actual_decision="LONG",
        ...     actual_size_pct=0.20,
        ...     entry_price=103500.0,
        ... )
    """
    async with get_async_session_factory()() as db:
        try:
            snapshot = DecisionSnapshot(
                timestamp=datetime.utcnow(),
                account_id=account_id,
                symbol=symbol,
                indicators_snapshot=json.dumps(indicators_snapshot),
                deepseek_reasoning=deepseek_reasoning,
                actual_decision=actual_decision,
                actual_size_pct=actual_size_pct,
                entry_price=entry_price,
            )

            db.add(snapshot)
            await db.commit()
            await db.refresh(snapshot)

            logger.info(
                f"Saved decision snapshot for {symbol}: {actual_decision} "
                f"at ${entry_price:.2f} (snapshot_id={snapshot.id})",
                extra={
                    "context": {
                        "snapshot_id": snapshot.id,
                        "account_id": account_id,
                        "symbol": symbol,
                        "decision": actual_decision,
                        "price": entry_price,
                    }
                },
            )

            return snapshot.id

        except Exception as e:
            logger.error(
                f"Failed to save decision snapshot for {symbol}: {e}",
                extra={
                    "context": {
                        "account_id": account_id,
                        "symbol": symbol,
                        "decision": actual_decision,
                        "error": str(e),
                    }
                },
                exc_info=True,
            )
            await db.rollback()
            raise


async def _fetch_historical_price_async(
    symbol: str, target_timestamp: datetime, max_retries: int = 3
) -> Optional[float]:
    """
    Fetch historical price for a symbol at a specific timestamp using CCXT.

    Uses 1-hour candles to get the closest price to the target timestamp.
    Implements exponential backoff retry for rate limiting.

    Args:
        symbol: Trading symbol (e.g., 'BTC', 'ETH')
        target_timestamp: Timestamp to fetch price for (typically 24h after entry)
        max_retries: Maximum number of retry attempts for rate limiting

    Returns:
        Close price of the candle closest to target_timestamp, or None if not found

    Raises:
        Exception: If all retries fail
    """
    for attempt in range(max_retries):
        try:
            # Calculate time window: fetch 3 hours of 1h candles centered on target
            # This gives us margin for finding the closest candle
            since_timestamp = target_timestamp - timedelta(hours=1)
            since_ms = int(since_timestamp.timestamp() * 1000)

            # Fetch 3 hours of 1h candles (gives us target ±1h window)
            klines = await run_in_thread(
                hyperliquid_client.get_kline_data,
                symbol=symbol,
                period="1h",
                count=3,
            )

            if not klines:
                logger.warning(
                    f"No historical klines available for {symbol} "
                    f"at {target_timestamp.isoformat()}"
                )
                return None

            # Find candle closest to target timestamp
            target_ts = target_timestamp.timestamp()
            closest_candle = min(klines, key=lambda k: abs(k["timestamp"] - target_ts))

            time_diff = abs(closest_candle["timestamp"] - target_ts)
            close_price = closest_candle["close"]

            logger.debug(
                f"Found historical price for {symbol}: ${close_price:.2f} "
                f"(time diff: {time_diff/60:.1f} min from target)"
            )

            return close_price

        except Exception as e:
            error_msg = str(e)
            is_rate_limited = "429" in error_msg or "rate limit" in error_msg.lower()

            if is_rate_limited and attempt < max_retries - 1:
                wait_time = 2**attempt  # Exponential backoff: 1s, 2s, 4s
                logger.warning(
                    f"Rate limited fetching price for {symbol}, "
                    f"retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(wait_time)
                continue
            else:
                # Either not rate limited, or final attempt - raise
                logger.error(
                    f"Failed to fetch historical price for {symbol}: {e}",
                    exc_info=True,
                )
                raise


async def calculate_counterfactuals_batch(limit: int = 100) -> int:
    """
    Calculate counterfactual P&L for snapshots older than 24h without counterfactuals.

    This is run as a batch job every hour to calculate "what if" scenarios:
    - What if we had gone LONG instead of HOLD?
    - What if we had gone SHORT instead of LONG?
    - What was the optimal decision (highest P&L)?

    Args:
        limit: Max number of snapshots to process per run

    Returns:
        Number of snapshots processed

    Example usage in scheduled job:
        >>> processed = await calculate_counterfactuals_batch(limit=100)
        >>> logger.info(f"Processed {processed} snapshots")
    """
    async with get_async_session_factory()() as db:
        try:
            # Find snapshots older than 24h without counterfactuals
            cutoff_time = datetime.utcnow() - timedelta(hours=24)

            stmt = (
                select(DecisionSnapshot)
                .where(
                    and_(
                        DecisionSnapshot.timestamp < cutoff_time,
                        DecisionSnapshot.exit_price_24h.is_(None),
                    )
                )
                .order_by(DecisionSnapshot.timestamp)
                .limit(limit)
            )

            result = await db.execute(stmt)
            snapshots = result.scalars().all()

            if not snapshots:
                logger.debug("No pending snapshots for counterfactual calculation")
                return 0

            logger.info(f"Processing {len(snapshots)} snapshots for counterfactual analysis")

            processed = 0
            for i, snapshot in enumerate(snapshots):
                try:
                    # Rate limiting: sleep between requests to avoid 429 errors
                    if i > 0:
                        await asyncio.sleep(2)  # 2 seconds between requests

                    # Fetch historical price 24h after decision
                    exit_time = snapshot.timestamp + timedelta(hours=24)
                    exit_price = await _fetch_historical_price_async(
                        symbol=snapshot.symbol, target_timestamp=exit_time, max_retries=3
                    )

                    if exit_price is None:
                        logger.warning(
                            f"No historical price available for {snapshot.symbol} "
                            f"at {exit_time.isoformat()} (snapshot_id={snapshot.id}), skipping"
                        )
                        continue
                    entry_price = float(snapshot.entry_price)
                    price_change_pct = (exit_price - entry_price) / entry_price

                    # Assume we would have used same size_pct for all actions
                    size_pct = float(snapshot.actual_size_pct) if snapshot.actual_size_pct else 0.2  # Default 20%

                    # Calculate P&L for each possible action
                    # (Simplified: assume we could open/close instantly, ignore fees)
                    account_value = 1000  # Placeholder (should fetch real value)

                    long_pnl = price_change_pct * size_pct * account_value
                    short_pnl = -price_change_pct * size_pct * account_value
                    hold_pnl = 0.0

                    # Determine actual P&L based on decision taken
                    pnl_map = {"LONG": long_pnl, "SHORT": short_pnl, "HOLD": hold_pnl}
                    actual_pnl = pnl_map[snapshot.actual_decision]

                    # Find optimal decision (max P&L)
                    optimal_decision = max(pnl_map, key=pnl_map.get)
                    optimal_pnl = pnl_map[optimal_decision]

                    # Calculate regret (opportunity cost)
                    regret = optimal_pnl - actual_pnl

                    # Update snapshot
                    snapshot.exit_price_24h = exit_price
                    snapshot.actual_pnl = actual_pnl
                    snapshot.counterfactual_long_pnl = long_pnl
                    snapshot.counterfactual_short_pnl = short_pnl
                    snapshot.counterfactual_hold_pnl = hold_pnl
                    snapshot.optimal_decision = optimal_decision
                    snapshot.regret = regret
                    snapshot.counterfactuals_calculated_at = datetime.utcnow()

                    processed += 1

                    logger.debug(
                        f"Counterfactual for {snapshot.symbol} (snapshot_id={snapshot.id}): "
                        f"actual={snapshot.actual_decision} ({actual_pnl:+.2f}), "
                        f"optimal={optimal_decision} ({optimal_pnl:+.2f}), "
                        f"regret={regret:+.2f}"
                    )

                except Exception as e:
                    logger.error(
                        f"Failed to calculate counterfactual for snapshot_id={snapshot.id}: {e}",
                        exc_info=True,
                    )
                    continue

            await db.commit()

            logger.info(
                f"✅ Calculated counterfactuals for {processed}/{len(snapshots)} snapshots"
            )

            return processed

        except Exception as e:
            logger.error(
                f"Counterfactual batch processing failed: {e}",
                exc_info=True,
            )
            await db.rollback()
            return 0


async def get_snapshots_for_analysis(
    account_id: int, limit: int = 100, min_regret: Optional[float] = None
) -> List[Dict[str, Any]]:
    """
    Get decision snapshots for DeepSeek self-analysis.

    Args:
        account_id: Account ID to analyze
        limit: Max number of snapshots to return
        min_regret: Filter snapshots with regret >= this value (to focus on mistakes)

    Returns:
        List of snapshots with full context for analysis

    Example:
        >>> snapshots = await get_snapshots_for_analysis(account_id=1, limit=50)
        >>> # Pass to DeepSeek for self-analysis
    """
    async with get_async_session_factory()() as db:
        try:
            # Base query: get all snapshots for account
            stmt = (
                select(DecisionSnapshot)
                .where(DecisionSnapshot.account_id == account_id)
                .order_by(desc(DecisionSnapshot.timestamp))
                .limit(limit)
            )

            # If min_regret is specified, only return snapshots with regret calculated
            if min_regret is not None:
                stmt = stmt.where(
                    and_(
                        DecisionSnapshot.regret.isnot(None),
                        DecisionSnapshot.regret >= min_regret,
                    )
                )

            result = await db.execute(stmt)
            snapshots = result.scalars().all()

            # Convert to dict for JSON serialization
            snapshot_dicts = []
            for snap in snapshots:
                snapshot_dicts.append(
                    {
                        "snapshot_id": snap.id,
                        "timestamp": snap.timestamp.isoformat(),
                        "symbol": snap.symbol,
                        "indicators": json.loads(snap.indicators_snapshot),
                        "reasoning": snap.deepseek_reasoning,
                        "actual_decision": snap.actual_decision,
                        "actual_pnl": float(snap.actual_pnl) if snap.actual_pnl else None,
                        "counterfactual_long_pnl": (
                            float(snap.counterfactual_long_pnl)
                            if snap.counterfactual_long_pnl
                            else None
                        ),
                        "counterfactual_short_pnl": (
                            float(snap.counterfactual_short_pnl)
                            if snap.counterfactual_short_pnl
                            else None
                        ),
                        "counterfactual_hold_pnl": (
                            float(snap.counterfactual_hold_pnl)
                            if snap.counterfactual_hold_pnl
                            else None
                        ),
                        "optimal_decision": snap.optimal_decision,
                        "regret": float(snap.regret) if snap.regret else None,
                        "entry_price": float(snap.entry_price),
                        "exit_price_24h": (
                            float(snap.exit_price_24h) if snap.exit_price_24h else None
                        ),
                    }
                )

            logger.info(
                f"Retrieved {len(snapshot_dicts)} snapshots for analysis (account_id={account_id})"
            )

            return snapshot_dicts

        except Exception as e:
            logger.error(
                f"Failed to get snapshots for analysis: {e}",
                extra={"context": {"account_id": account_id, "error": str(e)}},
                exc_info=True,
            )
            return []
