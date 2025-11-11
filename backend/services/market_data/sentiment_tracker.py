"""
Sentiment Tracker Service - Fear & Greed Index

Tracks crypto market sentiment using CoinMarketCap Fear & Greed Index.
Used as a CONTRARIAN indicator: Extreme Fear = Buy opportunity, Extreme Greed = Sell signal.

Reference: Video Rizzo 10:13-11:03
API: https://coinmarketcap.com/api/ (free, 30 req/min without API key)
"""

from typing import Dict, Optional
from datetime import datetime, timedelta
import logging
import os

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)


class SentimentTracker:
    """
    Tracks Fear & Greed Index from CoinMarketCap.

    Scale:
    - 0-24: EXTREME FEAR (market panic, BUY opportunity)
    - 25-49: FEAR (bearish sentiment, possible buy)
    - 50-74: GREED (bullish sentiment, market optimistic)
    - 75-100: EXTREME GREED (market euphoria, consider SELL)

    Usage:
        tracker = SentimentTracker()
        sentiment = tracker.get_sentiment()
        print(f"Fear & Greed Index: {sentiment['value']}/100 ({sentiment['classification']})")
    """

    # CoinMarketCap Pro API endpoint (requires API key)
    API_URL = "https://pro-api.coinmarketcap.com/v3/fear-and-greed/latest"

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize sentiment tracker.

        Args:
            api_key: Optional CoinMarketCap API key (default: env var CMC_API_KEY)
                     API works WITHOUT key (30 req/min limit)
        """
        self.api_key = api_key or os.getenv("CMC_API_KEY")
        self.cache: Optional[Dict] = None
        self.cache_time: Optional[datetime] = None
        self.cache_ttl = 3600  # 1 hour (sentiment changes slowly, avoid rate limits)

    def get_sentiment(self) -> Dict:
        """
        Get current Fear & Greed Index from CoinMarketCap.

        Returns:
            {
                "value": 27,  # 0-100
                "classification": "Fear",  # Fear, Greed, etc.
                "timestamp": "2025-11-07T14:30:00Z",
                "interpretation": "Market in fear, possible buy opportunity",
                "signal": "contrarian_buy",  # contrarian_buy, contrarian_sell, neutral
            }

        Note: If API fails, returns neutral fallback (value=50) to avoid blocking trading.
        """
        # Check cache
        if self.cache and self.cache_time:
            elapsed = (datetime.utcnow() - self.cache_time).total_seconds()
            if elapsed < self.cache_ttl:
                logger.debug(f"Using cached sentiment data (age: {elapsed:.0f}s)")
                return self.cache

        try:
            logger.info("Fetching Fear & Greed Index from CoinMarketCap...")

            # Prepare headers with API key (REQUIRED for Pro API)
            if not self.api_key:
                logger.warning("No CoinMarketCap API key provided, using fallback")
                return self._neutral_fallback()

            headers = {
                "X-CMC_PRO_API_KEY": self.api_key,
                "Accept": "application/json",
            }

            # Fetch data
            response = requests.get(
                self.API_URL,
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()

            data = response.json()

            # Parse CoinMarketCap Pro API response
            # Structure: {"data": {"value": 27, "value_classification": "Fear", "timestamp": "..."}}
            if "data" not in data:
                logger.error(f"Unexpected API response format: {data}")
                return self._neutral_fallback()

            data_obj = data["data"]

            value = int(data_obj.get("value", 50))
            classification = data_obj.get("value_classification", "Neutral")

            # Build result
            result = {
                "value": value,
                "classification": classification,
                "timestamp": datetime.utcnow().isoformat(),
                "interpretation": self._interpret_sentiment(value, classification),
                "signal": self._get_trading_signal(value),
            }

            # Update cache
            self.cache = result
            self.cache_time = datetime.utcnow()

            logger.info(
                f"Sentiment Index: {value}/100 ({classification}) - Signal: {result['signal']}"
            )
            return result

        except requests.exceptions.RequestException as e:
            logger.error(
                f"Failed to fetch sentiment from CoinMarketCap: {e}",
                exc_info=True,
            )
            # Return neutral fallback (don't block trading due to API failure)
            return self._neutral_fallback()

        except Exception as e:
            logger.error("Unexpected error fetching sentiment", exc_info=True)
            return self._neutral_fallback()

    def _interpret_sentiment(self, value: int, classification: str) -> str:
        """
        Interpret sentiment for AI (contrarian strategy).

        Contrarian logic:
        - Extreme Fear (0-24) → Everyone panicking → BUY opportunity
        - Fear (25-49) → Bearish sentiment → Possible buy
        - Greed (50-74) → Bullish sentiment → Market optimistic
        - Extreme Greed (75-100) → Euphoria → Consider SELL
        """
        if value <= 24:
            return (
                "EXTREME FEAR - Market panic, strong CONTRARIAN BUY signal. "
                "Retail investors selling, smart money accumulating."
            )
        elif value <= 49:
            return (
                "FEAR - Bearish sentiment dominates. "
                "Possible buy opportunity if other indicators confirm support."
            )
        elif value <= 74:
            return (
                "GREED - Bullish sentiment, market optimistic. "
                "Normal market conditions, no extreme signals."
            )
        else:
            return (
                "EXTREME GREED - Market euphoria, overheated. "
                "CONTRARIAN SELL signal: Consider taking profits or reducing exposure."
            )

    def _get_trading_signal(self, value: int) -> str:
        """
        Get contrarian trading signal based on sentiment.

        Returns:
            "contrarian_buy" | "contrarian_sell" | "neutral"
        """
        if value <= 24:
            return "contrarian_buy"  # Extreme fear = buy
        elif value >= 75:
            return "contrarian_sell"  # Extreme greed = sell
        else:
            return "neutral"

    def _neutral_fallback(self) -> Dict:
        """
        Return neutral fallback when API fails.
        """
        logger.warning("Using neutral sentiment fallback (API failed)")
        return {
            "value": 50,
            "classification": "Neutral",
            "timestamp": datetime.utcnow().isoformat(),
            "interpretation": "Sentiment data unavailable (API error). Assuming neutral market.",
            "signal": "neutral",
            "error": "API unavailable",
        }

    def get_sentiment_for_ai(self) -> str:
        """
        Generate formatted summary for AI system prompt.

        Returns:
            Formatted string ready to insert in AI prompt
        """
        sentiment = self.get_sentiment()

        value = sentiment["value"]
        classification = sentiment["classification"]
        interpretation = sentiment["interpretation"]
        signal = sentiment["signal"]

        # Emoji for visual signal
        signal_emoji = {
            "contrarian_buy": "🟢",
            "contrarian_sell": "🔴",
            "neutral": "⚪",
        }.get(signal, "⚪")

        summary = f"""
