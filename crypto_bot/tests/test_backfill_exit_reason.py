"""Tests for scripts.backfill_exit_reason."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crypto_bot.scripts import backfill_exit_reason
from crypto_bot.scripts.backfill_exit_reason import (
    DEFAULT_WINDOW_START,
    NEW_LABEL,
    OLD_LABEL,
    process_file,
)

_ = backfill_exit_reason  # silence unused


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    records = [
        # 1) corrupted winner: should be fixed
        {
            "symbol": "BTC",
            "entry_price": 100.0,
            "exit_price": 110.0,
            "pnl_usd": 12.5,
            "pnl_pct": 0.10,
            "exit_reason": OLD_LABEL,
            "hold_duration_minutes": 30,
            "timestamp": "2026-04-06T12:00:00",
        },
        # 2) legitimate stop_loss loser: should NOT be touched
        {
            "symbol": "ETH",
            "entry_price": 200.0,
            "exit_price": 190.0,
            "pnl_usd": -8.0,
            "pnl_pct": -0.05,
            "exit_reason": OLD_LABEL,
            "hold_duration_minutes": 15,
            "timestamp": "2026-04-06T13:00:00",
        },
        # 3) already-labeled external_close: should NOT be touched
        {
            "symbol": "SOL",
            "entry_price": 50.0,
            "exit_price": 55.0,
            "pnl_usd": 5.0,
            "pnl_pct": 0.10,
            "exit_reason": NEW_LABEL,
            "hold_duration_minutes": 22,
            "timestamp": "2026-04-06T14:00:00",
        },
        # 4) outside time window: should NOT be touched
        {
            "symbol": "DOGE",
            "entry_price": 0.10,
            "exit_price": 0.11,
            "pnl_usd": 1.0,
            "pnl_pct": 0.10,
            "exit_reason": OLD_LABEL,
            "hold_duration_minutes": 10,
            "timestamp": "2026-04-05T23:59:59",
        },
    ]
    path = tmp_path / "outcomes_2026_04.jsonl"
    _write_jsonl(path, records)
    return path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_dry_run_does_not_write(sample_file: Path) -> None:
    report = process_file(sample_file, DEFAULT_WINDOW_START, apply=False)
    assert report.corrected == 1
    assert report.symbols == {"BTC"}
    assert report.total_pnl_usd == pytest.approx(12.5)
    # File on disk unchanged.
    on_disk = _read_jsonl(sample_file)
    assert on_disk[0]["exit_reason"] == OLD_LABEL
    assert not sample_file.with_suffix(".jsonl.bak").exists()


def test_apply_fixes_only_corrupted_winner(sample_file: Path) -> None:
    report = process_file(sample_file, DEFAULT_WINDOW_START, apply=True)
    assert report.corrected == 1

    on_disk = _read_jsonl(sample_file)
    btc, eth, sol, doge = on_disk
    assert btc["exit_reason"] == NEW_LABEL  # fixed
    assert eth["exit_reason"] == OLD_LABEL  # untouched (loser)
    assert sol["exit_reason"] == NEW_LABEL  # untouched (already labeled)
    assert doge["exit_reason"] == OLD_LABEL  # untouched (out of window)

    backup = sample_file.with_suffix(".jsonl.bak")
    assert backup.exists()
    backup_records = _read_jsonl(backup)
    assert backup_records[0]["exit_reason"] == OLD_LABEL


def test_idempotent_no_double_backup(sample_file: Path) -> None:
    process_file(sample_file, DEFAULT_WINDOW_START, apply=True)
    backup = sample_file.with_suffix(".jsonl.bak")
    first_mtime = backup.stat().st_mtime

    report = process_file(sample_file, DEFAULT_WINDOW_START, apply=True)
    assert report.corrected == 0
    # Backup not overwritten.
    assert backup.stat().st_mtime == first_mtime
