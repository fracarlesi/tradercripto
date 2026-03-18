"""
Trade Journal
==============

Persistent per-session trade log stored as JSON files.
Each trading day gets its own file in data/trade_journal/.

Provides the historical data needed by the Scorecard for
promotion state evaluation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_JOURNAL_DIR = Path("data/trade_journal")


@dataclass
class TradeRecord:
    """Single trade record for journaling."""

    trade_id: str
    symbol: str
    direction: str
    setup_type: str
    entry_price: str  # Decimal as string
    entry_time: str
    contracts: int
    stop_price: str
    target_price: str
    exit_price: Optional[str] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None
    pnl: Optional[str] = None
    regime: Optional[str] = None
    confidence: Optional[str] = None


@dataclass
class SessionRecord:
    """All trades from a single trading session (day)."""

    date: str
    trades: List[TradeRecord] = field(default_factory=list)
    total_pnl: str = "0"
    trade_count: int = 0


class TradeJournal:
    """Persistent trade journal with JSON file storage."""

    def __init__(self, journal_dir: Path | str | None = None) -> None:
        self._dir = Path(journal_dir) if journal_dir else _JOURNAL_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._today_session: Optional[SessionRecord] = None

    def _get_session(self) -> SessionRecord:
        """Get or create today's session record."""
        today = date.today().isoformat()
        if self._today_session is None or self._today_session.date != today:
            # Try to load existing file
            path = self._dir / f"{today}.json"
            if path.exists():
                try:
                    with open(path) as f:
                        data = json.load(f)
                    self._today_session = SessionRecord(
                        date=data["date"],
                        trades=[TradeRecord(**t) for t in data.get("trades", [])],
                        total_pnl=data.get("total_pnl", "0"),
                        trade_count=data.get("trade_count", 0),
                    )
                except Exception as e:
                    logger.warning("Failed to load journal %s: %s", path, e)
                    self._today_session = SessionRecord(date=today)
            else:
                self._today_session = SessionRecord(date=today)
        return self._today_session

    def record_entry(
        self,
        trade_id: str,
        symbol: str,
        direction: str,
        setup_type: str,
        entry_price: Decimal,
        contracts: int,
        stop_price: Decimal,
        target_price: Decimal,
        regime: str | None = None,
        confidence: Decimal | None = None,
    ) -> None:
        """Record a trade entry."""
        session = self._get_session()

        record = TradeRecord(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            setup_type=setup_type,
            entry_price=str(entry_price),
            entry_time=datetime.now().isoformat(),
            contracts=contracts,
            stop_price=str(stop_price),
            target_price=str(target_price),
            regime=regime,
            confidence=str(confidence) if confidence else None,
        )

        session.trades.append(record)
        session.trade_count = len(session.trades)
        self._save_session(session)

        logger.info(
            "Journal: recorded entry %s %s %s @ %s",
            trade_id, direction, symbol, entry_price,
        )

    def record_exit(
        self,
        trade_id: str,
        exit_price: Decimal,
        exit_reason: str,
        pnl: Decimal,
    ) -> None:
        """Update a trade record with exit details."""
        session = self._get_session()

        for trade in session.trades:
            if trade.trade_id == trade_id:
                trade.exit_price = str(exit_price)
                trade.exit_time = datetime.now().isoformat()
                trade.exit_reason = exit_reason
                trade.pnl = str(pnl)
                break
        else:
            logger.warning("Journal: trade_id %s not found for exit", trade_id)
            return

        # Update total PnL
        total = Decimal("0")
        for t in session.trades:
            if t.pnl is not None:
                total += Decimal(t.pnl)
        session.total_pnl = str(total)

        self._save_session(session)

        logger.info(
            "Journal: recorded exit %s @ %s reason=%s pnl=%s",
            trade_id, exit_price, exit_reason, pnl,
        )

    def load_sessions(self, days: int = 30) -> List[SessionRecord]:
        """Load session records for the last N calendar days.

        Args:
            days: Number of calendar days to look back.

        Returns:
            List of SessionRecord sorted by date ascending.
        """
        sessions: List[SessionRecord] = []
        today = date.today()

        for i in range(days):
            d = today - __import__("datetime").timedelta(days=i)
            path = self._dir / f"{d.isoformat()}.json"
            if path.exists():
                try:
                    with open(path) as f:
                        data = json.load(f)
                    session = SessionRecord(
                        date=data["date"],
                        trades=[TradeRecord(**t) for t in data.get("trades", [])],
                        total_pnl=data.get("total_pnl", "0"),
                        trade_count=data.get("trade_count", 0),
                    )
                    sessions.append(session)
                except Exception as e:
                    logger.warning("Failed to load journal %s: %s", path, e)

        return sorted(sessions, key=lambda s: s.date)

    def _save_session(self, session: SessionRecord) -> None:
        """Persist session to JSON file."""
        path = self._dir / f"{session.date}.json"
        data = {
            "date": session.date,
            "trades": [asdict(t) for t in session.trades],
            "total_pnl": session.total_pnl,
            "trade_count": session.trade_count,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def get_today_pnl(self) -> Decimal:
        """Get today's total P&L."""
        session = self._get_session()
        return Decimal(session.total_pnl)

    def get_today_trade_count(self) -> int:
        """Get today's trade count."""
        session = self._get_session()
        return session.trade_count
