"""
AI Decision Service - Handles AI model API calls for trading decisions
"""

import hashlib
import json
import logging
import random
import time
from collections.abc import Callable
from decimal import Decimal
from threading import Lock

import requests
from sqlalchemy.orm import Session

from database.models import Account, AIDecisionLog
from services.market_data.news_feed import fetch_latest_news
from services.agents.long_agent import get_long_agent_prompt
from services.agents.short_agent import get_short_agent_prompt

logger = logging.getLogger(__name__)

# Import Prophet forecaster

# Import TOON encoder for efficient LLM communication (30-60% token savings)
from services.toon_encoder import encode as toon_encode, estimate_token_savings

#  mode API keys that should be skipped
DEMO_API_KEYS = {"default-key-please-update-in-settings", "default", "", None}


# Load all available crypto symbols from Hyperliquid dynamically
def _load_all_supported_symbols() -> dict[str, str]:
    """Load all available crypto symbols from Hyperliquid"""
    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        info = Info(constants.MAINNET_API_URL)
        meta = info.meta()

        symbols = {}
        for asset in meta.get("universe", []):
            symbol = asset.get("name")
            if symbol:
                symbols[symbol] = symbol  # Use symbol as name for simplicity

        logger.info(f"Loaded {len(symbols)} crypto symbols from Hyperliquid")
        return symbols
    except Exception as e:
        logger.error(f"Failed to load symbols from Hyperliquid: {e}", exc_info=True)
        # Fallback to basic list
        return {
            "BTC": "Bitcoin",
            "ETH": "Ethereum",
            "SOL": "Solana",
            "DOGE": "Dogecoin",
            "XRP": "Ripple",
            "BNB": "Binance Coin",
        }


SUPPORTED_SYMBOLS: dict[str, str] = _load_all_supported_symbols()


def _is_default_api_key(api_key: str) -> bool:
    """Check if the API key is a default/placeholder key that should be skipped"""
    return api_key in DEMO_API_KEYS


def _format_technical_analysis(technical_factors: dict) -> str:
    """
    Format technical analysis for AI prompt using TOON format.

    TOON reduces token count by 30-60% compared to JSON for tabular data.
    Now passes ALL analyzed symbols (not just top 5) for better AI decisions.
    """
    if not technical_factors or not technical_factors.get("recommendations"):
        return "Technical Analysis: Not available (insufficient historical data)"

    recommendations = technical_factors.get("recommendations", [])

    lines = ["Technical Analysis (Quantitative Signals - ALL analyzed symbols in TOON format):"]
    lines.append("")
    lines.append("Signal Interpretation:")
    lines.append("  • Score >= 0.90: STRONG technical buy signal - high confidence (BUY)")
    lines.append("  • Score 0.80-0.89: Moderate signal - consider buying with caution")
    lines.append("  • Score 0.70-0.79: Weak - HOLD or wait for better signal")
    lines.append("  • Score < 0.90: NO NEW POSITIONS (threshold raised to reduce overtrading)")
    lines.append("  • Score < 0.70: Very weak signal - consider selling or avoid buying")
    lines.append("  • Momentum: Measures upward price trend strength (higher = stronger uptrend)")
    lines.append("  • Support: Measures support level strength (higher = stronger buying pressure)")
    lines.append("")

    # Convert recommendations to TOON format (ALL symbols, not just top 5)
    toon_data = toon_encode(recommendations, root_name="technical_recommendations")
    lines.append(toon_data)

    lines.append("")
    lines.append(f"Total symbols analyzed: {len(recommendations)}")
    lines.append("IMPORTANT: Prioritize symbols with high combined scores (>0.90) for BUY decisions.")

    return "\n".join(lines)


async def _format_pivot_points(portfolio: dict, prices: dict[str, float], high_score_symbols: list[str] = None) -> str:
    """
    Format pivot points analysis for AI prompt (RIZZO VIDEO INTEGRATION).

    Calculates pivot points for:
    1. All active positions (to decide hold/sell)
    2. High-score symbols (score > 0.7) - OPTION B

    Args:
        portfolio: Portfolio data with positions
        prices: Current market prices
        high_score_symbols: Optional list of high-score symbols (default: top 3 by price)

    Returns:
        Formatted string with pivot points analysis
    """
    try:
        from services.market_data.pivot_calculator import get_pivot_calculator

        calculator = get_pivot_calculator()

        lines = ["Pivot Points Analysis (Support & Resistance Levels - RIZZO VIDEO):"]
        lines.append("")
        lines.append("These are recurring pattern levels where price tends to BOUNCE:")
        lines.append("- Near S1/S2 (support) → Price likely BOUNCES UP (LONG opportunity)")
        lines.append("- Near R1/R2 (resistance) → Price likely BOUNCES DOWN (SHORT opportunity)")
        lines.append("- Above PP = Bullish zone | Below PP = Bearish zone")
        lines.append("")

        # 1. Pivot points for active positions
        positions = portfolio.get("positions", [])
        if positions:
            lines.append("📊 ACTIVE POSITIONS - Pivot Analysis:")
            lines.append("")

            for pos in positions:
                symbol = pos.get("symbol")
                current_price = prices.get(symbol, 0)

                if current_price <= 0:
                    continue

                # Calculate pivots
                pivots = await calculator.calculate_pivot_points(symbol, current_price)

                if "error" in pivots or not pivots.get("pivot_point"):
                    lines.append(f"  • {symbol}: Pivot data unavailable")
                    continue

                # Format pivot info
                pp = pivots["PP"]
                r1 = pivots["R1"]
                s1 = pivots["S1"]
                signal = pivots["signal"]
                interpretation = pivots["interpretation"]

                signal_emoji = {
                    "long_opportunity": "🟢",
                    "short_opportunity": "🔴",
                    "bullish_zone": "🔵",
                    "bearish_zone": "🟠",
                }.get(signal, "⚪")

                lines.append(f"  • {symbol} {signal_emoji}: ${current_price:,.2f}")
                lines.append(f"    - PP: ${pp:,.2f} | R1: ${r1:,.2f} | S1: ${s1:,.2f}")
                lines.append(f"    - {interpretation[:100]}...")  # Truncate
                lines.append("")

        # 2. Pivot points for high-score symbols (OPTION B: score > 0.7)
        lines.append("🔍 HIGH-SCORE SYMBOLS - Pivot Analysis (score > 0.7):")
        lines.append("")

        # Use high-score symbols if provided, otherwise fallback to top 3 by price
        if high_score_symbols:
            target_symbols = high_score_symbols[:10]  # Limit to top 10 for performance
        else:
            # Fallback: top 3 by price (backwards compatibility)
            sorted_symbols = sorted(prices.items(), key=lambda x: x[1], reverse=True)[:3]
            target_symbols = [sym for sym, _ in sorted_symbols]

        for symbol in target_symbols:
            # Skip if already have position
            if any(pos.get("symbol") == symbol for pos in positions):
                continue

            price = prices.get(symbol, 0)
            if price <= 0:
                continue

            pivots = await calculator.calculate_pivot_points(symbol, price)

            if "error" in pivots or not pivots.get("pivot_point"):
                continue

            pp = pivots["PP"]
            r1 = pivots["R1"]
            s1 = pivots["S1"]
            signal = pivots["signal"]
            interpretation = pivots["interpretation"]

            signal_emoji = {
                "long_opportunity": "🟢",
                "short_opportunity": "🔴",
                "bullish_zone": "🔵",
                "bearish_zone": "🟠",
            }.get(signal, "⚪")

            lines.append(f"  • {symbol} {signal_emoji}: ${price:,.2f}")
            lines.append(f"    - PP: ${pp:,.2f} | R1: ${r1:,.2f} | S1: ${s1:,.2f}")
            lines.append(f"    - {interpretation[:100]}...")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
        lines.append("PIVOT POINT TRADING RULES:")
        lines.append("  1. If price NEAR S1 (< 2% distance) → Consider LONG (bounce up likely)")
        lines.append("  2. If price NEAR R1 (< 2% distance) → Consider SHORT (bounce down likely)")
        lines.append("  3. If price BREAKS R1 upward → Strong BULLISH (target R2)")
        lines.append("  4. If price BREAKS S1 downward → Strong BEARISH (target S2)")
        lines.append("  5. Above PP = Bullish bias | Below PP = Bearish bias")
        lines.append("")

        return "\n".join(lines)

    except Exception as e:
        logger.error("Failed to format pivot points", exc_info=True)
        return "Pivot Points Analysis: Unavailable (error)"


