"""
DeepSeek API Client
===================

Async HTTP client for DeepSeek chat API with:
- Rate limiting to respect budget constraints
- Retry with exponential backoff
- Response parsing and validation
- Strategy selection and market analysis methods

Budget: ~$15/month = ~300 decisions/day = ~$0.50/day
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

import httpx
from pydantic import BaseModel, Field, field_validator

from .prompts import STRATEGY_SELECTION_PROMPT, MARKET_ANALYSIS_PROMPT

logger = logging.getLogger(__name__)


# =============================================================================
# Pydantic Models for LLM Responses
# =============================================================================


class StrategyType(str, Enum):
    """Available trading strategies."""
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    FUNDING_ARB = "funding_arb"


class DirectionType(str, Enum):
    """Trade direction."""
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class StrategyDecision(BaseModel):
    """
    LLM response for strategy selection.
    
    Represents the LLM's decision on which strategy to use
    for a specific trading opportunity.
    """
    
    strategy: StrategyType = Field(
        description="Selected trading strategy"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score 0.0-1.0"
    )
    direction: DirectionType = Field(
        description="Suggested trade direction"
    )
    reasoning: str = Field(
        description="Brief explanation of the decision"
    )
    entry_conditions: List[str] = Field(
        default_factory=list,
        description="Key conditions that support this strategy"
    )
    risk_factors: List[str] = Field(
        default_factory=list,
        description="Potential risk factors identified"
    )
    
    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, v: Any) -> float:
        """Ensure confidence is in 0-1 range."""
        if isinstance(v, str):
            v = float(v.strip().rstrip("%")) / 100 if "%" in v else float(v)
        if v > 1.0:
            v = v / 100  # Convert from percentage
        return max(0.0, min(1.0, v))


class MarketAnalysis(BaseModel):
    """
    LLM response for overall market analysis.
    
    Provides a broader market context assessment.
    """
    
    regime: Literal["bullish", "bearish", "neutral", "volatile"] = Field(
        description="Overall market regime assessment"
    )
    trend_strength: float = Field(
        ge=0.0,
        le=1.0,
        description="Strength of current trend"
    )
    risk_level: Literal["low", "medium", "high", "extreme"] = Field(
        description="Current risk level assessment"
    )
    summary: str = Field(
        description="Brief market summary"
    )
    recommended_strategies: List[StrategyType] = Field(
        default_factory=list,
        description="Strategies that work well in current conditions"
    )
    avoid_strategies: List[StrategyType] = Field(
        default_factory=list,
        description="Strategies to avoid in current conditions"
    )


# =============================================================================
# Rate Limiter
# =============================================================================


@dataclass
class RateLimiter:
    """
    Simple rate limiter to respect budget constraints.
    
    Tracks daily usage and prevents exceeding limits.
    """
    
    max_per_day: int = 300
    _count_today: int = 0
    _current_date: date = field(default_factory=date.today)
    _last_request_time: float = 0.0
    _min_interval_seconds: float = 1.0  # Min time between requests
    
    def reset_if_new_day(self) -> None:
        """Reset counter if it's a new day."""
        today = date.today()
        if today != self._current_date:
            self._count_today = 0
            self._current_date = today
            logger.info("Rate limiter reset for new day")
    
    def can_make_request(self) -> bool:
        """Check if we can make another request."""
        self.reset_if_new_day()
        return self._count_today < self.max_per_day
    
    async def wait_if_needed(self) -> None:
        """Wait if we need to rate limit."""
        self.reset_if_new_day()
        
        # Check daily limit
        if not self.can_make_request():
            raise RateLimitExceeded(
                f"Daily limit of {self.max_per_day} requests exceeded"
            )
        
        # Ensure minimum interval between requests
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval_seconds:
            await asyncio.sleep(self._min_interval_seconds - elapsed)
    
    def record_request(self) -> None:
        """Record that a request was made."""
        self._count_today += 1
        self._last_request_time = time.time()
    
    @property
    def remaining_today(self) -> int:
        """Get remaining requests for today."""
        self.reset_if_new_day()
        return max(0, self.max_per_day - self._count_today)
    
    @property
    def usage_pct(self) -> float:
        """Get usage percentage for today."""
        self.reset_if_new_day()
        return (self._count_today / self.max_per_day) * 100


# =============================================================================
# Exceptions
# =============================================================================


class LLMError(Exception):
    """Base exception for LLM errors."""
    pass


class RateLimitExceeded(LLMError):
    """Raised when rate limit is exceeded."""
    pass


class APIError(LLMError):
    """Raised when API returns an error."""
    pass


class ParseError(LLMError):
    """Raised when response parsing fails."""
    pass


# =============================================================================
# DeepSeek Client
# =============================================================================


