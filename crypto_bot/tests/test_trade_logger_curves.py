"""Tests for STAGE A trade_logger extensions: predicted levels + real curves.

Verifies:
- log_decision auto-assigns ``trade_id`` to non-HOLD records.
- log_outcome writes a per-trade sidecar JSON when curves are passed,
  using an atomic ``os.replace`` write path.
- exit_reason_v2 is populated from the legacy enum if not provided.
- Legacy JSONL records (without trade_id / curves) deserialize via
  ``get_training_data`` without raising.
"""

from __future__ import annotations

import json
from pathlib import Path

from crypto_bot.flag_trader.trade_logger import (
    FlagTradeLogger,
    TradeRecord,
    _map_exit_reason_to_v2,
)


def _make_decision(symbol: str = "BTC", action: str = "BUY") -> TradeRecord:
    return TradeRecord(
        timestamp="2026-04-07T12:00:00+00:00",
        symbol=symbol,
        action=action,
        action_id=2 if action == "BUY" else 0,
        confidence=1.7,
        log_prob=-0.4,
        candles_summary={"last_close": 100.0},
        portfolio={"total_account_value": 1000.0},
        predicted_tp_pct=2.5,
        predicted_sl_pct=1.0,
        predicted_tp_price=102.5,
        predicted_sl_price=99.0,
        expiry_at="2026-04-07T20:30:00+00:00",
        k_candles=34,
        candle_interval_sec=900,
    )


def test_log_decision_assigns_trade_id(tmp_path: Path) -> None:
    logger = FlagTradeLogger(log_dir=tmp_path)
    rec = _make_decision()
    assert rec.trade_id is None
    logger.log_decision(rec)
    assert rec.trade_id is not None
    assert len(rec.trade_id) >= 16


def test_log_outcome_writes_sidecar_with_curves(tmp_path: Path) -> None:
    logger = FlagTradeLogger(log_dir=tmp_path)
    rec = _make_decision()
    logger.log_decision(rec)
    trade_id = rec.trade_id

    real_high = [100.5, 101.0, 101.7, 102.6]
    real_low = [99.8, 100.2, 100.9, 101.5]
    logger.log_outcome(
        symbol="BTC",
        entry_price=100.0,
        exit_price=102.6,
        pnl_usd=2.6,
        pnl_pct=2.6,
        exit_reason="take_profit",
        hold_duration_minutes=60.0,
        side="long",
        real_high_curve=real_high,
        real_low_curve=real_low,
    )

    sidecar = tmp_path / "forecasts" / f"{trade_id}.json"
    assert sidecar.exists(), "sidecar must be written for trades with curves"
    data = json.loads(sidecar.read_text())
    assert data["trade_id"] == trade_id
    assert data["real_high_curve"] == real_high
    assert data["real_low_curve"] == real_low
    assert data["real_observed_k"] == 4
    assert data["exit_reason_v2"] == "tp"
    assert data["k_candles"] == 34


def test_legacy_record_roundtrip(tmp_path: Path) -> None:
    """Records persisted by an older bot version (no STAGE A fields) must
    still be readable via ``get_training_data``."""
    logger = FlagTradeLogger(log_dir=tmp_path)
    legacy = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "symbol": "ETH",
        "action": "BUY",
        "action_id": 2,
        "confidence": 1.6,
        "log_prob": -0.3,
        "candles_summary": {},
        "portfolio": {},
        "entry_price": 2000.0,
        "exit_price": 2050.0,
        "pnl_usd": 50.0,
        "pnl_pct": 2.5,
        "exit_reason": "take_profit",
        "hold_duration_minutes": 30.0,
    }
    out_file = tmp_path / "outcomes_2026_01.jsonl"
    out_file.write_text(json.dumps(legacy) + "\n")
    records = logger.get_training_data()
    assert len(records) == 1
    assert records[0]["symbol"] == "ETH"
    assert "trade_id" not in records[0]


def test_reversal_trade_id_lookup_beats_fifo(tmp_path: Path) -> None:
    """Rapid reversal: SELL then BUY on BTC before first outcome lands.

    With explicit ``trade_id`` in log_outcome, the outcome MUST correlate to
    the matching decision (not the FIFO head by symbol).
    """
    logger = FlagTradeLogger(log_dir=tmp_path)

    sell_rec = _make_decision(symbol="BTC", action="SELL")
    logger.log_decision(sell_rec)
    sell_id = sell_rec.trade_id
    assert sell_id is not None

    buy_rec = _make_decision(symbol="BTC", action="BUY")
    logger.log_decision(buy_rec)
    buy_id = buy_rec.trade_id
    assert buy_id is not None
    assert buy_id != sell_id

    # Close the BUY (newer) first by explicit trade_id.  Under FIFO-by-symbol
    # this would incorrectly pop the SELL record.
    logger.log_outcome(
        symbol="BTC",
        entry_price=100.0,
        exit_price=103.0,
        pnl_usd=3.0,
        pnl_pct=3.0,
        exit_reason="take_profit",
        hold_duration_minutes=15.0,
        side="long",
        trade_id=buy_id,
    )

    # The SELL record must still be pending (FIFO would have consumed it).
    assert "BTC" in logger._pending_trades
    remaining = logger._pending_trades["BTC"]
    assert len(remaining) == 1
    assert remaining[0].trade_id == sell_id
    assert remaining[0].action == "SELL"

    # Outcome file should contain exactly one record tagged with buy_id.
    out_files = list(tmp_path.glob("outcomes_*.jsonl"))
    assert len(out_files) == 1
    lines = [json.loads(line) for line in out_files[0].read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["trade_id"] == buy_id
    assert lines[0]["action"] == "BUY"

    # Now close the SELL with explicit trade_id; bucket must empty out.
    logger.log_outcome(
        symbol="BTC",
        entry_price=100.0,
        exit_price=99.0,
        pnl_usd=1.0,
        pnl_pct=1.0,
        exit_reason="take_profit",
        hold_duration_minutes=20.0,
        side="short",
        trade_id=sell_id,
    )
    assert "BTC" not in logger._pending_trades


def test_log_outcome_backward_compat_fifo(tmp_path: Path) -> None:
    """When ``trade_id`` is not provided, legacy FIFO-by-symbol path still works."""
    logger = FlagTradeLogger(log_dir=tmp_path)
    rec = _make_decision(symbol="ETH", action="BUY")
    logger.log_decision(rec)
    original_id = rec.trade_id

    logger.log_outcome(
        symbol="ETH",
        entry_price=2000.0,
        exit_price=2050.0,
        pnl_usd=50.0,
        pnl_pct=2.5,
        exit_reason="take_profit",
        hold_duration_minutes=10.0,
        side="long",
    )

    assert "ETH" not in logger._pending_trades
    out_files = list(tmp_path.glob("outcomes_*.jsonl"))
    assert len(out_files) == 1
    line = json.loads(out_files[0].read_text().splitlines()[0])
    assert line["trade_id"] == original_id


def test_map_exit_reason_to_v2() -> None:
    assert _map_exit_reason_to_v2("take_profit") == "tp"
    assert _map_exit_reason_to_v2("stop_loss") == "sl"
    assert _map_exit_reason_to_v2("trailing_stop") == "sl"
    assert _map_exit_reason_to_v2("violation_exit") == "sl"
    assert _map_exit_reason_to_v2("timeout") == "expiry"
    assert _map_exit_reason_to_v2("regime_change") == "expiry"
    assert _map_exit_reason_to_v2("manual") == "manual"
    assert _map_exit_reason_to_v2(None) is None
    assert _map_exit_reason_to_v2("") is None
