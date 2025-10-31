"""
AI Usage Tracker (T099)

Tracks AI API usage metrics for cost monitoring and optimization:
- Daily API calls count
- Daily token usage (input + output)
- Cache hit/miss tracking
- Estimated monthly cost calculation
- Historical metrics archiving

Features:
- Thread-safe operations
- Automatic daily reset at midnight
- Cost calculation based on provider pricing
- Integration with cache statistics
"""

import logging
from datetime import date
from threading import Lock

logger = logging.getLogger(__name__)


class AIUsageTracker:
    """
    Tracks AI API usage and costs for monitoring and optimization.

    Maintains daily counters that reset at midnight:
    - API calls made
    - Tokens used (input + output)
    - Cache hits/misses
    - Estimated cost

    Example:
        >>> tracker = AIUsageTracker()
        >>> tracker.record_api_call(input_tokens=1500, output_tokens=200)
        >>> tracker.record_cache_hit()
        >>> stats = tracker.get_usage_stats()
        >>> print(f"Daily cost: ${stats['daily_cost']:.4f}")
    """

    def __init__(self, provider: str = "deepseek"):
        """
        Initialize AI usage tracker.

        Args:
            provider: AI provider name for pricing (default: "deepseek")
        """
        self.provider = provider
        self._lock = Lock()

        # Current day tracking
        self._current_date = date.today()
        self._calls_today = 0
        self._input_tokens_today = 0
        self._output_tokens_today = 0
        self._cache_hits_today = 0
        self._cache_misses_today = 0

        # Previous day stats (for comparison)
        self._prev_calls = 0
        self._prev_tokens = 0
        self._prev_cost = 0.0

        # Provider pricing (per 1M tokens)
        self._pricing = self._get_pricing(provider)

        logger.info(f"AIUsageTracker initialized for provider={provider}")

    def _get_pricing(self, provider: str) -> dict[str, float]:
        """
        Get pricing for AI provider.

        Args:
            provider: Provider name

        Returns:
            Dictionary with input_cost_per_million and output_cost_per_million
        """
        pricing_table = {
            "deepseek": {"input_cost_per_million": 0.14, "output_cost_per_million": 0.28},
            "openai-gpt4": {"input_cost_per_million": 10.0, "output_cost_per_million": 30.0},
            "openai-gpt3.5": {"input_cost_per_million": 0.5, "output_cost_per_million": 1.5},
        }

        return pricing_table.get(provider.lower(), pricing_table["deepseek"])

    def _check_and_reset_if_new_day(self) -> None:
        """
        Check if a new day has started and reset counters if needed.

        Called automatically before any operation.
        """
        today = date.today()

        if today != self._current_date:
            # New day - archive previous day and reset
            logger.info(
                f"New day detected ({today}), resetting usage counters. "
                f"Previous day: {self._calls_today} calls, "
                f"{self._input_tokens_today + self._output_tokens_today} tokens, "
                f"${self._calculate_daily_cost():.4f}"
            )

            # Save previous day stats
            self._prev_calls = self._calls_today
            self._prev_tokens = self._input_tokens_today + self._output_tokens_today
            self._prev_cost = self._calculate_daily_cost()

            # Reset counters
            self._current_date = today
            self._calls_today = 0
            self._input_tokens_today = 0
            self._output_tokens_today = 0
            self._cache_hits_today = 0
            self._cache_misses_today = 0

    def record_api_call(self, input_tokens: int, output_tokens: int) -> None:
        """
        Record an AI API call with token usage.

        Args:
            input_tokens: Number of input tokens used
            output_tokens: Number of output tokens generated

        Example:
            >>> tracker.record_api_call(input_tokens=1500, output_tokens=200)
        """
        with self._lock:
            self._check_and_reset_if_new_day()

            self._calls_today += 1
            self._input_tokens_today += input_tokens
            self._output_tokens_today += output_tokens

            cost = self._calculate_call_cost(input_tokens, output_tokens)

            logger.debug(
                f"AI API call recorded: in={input_tokens}, out={output_tokens}, "
                f"cost=${cost:.6f}, total_today={self._calls_today}"
            )

    def record_cache_hit(self) -> None:
        """
        Record a cache hit (decision reused without AI call).

        Example:
            >>> tracker.record_cache_hit()
        """
        with self._lock:
            self._check_and_reset_if_new_day()
            self._cache_hits_today += 1

            logger.debug(f"Cache hit recorded, total_today={self._cache_hits_today}")

    def record_cache_miss(self) -> None:
        """
        Record a cache miss (new AI call required).

        Example:
            >>> tracker.record_cache_miss()
        """
        with self._lock:
            self._check_and_reset_if_new_day()
            self._cache_misses_today += 1

            logger.debug(f"Cache miss recorded, total_today={self._cache_misses_today}")

    def _calculate_call_cost(self, input_tokens: int, output_tokens: int) -> float:
        """
        Calculate cost for a single API call.

        Args:
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens

        Returns:
            Cost in USD
        """
        input_cost = (input_tokens / 1_000_000) * self._pricing["input_cost_per_million"]
        output_cost = (output_tokens / 1_000_000) * self._pricing["output_cost_per_million"]
        return input_cost + output_cost

    def _calculate_daily_cost(self) -> float:
        """
        Calculate total cost for today.

        Returns:
            Daily cost in USD
        """
        return self._calculate_call_cost(self._input_tokens_today, self._output_tokens_today)

    def get_usage_stats(self) -> dict:
        """
        Get current usage statistics.

        Returns:
            Dictionary with usage metrics:
            - date: Current date
            - calls_today: Number of API calls today
            - input_tokens_today: Input tokens used today
            - output_tokens_today: Output tokens used today
            - total_tokens_today: Total tokens used today
            - cache_hits_today: Cache hits today
            - cache_misses_today: Cache misses today
            - cache_hit_rate: Cache hit rate percentage
            - daily_cost: Estimated cost for today (USD)
            - estimated_monthly_cost: Estimated monthly cost (USD)
            - provider: AI provider name

        Example:
            >>> stats = tracker.get_usage_stats()
            >>> print(f"Today: {stats['calls_today']} calls, ${stats['daily_cost']:.4f}")
        """
        with self._lock:
            self._check_and_reset_if_new_day()

            total_tokens = self._input_tokens_today + self._output_tokens_today
            daily_cost = self._calculate_daily_cost()

            # Estimate monthly cost (daily cost × 30)
            estimated_monthly_cost = daily_cost * 30

            # Calculate cache hit rate
            total_cache_requests = self._cache_hits_today + self._cache_misses_today
            cache_hit_rate = (
                (self._cache_hits_today / total_cache_requests * 100)
                if total_cache_requests > 0
                else 0.0
            )

            return {
                "date": self._current_date.isoformat(),
                "calls_today": self._calls_today,
                "input_tokens_today": self._input_tokens_today,
                "output_tokens_today": self._output_tokens_today,
                "total_tokens_today": total_tokens,
                "cache_hits_today": self._cache_hits_today,
                "cache_misses_today": self._cache_misses_today,
                "total_cache_requests": total_cache_requests,
                "cache_hit_rate": round(cache_hit_rate, 2),
                "daily_cost": round(daily_cost, 6),
                "estimated_monthly_cost": round(estimated_monthly_cost, 4),
                "provider": self.provider,
                "pricing": self._pricing,
            }

    def get_previous_day_stats(self) -> dict:
        """
        Get previous day's statistics for comparison.

        Returns:
            Dictionary with previous day stats
        """
        return {
            "calls": self._prev_calls,
            "tokens": self._prev_tokens,
            "cost": round(self._prev_cost, 6),
        }

    def reset_today(self) -> None:
        """
        Manually reset today's counters.

        Useful for testing or administrative reset.
        """
        with self._lock:
            logger.info("Manually resetting today's usage counters")

            # Save current as previous
            self._prev_calls = self._calls_today
            self._prev_tokens = self._input_tokens_today + self._output_tokens_today
            self._prev_cost = self._calculate_daily_cost()

            # Reset counters
            self._calls_today = 0
            self._input_tokens_today = 0
            self._output_tokens_today = 0
            self._cache_hits_today = 0
            self._cache_misses_today = 0