class DeepSeekClient:
    """
    Async client for DeepSeek chat API.
    
    Provides methods for:
    - Generic chat completion
    - Strategy selection with structured output
    - Market analysis with structured output
    
    Features:
    - Rate limiting to respect budget
    - Retry with exponential backoff
    - Response parsing and validation
    
    Example:
        client = DeepSeekClient()
        
        # Simple chat
        response = await client.chat([
            {"role": "user", "content": "What is the best trading strategy for volatile markets?"}
        ])
        
        # Strategy selection
        decision = await client.select_strategy({
            "symbol": "ETH",
            "market_regime": "bullish",
            "adx": 35.5,
            "rsi": 62.3,
            "volume_ratio": 1.8,
        })
    """
    
    BASE_URL = "https://api.deepseek.com/v1/chat/completions"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "deepseek-chat",
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: int = 120,
        max_retries: int = 3,
        retry_delay: float = 5.0,
        decisions_per_day: int = 300,
    ) -> None:
        """
        Initialize DeepSeek client.
        
        Args:
            api_key: DeepSeek API key (defaults to DEEPSEEK_API_KEY env var)
            model: Model name to use
            temperature: Sampling temperature (0.0-2.0)
            max_tokens: Maximum tokens in response
            timeout: Request timeout in seconds
            max_retries: Maximum retry attempts
            retry_delay: Base delay between retries in seconds
            decisions_per_day: Maximum LLM decisions per day (budget)
        """
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        # Rate limiting
        self._rate_limiter = RateLimiter(max_per_day=decisions_per_day)
        
        # HTTP client (created on first use)
        self._client: Optional[httpx.AsyncClient] = None
        
        # Statistics
        self._total_requests: int = 0
        self._total_tokens: int = 0
        self._total_errors: int = 0
        
        if not self.api_key:
            logger.warning(
                "DEEPSEEK_API_KEY not set. LLM features will be unavailable."
            )
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client
    
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    @property
    def is_available(self) -> bool:
        """Check if client is configured and ready."""
        return bool(self.api_key) and self._rate_limiter.can_make_request()
    
    @property
    def remaining_requests(self) -> int:
        """Get remaining requests for today."""
        return self._rate_limiter.remaining_today
    
    @property
    def stats(self) -> Dict[str, Any]:
        """Get client statistics."""
        return {
            "total_requests": self._total_requests,
            "total_tokens": self._total_tokens,
            "total_errors": self._total_errors,
            "remaining_today": self._rate_limiter.remaining_today,
            "usage_pct": round(self._rate_limiter.usage_pct, 1),
            "is_available": self.is_available,
        }
    
    # =========================================================================
    # Core Chat Method
    # =========================================================================
    
    async def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Send a chat completion request.
        
        Args:
            messages: List of message dicts with "role" and "content"
            temperature: Override default temperature
            max_tokens: Override default max_tokens
            response_format: Optional response format specification
            
        Returns:
            Generated text response
            
        Raises:
            RateLimitExceeded: If daily limit exceeded
            APIError: If API returns an error
            LLMError: If request fails after retries
        """
        if not self.api_key:
            raise LLMError("DeepSeek API key not configured")
        
        # Rate limiting
        await self._rate_limiter.wait_if_needed()
        
        # Prepare request
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature or self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        
        if response_format:
            payload["response_format"] = response_format
        
        # Retry loop
        last_error: Optional[Exception] = None
        
        for attempt in range(self.max_retries):
            try:
                client = await self._get_client()
                
                response = await client.post(self.BASE_URL, json=payload)
                
                # Record successful request
                self._rate_limiter.record_request()
                self._total_requests += 1
                
                # Check for API errors
                if response.status_code != 200:
                    error_data = response.json() if response.content else {}
                    error_msg = error_data.get("error", {}).get("message", response.text)
                    raise APIError(f"API error {response.status_code}: {error_msg}")
                
                # Parse response
                data = response.json()
                
                # Track token usage
                if "usage" in data:
                    self._total_tokens += data["usage"].get("total_tokens", 0)
                
                # Extract content
                content = data["choices"][0]["message"]["content"]
                return content.strip()
                
            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(
                    "Request timeout (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    e,
                )
                
            except httpx.HTTPStatusError as e:
                last_error = e
                # Check if retryable (rate limit or server error)
                if e.response.status_code in (429, 500, 502, 503, 504):
                    logger.warning(
                        "Retryable error (attempt %d/%d): %s",
                        attempt + 1,
                        self.max_retries,
                        e,
                    )
                else:
                    self._total_errors += 1
                    raise APIError(f"HTTP error: {e}")
                    
            except APIError:
                self._total_errors += 1
                raise
                
            except Exception as e:
                last_error = e
                logger.error(
                    "Unexpected error (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    e,
                )
            
            # Exponential backoff
            if attempt < self.max_retries - 1:
                delay = self.retry_delay * (2 ** attempt)
                await asyncio.sleep(delay)
        
        # All retries failed
        self._total_errors += 1
        raise LLMError(f"Request failed after {self.max_retries} attempts: {last_error}")
    
    # =========================================================================
    # Strategy Selection
    # =========================================================================
    
    async def select_strategy(
        self,
        context: Dict[str, Any],
    ) -> StrategyDecision:
        """
        Select the best trading strategy for a given context.
        
        Args:
            context: Dict containing:
                - symbol: Trading symbol (e.g., "ETH")
                - market_regime: Current regime ("bullish", "bearish", etc.)
                - adx: ADX value (trend strength)
                - rsi: RSI value
                - volatility_score: Volatility score 0-1
                - volume_score: Volume score 0-1
                - funding_rate: Current funding rate
                - opportunity_score: Overall opportunity score
                - recent_performance: Dict of strategy -> recent performance
                
        Returns:
            StrategyDecision with selected strategy, confidence, and reasoning
            
        Raises:
            ParseError: If response cannot be parsed
            LLMError: If request fails
        """
        # Format the prompt
        prompt = STRATEGY_SELECTION_PROMPT.format(**context)
        
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert quantitative trading assistant. "
                    "Your task is to select the optimal trading strategy based on market conditions. "
                    "Always respond with valid JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        
        try:
            response = await self.chat(
                messages,
                response_format={"type": "json_object"},
            )
            
            # Parse JSON response
            data = self._parse_json_response(response)
            
            # Validate and create decision
            return StrategyDecision(
                strategy=data.get("strategy", "momentum"),
                confidence=data.get("confidence", 0.5),
                direction=data.get("direction", "neutral"),
                reasoning=data.get("reasoning", "No reasoning provided"),
                entry_conditions=data.get("entry_conditions", []),
                risk_factors=data.get("risk_factors", []),
            )
            
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise ParseError(f"Failed to parse strategy response: {e}")
    
    async def analyze_market(
        self,
        snapshot: Dict[str, Any],
    ) -> MarketAnalysis:
        """
        Analyze overall market conditions.
        
        Args:
            snapshot: Dict containing market data:
                - btc_price: Current BTC price
                - btc_change_24h: BTC 24h change %
                - total_volume_24h: Total market volume
                - top_gainers: List of top gaining symbols
                - top_losers: List of top losing symbols
                - avg_funding_rate: Average funding rate
                
        Returns:
            MarketAnalysis with regime, risk level, and recommendations
            
        Raises:
            ParseError: If response cannot be parsed
            LLMError: If request fails
        """
        from .prompts import MARKET_ANALYSIS_PROMPT
        
        prompt = MARKET_ANALYSIS_PROMPT.format(**snapshot)
        
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert crypto market analyst. "
                    "Analyze the market data and provide a structured assessment. "
                    "Always respond with valid JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        
        try:
            response = await self.chat(
                messages,
                response_format={"type": "json_object"},
            )
            
            data = self._parse_json_response(response)
            
            return MarketAnalysis(
                regime=data.get("regime", "neutral"),
                trend_strength=data.get("trend_strength", 0.5),
                risk_level=data.get("risk_level", "medium"),
                summary=data.get("summary", "No summary provided"),
                recommended_strategies=[
                    StrategyType(s) for s in data.get("recommended_strategies", [])
                    if s in [e.value for e in StrategyType]
                ],
                avoid_strategies=[
                    StrategyType(s) for s in data.get("avoid_strategies", [])
                    if s in [e.value for e in StrategyType]
                ],
            )
            
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise ParseError(f"Failed to parse market analysis: {e}")
    
    # =========================================================================
    # Helper Methods
    # =========================================================================
    
    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        """
        Parse JSON from LLM response.
        
        Handles common LLM quirks like markdown code blocks.
        """
        # Clean up response
        text = response.strip()
        
        # Remove markdown code blocks if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last lines (```json and ```)
            lines = lines[1:-1] if lines[-1].startswith("```") else lines[1:]
            text = "\n".join(lines)
        
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON from the response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(text[start:end])
            raise


# =============================================================================
# Factory Function
# =============================================================================


def create_deepseek_client(
    config: Optional[Any] = None,
) -> DeepSeekClient:
    """
    Create a DeepSeek client from configuration.
    
    Args:
        config: Optional LLMConfig from config loader
        
    Returns:
        Configured DeepSeekClient instance
    """
    if config is None:
        # Try to load from global config
        try:
            from simple_bot.config.loader import get_config
            config = get_config().llm
        except Exception:
            pass
    
    if config is not None:
        return DeepSeekClient(
            api_key=config.api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout,
            max_retries=config.retry_attempts,
            retry_delay=config.retry_delay_seconds,
            decisions_per_day=config.decisions_per_day,
        )
    
    # Use defaults
    return DeepSeekClient()
