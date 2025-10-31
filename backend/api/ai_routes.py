"""
AI Usage API Routes (T100)

Provides endpoints for monitoring AI API usage, costs, and cache statistics.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.ai_decision_service import get_decision_cache
from services.infrastructure.usage_tracker import get_usage_tracker
from services.news_feed import get_news_cache_stats

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["AI"])


class AIUsageResponse(BaseModel):
    """Response model for AI usage statistics."""

    # Daily usage
    date: str = Field(..., description="Current date (ISO format)")
    calls_today: int = Field(..., description="Number of AI API calls today")
    input_tokens_today: int = Field(..., description="Input tokens used today")
    output_tokens_today: int = Field(..., description="Output tokens used today")
    total_tokens_today: int = Field(..., description="Total tokens used today")

    # Cache statistics
    cache_hits_today: int = Field(..., description="AI decision cache hits today")
    cache_misses_today: int = Field(..., description="AI decision cache misses today")
    cache_hit_rate: float = Field(..., description="Cache hit rate percentage (0-100)")

    # Cost tracking
    daily_cost: float = Field(..., description="Estimated cost for today (USD)")
    estimated_monthly_cost: float = Field(..., description="Estimated monthly cost (USD)")

    # Provider info
    provider: str = Field(..., description="AI provider name")

    # News cache stats
    news_cache_hits: int = Field(..., description="News cache hits")
    news_cache_misses: int = Field(..., description="News cache misses")
    news_cache_hit_rate: float = Field(..., description="News cache hit rate percentage")
    news_cache_age_seconds: float | None = Field(None, description="Age of cached news in seconds")

    # Decision cache stats
    decision_cache_hits: int = Field(..., description="Decision cache hits")
    decision_cache_misses: int = Field(..., description="Decision cache misses")
    decision_cache_hit_rate: float = Field(..., description="Decision cache hit rate percentage")
    decision_cached_entries: int = Field(..., description="Number of cached decisions")

    class Config:
        json_schema_extra = {
            "example": {
                "date": "2025-10-31",
                "calls_today": 48,
                "input_tokens_today": 72000,
                "output_tokens_today": 9600,
                "total_tokens_today": 81600,
                "cache_hits_today": 12,
                "cache_misses_today": 48,
                "cache_hit_rate": 20.0,
                "daily_cost": 0.012384,
                "estimated_monthly_cost": 0.3715,
                "provider": "deepseek",
                "news_cache_hits": 45,
                "news_cache_misses": 3,
                "news_cache_hit_rate": 93.75,
                "news_cache_age_seconds": 1847.3,
                "decision_cache_hits": 12,
                "decision_cache_misses": 48,
                "decision_cache_hit_rate": 20.0,
                "decision_cached_entries": 8,
            }
        }


@router.get("/usage", response_model=AIUsageResponse)
async def get_ai_usage() -> AIUsageResponse:
    """
    Get AI API usage statistics and costs (T100).

    Returns comprehensive usage metrics including:
    - Daily API call count
    - Token usage (input/output)
    - Cache hit rates (news, decisions)
    - Estimated costs (daily, monthly)

    Example response:
    ```json
    {
      "date": "2025-10-31",
      "calls_today": 48,
      "total_tokens_today": 81600,
      "cache_hit_rate": 20.0,
      "daily_cost": 0.012384,
      "estimated_monthly_cost": 0.3715,
      "provider": "deepseek"
    }
    ```

    Returns:
        AIUsageResponse with current usage statistics

    Raises:
        HTTPException: If unable to retrieve usage statistics
    """
    try:
        # Get usage tracker stats
        tracker = get_usage_tracker()
        usage_stats = tracker.get_usage_stats()

        # Get news cache stats
        news_cache_stats = get_news_cache_stats()

        # Get decision cache stats
        decision_cache = get_decision_cache()
        decision_cache_stats = decision_cache.get_cache_stats()

        # Build response
        response = AIUsageResponse(
            # Daily usage
            date=usage_stats["date"],
            calls_today=usage_stats["calls_today"],
            input_tokens_today=usage_stats["input_tokens_today"],
            output_tokens_today=usage_stats["output_tokens_today"],
            total_tokens_today=usage_stats["total_tokens_today"],
            # Cache statistics
            cache_hits_today=usage_stats["cache_hits_today"],
            cache_misses_today=usage_stats["cache_misses_today"],
            cache_hit_rate=usage_stats["cache_hit_rate"],
            # Cost tracking
            daily_cost=usage_stats["daily_cost"],
            estimated_monthly_cost=usage_stats["estimated_monthly_cost"],
            # Provider
            provider=usage_stats["provider"],
            # News cache
            news_cache_hits=news_cache_stats["hits"],
            news_cache_misses=news_cache_stats["misses"],
            news_cache_hit_rate=news_cache_stats["hit_rate"],
            news_cache_age_seconds=news_cache_stats["age_seconds"],
            # Decision cache
            decision_cache_hits=decision_cache_stats["hits"],
            decision_cache_misses=decision_cache_stats["misses"],
            decision_cache_hit_rate=decision_cache_stats["hit_rate"],
            decision_cached_entries=decision_cache_stats["cached_entries"],
        )

        logger.info(
            f"AI usage stats retrieved: {usage_stats['calls_today']} calls, "
            f"${usage_stats['daily_cost']:.6f} cost today"
        )

        return response

    except Exception as err:
        logger.error(f"Failed to get AI usage stats: {err}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve AI usage statistics: {str(err)}"
        )


@router.post("/usage/reset")
async def reset_ai_usage() -> dict[str, str]:
    """
    Manually reset today's AI usage counters.

    This endpoint allows manual reset of usage statistics,
    useful for testing or administrative purposes.

    Note: Daily counters automatically reset at midnight.

    Returns:
        Success message

    Example response:
    ```json
    {
      "message": "AI usage counters reset successfully"
    }
    ```
    """
    try:
        tracker = get_usage_tracker()
        tracker.reset_today()

        logger.info("AI usage counters manually reset")

        return {"message": "AI usage counters reset successfully"}

    except Exception as err:
        logger.error(f"Failed to reset AI usage: {err}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to reset AI usage: {str(err)}")


@router.get("/cache/stats")
async def get_cache_stats() -> dict:
    """
    Get detailed cache statistics for news and decisions.

    Returns comprehensive cache metrics for monitoring and optimization.

    Returns:
        Dictionary with news_cache and decision_cache statistics

    Example response:
    ```json
    {
      "news_cache": {
        "hits": 45,
        "misses": 3,
        "hit_rate": 93.75,
        "age_seconds": 1847.3,
        "ttl_seconds": 3600
      },
      "decision_cache": {
        "hits": 12,
        "misses": 48,
        "hit_rate": 20.0,
        "cached_entries": 8,
        "window_seconds": 600
      }
    }
    ```
    """
    try:
        # Get news cache stats
        news_stats = get_news_cache_stats()

        # Get decision cache stats
        decision_cache = get_decision_cache()
        decision_stats = decision_cache.get_cache_stats()

        return {"news_cache": news_stats, "decision_cache": decision_stats}

    except Exception as err:
        logger.error(f"Failed to get cache stats: {err}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve cache statistics: {str(err)}"
        )
