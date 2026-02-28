"""
HLQuantBot Trade Memory Service
================================

Stores trade outcomes with full context for LLM learning.

Features:
- Persists trade context (regime, ADX, RSI, LLM confidence, outcome)
- Computes aggregate statistics (win rate by regime, symbol, etc.)
- Builds smart LLM context (recent trades + stats, ~500 tokens max)
- Anti-saturation: only sends relevant summary, not full history

The LLM receives:
1. Aggregate stats: "Overall: 65% win rate (13/20), +$45.20"
2. Regime stats: "BTC in TREND regime: 80% win rate (8/10)"
3. Recent similar trades: last 5-10 relevant trades

Author: Francesco Carlesi
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class TradeOutcome:
    """Complete trade record with context for learning."""

    # Trade identification
    trade_id: str
    symbol: str
    timestamp: datetime

    # Entry context
    direction: str  # "long" or "short"
    regime: str  # "trend", "range", "chaos"
    entry_price: float
    position_size: float

    # Indicators at entry
    adx: float
    rsi: float
    atr: float
    atr_pct: float
    ema50: Optional[float] = None
    ema200: Optional[float] = None

    # LLM decision context
    llm_decision: str = "ALLOW"  # "ALLOW" or "DENY"
    llm_confidence: float = 0.5
    llm_reason: str = ""

    # Trade outcome (filled after close)
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    duration_minutes: Optional[float] = None
    is_winner: Optional[bool] = None
    exit_reason: str = ""  # "tp_hit", "sl_hit", "manual", "timeout"

    # Strategy info
    strategy: str = "trend_follow"
    setup_type: str = "breakout"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradeOutcome":
        """Deserialize from dict."""
        data = data.copy()
        if isinstance(data.get("timestamp"), str):
            data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)


@dataclass
class AggregateStats:
    """Aggregated performance statistics."""

    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    avg_duration_min: float = 0.0

    @property
    def win_rate(self) -> float:
        """Calculate win rate percentage."""
        if self.total_trades == 0:
            return 0.0
        return (self.wins / self.total_trades) * 100

    @property
    def profit_factor(self) -> float:
        """Gross profit / gross loss."""
        if self.avg_loser == 0:
            return float("inf") if self.avg_winner > 0 else 0.0
        total_wins = self.wins * abs(self.avg_winner)
        total_losses = self.losses * abs(self.avg_loser)
        return total_wins / total_losses if total_losses > 0 else 0.0

    def to_summary(self) -> str:
        """Generate human-readable summary for LLM."""
        if self.total_trades == 0:
            return "No trades yet"
        return (
            f"{self.win_rate:.0f}% win rate ({self.wins}/{self.total_trades}), "
            f"P&L: ${self.total_pnl:+.2f}, "
            f"Avg win: ${self.avg_winner:.2f}, Avg loss: ${self.avg_loser:.2f}"
        )


# =============================================================================
# Trade Memory Service
# =============================================================================

class TradeMemory:
    """
    In-memory trade history with persistence.

    Stores trade outcomes with full context for LLM learning.
    Automatically manages memory to avoid saturation.

    Usage:
        memory = TradeMemory()
        await memory.load()

        # Record trade entry
        memory.record_entry(trade_id="...", symbol="BTC", ...)

        # Update with outcome
        memory.record_outcome(trade_id="...", pnl=45.20, ...)

        # Get LLM context
        context = memory.get_llm_context(symbol="BTC", regime="trend")
    """

    # Configuration
    MAX_TRADES_IN_MEMORY = 500  # Keep last 500 trades in memory
    MAX_RECENT_FOR_LLM = 10    # Show max 10 recent trades to LLM
    PERSISTENCE_FILE = "trade_memory.json"

    def __init__(self, data_dir: Optional[Path] = None):
        """Initialize trade memory."""
        self._trades: Dict[str, TradeOutcome] = {}  # trade_id -> outcome
        self._closed_trades: List[TradeOutcome] = []  # Chronological list
        self._data_dir = data_dir or Path.home() / ".hlquantbot"
        self._lock = asyncio.Lock()

        logger.info("TradeMemory initialized (max=%d trades)", self.MAX_TRADES_IN_MEMORY)

    # =========================================================================
    # Persistence
    # =========================================================================

    async def load(self) -> None:
        """Load trade history from disk."""
        file_path = self._data_dir / self.PERSISTENCE_FILE

        if not file_path.exists():
            logger.info("No trade memory file found, starting fresh")
            return

        try:
            async with self._lock:
                with open(file_path, "r") as f:
                    data = json.load(f)

                for trade_data in data.get("closed_trades", []):
                    trade = TradeOutcome.from_dict(trade_data)
                    self._closed_trades.append(trade)

                # Trim to max size
                if len(self._closed_trades) > self.MAX_TRADES_IN_MEMORY:
                    self._closed_trades = self._closed_trades[-self.MAX_TRADES_IN_MEMORY:]

                logger.info("Loaded %d trades from memory", len(self._closed_trades))

        except Exception as e:
            logger.error("Failed to load trade memory: %s", e)

    async def save(self) -> None:
        """Persist trade history to disk."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        file_path = self._data_dir / self.PERSISTENCE_FILE

        try:
            async with self._lock:
                data = {
                    "version": 1,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                    "closed_trades": [t.to_dict() for t in self._closed_trades[-self.MAX_TRADES_IN_MEMORY:]],
                }

                with open(file_path, "w") as f:
                    json.dump(data, f, indent=2)

                logger.debug("Saved %d trades to memory", len(self._closed_trades))

        except Exception as e:
            logger.error("Failed to save trade memory: %s", e)

    # =========================================================================
    # Recording
    # =========================================================================

    def record_entry(
        self,
        trade_id: str,
        symbol: str,
        direction: str,
        regime: str,
        entry_price: float,
        position_size: float,
        adx: float,
        rsi: float,
        atr: float,
        atr_pct: float,
        llm_decision: str = "ALLOW",
        llm_confidence: float = 0.5,
        llm_reason: str = "",
        strategy: str = "trend_follow",
        setup_type: str = "breakout",
        ema50: Optional[float] = None,
        ema200: Optional[float] = None,
    ) -> None:
        """
        Record a new trade entry with full context.

        Call this when opening a position.
        """
        outcome = TradeOutcome(
            trade_id=trade_id,
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            direction=direction,
            regime=regime,
            entry_price=entry_price,
            position_size=position_size,
            adx=adx,
            rsi=rsi,
            atr=atr,
            atr_pct=atr_pct,
            ema50=ema50,
            ema200=ema200,
            llm_decision=llm_decision,
            llm_confidence=llm_confidence,
            llm_reason=llm_reason,
            strategy=strategy,
            setup_type=setup_type,
        )

        self._trades[trade_id] = outcome
        logger.info("Recorded trade entry: %s %s %s", trade_id, symbol, direction)

    def record_outcome(
        self,
        trade_id: str,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        duration_minutes: float,
        exit_reason: str = "unknown",
    ) -> None:
        """
        Record trade outcome after position close.

        Call this when closing a position.
        """
        if trade_id not in self._trades:
            logger.warning("Trade not found for outcome: %s", trade_id)
            return

        outcome = self._trades[trade_id]
        outcome.exit_price = exit_price
        outcome.pnl = pnl
        outcome.pnl_pct = pnl_pct
        outcome.duration_minutes = duration_minutes
        outcome.is_winner = pnl > 0
        outcome.exit_reason = exit_reason

        # Move to closed trades
        self._closed_trades.append(outcome)
        del self._trades[trade_id]

        # Trim memory if needed
        if len(self._closed_trades) > self.MAX_TRADES_IN_MEMORY:
            self._closed_trades = self._closed_trades[-self.MAX_TRADES_IN_MEMORY:]

        logger.info(
            "Recorded trade outcome: %s %s P&L=$%.2f (%s)",
            trade_id, outcome.symbol, pnl, "WIN" if pnl > 0 else "LOSS"
        )

        # Auto-save periodically
        if len(self._closed_trades) % 10 == 0:
            asyncio.create_task(self.save())

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(
        self,
        symbol: Optional[str] = None,
        regime: Optional[str] = None,
        days: int = 30,
    ) -> AggregateStats:
        """
        Calculate aggregate statistics.

        Args:
            symbol: Filter by symbol (None = all)
            regime: Filter by regime (None = all)
            days: Time window in days

        Returns:
            AggregateStats with computed metrics
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Filter trades
        filtered = [
            t for t in self._closed_trades
            if t.timestamp >= cutoff
            and t.is_winner is not None  # Has outcome
            and (symbol is None or t.symbol == symbol)
            and (regime is None or t.regime == regime)
        ]

        if not filtered:
            return AggregateStats()

        # Calculate stats
        wins = [t for t in filtered if t.is_winner]
        losses = [t for t in filtered if not t.is_winner]

        win_pnls = [t.pnl for t in wins if t.pnl is not None]
        loss_pnls = [t.pnl for t in losses if t.pnl is not None]
        all_pnls = [t.pnl for t in filtered if t.pnl is not None]
        durations = [t.duration_minutes for t in filtered if t.duration_minutes]

        return AggregateStats(
            total_trades=len(filtered),
            wins=len(wins),
            losses=len(losses),
            total_pnl=sum(all_pnls) if all_pnls else 0.0,
            avg_pnl=sum(all_pnls) / len(all_pnls) if all_pnls else 0.0,
            avg_winner=sum(win_pnls) / len(win_pnls) if win_pnls else 0.0,
            avg_loser=sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0,
            best_trade=max(all_pnls) if all_pnls else 0.0,
            worst_trade=min(all_pnls) if all_pnls else 0.0,
            avg_duration_min=sum(durations) / len(durations) if durations else 0.0,
        )

    def get_stats_by_regime(self, days: int = 30) -> Dict[str, AggregateStats]:
        """Get statistics broken down by regime."""
        regimes = set(t.regime for t in self._closed_trades if t.regime)
        return {
            regime: self.get_stats(regime=regime, days=days)
            for regime in regimes
        }

    def get_stats_by_symbol(self, days: int = 30) -> Dict[str, AggregateStats]:
        """Get statistics broken down by symbol."""
        symbols = set(t.symbol for t in self._closed_trades if t.symbol)
        return {
            symbol: self.get_stats(symbol=symbol, days=days)
            for symbol in symbols
        }

    # =========================================================================
    # LLM Context Builder
    # =========================================================================

    def get_llm_context(
        self,
        symbol: str,
        regime: str,
        max_tokens: int = 500,
    ) -> str:
        """
        Build smart context for LLM veto decision.

        Returns a compact summary (~500 tokens) with:
        1. Overall performance stats
        2. Performance in current regime
        3. Recent similar trades (same symbol or regime)

        Args:
            symbol: Current trade symbol
            regime: Current market regime
            max_tokens: Approximate max tokens for context

        Returns:
            Formatted context string for LLM prompt
        """
        lines = ["=== TRADING HISTORY ===", ""]

        # 1. Overall stats (last 30 days)
        overall = self.get_stats(days=30)
        if overall.total_trades > 0:
            lines.append(f"Overall (30d): {overall.to_summary()}")
        else:
            lines.append("Overall: No completed trades yet - this is a new system")

        # 2. Stats for current regime
        regime_stats = self.get_stats(regime=regime, days=30)
        if regime_stats.total_trades > 0:
            lines.append(f"In {regime.upper()} regime: {regime_stats.to_summary()}")

        # 3. Stats for this symbol
        symbol_stats = self.get_stats(symbol=symbol, days=30)
        if symbol_stats.total_trades >= 3:  # Only if meaningful sample
            lines.append(f"On {symbol}: {symbol_stats.to_summary()}")

        # 4. Recent similar trades (same symbol OR same regime)
        recent = self._get_recent_similar(symbol, regime, limit=self.MAX_RECENT_FOR_LLM)
        if recent:
            lines.extend(["", "Recent similar trades:"])
            for t in recent[-7:]:  # Last 7 max
                outcome = "WIN" if t.is_winner else "LOSS"
                lines.append(
                    f"  - {t.symbol} {t.direction} in {t.regime}: "
                    f"${t.pnl:+.2f} ({outcome}), ADX={t.adx:.0f}, LLM={t.llm_confidence:.0%}"
                )

        # 5. LLM decision accuracy (if enough data)
        accuracy = self._get_llm_accuracy()
        if accuracy:
            lines.extend(["", f"LLM accuracy: {accuracy}"])

        lines.append("")
        return "\n".join(lines)

    def _get_recent_similar(
        self,
        symbol: str,
        regime: str,
        limit: int = 10,
    ) -> List[TradeOutcome]:
        """Get recent trades similar to current setup."""
        # Prioritize same symbol, then same regime
        same_symbol = [
            t for t in self._closed_trades
            if t.symbol == symbol and t.is_winner is not None
        ][-limit:]

        same_regime = [
            t for t in self._closed_trades
            if t.regime == regime and t.symbol != symbol and t.is_winner is not None
        ][-(limit - len(same_symbol)):]

        # Combine and sort by time
        combined = same_symbol + same_regime
        combined.sort(key=lambda t: t.timestamp)

        return combined[-limit:]

    def _get_llm_accuracy(self) -> Optional[str]:
        """Calculate LLM decision accuracy."""
        # Get trades where LLM made a decision
        decided = [
            t for t in self._closed_trades
            if t.llm_decision in ("ALLOW", "DENY") and t.is_winner is not None
        ]

        if len(decided) < 5:  # Need enough data
            return None

        # ALLOW decisions that were winners = correct
        # DENY decisions we can't track (trade wasn't taken)
        # So we only track ALLOW accuracy
        allowed = [t for t in decided if t.llm_decision == "ALLOW"]
        if not allowed:
            return None

        correct = sum(1 for t in allowed if t.is_winner)
        accuracy = correct / len(allowed) * 100

        # High confidence accuracy
        high_conf = [t for t in allowed if t.llm_confidence >= 0.75]
        if high_conf:
            high_correct = sum(1 for t in high_conf if t.is_winner)
            high_acc = high_correct / len(high_conf) * 100
            return f"ALLOW decisions: {accuracy:.0f}% win rate ({correct}/{len(allowed)}), High-confidence (>75%): {high_acc:.0f}%"

        return f"ALLOW decisions: {accuracy:.0f}% win rate ({correct}/{len(allowed)})"

    # =========================================================================
    # Public API
    # =========================================================================

    @property
    def total_trades(self) -> int:
        """Total closed trades in memory."""
        return len(self._closed_trades)

    @property
    def open_trades(self) -> int:
        """Currently open trades."""
        return len(self._trades)

    def get_summary(self) -> Dict[str, Any]:
        """Get memory summary for monitoring."""
        overall = self.get_stats(days=30)
        return {
            "total_trades": self.total_trades,
            "open_trades": self.open_trades,
            "win_rate": overall.win_rate,
            "total_pnl": overall.total_pnl,
            "stats_by_regime": {
                k: {"win_rate": v.win_rate, "trades": v.total_trades}
                for k, v in self.get_stats_by_regime().items()
            },
        }


# =============================================================================
# Singleton Instance
# =============================================================================

_memory_instance: Optional[TradeMemory] = None


def get_trade_memory() -> TradeMemory:
    """Get or create singleton TradeMemory instance."""
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = TradeMemory()
    return _memory_instance


async def init_trade_memory(data_dir: Optional[Path] = None) -> TradeMemory:
    """Initialize and load trade memory."""
    global _memory_instance
    _memory_instance = TradeMemory(data_dir)
    await _memory_instance.load()
    return _memory_instance
