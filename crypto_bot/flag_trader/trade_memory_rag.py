"""
Trade Memory RAG — Similar Trade Retrieval (v2)
================================================

Finds past trades with similar market conditions and formats them
for injection into the LLM prompt. Scoring includes recency decay,
direction matching, time-of-day proximity, and aggregate stats.

No external dependencies (no vector DB, no embeddings).
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
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
        current_action: str | None = None,
    ) -> list[dict]:
        """Find past trades with similar market conditions.

        Scoring dimensions (max ~16 points):
        - Same symbol: +2
        - Same regime: +3
        - RSI proximity (<5: +3, <10: +2)
        - ATR proximity (<0.3: +1)
        - Same EMA9 slope sign: +1
        - Same direction (BUY/SELL): +2
        - Hour-of-day proximity (<3h: +1.5, <6h: +0.5)
        - Same day type (weekday/weekend): +0.5
        - Recency: 0 to +2 (exponential decay, half-life 7 days)
        """
        outcomes = self._load_outcomes()
        if not outcomes:
            return []

        completed = [r for r in outcomes if r.get("pnl_usd") is not None]
        if not completed:
            return []

        current_rsi = market_state.get("rsi")
        current_atr_pct = market_state.get("atr_pct")
        current_regime = market_state.get("regime")
        current_ema9_slope = market_state.get("ema9_slope")

        now = datetime.now(timezone.utc)
        current_hour = now.hour
        current_is_weekend = now.weekday() >= 5

        scored: list[tuple[float, dict]] = []
        for trade in completed:
            score = 0.0
            ms = trade.get("market_state_summary") or {}

            # --- Same symbol: +2 ---
            if trade.get("symbol") == symbol:
                score += 2.0

            # --- Same regime: +3 ---
            trade_regime = ms.get("regime")
            if trade_regime and current_regime and trade_regime == current_regime:
                score += 3.0

            # --- RSI proximity ---
            trade_rsi = ms.get("rsi")
            if trade_rsi is not None and current_rsi is not None:
                rsi_diff = abs(float(current_rsi) - float(trade_rsi))
                if rsi_diff < 5:
                    score += 3.0
                elif rsi_diff < 10:
                    score += 2.0

            # --- ATR proximity ---
            trade_atr_pct = ms.get("atr_pct")
            if trade_atr_pct is not None and current_atr_pct is not None:
                atr_diff = abs(float(current_atr_pct) - float(trade_atr_pct))
                if atr_diff < 0.3:
                    score += 1.0

            # --- Same EMA9 slope sign ---
            trade_ema9_slope = ms.get("ema9_slope")
            if trade_ema9_slope is not None and current_ema9_slope is not None:
                if (float(trade_ema9_slope) >= 0) == (float(current_ema9_slope) >= 0):
                    score += 1.0

            # --- Same direction (BUY/SELL): +2 ---
            if current_action:
                trade_action = trade.get("action", "").upper()
                if trade_action == current_action.upper():
                    score += 2.0

            # --- Hour-of-day proximity: +1.5 ---
            trade_hour = self._extract_hour(trade.get("timestamp", ""))
            if trade_hour is not None:
                hour_diff = min(abs(current_hour - trade_hour), 24 - abs(current_hour - trade_hour))
                if hour_diff <= 3:
                    score += 1.5
                elif hour_diff <= 6:
                    score += 0.5

            # --- Same day type (weekday/weekend): +0.5 ---
            trade_is_weekend = self._is_weekend(trade.get("timestamp", ""))
            if trade_is_weekend is not None and trade_is_weekend == current_is_weekend:
                score += 0.5

            # --- Recency decay: 0 to +2 (half-life 7 days) ---
            days_ago = self._days_ago(trade.get("timestamp", ""), now)
            if days_ago is not None:
                # Exponential decay: 2 * e^(-ln(2) * days / 7)
                score += 2.0 * math.exp(-0.693 * days_ago / 7.0)

            if score > 0:
                scored.append((score, trade))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [trade for _, trade in scored[:max_results]]

    def format_for_prompt(
        self,
        similar_trades: list[dict],
        symbol: str | None = None,
        current_action: str | None = None,
    ) -> str:
        """Format similar trades as text for prompt injection.

        Includes:
        1. Aggregate stats for the symbol+direction combo
        2. Individual similar trade details
        """
        if not similar_trades:
            return ""

        lines: list[str] = []
        now = datetime.now(timezone.utc)

        # --- Aggregate stats section ---
        if symbol or current_action:
            stats = self._compute_aggregate_stats(symbol, current_action)
            if stats:
                lines.append(stats)
                lines.append("")

        # --- Individual trades ---
        lines.append("Similar past trades:")
        for trade in similar_trades:
            action = trade.get("action", "?")
            sym = trade.get("symbol", "?")
            pnl_pct = trade.get("pnl_pct")
            exit_reason = trade.get("exit_reason", "unknown")
            hold_mins = trade.get("hold_duration_minutes")

            time_ago = self._format_time_ago(trade.get("timestamp", ""), now)

            ms = trade.get("market_state_summary") or {}
            context_parts: list[str] = []
            if ms.get("rsi") is not None:
                context_parts.append(f"RSI={ms['rsi']:.0f}")
            if ms.get("regime"):
                context_parts.append(f"regime={ms['regime']}")
            if ms.get("atr_pct") is not None:
                context_parts.append(f"ATR={ms['atr_pct']:.1f}%")
            context = ", ".join(context_parts)

            won = (pnl_pct or 0) > 0
            outcome = "WON" if won else "LOST"
            pnl_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "?"
            hold_str = f"{hold_mins:.0f}min" if hold_mins is not None else "?"

            line = f"- {time_ago}: {action} {sym}"
            if context:
                line += f", {context}"
            line += f" -> {outcome} {pnl_str} ({exit_reason}, {hold_str})"
            lines.append(line)

        return "\n".join(lines)

    def _compute_aggregate_stats(
        self,
        symbol: str | None,
        action: str | None,
    ) -> str:
        """Compute aggregate win/loss stats for a symbol+direction combo."""
        outcomes = self._load_outcomes()
        if not outcomes:
            return ""

        completed = [r for r in outcomes if r.get("pnl_usd") is not None]
        if not completed:
            return ""

        # Filter by symbol and/or direction
        filtered = completed
        if symbol:
            filtered = [r for r in filtered if r.get("symbol") == symbol]
        if action:
            filtered = [r for r in filtered if r.get("action", "").upper() == action.upper()]

        if not filtered:
            # Fall back to direction-only stats across all symbols
            if action:
                filtered = [r for r in completed if r.get("action", "").upper() == action.upper()]
            if not filtered:
                return ""

        wins = [r for r in filtered if (r.get("pnl_pct") or 0) > 0]
        losses = [r for r in filtered if (r.get("pnl_pct") or 0) <= 0]
        total = len(filtered)
        wr = len(wins) / total * 100 if total > 0 else 0
        avg_pnl = sum(r.get("pnl_pct", 0) for r in filtered) / total if total > 0 else 0
        avg_win = sum(r.get("pnl_pct", 0) for r in wins) / len(wins) if wins else 0
        avg_loss = sum(r.get("pnl_pct", 0) for r in losses) / len(losses) if losses else 0

        # Build label
        label_parts: list[str] = []
        if action:
            label_parts.append(action.upper())
        if symbol:
            label_parts.append(symbol)
        label = " ".join(label_parts) if label_parts else "All trades"

        # Recent streak (last 5)
        recent = filtered[-5:]
        streak = "".join("W" if (r.get("pnl_pct") or 0) > 0 else "L" for r in recent)

        stat_line = (
            f"Track record ({label}): {len(wins)}W/{len(losses)}L "
            f"({wr:.0f}% WR), avg PnL: {avg_pnl:+.2f}%, "
            f"avg win: {avg_win:+.2f}%, avg loss: {avg_loss:+.2f}%, "
            f"last 5: [{streak}]"
        )
        return stat_line

    @staticmethod
    def _extract_hour(ts_str: str) -> int | None:
        """Extract hour (UTC) from ISO timestamp."""
        if not ts_str:
            return None
        try:
            ts = datetime.fromisoformat(ts_str)
            return ts.hour
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _is_weekend(ts_str: str) -> bool | None:
        """Check if timestamp falls on weekend."""
        if not ts_str:
            return None
        try:
            ts = datetime.fromisoformat(ts_str)
            return ts.weekday() >= 5
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _days_ago(ts_str: str, now: datetime) -> float | None:
        """Compute days between timestamp and now."""
        if not ts_str:
            return None
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            delta = now - ts
            return delta.total_seconds() / 86400.0
        except (ValueError, TypeError):
            return None

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