### Sentiment Index (Fear & Greed) - CONTRARIAN INDICATOR

**Current Index**: {value}/100 ({classification}) {signal_emoji}

**Signal**: {signal.upper().replace('_', ' ')}

**Interpretation**: {interpretation}

**Trading Strategy**:
- Fear (0-49) → Contrarian BUY opportunity (others panic-selling)
- Greed (50-100) → Normal market or overheated (be cautious)
- Extreme Fear (<25) → STRONG BUY signal (maximum pessimism)
- Extreme Greed (>75) → STRONG SELL signal (maximum optimism, tops forming)

**Context**: Use sentiment as CONFIRMATION, not primary signal.
If technical analysis + pivot points say BUY AND sentiment is Fear → HIGH CONVICTION.
If technical says BUY but sentiment is Extreme Greed → REDUCE size or WAIT.
"""

        return summary.strip()


# Singleton instance
_sentiment_tracker: Optional[SentimentTracker] = None


def get_sentiment_tracker() -> SentimentTracker:
    """
    Get singleton instance of SentimentTracker.

    Usage:
        from services.market_data.sentiment_tracker import get_sentiment_tracker

        tracker = get_sentiment_tracker()
        sentiment = tracker.get_sentiment()
    """
    global _sentiment_tracker
    if _sentiment_tracker is None:
        # API key will be loaded from env var CMC_API_KEY in __init__
        _sentiment_tracker = SentimentTracker()
        logger.info("SentimentTracker singleton instance created")
    return _sentiment_tracker
