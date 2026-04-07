"""One-shot backfill: correct historical mislabeled exit_reason in outcomes_*.jsonl.

Background
----------
In LLM-only mode (``stop_loss_pct=0`` and ``take_profit_pct=0``) the
``_handle_position_closed`` exit_reason inference defaulted to ``"stop_loss"``
regardless of actual PnL: ``implied_tp`` and ``implied_sl`` both collapsed to
``entry_price`` and the tie-breaker fell to stop_loss. The bug was fixed in
commit 0df394a (deployed 2026-04-07) and now LLM-only closes are labeled
``"external_close"``.

This script rewrites historical JSONL outcome lines that match the smoking gun
(label == stop_loss AND pnl_usd > 0) within the affected window, replacing the
label with ``"external_close"``.

Safety
------
- Defaults to dry-run. Pass ``--apply`` to actually write.
- Always writes a ``<file>.bak`` next to the source before in-place rewrite.
- Idempotent: re-running on already-fixed files makes zero changes and skips
  creating a duplicate ``.bak``.

Usage
-----
    python3 -m crypto_bot.scripts.backfill_exit_reason --dry-run
    python3 -m crypto_bot.scripts.backfill_exit_reason --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Iterable

# Window during which LLM-only mode was active with the buggy inference.
# Confirm by grepping outcomes files before trusting this default.
DEFAULT_WINDOW_START = "2026-04-06T00:00:00"

OLD_LABEL = "stop_loss"
NEW_LABEL = "external_close"


@dataclass
class FileReport:
    path: Path
    corrected: int = 0
    total_pnl_usd: float = 0.0
    symbols: set[str] = field(default_factory=set)
    skipped_invalid: int = 0


def _should_fix(record: dict, window_start: str) -> bool:
    """Return True iff this record matches the bug's smoking-gun pattern."""
    if record.get("exit_reason") != OLD_LABEL:
        return False
    pnl_usd = record.get("pnl_usd")
    if pnl_usd is None:
        return False
    try:
        if Decimal(str(pnl_usd)) <= 0:
            return False
    except Exception:
        return False
    ts = record.get("timestamp")
    if not isinstance(ts, str) or ts < window_start:
        return False
    return True


def _process_lines(lines: Iterable[str], window_start: str) -> tuple[list[str], FileReport, bool]:
    """Return (new_lines, report, changed)."""
    report = FileReport(path=Path())
    new_lines: list[str] = []
    changed = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            new_lines.append(raw)
            continue
        try:
            rec = json.loads(stripped)
        except json.JSONDecodeError:
            report.skipped_invalid += 1
            new_lines.append(raw)
            continue
        if _should_fix(rec, window_start):
            rec["exit_reason"] = NEW_LABEL
            report.corrected += 1
            try:
                report.total_pnl_usd += float(rec.get("pnl_usd", 0))
            except (TypeError, ValueError):
                pass
            sym = rec.get("symbol")
            if isinstance(sym, str):
                report.symbols.add(sym)
            # Preserve trailing newline if present in original line.
            suffix = "\n" if raw.endswith("\n") else ""
            new_lines.append(json.dumps(rec) + suffix)
            changed = True
        else:
            new_lines.append(raw)
    return new_lines, report, changed


def process_file(path: Path, window_start: str, apply: bool) -> FileReport:
    with path.open("r", encoding="utf-8") as f:
        original_lines = f.readlines()
    new_lines, report, changed = _process_lines(original_lines, window_start)
    report.path = path
    if changed and apply:
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.writelines(new_lines)
        tmp.replace(path)
    return report


def _print_summary(reports: list[FileReport], apply: bool) -> None:
    header = "[APPLIED]" if apply else "[DRY RUN]"
    print(f"\n{header} Backfill exit_reason summary")
    print("=" * 78)
    print(f"{'file':<48} {'fixed':>7} {'total_pnl_usd':>16}")
    print("-" * 78)
    grand_fixed = 0
    grand_pnl = 0.0
    all_symbols: set[str] = set()
    for r in reports:
        print(f"{r.path.name:<48} {r.corrected:>7} {r.total_pnl_usd:>16.4f}")
        grand_fixed += r.corrected
        grand_pnl += r.total_pnl_usd
        all_symbols.update(r.symbols)
        if r.skipped_invalid:
            print(f"  (warning: {r.skipped_invalid} unparsable lines skipped in {r.path.name})")
    print("-" * 78)
    print(f"{'TOTAL':<48} {grand_fixed:>7} {grand_pnl:>16.4f}")
    print(f"\nSymbols affected ({len(all_symbols)}): {', '.join(sorted(all_symbols)) or '-'}")
    if not apply and grand_fixed > 0:
        print("\nRe-run with --apply to write changes (.bak backups will be created).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("/opt/hlquantbot_trade_logs"),
        help="Directory containing outcomes_*.jsonl files",
    )
    parser.add_argument(
        "--window-start",
        default=DEFAULT_WINDOW_START,
        help="ISO timestamp; only records with timestamp >= this are touched",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes (default is dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry-run mode (default True; --apply overrides)",
    )
    args = parser.parse_args(argv)

    apply = bool(args.apply)
    input_dir: Path = args.input_dir

    if not input_dir.is_dir():
        print(f"ERROR: input directory not found: {input_dir}")
        return 2

    files = sorted(input_dir.glob("outcomes_*.jsonl"))
    if not files:
        print(f"No outcomes_*.jsonl files in {input_dir}")
        return 0

    reports = [process_file(p, args.window_start, apply=apply) for p in files]
    _print_summary(reports, apply=apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
