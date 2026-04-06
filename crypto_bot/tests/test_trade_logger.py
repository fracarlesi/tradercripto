"""Tests for FlagTradeLogger — decisions, outcomes, fallback, env var."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from crypto_bot.flag_trader.trade_logger import FlagTradeLogger, TradeRecord


def _make_record(symbol: str = "BTC", action: str = "BUY") -> TradeRecord:
    return TradeRecord(
        timestamp="2026-04-06T00:00:00+00:00",
        symbol=symbol,
        action=action,
        action_id=2 if action == "BUY" else 0,
        confidence=1.8,
        log_prob=-0.2,
        candles_summary={"last_close": 100.0},
        portfolio={"total": 1000.0},
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_log_decision_writes_file(tmp_path: Path) -> None:
    tl = FlagTradeLogger(log_dir=tmp_path)
    tl.log_decision(_make_record("ETH", "BUY"))
    files = list(tmp_path.glob("decisions_*.jsonl"))
    assert len(files) == 1
    rows = _read_jsonl(files[0])
    assert rows[0]["symbol"] == "ETH"
    assert rows[0]["action"] == "BUY"


def test_log_outcome_with_pending_decision(tmp_path: Path) -> None:
    tl = FlagTradeLogger(log_dir=tmp_path)
    tl.log_decision(_make_record("BTC", "BUY"))
    tl.log_outcome(
        symbol="BTC",
        entry_price=100.0,
        exit_price=110.0,
        pnl_usd=10.0,
        pnl_pct=10.0,
        exit_reason="take_profit",
        hold_duration_minutes=30.0,
    )
    files = list(tmp_path.glob("outcomes_*.jsonl"))
    assert len(files) == 1
    rows = _read_jsonl(files[0])
    assert rows[0]["symbol"] == "BTC"
    assert rows[0]["pnl_usd"] == 10.0
    assert rows[0]["exit_reason"] == "take_profit"
    assert rows[0]["confidence"] == 1.8  # enriched from pending decision


def test_log_outcome_fallback_without_pending(tmp_path: Path) -> None:
    """After a process restart, _pending_trades is empty — outcome must still persist."""
    tl = FlagTradeLogger(log_dir=tmp_path)
    tl.log_outcome(
        symbol="SOL",
        entry_price=20.0,
        exit_price=22.0,
        pnl_usd=2.0,
        pnl_pct=10.0,
        exit_reason="stop_loss",
        hold_duration_minutes=5.0,
        side="long",
    )
    files = list(tmp_path.glob("outcomes_*.jsonl"))
    assert len(files) == 1
    rows = _read_jsonl(files[0])
    assert rows[0]["symbol"] == "SOL"
    assert rows[0]["pnl_usd"] == 2.0
    assert rows[0]["action"] == "BUY"  # side=long -> BUY fallback


def test_log_outcome_short_side_fallback(tmp_path: Path) -> None:
    tl = FlagTradeLogger(log_dir=tmp_path)
    tl.log_outcome(
        symbol="AVAX",
        entry_price=30.0,
        exit_price=28.0,
        pnl_usd=2.0,
        pnl_pct=6.6,
        exit_reason="take_profit",
        hold_duration_minutes=15.0,
        side="short",
    )
    rows = _read_jsonl(next(tmp_path.glob("outcomes_*.jsonl")))
    assert rows[0]["action"] == "SELL"


def test_get_training_data_roundtrip(tmp_path: Path) -> None:
    tl = FlagTradeLogger(log_dir=tmp_path)
    tl.log_decision(_make_record("BTC", "BUY"))
    tl.log_outcome(
        symbol="BTC",
        entry_price=1.0,
        exit_price=2.0,
        pnl_usd=1.0,
        pnl_pct=100.0,
        exit_reason="tp",
        hold_duration_minutes=1.0,
    )
    data = tl.get_training_data()
    assert len(data) == 1
    assert data[0]["symbol"] == "BTC"


def test_env_var_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HLQUANTBOT_DATA_DIR", str(tmp_path))
    tl = FlagTradeLogger()  # no explicit arg
    assert tl.log_dir == tmp_path / "trade_logs"
    assert tl.log_dir.exists()


def test_backwards_compat_update_outcome_alias(tmp_path: Path) -> None:
    tl = FlagTradeLogger(log_dir=tmp_path)
    assert tl.update_outcome == tl.log_outcome


def test_log_outcome_fifo_order(tmp_path: Path) -> None:
    """Two pending BUYs for BTC: the first close consumes the OLDEST (FIFO)."""
    tl = FlagTradeLogger(log_dir=tmp_path)
    first = _make_record("BTC", "BUY")
    first.confidence = 1.1
    second = _make_record("BTC", "BUY")
    second.confidence = 2.2
    tl.log_decision(first)
    tl.log_decision(second)

    tl.log_outcome(
        symbol="BTC",
        entry_price=100.0,
        exit_price=110.0,
        pnl_usd=10.0,
        pnl_pct=10.0,
        exit_reason="take_profit",
        hold_duration_minutes=30.0,
    )
    rows = _read_jsonl(next(tmp_path.glob("outcomes_*.jsonl")))
    assert len(rows) == 1
    assert rows[0]["confidence"] == 1.1  # oldest consumed
    # Second decision still pending
    assert "BTC" in tl._pending_trades
    assert len(tl._pending_trades["BTC"]) == 1
    assert tl._pending_trades["BTC"][0].confidence == 2.2


def test_pending_cap_drops_oldest(tmp_path: Path) -> None:
    tl = FlagTradeLogger(log_dir=tmp_path)
    for i in range(11):
        rec = _make_record("BTC", "BUY")
        rec.confidence = float(i)
        tl.log_decision(rec)
    assert len(tl._pending_trades["BTC"]) == 10
    # Oldest (confidence=0.0) was dropped; list now starts at 1.0
    assert tl._pending_trades["BTC"][0].confidence == 1.0
    assert tl._pending_trades["BTC"][-1].confidence == 10.0


def test_list_cleared_after_last_pop(tmp_path: Path) -> None:
    tl = FlagTradeLogger(log_dir=tmp_path)
    tl.log_decision(_make_record("BTC", "BUY"))
    tl.log_outcome(
        symbol="BTC",
        entry_price=1.0,
        exit_price=2.0,
        pnl_usd=1.0,
        pnl_pct=100.0,
        exit_reason="tp",
        hold_duration_minutes=1.0,
    )
    assert "BTC" not in tl._pending_trades
