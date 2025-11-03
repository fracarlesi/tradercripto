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

from database.models import Account, AIDecisionLog, Position
from services.asset_calculator import calc_positions_value
from services.market_data.news_feed import fetch_latest_news

logger = logging.getLogger(__name__)

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
        logger.error(f"Failed to load symbols from Hyperliquid: {e}")
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
            logger.error(f"Failed to generate AI decision: {err}")
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


def _get_portfolio_data(db: Session, account: Account) -> dict:
    """Get current portfolio positions and values"""
    positions = (
        db.query(Position)
        .filter(Position.account_id == account.id, Position.market == "CRYPTO")
        .all()
    )

    portfolio = {}
    for pos in positions:
        if float(pos.quantity) > 0:
            portfolio[pos.symbol] = {
                "quantity": float(pos.quantity),
                "avg_cost": float(pos.avg_cost),
                "current_value": float(pos.quantity) * float(pos.avg_cost),
            }

    return {
        "cash": float(account.current_cash),
        "frozen_cash": float(account.frozen_cash),
        "positions": portfolio,
        "total_assets": float(account.current_cash) + calc_positions_value(db, account.id),
    }


def call_ai_for_decision(
    account: Account, portfolio: dict, prices: dict[str, float]
) -> dict | None:
    """Call AI model API to get trading decision"""
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

        # Full prompt without optimization - preserves all information for quality (T093-T094 removed)
        prompt = f"""You are a cryptocurrency trading AI. Based on the following portfolio and market data, decide on a trading action.

Portfolio Data:
- Cash Available: ${portfolio["cash"]:.2f}
- Frozen Cash: ${portfolio["frozen_cash"]:.2f}
- Total Assets: ${portfolio["total_assets"]:.2f}
- Current Positions: {json.dumps(portfolio["positions"], indent=2)}

Current Market Prices (showing {len(prices)} available cryptocurrencies):
{json.dumps(prices, indent=2)}

Latest Crypto News (CoinJournal):
{news_section}

Available symbols for trading: {available_symbols}

Analyze ALL available cryptocurrencies, identify the BEST opportunity based on news, price trends, and market data.
Then respond with ONLY a JSON object in this exact format:
{{
  "operation": "buy" or "sell" or "hold",
  "symbol": one of the available symbols above,
  "target_portion_of_balance": 0.2,
  "reason": "Brief explanation of your decision"
}}

Rules:
- operation must be "buy", "sell", or "hold"
- For "buy": symbol is what to buy, target_portion_of_balance is % of cash to use (0.0-1.0)
- For "sell": symbol is what to sell, target_portion_of_balance is % of position to sell (0.0-1.0)
- For "hold": no action taken
- Keep target_portion_of_balance between 0.1 and 0.3 for risk management
- Only choose symbols you have data for"""

        # Count tokens for cost tracking (T095)
        input_tokens = count_tokens(prompt)
        logger.info(
            f"AI prompt generated: ~{input_tokens} input tokens (full news preserved for quality)"
        )

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {account.api_key}"}

        # Use OpenAI-compatible chat completions format
        payload = {
            "model": account.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 1000,
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
                    timeout=30,
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
                    logger.error(f"AI API request failed after {max_retries} attempts: {req_err}")
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
        logger.error(f"AI API request failed: {err}")
        return None
    except json.JSONDecodeError as err:
        logger.error(f"Failed to parse AI response as JSON: {err}")
        # Try to log the content that failed to parse
        try:
            if "text_content" in locals():
                logger.error(f"Content that failed to parse: {text_content[:500]}")
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
            positions = portfolio.get("positions", {})
            if symbol in positions:
                symbol_value = positions[symbol]["current_value"]
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
        logger.error(f"Failed to save AI decision log: {err}")
        db.rollback()


def get_active_ai_accounts(db: Session) -> list[Account]:
    """Get all active AI accounts that are not using default API key"""
    accounts = (
        db.query(Account).filter(Account.is_active == "true", Account.account_type == "AI").all()
    )

    if not accounts:
        return []

    # Filter out default accounts
    valid_accounts = [acc for acc in accounts if not _is_default_api_key(acc.api_key)]

    if not valid_accounts:
        logger.debug("No valid AI accounts found (all using default keys)")
        return []

    return valid_accounts
