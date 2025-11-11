"""
Market Data Orchestrator - Coordinates all microservices for complete JSON snapshot.

This module orchestrates:
1. Price fetching (all_mids)
2. Technical Analysis (142 symbols)
3. Pivot Points (batch, 142 symbols)
4. Prophet Forecasts (batch, 142 symbols, LITE mode)
5. Global indicators (sentiment, whale, news)
6. Portfolio state

Execution Strategy:
- Stage 1: Fetch prices (SEQUENTIAL - needed by everyone)
- Stage 2: Run per-symbol analyses in PARALLEL
- Stage 3: Fetch global indicators in PARALLEL
- Stage 4: Aggregate into MarketDataSnapshot

Performance:
- First run: ~3-5 minutes (warm cache)
- Subsequent runs: ~90 seconds (cached)
- API calls: 469-1889 depending on cache state
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

from .json_builder import MarketDataBuilder
from .cache_manager import get_cache_manager
from .schemas import MarketDataSnapshot

logger = logging.getLogger(__name__)


async def build_market_data_snapshot(
    account_id: int,
    enable_prophet: bool = True,
    prophet_mode: str = "lite",
    symbols_filter: Optional[List[str]] = None,
) -> MarketDataSnapshot:
    """
    Build complete market data snapshot for all 142 symbols (or filtered subset).

    This is the MAIN orchestration function that coordinates all microservices.

    Args:
        account_id: Account ID for portfolio data
        enable_prophet: Enable Prophet forecasts (default: True)
        prophet_mode: "lite" (7 days) or "full" (90 days)
        symbols_filter: Optional list of symbols to analyze (e.g., ["BTC", "ETH", "SOL"])
                       If None, analyzes ALL available symbols (~470)

    Returns:
        Complete MarketDataSnapshot with all data

    Example:
        >>> snapshot = await build_market_data_snapshot(account_id=1)
        >>> btc_data = next(s for s in snapshot["symbols"] if s["symbol"] == "BTC")
        >>> print(f"BTC score: {btc_data['technical_analysis']['score']}")

    Performance:
        - First run: ~3-5 minutes (Prophet cache miss)
        - Cached runs: ~90 seconds (all cached)
        - API calls: 469 (cached) to 1889 (first run)
    """
    start_time = time.time()
    logger.info("=" * 80)
    logger.info("🚀 MARKET DATA ORCHESTRATION STARTED")
    logger.info("=" * 80)

    cache_manager = get_cache_manager()

    try:
        # ============================================
        # STAGE 1: Fetch Market Prices (SEQUENTIAL)
        # ============================================
        # MUST be first - all other services need prices
        logger.info("STAGE 1: Fetching market prices...")
        stage1_start = time.time()

        prices = await _fetch_prices()
        all_symbols = list(prices.keys())

        # Apply filter if provided
        if symbols_filter:
            available_symbols = [s for s in all_symbols if s in symbols_filter]
            logger.info(f"Filtered to {len(available_symbols)}/{len(all_symbols)} symbols: {symbols_filter}")
        else:
            available_symbols = all_symbols

        logger.info(
            f"✅ STAGE 1 Complete: {len(prices)} prices fetched, "
            f"{len(available_symbols)} symbols to analyze "
            f"({time.time() - stage1_start:.1f}s)"
        )

        # ============================================
        # STAGE 2: Per-Symbol Analysis (PARALLEL)
        # ============================================
        logger.info(f"STAGE 2: Analyzing {len(available_symbols)} symbols (PARALLEL)...")
        stage2_start = time.time()

        # Launch all per-symbol analyses in parallel
        technical_task = asyncio.create_task(
            _fetch_technical_analysis(available_symbols)
        )

        pivot_task = asyncio.create_task(
            _fetch_pivot_points(available_symbols, prices, cache_manager)
        )

        prophet_task = asyncio.create_task(
            _fetch_prophet_forecasts(available_symbols, prophet_mode, cache_manager)
            if enable_prophet
            else _return_empty_dict()
        )

        # Wait for all analyses to complete
        technical_results, pivot_results, prophet_results = await asyncio.gather(
            technical_task,
            pivot_task,
            prophet_task,
            return_exceptions=True,  # Don't fail entire pipeline if one fails
        )

        # Handle exceptions
        if isinstance(technical_results, Exception):
            logger.error(f"Technical analysis failed: {technical_results}", exc_info=True)
            technical_results = {}

        if isinstance(pivot_results, Exception):
            logger.error(f"Pivot points failed: {pivot_results}", exc_info=True)
            pivot_results = {}

        if isinstance(prophet_results, Exception):
            logger.error(f"Prophet forecasts failed: {prophet_results}", exc_info=True)
            prophet_results = {}

        logger.info(
            f"✅ STAGE 2 Complete: "
            f"Technical={len(technical_results)}, "
            f"Pivots={len(pivot_results)}, "
            f"Prophet={len(prophet_results)} "
            f"({time.time() - stage2_start:.1f}s)"
        )

        # ============================================
        # STAGE 3: Global Indicators (PARALLEL)
        # ============================================
        logger.info("STAGE 3: Fetching global indicators (PARALLEL)...")
        stage3_start = time.time()

        sentiment_task = asyncio.create_task(_fetch_sentiment())
        whale_task = asyncio.create_task(_fetch_whale_alerts())
        news_task = asyncio.create_task(_fetch_news())
        portfolio_task = asyncio.create_task(_fetch_portfolio(account_id))

        sentiment, whale_alerts, news, portfolio = await asyncio.gather(
            sentiment_task,
            whale_task,
            news_task,
            portfolio_task,
            return_exceptions=True,
        )

        # Handle exceptions
        if isinstance(sentiment, Exception):
            logger.error(f"Sentiment fetch failed: {sentiment}", exc_info=True)
            sentiment = _default_sentiment()

        if isinstance(whale_alerts, Exception):
            logger.error(f"Whale alerts fetch failed: {whale_alerts}", exc_info=True)
            whale_alerts = []

        if isinstance(news, Exception):
            logger.error(f"News fetch failed: {news}", exc_info=True)
            news = []

        if isinstance(portfolio, Exception):
            logger.error(f"Portfolio fetch failed: {portfolio}", exc_info=True)
            raise  # Portfolio is critical - can't continue

        logger.info(
            f"✅ STAGE 3 Complete: "
            f"Sentiment={sentiment['value']}, "
            f"Whale={len(whale_alerts)}, "
            f"News={len(news)} "
            f"({time.time() - stage3_start:.1f}s)"
        )

        # ============================================
        # STAGE 4: Build Unified JSON (SEQUENTIAL)
        # ============================================
        logger.info("STAGE 4: Building unified JSON snapshot...")
        stage4_start = time.time()

        builder = MarketDataBuilder()

        snapshot = (
            builder.set_prices(prices)
            .set_technical_analysis(technical_results)
            .set_pivot_points(pivot_results)
            .set_prophet_forecasts(prophet_results)
            .set_sentiment(sentiment)
            .set_whale_alerts(whale_alerts)
            .set_news(news)
            .set_portfolio(portfolio)
            .build(validate=True)  # Auto-validate structure
        )

        logger.info(
            f"✅ STAGE 4 Complete: JSON built with {len(snapshot['symbols'])} symbols "
            f"({time.time() - stage4_start:.1f}s)"
        )

        # ============================================
        # Summary
        # ============================================
        total_time = time.time() - start_time
        logger.info("=" * 80)
        logger.info(f"✅ ORCHESTRATION COMPLETE: {total_time:.1f}s")
        logger.info(f"   - Symbols analyzed: {len(snapshot['symbols'])}")
        logger.info(f"   - With Prophet forecasts: {len(prophet_results)}")
        logger.info(f"   - Global indicators: sentiment, {len(whale_alerts)} whales, {len(news)} news")
        logger.info(f"   - Cache stats: {cache_manager.get_stats()['hit_rate']:.1%} hit rate")
        logger.info("=" * 80)

        return snapshot

    except Exception as e:
        total_time = time.time() - start_time
        logger.error("=" * 80)
        logger.error(f"❌ ORCHESTRATION FAILED after {total_time:.1f}s: {e}")
        logger.error("=" * 80)
        raise


# ============================================
# Stage 1: Price Fetching
# ============================================

async def _fetch_prices() -> Dict[str, float]:
    """Fetch all market prices using Hyperliquid all_mids()."""
    from services.market_data.hyperliquid_market_data import hyperliquid_client

    try:
        # Use existing service
        prices = hyperliquid_client.get_all_prices()

        if not prices:
            raise ValueError("No prices returned from Hyperliquid")

        logger.info(f"Fetched {len(prices)} market prices")
        return prices

    except Exception as e:
        logger.error(f"Failed to fetch prices: {e}", exc_info=True)
        raise


# ============================================
# Stage 2: Per-Symbol Analyses
# ============================================

async def _fetch_technical_analysis(symbols: List[str]) -> Dict[str, dict]:
    """Fetch technical analysis for all symbols."""
    from services.technical_analysis_service import get_technical_analysis_structured

    try:
        logger.info(f"Starting technical analysis for {len(symbols)} symbols...")

        # Run in thread pool (CPU-bound operation)
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,  # Use default executor
            get_technical_analysis_structured,
            symbols,
        )

        logger.info(f"Technical analysis complete: {len(results)} symbols")
        return results

    except Exception as e:
        logger.error(f"Technical analysis failed: {e}", exc_info=True)
        return {}


async def _fetch_pivot_points(
    symbols: List[str],
    prices: Dict[str, float],
    cache_manager,
) -> Dict[str, dict]:
    """Fetch pivot points for all symbols (batch mode)."""
    from services.market_data.pivot_calculator import calculate_pivot_points_batch

    try:
        logger.info(f"Starting pivot points calculation for {len(symbols)} symbols...")

        results = await calculate_pivot_points_batch(
            symbols=symbols,
            prices=prices,
            cache_manager=cache_manager,
        )

        logger.info(f"Pivot points complete: {len(results)} symbols")
        return results

    except Exception as e:
        logger.error(f"Pivot points calculation failed: {e}", exc_info=True)
        return {}


async def _fetch_prophet_forecasts(
    symbols: List[str],
    mode: str,
    cache_manager,
) -> Dict[str, dict]:
    """Fetch Prophet forecasts for all symbols (batch mode, LITE)."""
    from services.market_data.prophet_forecaster import calculate_prophet_forecasts_batch

    try:
        logger.info(f"Starting Prophet forecasts for {len(symbols)} symbols (mode={mode})...")

        results = await calculate_prophet_forecasts_batch(
            symbols=symbols,
            mode=mode,
            cache_manager=cache_manager,
        )

        logger.info(f"Prophet forecasts complete: {len(results)} symbols")
        return results

    except Exception as e:
        logger.error(f"Prophet forecasts failed: {e}", exc_info=True)
        return {}


async def _return_empty_dict() -> Dict[str, dict]:
    """Return empty dict (for disabled Prophet)."""
    return {}


# ============================================
# Stage 3: Global Indicators
# ============================================

async def _fetch_sentiment() -> dict:
    """Fetch Fear & Greed sentiment index."""
    from services.market_data.sentiment_tracker import get_sentiment_tracker

    try:
        tracker = get_sentiment_tracker()

        # Fetch current sentiment (cached internally)
        sentiment_data = tracker.get_sentiment()
        value = sentiment_data["value"]
        label = sentiment_data["classification"]
        signal = sentiment_data["signal"]

        result = {
            "value": value,
            "label": label,
            "signal": signal,
            "last_updated": datetime.utcnow().isoformat(),
        }

        logger.info(f"Sentiment: {label} ({value})")
        return result

    except Exception as e:
        logger.error(f"Failed to fetch sentiment: {e}", exc_info=True)
        return _default_sentiment()


async def _fetch_whale_alerts() -> List[dict]:
    """Fetch recent whale transaction alerts."""
    from services.market_data.whale_tracker import get_whale_tracker

    try:
        tracker = get_whale_tracker()

        # Fetch recent alerts (last 10 minutes)
        alerts = tracker.get_recent_alerts()

        # Convert to structured format
        formatted_alerts = []
        for alert in alerts[:10]:  # Limit to last 10
            formatted_alerts.append({
                "symbol": alert.get("symbol", "UNKNOWN"),
                "amount_usd": alert.get("amount_usd", 0),
                "transaction_type": alert.get("type", "transfer"),
                "from_address": alert.get("from", "unknown"),
                "to_address": alert.get("to", "unknown"),
                "timestamp": alert.get("timestamp", datetime.utcnow().isoformat()),
                "signal": alert.get("signal", "neutral"),
            })

        logger.info(f"Whale alerts: {len(formatted_alerts)} recent transactions")
        return formatted_alerts

    except Exception as e:
        logger.error(f"Failed to fetch whale alerts: {e}", exc_info=True)
        return []


async def _fetch_news() -> List[dict]:
    """Fetch latest crypto news."""
    from services.market_data.news_feed import fetch_latest_news
    from services.market_data.news_cache import get_news_cache

    try:
        # Fetch news (cached internally for 1h)
        news_text = fetch_latest_news()

        if not news_text:
            logger.warning("No news available")
            return []

        # Parse news text into structured format
        # Format: "• Headline 1\n• Headline 2\n..."
        headlines = [
            line.strip("• ").strip()
            for line in news_text.split("\n")
            if line.strip().startswith("•")
        ]

        formatted_news = []
        for headline in headlines[:10]:  # Limit to 10 headlines
            formatted_news.append({
                "headline": headline,
                "summary": None,
                "url": "https://coinjournal.net",  # Default source
                "published_at": datetime.utcnow().isoformat(),
                "sentiment": None,
                "mentioned_symbols": [],  # TODO: Extract symbols from headline
            })

        logger.info(f"News: {len(formatted_news)} headlines")
        return formatted_news

    except Exception as e:
        logger.error(f"Failed to fetch news: {e}", exc_info=True)
        return []


async def _fetch_portfolio(account_id: int) -> dict:
    """Fetch portfolio state for account."""
    from database.connection import async_session_factory
    from database.models import Account
    from sqlalchemy import select
    from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

    try:
        # Get account from database
        async with async_session_factory() as db:
            stmt = select(Account).where(Account.id == account_id)
            result = await db.execute(stmt)
            account = result.scalar_one_or_none()

            if not account:
                raise ValueError(f"Account {account_id} not found")

            # Fetch current state from Hyperliquid
            user_state = await hyperliquid_trading_service.get_user_state_async()

            margin_summary = user_state.get("marginSummary", {})
            positions_data = user_state.get("assetPositions", [])

            # Get all current prices
            all_mids = await hyperliquid_trading_service.get_all_mids_async()

            # Build portfolio state
            total_assets = float(margin_summary.get("accountValue", 0))
            available_cash = float(margin_summary.get("withdrawable", 0))

            # Format positions
            positions = []
            positions_value = 0.0
            unrealized_pnl_total = 0.0

            for pos in positions_data:
                symbol = pos.get("position", {}).get("coin")
                szi = float(pos.get("position", {}).get("szi", 0))

                if abs(szi) < 0.0001:  # Skip tiny positions
                    continue

                entry_px = float(pos.get("position", {}).get("entryPx", 0))
                current_price = float(all_mids.get(symbol, entry_px))
                unrealized_pnl = float(pos.get("position", {}).get("unrealizedPnl", 0))

                side = "LONG" if szi > 0 else "SHORT"
                quantity = abs(szi)
                market_value = quantity * current_price
                unrealized_pnl_pct = (unrealized_pnl / (quantity * entry_px) * 100) if entry_px > 0 else 0

                positions.append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "side": side,
                    "entry_price": entry_px,
                    "current_price": current_price,
                    "unrealized_pnl": unrealized_pnl,
                    "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                    "market_value": market_value,
                })

                positions_value += market_value
                unrealized_pnl_total += unrealized_pnl

            # Strategy weights (from account or defaults)
            strategy_weights = account.strategy_weights or {
                "prophet": 0.5,
                "pivot_points": 0.8,
                "technical_analysis": 0.7,
                "whale_alerts": 0.4,
                "sentiment": 0.3,
                "news": 0.2,
            }

            result = {
                "total_assets": total_assets,
                "available_cash": available_cash,
                "positions_value": positions_value,
                "unrealized_pnl": unrealized_pnl_total,
                "positions": positions,
                "strategy_weights": strategy_weights,
            }

            logger.info(
                f"Portfolio: ${total_assets:.2f} total, "
                f"{len(positions)} positions, "
                f"${unrealized_pnl_total:+.2f} PNL"
            )

            return result

    except Exception as e:
        logger.error(f"Failed to fetch portfolio: {e}", exc_info=True)
        raise


# ============================================
# Defaults & Fallbacks
# ============================================

def _default_sentiment() -> dict:
    """Return default neutral sentiment."""
    return {
        "value": 50,
        "label": "NEUTRAL",
        "signal": "neutral",
        "last_updated": datetime.utcnow().isoformat(),
    }
