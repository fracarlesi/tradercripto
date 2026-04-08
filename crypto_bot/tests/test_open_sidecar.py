"""Smoke tests for the open-trade forecast sidecar helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from crypto_bot.flag_trader.open_sidecar import (
    delete_open_sidecar,
    list_open_sidecars,
    read_open_sidecar,
    resolve_open_dir,
    write_open_sidecar,
)


def test_write_read_delete_roundtrip(tmp_path: Path) -> None:
    trade_id = "abc123"
    opened = datetime(2026, 4, 8, 14, 32, 11, tzinfo=timezone.utc)
    expiry = datetime(2026, 4, 8, 15, 2, 11, tzinfo=timezone.utc)

    path = write_open_sidecar(
        trade_id,
        symbol="FET",
        side="long",
        entry_price=Decimal("0.2285"),
        predicted_tp_price=Decimal("0.2309"),
        predicted_sl_price=Decimal("0.2239"),
        predicted_tp_pct=1.07,
        predicted_sl_pct=2.02,
        opened_at=opened,
        expiry_at=expiry,
        base=tmp_path,
    )
    assert path is not None and path.exists()

    loaded = read_open_sidecar(trade_id, base=tmp_path)
    assert loaded is not None
    assert loaded["trade_id"] == trade_id
    assert loaded["symbol"] == "FET"
    assert loaded["side"] == "long"
    assert loaded["entry_price"] == 0.2285
    assert loaded["predicted_tp_price"] == 0.2309
    assert loaded["predicted_sl_price"] == 0.2239
    assert loaded["predicted_tp_pct"] == 1.07
    assert loaded["predicted_sl_pct"] == 2.02
    assert loaded["opened_at"].startswith("2026-04-08T14:32:11")
    assert loaded["expiry_at"].startswith("2026-04-08T15:02:11")

    rows = list_open_sidecars(base=tmp_path)
    assert len(rows) == 1
    assert rows[0]["trade_id"] == trade_id

    assert delete_open_sidecar(trade_id, base=tmp_path) is True
    # Second unlink is a no-op and must not raise.
    assert delete_open_sidecar(trade_id, base=tmp_path) is False
    assert read_open_sidecar(trade_id, base=tmp_path) is None
    assert list_open_sidecars(base=tmp_path) == []


def test_resolve_open_dir_creates_directory(tmp_path: Path) -> None:
    open_dir = resolve_open_dir(base=tmp_path)
    assert open_dir.exists()
    assert open_dir.name == "open"
    assert open_dir.parent.name == "forecasts"


def test_empty_trade_id_is_noop(tmp_path: Path) -> None:
    assert write_open_sidecar("", symbol="X", side="long", entry_price=1.0, base=tmp_path) is None
    assert delete_open_sidecar("", base=tmp_path) is False
    assert read_open_sidecar("", base=tmp_path) is None
