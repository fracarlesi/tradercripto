"""
Whale Alert Tracker - Track Large Crypto Transactions

Tracks whale transactions (10M+ USD) using official Whale Alert API.
Whales are large investors whose transactions can move market prices.

Reference: Video Rizzo 11:03-13:10
API: Official Whale Alert API ($14.95/month Whale Limits Plan)
"""

from typing import Dict, List, Optional
from datetime import datetime, timedelta
import logging
import os

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)


class WhaleTracker:
    """
    Tracker for whale alert transactions using Official Whale Alert API.

    Whales = Large investors moving 10M+ USD in single transactions.
    When whales move → Prices often follow.

    Examples:
    - Whale transfers 100M USDT from exchange → Possible selling pressure
    - Whale transfers 100M BTC to exchange → Likely preparing to sell
    - Whale transfers to cold wallet → Accumulation (bullish)

    Usage:
        tracker = WhaleTracker()
        alerts = tracker.get_recent_alerts(min_amount_usd=10_000_000)

        for alert in alerts:
            print(f"{alert['description']} - {alert['amount_usd']:,.0f} USD")
    """

    # Official Whale Alert API endpoint
    WHALE_API_URL = "https://api.whale-alert.io/v1/transactions"

    def __init__(self, api_key: Optional[str] = None, min_amount_usd: float = 10_000_000):
        """
        Initialize whale tracker.

        Args:
            api_key: Whale Alert API key (default: env var WHALE_ALERT_API_KEY)
            min_amount_usd: Minimum transaction amount to track (default: 10M USD)
        """
        self.api_key = api_key or os.getenv("WHALE_ALERT_API_KEY")
        self.min_amount_usd = min_amount_usd
        self.last_alerts: List[Dict] = []
        self.seen_ids: set = set()  # Track seen IDs to avoid duplicates

    def get_recent_alerts(
        self,
        min_amount_usd: Optional[float] = None,
        limit: int = 10,
    ) -> List[Dict]:
        """
        Get recent whale alert transactions from official API.

        Args:
            min_amount_usd: Override minimum amount (default: use instance setting)
            limit: Maximum number of alerts to return (default: 10)

        Returns:
            List of alerts:
            [
                {
                    "id": "abc123",
                    "timestamp": "2025-11-07T14:29:00Z",
                    "amount_usd": 104140000,
                    "symbol": "USDT",
                    "from": "binance",
                    "to": "unknown",
                    "description": "104M USDT transferred from Binance",
                    "link": "https://whale-alert.io/transaction/...",
                },
                ...
            ]

        Note: Returns empty list if API fails (don't block trading)
        """
        if not self.api_key:
            logger.warning("No Whale Alert API key provided, returning empty alerts")
            return []

        min_amount = min_amount_usd or self.min_amount_usd

        try:
            logger.info(f"Fetching whale alerts (min ${min_amount/1_000_000:.0f}M)...")

            # Fetch from official API
            alerts = self._fetch_from_api(self.WHALE_API_URL, min_amount, limit)

            if alerts:
                logger.info(f"Found {len(alerts)} new whale alerts")
                self.last_alerts = alerts
            else:
                logger.warning("No whale alerts fetched")

            return alerts

        except Exception as e:
            logger.error("Unexpected error fetching whale alerts", exc_info=True)
            return []

    def _fetch_from_api(
        self,
        api_url: str,
        min_amount_usd: float,
        limit: int,
    ) -> List[Dict]:
        """
        Fetch alerts from official Whale Alert API.

        Args:
            api_url: API endpoint URL
            min_amount_usd: Minimum amount filter
            limit: Max results

        Returns:
            List of whale alerts (or empty if failed)
        """
        try:
            # Calculate time range (last 10 minutes)
            end_time = int(datetime.utcnow().timestamp())
            start_time = end_time - 600  # 10 minutes ago

            # Official API parameters
            params = {
                "api_key": self.api_key,
                "start": start_time,
                "end": end_time,
                "min_value": int(min_amount_usd),
                "limit": limit,
            }

            # HTTP GET request with API key
            response = requests.get(
                api_url,
                params=params,
                headers={
                    "Accept": "application/json",
                },
                timeout=10,
            )

            # Check response
            if response.status_code != 200:
                logger.warning(f"Whale API returned status {response.status_code}: {response.text}")
                return []

            data = response.json()

            # Check API response status
            if data.get("result") != "success":
                logger.warning(f"Whale API returned error: {data.get('message', 'Unknown error')}")
                return []

            # Parse response (official API structure)
            raw_alerts = data.get("transactions", [])

            if not raw_alerts:
                logger.debug("No alerts in API response")
                return []

            # Filter and parse alerts
            alerts = []
            for raw in raw_alerts:
                # Skip if already seen
                alert_id = str(raw.get("id") or raw.get("hash", ""))
                if alert_id in self.seen_ids:
                    continue

                # Parse amount - official API uses "amount_usd" field
                amount_usd = float(raw.get("amount_usd", 0))
                if amount_usd < min_amount_usd:
                    continue

                # Parse fields
                symbol = raw.get("symbol", "UNKNOWN")
                timestamp_unix = raw.get("timestamp", int(datetime.utcnow().timestamp()))
                timestamp = datetime.fromtimestamp(timestamp_unix).isoformat()

                # Parse from/to (official API structure)
                from_owner = "unknown"
                to_owner = "unknown"

                if "from" in raw and isinstance(raw["from"], dict):
                    from_owner = raw["from"].get("owner", raw["from"].get("owner_type", "unknown"))
                elif "from" in raw:
                    from_owner = str(raw["from"])

                if "to" in raw and isinstance(raw["to"], dict):
                    to_owner = raw["to"].get("owner", raw["to"].get("owner_type", "unknown"))
                elif "to" in raw:
                    to_owner = str(raw["to"])

                # Build alert
                alert = {
                    "id": alert_id,
                    "timestamp": timestamp,
                    "amount_usd": amount_usd,
                    "symbol": symbol,
                    "from": from_owner,
                    "to": to_owner,
                    "description": self._format_description(
                        amount_usd, symbol, from_owner, to_owner
                    ),
                    "link": f"https://whale-alert.io/transaction/{raw.get('blockchain', 'bitcoin')}/{raw.get('hash', alert_id)}",
                }

                alerts.append(alert)
                self.seen_ids.add(alert_id)

                # Limit results
                if len(alerts) >= limit:
                    break

            return alerts

        except requests.exceptions.RequestException as e:
            logger.error(f"Whale API request failed: {e}", exc_info=True)
            return []

        except Exception as e:
            logger.error(f"Failed to parse whale alerts: {e}", exc_info=True)
            return []

    def _format_description(
        self,
        amount_usd: float,
        symbol: str,
        from_owner: str,
        to_owner: str,
    ) -> str:
        """
        Format human-readable description (like in video).

        Example: "104M USDT transferred from Binance to unknown wallet"
        """
        # Format amount (104M, 1.2B, etc.)
        if amount_usd >= 1_000_000_000:
            amount_str = f"{amount_usd / 1_000_000_000:.1f}B"
        elif amount_usd >= 1_000_000:
            amount_str = f"{amount_usd / 1_000_000:.0f}M"
        else:
            amount_str = f"{amount_usd:,.0f}"

        return f"{amount_str} {symbol} transferred from {from_owner} to {to_owner}"

    def get_whale_summary_for_ai(self) -> str:
        """
        Generate formatted summary for AI system prompt.

        Returns:
            Formatted string with whale alerts
        """
        if not self.last_alerts:
            return "🐋 Whale Alerts: No recent large transactions detected (last 5 min)."

        summary_lines = [
            "### Whale Alerts (Large Transactions >$50M)",
            "",
            f"🐋 **{len(self.last_alerts)} LARGE TRANSACTIONS** detected in last 5 minutes:",
            "",
        ]

        # Show top 3 most recent
        for i, alert in enumerate(self.last_alerts[:3], 1):
            amount = alert["amount_usd"]
            symbol = alert["symbol"]
            from_owner = alert["from"]
            to_owner = alert["to"]

            # Interpret direction
            interpretation = ""
            if "exchange" in from_owner.lower() and "unknown" in to_owner.lower():
                interpretation = "⚠️ BEARISH - Whale moving FROM exchange (possible sell prep)"
            elif "unknown" in from_owner.lower() and "exchange" in to_owner.lower():
                interpretation = "🔴 BEARISH - Whale moving TO exchange (likely selling soon)"
            elif "exchange" in from_owner.lower() and "exchange" in to_owner.lower():
                interpretation = "⚪ NEUTRAL - Exchange-to-exchange transfer"
            elif "unknown" in to_owner.lower():
                interpretation = "🟢 BULLISH - Whale moving to cold storage (accumulation)"

            summary_lines.append(
                f"{i}. **${amount/1_000_000:.0f}M {symbol}**: {from_owner} → {to_owner}"
            )
            if interpretation:
                summary_lines.append(f"   {interpretation}")
            summary_lines.append("")

        summary_lines.extend(
            [
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "",
                "**Whale Alert Trading Rules**:",
                "- TO exchange = Likely SELLING soon (bearish for next 10-30 min)",
                "- FROM exchange = Possible selling prep OR withdrawal (monitor closely)",
                "- To cold wallet = Accumulation (bullish, whales holding long-term)",
                "- Large volume = Higher impact on price (>100M = major move likely)",
                "",
                "**Context**: Whale alerts are SHORT-TERM signals (10-30 min window).",
                "Use as CONFIRMATION with technical analysis, not standalone signal.",
                "",
            ]
        )

        return "\n".join(summary_lines)


# Singleton instance
_whale_tracker: Optional[WhaleTracker] = None


def get_whale_tracker() -> WhaleTracker:
    """
    Get singleton instance of WhaleTracker.

    Usage:
        from services.market_data.whale_tracker import get_whale_tracker

        tracker = get_whale_tracker()
        alerts = tracker.get_recent_alerts()
    """
    global _whale_tracker
    if _whale_tracker is None:
        _whale_tracker = WhaleTracker()
        logger.info("WhaleTracker singleton instance created")
    return _whale_tracker