# Global usage tracker instance (singleton pattern)
_global_usage_tracker: AIUsageTracker | None = None


def get_usage_tracker(provider: str = "deepseek") -> AIUsageTracker:
    """
    Get the global AI usage tracker instance (singleton).

    Args:
        provider: AI provider for pricing (default: "deepseek")

    Returns:
        Global AIUsageTracker instance

    Example:
        >>> tracker = get_usage_tracker()
        >>> tracker.record_api_call(input_tokens=1500, output_tokens=200)
    """
    global _global_usage_tracker

    if _global_usage_tracker is None:
        _global_usage_tracker = AIUsageTracker(provider=provider)
        logger.info(f"Global AI usage tracker instance created for provider={provider}")

    return _global_usage_tracker


async def reset_ai_usage_daily() -> None:
    """
    Daily job to reset AI usage counters at midnight (T101).

    This function is called by the scheduler at midnight to:
    - Archive previous day's metrics to database (future enhancement)
    - Reset daily counters to zero

    The AIUsageTracker automatically resets on date change, but this job
    ensures cleanup happens at a predictable time and can trigger additional
    archival tasks.

    Note: This is an async function for scheduler compatibility.
    """
    try:
        tracker = get_usage_tracker()

        # Get stats before reset (for logging)
        stats = tracker.get_usage_stats()

        logger.info(
            f"Daily AI usage reset triggered. "
            f"Yesterday: {stats['calls_today']} calls, "
            f"{stats['total_tokens_today']} tokens, "
            f"${stats['daily_cost']:.4f}"
        )

        # Reset counters (this also saves current as previous)
        tracker.reset_today()

        # TODO: Archive metrics to database for historical analysis (T102)
        # This would store yesterday's stats in a AIUsageHistory table

        logger.info("Daily AI usage reset completed successfully")

    except Exception as err:
        logger.error(f"Failed to reset daily AI usage: {err}", exc_info=True)