def _format_sentiment() -> str:
    """
    Format Fear & Greed sentiment index for AI prompt (RIZZO VIDEO INTEGRATION).

    Returns:
        Formatted string with sentiment analysis
    """
    try:
        from services.market_data.sentiment_tracker import get_sentiment_tracker

        tracker = get_sentiment_tracker()
        return tracker.get_sentiment_for_ai()

    except Exception as e:
        logger.error("Failed to format sentiment index", exc_info=True)
        return "Sentiment Index: Unavailable (error)"


def _get_strategy_weights(account: Account) -> dict:
    """
    Get strategy weights from account or return defaults.

    Weights determine how much importance AI gives to each indicator.
    Higher weight = more important in decision-making.

    **LEARNING INTEGRATION**:
    - Reads from `account.indicator_weights` (learned weights from DeepSeek self-analysis)
    - Falls back to `account.strategy_weights` (manual overrides, legacy)
    - Falls back to default weights if neither is set

    Args:
        account: Account model with optional indicator_weights/strategy_weights JSON field

    Returns:
        Dict with weights for each indicator (0.0-1.0)
    """
    # Default weights (as recommended in video + Prophet)
    default_weights = {
        "pivot_points": 0.8,  # Highest priority (pattern recognition)
        "prophet": 0.5,  # PRIMARY BIAS (price forecasting - RIZZO PRIORITY 6)
        "rsi_macd": 0.5,  # Medium-high (technical analysis)
        "whale_alerts": 0.4,  # Medium (short-term signal)
        "sentiment": 0.3,  # Low (contrarian indicator, noisy)
        "news": 0.2,  # Lowest (mostly noise in crypto)
    }

    # Priority: indicator_weights (learned) > strategy_weights (manual) > defaults
    if account.indicator_weights:
        # Use learned weights from DeepSeek self-analysis (auto-applied daily)
        weights = {**default_weights, **account.indicator_weights}
        logger.debug(
            f"Using learned indicator_weights for account {account.id}: {account.indicator_weights}"
        )
    elif account.strategy_weights:
        # Fallback to manual strategy_weights (legacy, or manual override)
        weights = {**default_weights, **account.strategy_weights}
        logger.debug(
            f"Using manual strategy_weights for account {account.id}: {account.strategy_weights}"
        )
    else:
        # Fallback to defaults
        weights = default_weights
        logger.debug(f"Using default weights for account {account.id}")

    return weights


def _format_whale_alerts() -> str:
    """
    Format whale alerts for AI prompt (RIZZO VIDEO INTEGRATION).

    Returns:
        Formatted string with whale transaction alerts
    """
    try:
        from services.market_data.whale_tracker import get_whale_tracker

        tracker = get_whale_tracker()

        # Fetch recent alerts (will use cache if called recently)
        tracker.get_recent_alerts()

        # Return formatted summary
        return tracker.get_whale_summary_for_ai()

    except Exception as e:
        logger.error("Failed to format whale alerts", exc_info=True)
        return "Whale Alerts: Unavailable (error)"


def optimize_ai_prompt(
    news_summary: str,
    market_data: dict,
    portfolio: dict,
    max_headlines: int = 10,
    max_chars_per_headline: int = 100,
) -> tuple[str, int]:
    """
    Optimize AI prompt by summarizing news to headlines + key points (T093).

    Reduces token count from ~5000 to ~500 (90% reduction) by:
    - Limiting to top N most recent headlines
    - Truncating each headline to max_chars_per_headline
    - Removing redundant information

    Args:
        news_summary: Full news content from fetch_latest_news()
        market_data: Market prices dictionary
        portfolio: Portfolio data dictionary
        max_headlines: Maximum number of news headlines to include (default: 10)
        max_chars_per_headline: Max characters per headline (default: 100)

    Returns:
        Tuple of (optimized_news_summary, estimated_token_count)

    Example:
        >>> news = fetch_latest_news()
        >>> optimized, tokens = optimize_ai_prompt(news, prices, portfolio)
        >>> print(f"Reduced to {tokens} tokens")
    """
    if not news_summary:
        return "No recent news available.", 10

    # Split news into individual entries (by newline)
    news_lines = [line.strip() for line in news_summary.split("\n") if line.strip()]

    # Take only top N most recent headlines
    headlines = news_lines[:max_headlines]

    # Truncate each headline to max_chars
    optimized_headlines = []
    for headline in headlines:
        if len(headline) > max_chars_per_headline:
            # Truncate and add ellipsis
            truncated = headline[:max_chars_per_headline].rstrip() + "..."
        else:
            truncated = headline

        optimized_headlines.append(truncated)

    # Join headlines
    optimized_news = "\n".join(optimized_headlines)

    # Estimate token count (rough estimate: ~4 chars per token)
    estimated_tokens = len(optimized_news) // 4

    logger.debug(
        f"Prompt optimization: {len(news_summary)} chars → {len(optimized_news)} chars "
        f"({len(optimized_news) / len(news_summary) * 100:.1f}%), ~{estimated_tokens} tokens"
    )

    return optimized_news, estimated_tokens


def count_tokens(text: str) -> int:
    """
    Estimate token count for text (T095).

    Uses rough approximation: 1 token ≈ 4 characters for English text.
    This is based on OpenAI's tokenizer averages.

    For more accurate counting, use tiktoken library (optional dependency).

    Args:
        text: Text to count tokens for

    Returns:
        Estimated token count

    Example:
        >>> prompt = "Analyze the following market data..."
        >>> tokens = count_tokens(prompt)
        >>> cost = tokens * 0.14 / 1_000_000  # DeepSeek pricing
    """
    # Rough estimate: 1 token ≈ 4 characters
    # This is conservative (overestimates tokens slightly)
    return len(text) // 4


