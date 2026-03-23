"""
Trade Memory RAG — Similar Trade Retrieval
==========================================

Finds past trades with similar market conditions and formats them
for injection into the LLM prompt. Simple scoring-based approach,
no external dependencies (no vector DB, no embeddings).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .trade_logger import FlagTradeLogger

logger = logging.getLogger(__name__)

# Cache TTL in seconds
_CACHE_TTL = 300  # 5 minutes


class TradeMemoryRAG:
    """Retrieves past trades with similar market conditions for prompt injection."""

    def __init__(self, trade_logger: FlagTradeLogger) -> None:
        self.logger = trade_logger
        self._cache: list[dict] = []
        self._cache_timestamp: float = 0

    def _load_outcomes(self) -> list[dict]:
        """Load outcomes with 5-minute cache."""
        now = time.monotonic()
        if now - self._cache_timestamp < _CACHE_TTL and self._cache:
            return self._cache

        self._cache = self.logger.get_training_data()
        self._cache_timestamp = now
        logger.debug("TradeMemoryRAG: loaded %d outcome records", len(self._cache))
        return self._cache

    def find_similar_trades(
        self,
        symbol: str,
        market_state: dict,
        max_results: int = 5,
    ) -> list[dict]:
        """Find past trades with similar market conditions.

        Args:
            symbol: Current asset symbol.
            market_state: Dict with keys: rsi, adx, regime, atr_pct, ema9_slope.
            max_results: Maximum number of similar trades to return.

        Returns:
            List of trade dicts sorted by similarity score (highest first).
        """
        outcomes = self._load_outcomes()
        if not outcomes:
            return []

        # Only trades with actual outcome
        completed = [r for r in outcomes if r.get("pnl_usd") is not None]
        if not completed:
            return []

        current_rsi = market_state.get("rsi")
        current_adx = market_state.get("adx")
        current_regime = market_state.get("regime")
        current_atr_pct = market_state.get("atr_pct")
        current_ema9_slope = market_state.get("ema9_slope")

        scored: list[tuple[float, dict]] = []
        for trade in completed:
            score = 0.0
            ms = trade.get("market_state_summary") or {}

            # Same symbol: +2
            if trade.get("symbol") == symbol:
                score += 2.0

            # Same regime: +3
            trade_regime = ms.get("regime")
            if trade_regime and current_regime and trade_regime == current_regime:
                score += 3.0

            # RSI proximity
            trade_rsi = ms.get("rsi")
            if trade_rsi is not None and current_rsi is not None:
                rsi_diff = abs(float(current_rsi) - float(trade_rsi))
                if rsi_diff < 5:
                    score += 3.0
                elif rsi_diff < 10:
                    score += 2.0

            # ATR proximity
            trade_atr_pct = ms.get("atr_pct")
            if trade_atr_pct is not None and current_atr_pct is not None:
                atr_diff = abs(float(current_atr_pct) - float(trade_atr_pct))
                if atr_diff < 0.3:
                    score += 1.0

            # Same EMA9 slope sign
            trade_ema9_slope = ms.get("ema9_slope")
            if trade_ema9_slope is not None and current_ema9_slope is not None:
                if (float(trade_ema9_slope) >= 0) == (float(current_ema9_slope) >= 0):
                    score += 1.0

            # Only include trades with some similarity
            if score > 0:
                scored.append((score, trade))

        # Sort by score descending, take top N
        scored.sort(key=lambda x: x[0], reverse=True)
        return [trade for _, trade in scored[:max_results]]

    def format_for_prompt(self, similar_trades: list[dict]) -> str:
        """Format similar trades as text for prompt injection.

        Args:
            similar_trades: List of trade dicts from find_similar_trades.

        Returns:
            Formatted text block, or empty string if no trades.
        """
        if not similar_trades:
            return ""

        lines = ["Your past trades in similar conditions:"]
        now = datetime.now(timezone.utc)

        for trade in similar_trades:
            action = trade.get("action", "?")
            symbol = trade.get("symbol", "?")
            pnl_pct = trade.get("pnl_pct")
            exit_reason = trade.get("exit_reason", "unknown")
            hold_mins = trade.get("hold_duration_minutes")

            # Time ago
            ts_str = trade.get("timestamp", "")
            time_ago = self._format_time_ago(ts_str, now)

            # Market state at decision
            ms = trade.get("market_state_summary") or {}
            rsi_str = f"RSI={ms['rsi']:.0f}" if ms.get("rsi") is not None else ""
            regime_str = f"regime={ms['regime']}" if ms.get("regime") else ""
            context_parts = [p for p in [rsi_str, regime_str] if p]
            context = ", ".join(context_parts)

            # Outcome
            won = (pnl_pct or 0) > 0
            outcome = "WON" if won else "LOST"
            pnl_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "?"
            hold_str = f"{hold_mins:.0f}min" if hold_mins is not None else "?"

            line = f"- {time_ago}: {action} {symbol}"
            if context:
                line += f", {context}"
            line += f" -> {outcome} {pnl_str} ({exit_reason}, {hold_str})"
            lines.append(line)

        return "\n".join(lines)

    @staticmethod
    def _format_time_ago(ts_str: str, now: datetime) -> str:
        """Format a timestamp as relative time (e.g., '2d ago')."""
        if not ts_str:
            return "?ago"
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            delta = now - ts
            days = delta.days
            hours = delta.seconds // 3600
            if days > 0:
                return f"{days}d ago"
            if hours > 0:
                return f"{hours}h ago"
            return "just now"
        except (ValueError, TypeError):
            return "?ago"