def calculate_api_cost(input_tokens: int, output_tokens: int, provider: str = "deepseek") -> float:
    """
    Calculate API call cost in USD (T095).

    Pricing (as of 2025):
    - DeepSeek: $0.14 per 1M input tokens, $0.28 per 1M output tokens

    Args:
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        provider: API provider ("deepseek" or custom)

    Returns:
        Cost in USD

    Example:
        >>> cost = calculate_api_cost(input_tokens=1500, output_tokens=200)
        >>> print(f"API call cost: ${cost:.6f}")
    """
    if provider.lower() == "deepseek":
        # DeepSeek pricing
        input_cost_per_million = 0.14
        output_cost_per_million = 0.28
    else:
        # Default to DeepSeek pricing
        input_cost_per_million = 0.14
        output_cost_per_million = 0.28

    input_cost = (input_tokens / 1_000_000) * input_cost_per_million
    output_cost = (output_tokens / 1_000_000) * output_cost_per_million

    total_cost = input_cost + output_cost

    return total_cost


class AIDecisionCache:
    """
    Cache for AI trading decisions with market state hashing (T096).

    Prevents duplicate AI calls when market state is similar within a time window.
    Uses MD5 hash of (price, position, news_ids) to identify similar market states.

    Example:
        >>> cache = AIDecisionCache(window_seconds=600)  # 10 minute window
        >>> decision = cache.get_or_generate_decision(
        ...     market_state={"price": 50000, "position": 0.5, "news_ids": "abc123"},
        ...     generate_func=lambda: call_ai_for_decision(account, portfolio, prices)
        ... )
    """

    def __init__(self, window_seconds: int = 600):
        """
        Initialize AI decision cache.

        Args:
            window_seconds: Cache window in seconds (default: 600 = 10 minutes)
        """
        self.window_seconds = window_seconds
        self._cache: dict[str, tuple[dict, float]] = {}  # {state_hash: (decision, timestamp)}
        self._lock = Lock()

        # Metrics
        self._cache_hits = 0
        self._cache_misses = 0

        logger.info(
            f"AIDecisionCache initialized with window={window_seconds}s ({window_seconds / 60:.1f} minutes)"
        )

    def _hash_market_state(self, price: float, position: float, news_ids: str) -> str:
        """
        Hash market state to detect similar conditions (T096).

        Uses MD5 hash of concatenated values to create unique state identifier.

        Args:
            price: Current market price
            position: Current position size
            news_ids: Hash or concatenation of news article IDs

        Returns:
            MD5 hash string (32 characters)
        """
        # Create state string
        state_str = f"{price:.2f}|{position:.4f}|{news_ids}"

        # Generate MD5 hash
        state_hash = hashlib.md5(state_str.encode()).hexdigest()

        return state_hash

    def get_or_generate_decision(
        self,
        price: float,
        position: float,
        news_summary: str,
        generate_func: Callable[[], dict | None],
    ) -> dict | None:
        """
        Get cached decision or generate new one if cache miss (T097).

        Checks cache by market state hash. If recent decision exists (within window),
        returns it (cache hit). Otherwise, calls generate_func to get fresh decision
        (cache miss) and caches it.

        Args:
            price: Current market price
            position: Current position size
            news_summary: News content for hashing
            generate_func: Function to call on cache miss (e.g., call_ai_for_decision)

        Returns:
            AI decision dictionary or None

        Example:
            >>> decision = cache.get_or_generate_decision(
            ...     price=50000.0,
            ...     position=0.5,
            ...     news_summary="Bitcoin news...",
            ...     generate_func=lambda: call_ai_for_decision(account, portfolio, prices)
            ... )
        """
        # Create news hash (simple hash of news content)
        news_hash = hashlib.md5(news_summary.encode()).hexdigest()[:8]

        # Hash market state
        state_hash = self._hash_market_state(price, position, news_hash)

        with self._lock:
            # Clean up expired entries
            self._cleanup_expired()

            # Check cache
            if state_hash in self._cache:
                cached_decision, timestamp = self._cache[state_hash]
                cache_age = time.time() - timestamp

                if cache_age < self.window_seconds:
                    # Cache hit
                    self._cache_hits += 1
                    logger.info(
                        f"AI decision cache HIT (age: {cache_age:.0f}s, "
                        f"hits: {self._cache_hits}, misses: {self._cache_misses})"
                    )
                    return cached_decision

            # Cache miss - generate new decision
            self._cache_misses += 1
            logger.info(
                f"AI decision cache MISS (hits: {self._cache_hits}, misses: {self._cache_misses})"
            )

        # Generate decision (outside lock to avoid blocking)
        try:
            fresh_decision = generate_func()

            # Cache the decision
            with self._lock:
                self._cache[state_hash] = (fresh_decision, time.time())

            logger.info(f"AI decision cached with state_hash={state_hash[:8]}")

            return fresh_decision

        except Exception as err:
            logger.error(f"Failed to generate AI decision: {err}", exc_info=True)
            return None

    def _cleanup_expired(self) -> None:
        """
        Remove expired cache entries (T097).

        Called automatically during get_or_generate_decision().
        """
        current_time = time.time()
        expired_keys = [
            key
            for key, (_, timestamp) in self._cache.items()
            if current_time - timestamp >= self.window_seconds
        ]

        for key in expired_keys:
            del self._cache[key]

        if expired_keys:
            logger.debug(f"Cleaned up {len(expired_keys)} expired AI decision cache entries")

    def invalidate(self) -> None:
        """
        Manually invalidate all cached decisions.

        Useful for testing or when you want to force fresh AI decisions.
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            logger.info(f"AI decision cache manually invalidated ({count} entries cleared)")

    def get_cache_stats(self) -> dict:
        """
        Get cache statistics for monitoring (T098).

        Returns:
            Dictionary with cache metrics:
            - hits: Number of cache hits
            - misses: Number of cache misses
            - hit_rate: Percentage of cache hits (0-100)
            - cached_entries: Number of cached decisions
            - window_seconds: Configured cache window
        """
        total_requests = self._cache_hits + self._cache_misses
        hit_rate = (self._cache_hits / total_requests * 100) if total_requests > 0 else 0.0

        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "total_requests": total_requests,
            "hit_rate": round(hit_rate, 2),
            "cached_entries": len(self._cache),
            "window_seconds": self.window_seconds,
        }

    def reset_stats(self) -> None:
        """Reset cache statistics counters."""
        with self._lock:
            self._cache_hits = 0
            self._cache_misses = 0
            logger.info("AI decision cache statistics reset")


# Global decision cache instance (singleton pattern)
# Default window: 10 minutes (600 seconds)
_global_decision_cache: AIDecisionCache | None = None


def get_decision_cache(window_seconds: int = 600) -> AIDecisionCache:
    """
    Get the global AI decision cache instance (singleton).

    Args:
        window_seconds: Cache window if creating new instance (default: 600)

    Returns:
        Global AIDecisionCache instance

    Example:
        >>> cache = get_decision_cache(window_seconds=300)  # 5 minutes
        >>> decision = cache.get_or_generate_decision(...)
    """
    global _global_decision_cache

    if _global_decision_cache is None:
        _global_decision_cache = AIDecisionCache(window_seconds=window_seconds)
        logger.info(f"Global AI decision cache instance created with window={window_seconds}s")

    return _global_decision_cache


async def call_ai_for_decision(
    account: Account, portfolio: dict, prices: dict[str, float]
) -> dict | None:
    """Call AI model API to get trading decision (async for pivot points calculation)"""
    # Check if this is a default API key
    if _is_default_api_key(account.api_key):
        logger.info(f"Skipping AI trading for account {account.name} - using default API key")
        return None

    try:
        # Fetch news (with caching - T091)
        news_summary = fetch_latest_news()
        news_section = news_summary if news_summary else "No recent CoinJournal news available."

        # Generate list of available symbols
        available_symbols = ", ".join([f'"{s}"' for s in SUPPORTED_SYMBOLS.keys()])

        # Format technical analysis for AI (CRITICAL: quantitative signals)
        technical_section = _format_technical_analysis(portfolio.get("technical_factors", {}))

        # OPTION B: Extract high-score symbols (score > 0.7) for Prophet + Pivot analysis
        technical_factors = portfolio.get("technical_factors", {})
        recommendations = technical_factors.get("recommendations", [])

        # Filter symbols with score > 0.7 and sort by score (descending)
        high_score_symbols = [
            rec["symbol"]
            for rec in sorted(recommendations, key=lambda x: x.get("score", 0), reverse=True)
            if rec.get("score", 0) > 0.7
        ]

        logger.info(f"Filtered {len(high_score_symbols)} high-score symbols (score > 0.7) for Prophet+Pivot analysis: {high_score_symbols[:5]}...")

        # RIZZO VIDEO INTEGRATION: Calculate pivot points for high-score symbols (OPTION B)
        pivot_section = await _format_pivot_points(portfolio, prices, high_score_symbols)

        # RIZZO VIDEO INTEGRATION: Get Fear & Greed sentiment index
        sentiment_section = _format_sentiment()

        # RIZZO VIDEO INTEGRATION: Get whale alerts (large transactions)
        whale_section = _format_whale_alerts()

        # RIZZO VIDEO INTEGRATION (PRIORITY 6): Get Prophet forecasts for high-score symbols (OPTION B)
        # Calculate profit % for each position to help AI make informed decisions
        positions_with_profit = []
        for pos in portfolio.get("positions", []):
            symbol = pos.get("symbol")
            entry_price = pos.get("avg_cost", 0)
            current_price = prices.get(symbol, entry_price)

            if entry_price > 0:
                profit_pct = ((current_price - entry_price) / entry_price) * 100
            else:
                profit_pct = 0

            pos_with_profit = pos.copy()
            pos_with_profit["current_price"] = current_price
            pos_with_profit["profit_pct"] = round(profit_pct, 2)
            positions_with_profit.append(pos_with_profit)

        # Convert prices dict to TOON format for efficient LLM communication
        prices_list = [{"symbol": s, "price": p} for s, p in prices.items()]
        prices_toon = toon_encode(prices_list, root_name="market_prices")

        # Convert positions to TOON format
        positions_toon = toon_encode(positions_with_profit, root_name="positions") if positions_with_profit else "positions[0]:\n(No active positions)"

        # Log token savings (for monitoring efficiency)
        prices_json = json.dumps(prices, indent=2)
        savings = estimate_token_savings(prices_json, prices_toon)
        logger.info(
            f"TOON encoding savings: {savings['savings_pct']}% "
            f"({savings['savings_tokens']} tokens saved, "
            f"{savings['toon_tokens_estimate']} vs {savings['json_tokens_estimate']})"
        )

        # Get strategy weights from account (or use defaults)
        weights = _get_strategy_weights(account)

        # Full prompt without optimization - preserves all information for quality (T093-T094 removed)
        prompt = f"""You are a cryptocurrency trading AI. Based on the following portfolio and market data, decide on a trading action.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 STRATEGY WEIGHTS - PRIORITIZE INDICATORS (RIZZO VIDEO)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**CRITICAL**: When analyzing market data, give MORE importance to higher-weighted indicators.
When indicators CONFLICT, FAVOR the higher-weighted signals.

**Your Strategy Configuration**:
- 🔴 Pivot Points (weight: {weights['pivot_points']:.2f}) ← {"HIGHEST PRIORITY" if weights['pivot_points'] >= 0.7 else "HIGH PRIORITY" if weights['pivot_points'] >= 0.5 else "MEDIUM PRIORITY"}
- 🟣 Prophet Forecast (weight: {weights['prophet']:.2f}) ← PRIMARY BIAS (price forecasting - RIZZO PRIORITY 6)
- 🟡 RSI/MACD Technical (weight: {weights['rsi_macd']:.2f}) ← {"HIGH PRIORITY" if weights['rsi_macd'] >= 0.5 else "MEDIUM PRIORITY"}
- 🟢 Whale Alerts (weight: {weights['whale_alerts']:.2f}) ← {"HIGH PRIORITY" if weights['whale_alerts'] >= 0.5 else "MEDIUM PRIORITY" if weights['whale_alerts'] >= 0.3 else "LOW PRIORITY"}
- 🔵 Sentiment Index (weight: {weights['sentiment']:.2f}) ← {"MEDIUM PRIORITY" if weights['sentiment'] >= 0.3 else "LOW PRIORITY"}
- ⚪ News (weight: {weights['news']:.2f}) ← {"LOW PRIORITY" if weights['news'] <= 0.3 else "MEDIUM PRIORITY"}

**Decision-Making Rules**:
1. When Prophet (weight {weights['prophet']:.2f}) says BULLISH + Pivot Points (weight {weights['pivot_points']:.2f}) confirm:
   → HIGHEST CONVICTION LONG (both primary indicators align!)

2. When Prophet (weight {weights['prophet']:.2f}) conflicts with Sentiment (weight {weights['sentiment']:.2f}):
   → FOLLOW Prophet (higher weight + PRIMARY BIAS)

3. When RSI/MACD (weight {weights['rsi_macd']:.2f}) conflicts with Whale Alerts (weight {weights['whale_alerts']:.2f}):
   → Compare weights: Higher weight indicator takes priority

4. When ALL indicators align (same direction) → MAXIMUM CONVICTION trade (rare!)

5. When indicators conflict with similar weights → REDUCE position size or HOLD

**Example Decision Process**:
- Prophet: BULLISH +1.3% (0.5 weight) + Pivot: LONG (0.8 weight) + RSI: LONG (0.5 weight) + Sentiment: FEAR (0.3 weight)
- Total LONG score: 0.5 + 0.8 + 0.5 = 1.8
- Total SHORT score: 0.3
- Decision: VERY STRONG LONG (prophet + pivot + technical confirm, sentiment is contrarian)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Portfolio Data:
- Total Assets: ${portfolio.get("total_assets", 0):.2f}
- Current Positions (with profit/loss % in TOON format):
{positions_toon}

Current Market Prices ({len(prices)} available cryptocurrencies in TOON format):
{prices_toon}

{technical_section}

{pivot_section}
{sentiment_section}

{whale_section}

Latest Crypto News (CoinJournal):
{news_section}

Available symbols for trading: {available_symbols}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🧠 INTELLIGENT PROFIT/LOSS MANAGEMENT (HIGHEST PRIORITY - EVALUATE FIRST!)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CRITICAL: You have FULL FLEXIBILITY and JUDGMENT when making profit/loss decisions.
The thresholds below are EVALUATION TRIGGERS, not automatic commands.

1. 📈 TAKE PROFIT EVALUATION (profit_pct > +5%):
   When a position crosses +5% profit, EVALUATE whether to sell using ALL available data:

   🟢 CONSIDER HOLDING if:
      • Technical score STILL > 0.8 (strong momentum continuing)
      • News sentiment POSITIVE (upcoming catalysts, partnerships, adoption)
      • Price trend STRONG upward (potential for more gains)
      • Recent volume INCREASING (market interest growing)

   🔴 CONSIDER SELLING if:
      • Technical score DROPPED significantly (momentum weakening)
      • News sentiment NEGATIVE (regulatory concerns, hacks, FUD)
      • Better opportunities available (sell winner to buy higher-potential asset)
      • Position reached extreme profit (>15-20%) without fundamental support

   💡 SMART EXAMPLES:
      • MERL at +12% profit, score 0.987, positive news → HOLD (still strong)
      • MERL at +12% profit, score 0.42, negative news → SELL (momentum lost)
      • MERL at +8% profit, but NEW coin has score 0.95 vs MERL's 0.65 → SELL MERL, BUY NEW

2. 📉 STOP LOSS EVALUATION (profit_pct < -5%):
   When a position crosses -5% loss, EVALUATE severity and context:

   🔴 MUST SELL IMMEDIATELY if:
      • Loss approaching -8% or worse (protecting capital is critical)
      • Technical score < 0.3 (strong downward momentum)
      • Negative news (project issues, security breach, regulatory action)
      • No signs of recovery (continued downtrend)

   🟡 MAY HOLD if loss JUST crossed -5% AND:
      • Technical score RECOVERING (>0.6 after recent dip)
      • Strong fundamentals remain (temporary market dip, not project issue)
      • Positive news catalyst coming (major update, listing, partnership)

   ⚠️ DEFAULT BIAS: When in doubt at -5% → SELL (better safe than sorry)

3. 🔄 PORTFOLIO OPTIMIZATION STRATEGY:
   You should ACTIVELY manage the portfolio by REBALANCING positions:

   📊 CONSIDER SELLING even if NO profit/loss trigger when:
      • A BETTER opportunity appears (higher technical score + positive news)
      • Position is stagnant (0-2% profit, low score, better alternatives exist)
      • Need cash to buy high-conviction opportunity (sell weakest position)

   💡 REBALANCING EXAMPLE:
      Current: DOOD (+2%, score 0.55), MERL (+3%, score 0.60)
      Opportunity: ONDO (score 0.92, breaking out, positive news)
      Decision: SELL DOOD (weakest), BUY ONDO (strongest opportunity)

4. 🎯 DECISION-MAKING FACTORS (ALL MATTER EQUALLY):
   When evaluating any sell decision, WEIGH ALL these factors:
   ✓ Current profit/loss % (trigger point)
   ✓ Technical score (momentum + support strength)
   ✓ News sentiment (positive/negative catalysts)
   ✓ Price trend direction (up/down/sideways)
   ✓ Alternative opportunities (compare with other available coins)
   ✓ Commission costs (0.10% roundtrip - factor in but don't let it paralyze you)

5. 💰 POSITION SIZE & CONSTRAINTS:
   - Positions are ~$10 each (Hyperliquid minimum order size)
   - You can ONLY sell 100% (no partial sells - $5 would be rejected)
   - Max 4-5 positions with current balance (~$47)

   ⚠️ IMPORTANT - AUTOMATIC $10 FLOOR ENFORCEMENT:
   The system automatically enforces the $10 minimum order size. If your percentage allocation
   (e.g., 25% of $37.95 = $9.49) calculates to less than $10, the validation layer will
   automatically bump it to exactly $10 (as long as you have ≥$10 cash available).

   **DON'T pre-reject trades due to minimum order size concerns!**
   • If you want to buy at 25% but it's below $10 → Still choose BUY, system will bump to $10
   • Example: Cash=$37.95, want 25% allocation → System auto-adjusts to $10 order
   • Only avoid trade if cash < $10 (truly cannot meet minimum)

   Let the validation handle the floor adjustment automatically - focus on signal quality!

6. 🎲 CAPITAL ALLOCATION STRATEGY (CRITICAL - TOP PRIORITY!):
   **RULE #1: CHECK SCORE FIRST - If score < 0.95, ALWAYS use 25% MAX (target_portion: 0.25)**

   ⚡ EXCEPTION: 100% allocation (target_portion: 1.0) ONLY when ALL 3 conditions TRUE:
      1. Technical score >= 0.95 (EXCEPTIONAL, not just "good"!)
      2. AND Momentum >= 0.90 (extremely strong trending)
      3. AND Positive fundamental news (catalyst present)

      **ALL 3 MUST BE TRUE!** If score < 0.95 → use 25% even if momentum is 0.95!

   📊 NORMAL RULE (99% of cases): Higher conviction with 50% allocations:
      • Score 0.90-0.84 → **ALWAYS use target_portion: 0.5** (50% allocation for strong signals)
      • **CRITICAL: Only buy when score >= 0.90** (selective approach, quality over quantity)
      • Build portfolio of 2-3 positions at 50% each for concentrated conviction
      • Example: Have MERL at 50% + see ZEC score 0.80 → BUY ZEC at 50% (strong signal!)
      • Continue adding 50% positions until: (a) you have 2-3 positions, OR (b) no signals >= 0.90
      • **ONLY trade on STRONG signals >= 0.90** - weak signals waste capital on fees
      • Risk management: fewer trades = less fees, higher conviction = better win rate

   ❌ Score < 0.90 → HOLD or SELL (no new positions, too weak, high risk of loss)

   💡 ALLOCATION EXAMPLES (PAY ATTENTION - These show the EXACT logic):
      • Score 0.92, Momentum 0.95, News: BTC ETF approval → 100% OK! (score >= 0.95 ✓)
      • Score 0.95, Momentum 0.95, News: positive → 100% OK! (exactly 0.95 counts ✓)
      • Score 0.84, Momentum 0.95, News: positive → 50%! (score >= 0.90, < 0.95 → 50%)
      • Score 0.80, Momentum 0.93, News: positive → 50%! (score >= 0.90 → 50%)
      • Score 0.90, Momentum 0.88, News: neutral → 50% (score exactly 0.90 → 50%)
      • Score 0.70, Momentum 0.90, News: positive → HOLD (score < 0.90, too weak)
      • Score 0.60, Momentum 0.70, News: positive → HOLD (score < 0.90, avoid!)

   🎯 CRITICAL DECISION TREE:
      1. Is score >= 0.95?
         → NO: Check if score >= 0.90...
         → YES: Check momentum and news for 100% allocation
      2. Is score >= 0.90 (but < 0.95)?
         → YES: Use 50% allocation (target_portion: 0.5)
         → NO: HOLD (score < 0.90 is too weak, skip trade)
      3. If score >= 0.95, is momentum >= 0.90 AND news positive?
         → YES: Use 100% allocation (target_portion: 1.0)
         → NO: Use 50% allocation (target_portion: 0.5)

   🎯 PHILOSOPHY:
      • Exceptional opportunities (score >= 0.95 + all conditions) deserve 100% commitment
      • Strong opportunities (0.90-0.84) deserve concentrated bets (50%)
      • Weak signals (< 0.90) deserve patience (HOLD) - avoid wasting capital on fees

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Analyze ALL available cryptocurrencies, identify the BEST opportunity based on PROFIT/LOSS TRIGGERS FIRST, then TECHNICAL ANALYSIS, news, and market data.

**IMPORTANT - SHORT ONLY MODE ENABLED**:
Currently ONLY SHORT positions are allowed. DO NOT output "buy" operations.
- If you see a bullish opportunity → output "hold" (we are not taking LONG positions)
- Only output "short" when you see clear bearish signals (technical score < 0.3)
- You can "sell" to close existing positions
- This is temporary until we improve the LONG strategy

Then respond with ONLY a JSON object in this exact format:
{{
  "operation": "sell" or "short" or "hold",
  "symbol": one of the available symbols above,
  "target_portion_of_balance": 1.0,
  "leverage": 1,
  "reason": "Brief explanation citing profit/loss % OR technical score and other factors"
}}

DECISION-MAKING APPROACH (HOLISTIC & INTELLIGENT):
Your job is to make the SMARTEST decision possible considering ALL factors together:

🎯 PRIMARY FOCUS: Maximize portfolio value through intelligent trading
   • Check existing positions first - are any at profit/loss triggers?
   • Evaluate if current positions should be held or sold
   • Consider new opportunities only after optimizing current holdings

📊 WEIGH ALL FACTORS TOGETHER (not sequentially):
   1. Position profit/loss % (triggers at ±5%)
   2. Technical scores (momentum + support strength)
   3. News sentiment (catalysts, risks, market narrative)
   4. Price trends (direction, volume, market conditions)
   5. Comparative opportunities (which asset has BEST risk/reward NOW?)

💡 BE PROACTIVE ABOUT REBALANCING:
   • Don't wait for triggers - sell weak positions for strong opportunities
   • Think like a portfolio manager: "Is this the best use of capital RIGHT NOW?"
   • Consider opportunity cost: holding mediocre position vs buying strong one

Rules - OPERATIONS:
- operation must be "buy", "short", "sell", or "hold"
- "buy": Open LONG position (profit when price goes UP) - use when technical score > 0.7
- "short": Open SHORT position (profit when price goes DOWN) - use when technical score < 0.3 (indicates WEAKNESS, price likely to fall)
- "sell": Close existing position (long or short)
- "hold": No action taken when signals are unclear (0.3-0.7 range)

Rules - POSITION SIZING:
- target_portion_of_balance: % of available capital to use (0.0-1.0)
- For "buy"/"short": % of cash to allocate for new position
- For "sell": % of existing position to close (0.0-1.0, where 1.0 = close 100%)

Rules - LEVERAGE (CRITICAL):
- leverage: Multiplier for position size (1-10 allowed, use intelligently based on signal strength)
- leverage=1: No leverage (1:1 capital) - safest, use for weak signals (0.3-0.5 or 0.5-0.7)
- leverage=2-3: Moderate leverage - use for moderate signals (0.5-0.65 or 0.35-0.5)
- leverage=4-5: High leverage - use for strong signals (0.65-0.90 or 0.25-0.35)
- leverage=6-10: Very high leverage - use ONLY for VERY STRONG signals (>0.8 or <0.2)
- Higher leverage = Higher profit potential BUT MUCH HIGHER RISK
- CONSERVATIVE APPROACH: For technical score 0.7-0.8 or 0.2-0.3, use leverage 2-4x maximum
- AGGRESSIVE APPROACH: For technical score >0.8 or <0.2, you can use up to 10x leverage
- You have FULL FREEDOM on target_portion_of_balance (0.0-1.0) - use ANY percentage you think is optimal
- CRITICAL: You can ONLY sell positions that are listed in "Current Positions" above
- CRITICAL: If a symbol is NOT in "Current Positions", you CANNOT sell it (choose "buy" or "hold" instead)
- IMPORTANT: Consider selling underperforming positions to free up cash for better opportunities
- IMPORTANT: Diversification is key - don't get stuck in losing positions just because cash is low
- IMPORTANT: You can sell up to 100% of a position if needed to rebalance the portfolio
- IMPORTANT: ALWAYS prefer symbols with Technical Score > 0.90 for BUY decisions
- Scores below 0.90 are too weak and waste capital on fees → HOLD instead
- Only choose symbols you have data for

CRITICAL INVESTMENT PRINCIPLES:
1. FOLLOW TECHNICAL SIGNALS: If a symbol has Score > 0.7, strongly consider buying it
2. UNIT PRICE IS IRRELEVANT: What matters is PERCENTAGE GAIN, not number of coins
3. Buying $10 of BTC at $100,000/coin or $10 of DOGE at $0.17/coin gives the SAME return if both go up 10%
4. DO NOT favor cheap coins (like DOGE) just because you can buy "more coins"
5. Focus on: TECHNICAL SCORES FIRST, then news sentiment, market trends
6. A $100,000 Bitcoin with Score 0.8 is BETTER than a $0.17 DOGE with Score 0.4
7. Evaluate each cryptocurrency on TECHNICAL PERFORMANCE SIGNALS, not on unit price"""

        # Count tokens for cost tracking (T095)
        input_tokens = count_tokens(prompt)
        logger.info(
            f"AI prompt generated: ~{input_tokens} input tokens (full news preserved for quality)"
        )

        # Log complete prompt for TOON format verification (user can inspect what LLM receives)
        logger.info(f"=== COMPLETE AI PROMPT (TOON FORMAT) ===\n{prompt}\n=== END PROMPT ===")

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {account.api_key}"}

        # Use OpenAI-compatible chat completions format
        payload = {
            "model": account.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            # No max_tokens limit - let DeepSeek Reasoner use as many tokens as needed
        }

        # Construct API endpoint URL
        # Remove trailing slash from base_url if present
        base_url = account.base_url.rstrip("/")
        # Use /chat/completions endpoint (OpenAI-compatible)
        api_endpoint = f"{base_url}/chat/completions"

        # Retry logic for rate limiting
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    api_endpoint,
                    headers=headers,
                    json=payload,
                    timeout=300,  # 5 minutes for DeepSeek R1 long reasoning chains
                    verify=False,  # Disable SSL verification for custom AI endpoints
                )

                if response.status_code == 200:
                    break  # Success, exit retry loop
                elif response.status_code == 429:
                    # Rate limited, wait and retry
                    wait_time = (2**attempt) + random.uniform(
                        0, 1
                    )  # Exponential backoff with jitter
                    logger.warning(
                        f"AI API rate limited (attempt {attempt + 1}/{max_retries}), waiting {wait_time:.1f}s..."
                    )
                    if attempt < max_retries - 1:  # Don't wait on the last attempt
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(
                            f"AI API rate limited after {max_retries} attempts: {response.text}"
                        )
                        return None
                else:
                    logger.error(f"AI API returned status {response.status_code}: {response.text}")
                    return None
            except requests.RequestException as req_err:
                if attempt < max_retries - 1:
                    wait_time = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"AI API request failed (attempt {attempt + 1}/{max_retries}), retrying in {wait_time:.1f}s: {req_err}"
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"AI API request failed after {max_retries} attempts: {req_err}", exc_info=True)
                    return None

        result = response.json()

        # Track token usage from API response (T095)
        usage = result.get("usage", {})
        api_input_tokens = usage.get("prompt_tokens", input_tokens)  # Fallback to estimate
        api_output_tokens = usage.get("completion_tokens", 0)
        api_total_tokens = usage.get("total_tokens", api_input_tokens + api_output_tokens)

        # Calculate cost (T095)
        call_cost = calculate_api_cost(api_input_tokens, api_output_tokens, provider="deepseek")

        logger.info(
            f"AI API response received: "
            f"input={api_input_tokens} tokens, "
            f"output={api_output_tokens} tokens, "
            f"total={api_total_tokens} tokens, "
            f"cost=${call_cost:.6f}"
        )

        # Extract text from OpenAI-compatible response format
        if "choices" in result and len(result["choices"]) > 0:
            choice = result["choices"][0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "")

            # Check if response was truncated due to length limit
            if finish_reason == "length":
                logger.warning(
                    "AI response was truncated due to token limit. Consider increasing max_tokens."
                )
                # Try to get content from reasoning field if available (some models put partial content there)
                text_content = message.get("reasoning", "") or message.get("content", "")
            else:
                text_content = message.get("content", "")

            if not text_content:
                logger.error(f"Empty content in AI response: {result}")
                return None

            # Try to extract JSON from the text
            # Sometimes AI might wrap JSON in markdown code blocks
            text_content = text_content.strip()
            if "```json" in text_content:
                text_content = text_content.split("```json")[1].split("```")[0].strip()
            elif "```" in text_content:
                text_content = text_content.split("```")[1].split("```")[0].strip()

            # Handle potential JSON parsing issues with escape sequences
            try:
                decision = json.loads(text_content)
            except json.JSONDecodeError as parse_err:
                # Try to fix common JSON issues
                logger.warning(f"Initial JSON parse failed: {parse_err}")
                logger.warning(f"Problematic content: {text_content[:200]}...")

                # Try to clean up the text content
                cleaned_content = text_content

                # Replace problematic characters that might break JSON
                cleaned_content = cleaned_content.replace("\n", " ")
                cleaned_content = cleaned_content.replace("\r", " ")
                cleaned_content = cleaned_content.replace("\t", " ")

                # Handle unescaped quotes in strings by escaping them
                import re

                # Try a simpler approach to fix common JSON issues
                # Replace smart quotes and em-dashes with regular equivalents
                cleaned_content = cleaned_content.replace('"', '"').replace('"', '"')
                cleaned_content = cleaned_content.replace(""", "'").replace(""", "'")
                cleaned_content = cleaned_content.replace("–", "-").replace("—", "-")
                cleaned_content = cleaned_content.replace("‑", "-")  # Non-breaking hyphen

                # Try parsing again
                try:
                    decision = json.loads(cleaned_content)
                    logger.info("Successfully parsed JSON after cleanup")
                except json.JSONDecodeError:
                    # If still failing, try to extract just the essential parts
                    logger.error(
                        "JSON parsing failed even after cleanup, attempting manual extraction"
                    )
                    try:
                        # Extract operation, symbol, and target_portion manually
                        operation_match = re.search(r'"operation":\s*"([^"]+)"', text_content)
                        symbol_match = re.search(r'"symbol":\s*"([^"]+)"', text_content)
                        portion_match = re.search(
                            r'"target_portion_of_balance":\s*([0-9.]+)', text_content
                        )
                        reason_match = re.search(r'"reason":\s*"([^"]*)', text_content)

                        if operation_match and symbol_match and portion_match:
                            decision = {
                                "operation": operation_match.group(1),
                                "symbol": symbol_match.group(1),
                                "target_portion_of_balance": float(portion_match.group(1)),
                                "reason": reason_match.group(1)
                                if reason_match
                                else "AI response parsing issue",
                            }
                            logger.info("Successfully extracted AI decision manually")
                        else:
                            raise json.JSONDecodeError(
                                "Could not extract required fields", text_content, 0
                            )
                    except Exception:
                        raise parse_err  # Re-raise original error

            # Validate that decision is a dict with required structure
            if not isinstance(decision, dict):
                logger.error(f"AI response is not a dict: {type(decision)}")
                return None

            logger.info(f"AI decision for {account.name}: {decision}")
            return decision

        logger.error(f"Unexpected AI response format: {result}")
        return None

    except requests.RequestException as err:
        logger.error(f"AI API request failed: {err}", exc_info=True)
        return None
    except json.JSONDecodeError as err:
        logger.error(f"Failed to parse AI response as JSON: {err}", exc_info=True)
        # Try to log the content that failed to parse
        try:
            if "text_content" in locals():
                logger.error(f"Content that failed to parse: {text_content[:500]}", exc_info=True)
        except:
            pass
        return None
    except Exception as err:
        logger.error(f"Unexpected error calling AI: {err}", exc_info=True)
        return None


def save_ai_decision(
    db: Session,
    account: Account,
    decision: dict,
    portfolio: dict,
    executed: bool = False,
    order_id: int | None = None,
) -> None:
    """Save AI decision to the decision log"""
    try:
        operation = decision.get("operation", "").lower() if decision.get("operation") else ""
        symbol_raw = decision.get("symbol")
        symbol = symbol_raw.upper() if symbol_raw else None
        target_portion = (
            float(decision.get("target_portion_of_balance", 0))
            if decision.get("target_portion_of_balance") is not None
            else 0.0
        )
        reason = decision.get("reason", "No reason provided")

        # Calculate previous portion for the symbol
        prev_portion = 0.0
        if operation in ["sell", "hold"] and symbol:
            # Handle positions as list (from auto_trader) or dict (legacy)
            positions = portfolio.get("positions", [])

            # Find position for this symbol
            position = None
            if isinstance(positions, list):
                # New format: list of position dicts
                position = next((p for p in positions if p.get("symbol") == symbol), None)
            elif isinstance(positions, dict):
                # Legacy format: dict keyed by symbol
                position = positions.get(symbol)

            if position:
                # Calculate current value of this position
                quantity = position.get("quantity", 0)
                avg_cost = position.get("avg_cost", 0)
                symbol_value = quantity * avg_cost

                total_balance = portfolio["total_assets"]
                if total_balance > 0:
                    prev_portion = symbol_value / total_balance

        # Create decision log entry
        from datetime import datetime, timezone

        decision_log = AIDecisionLog(
            account_id=account.id,
            decision_time=datetime.now(timezone.utc),
            reason=reason,
            operation=operation,
            symbol=symbol if operation != "hold" else None,
            prev_portion=Decimal(str(prev_portion)),
            target_portion=Decimal(str(target_portion)),
            total_balance=Decimal(str(portfolio["total_assets"])),
            executed=executed,
            order_id=order_id,
        )

        db.add(decision_log)
        db.commit()

        symbol_str = symbol if symbol else "N/A"
        logger.info(
            f"Saved AI decision log for account {account.name}: {operation} {symbol_str} "
            f"prev_portion={prev_portion:.4f} target_portion={target_portion:.4f} executed={executed}"
        )

    except Exception as err:
        logger.error(f"Failed to save AI decision log: {err}", exc_info=True)
        db.rollback()


def get_active_ai_accounts(db: Session) -> list[Account]:
    """Get all active AI accounts that are not using default API key"""
    accounts = (
        db.query(Account).filter(Account.is_active == True, Account.account_type == "AI").all()
    )

    if not accounts:
        return []

    # Filter out default accounts
    valid_accounts = [acc for acc in accounts if not _is_default_api_key(acc.api_key)]

    if not valid_accounts:
        logger.debug("No valid AI accounts found (all using default keys)")
        return []

    return valid_accounts


async def call_ai_for_agent_decision(
    account: Account,
    portfolio: dict,
    prices: dict[str, float],
    agent_type: str
) -> dict | None:
    """
    Call AI model API to get trading decision for a specific agent type.

    This is similar to call_ai_for_decision but appends the agent-specific
    prompt suffix (LONG or SHORT) to guide the AI's response.

    Args:
        account: Trading account with API credentials
        portfolio: Portfolio data with positions and technical factors
        prices: Current market prices
        agent_type: "LONG" or "SHORT" - determines which agent prompt to use

    Returns:
        AI decision dict with operation, symbol, target_portion_of_balance, leverage, reason
        or None if error/skipped
    """
    # Check if this is a default API key
    if _is_default_api_key(account.api_key):
        logger.info(f"Skipping AI trading for account {account.name} - using default API key")
        return None

    try:
        # Fetch news (with caching)
        news_summary = fetch_latest_news()
        news_section = news_summary if news_summary else "No recent CoinJournal news available."

        # Generate list of available symbols
        available_symbols = ", ".join([f'"{s}"' for s in SUPPORTED_SYMBOLS.keys()])

        # Format technical analysis for AI
        technical_section = _format_technical_analysis(portfolio.get("technical_factors", {}))

        # Extract high-score symbols for pivot analysis
        technical_factors = portfolio.get("technical_factors", {})
        recommendations = technical_factors.get("recommendations", [])

        high_score_symbols = [
            rec["symbol"]
            for rec in sorted(recommendations, key=lambda x: x.get("score", 0), reverse=True)
            if rec.get("score", 0) > 0.7
        ]

        logger.info(f"[{agent_type}] Filtered {len(high_score_symbols)} high-score symbols for analysis")

        # Calculate pivot points
        pivot_section = await _format_pivot_points(portfolio, prices, high_score_symbols)

        # Get sentiment and whale data
        sentiment_section = _format_sentiment()
        whale_section = _format_whale_alerts()

        # Calculate profit % for each position
        positions_with_profit = []
        for pos in portfolio.get("positions", []):
            symbol = pos.get("symbol")
            entry_price = pos.get("avg_cost", 0)
            current_price = prices.get(symbol, entry_price)

            if entry_price > 0:
                profit_pct = ((current_price - entry_price) / entry_price) * 100
            else:
                profit_pct = 0

            pos_with_profit = pos.copy()
            pos_with_profit["current_price"] = current_price
            pos_with_profit["profit_pct"] = round(profit_pct, 2)
            positions_with_profit.append(pos_with_profit)

        # Convert to TOON format
        prices_list = [{"symbol": s, "price": p} for s, p in prices.items()]
        prices_toon = toon_encode(prices_list, root_name="market_prices")
        positions_toon = toon_encode(positions_with_profit, root_name="positions") if positions_with_profit else "positions[0]:\n(No active positions)"

        # Get strategy weights
        weights = _get_strategy_weights(account)

        # Get agent-specific prompt suffix
        if agent_type == "LONG":
            agent_prompt_suffix = get_long_agent_prompt()
        elif agent_type == "SHORT":
            agent_prompt_suffix = get_short_agent_prompt()
        else:
            logger.error(f"Invalid agent_type: {agent_type}")
            return None

        # Build prompt - simplified version focused on agent task
        prompt = f"""You are a cryptocurrency trading AI specialized for {agent_type} positions.

Portfolio Data:
- Total Assets: ${portfolio.get("total_assets", 0):.2f}
- Available Cash: ${portfolio.get("cash", 0):.2f} (50% allocated to {agent_type} agent)
- Current Positions:
{positions_toon}

Current Market Prices ({len(prices)} cryptocurrencies):
{prices_toon}

{technical_section}

{pivot_section}
{sentiment_section}

{whale_section}

Latest Crypto News:
{news_section}

Available symbols: {available_symbols}

{agent_prompt_suffix}

Analyze the market and respond with ONLY a JSON object:
{{
  "operation": "buy" or "short" or "sell" or "hold",
  "symbol": "SYMBOL",
  "target_portion_of_balance": 0.0 to 1.0,
  "leverage": 1 to 10,
  "reason": "Brief explanation"
}}

Remember: You are the {agent_type} agent. Focus ONLY on {agent_type.lower()} opportunities!
"""

        # Count tokens
        input_tokens = count_tokens(prompt)
        logger.info(f"[{agent_type}] AI prompt: ~{input_tokens} tokens")

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {account.api_key}"}

        payload = {
            "model": account.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        }

        base_url = account.base_url.rstrip("/")
        api_endpoint = f"{base_url}/chat/completions"

        # Retry logic
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    api_endpoint,
                    headers=headers,
                    json=payload,
                    timeout=300,
                    verify=False,
                )

                if response.status_code == 200:
                    break
                elif response.status_code == 429:
                    wait_time = (2**attempt) + random.uniform(0, 1)
                    logger.warning(f"[{agent_type}] API rate limited, waiting {wait_time:.1f}s...")
                    if attempt < max_retries - 1:
                        time.sleep(wait_time)
                        continue
                    return None
                else:
                    logger.error(f"[{agent_type}] API returned {response.status_code}: {response.text}")
                    return None
            except requests.RequestException as req_err:
                if attempt < max_retries - 1:
                    time.sleep((2**attempt) + random.uniform(0, 1))
                    continue
                logger.error(f"[{agent_type}] API request failed: {req_err}", exc_info=True)
                return None

        result = response.json()

        # Extract text from response
        if "choices" in result and len(result["choices"]) > 0:
            text_content = result["choices"][0].get("message", {}).get("content", "")

            if not text_content:
                logger.error(f"[{agent_type}] Empty content in AI response")
                return None

            # Clean up response
            text_content = text_content.strip()
            if "```json" in text_content:
                text_content = text_content.split("```json")[1].split("```")[0].strip()
            elif "```" in text_content:
                text_content = text_content.split("```")[1].split("```")[0].strip()

            try:
                decision = json.loads(text_content)
            except json.JSONDecodeError:
                logger.error(f"[{agent_type}] Failed to parse JSON response")
                return None

            logger.info(f"[{agent_type}] AI decision: {decision}")
            return decision

        logger.error(f"[{agent_type}] Unexpected response format: {result}")
        return None

    except Exception as err:
        logger.error(f"[{agent_type}] Unexpected error: {err}", exc_info=True)
        return None
