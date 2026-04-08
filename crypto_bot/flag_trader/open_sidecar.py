"""Open-trade forecast sidecars.

STAGE A writes a lightweight JSON "open sidecar" per trade at entry fill time
containing the predicted TP/SL levels so the dashboard can overlay them on a
live chart / table even before the position is closed. The file lives under::

    $HLQUANTBOT_DATA_DIR/trade_logs/forecasts/open/<trade_id>.json

and is unlinked when the position closes. The schema is additive and
retro-compatible: missing fields are simply absent.

This module is intentionally free of heavy dependencies (no pydantic) so it
can be imported from both the execution engine and the read-only Flask
dashboard without pulling the full trading stack.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _resolve_forecasts_dir() -> Path:
    env_dir = os.environ.get("HLQUANTBOT_DATA_DIR")
    if env_dir:
        base = Path(env_dir) / "trade_logs"
    else:
        base = Path("data/trade_logs")
    return base / "forecasts"


def resolve_open_dir(base: Optional[Path] = None) -> Path:
    """Return the on-disk directory for open-trade sidecars.

    Ensures the directory exists (``mkdir -p`` semantics). ``base`` overrides
    the environment-driven resolution and is primarily used by tests.
    """
    forecasts_dir = Path(base) / "forecasts" if base is not None else _resolve_forecasts_dir()
    open_dir = forecasts_dir / "open"
    try:
        open_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("open_sidecar: failed to mkdir %s", open_dir)
    return open_dir


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def write_open_sidecar(
    trade_id: str,
    *,
    symbol: str,
    side: str,
    entry_price: Any,
    predicted_tp_price: Any = None,
    predicted_sl_price: Any = None,
    predicted_tp_pct: Any = None,
    predicted_sl_pct: Any = None,
    opened_at: Optional[datetime] = None,
    expiry_at: Optional[datetime] = None,
    base: Optional[Path] = None,
    extra: Optional[dict[str, Any]] = None,
) -> Optional[Path]:
    """Persist an open-trade sidecar JSON. Returns the path on success.

    Failures are logged but never raised — this is best-effort instrumentation
    and must not break the live trading flow.
    """
    if not trade_id:
        return None
    payload: dict[str, Any] = {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "entry_price": _jsonable(entry_price),
        "predicted_tp_price": _jsonable(predicted_tp_price),
        "predicted_sl_price": _jsonable(predicted_sl_price),
        "predicted_tp_pct": _jsonable(predicted_tp_pct),
        "predicted_sl_pct": _jsonable(predicted_sl_pct),
        "opened_at": _jsonable(opened_at or datetime.now(timezone.utc)),
        "expiry_at": _jsonable(expiry_at),
    }
    if extra:
        for k, v in extra.items():
            payload.setdefault(k, _jsonable(v))
    try:
        open_dir = resolve_open_dir(base=base)
        path = open_dir / f"{trade_id}.json"
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(payload, fh, default=str)
        tmp.replace(path)
        return path
    except OSError:
        logger.exception("open_sidecar: failed to write trade_id=%s", trade_id)
        return None


def delete_open_sidecar(trade_id: str, *, base: Optional[Path] = None) -> bool:
    """Remove an open-trade sidecar. Returns True if a file was removed.

    Silently tolerates missing files (position may have been closed before the
    sidecar was written, e.g. on a fast reject path).
    """
    if not trade_id:
        return False
    try:
        open_dir = resolve_open_dir(base=base)
        path = open_dir / f"{trade_id}.json"
        if path.exists():
            path.unlink()
            return True
    except OSError:
        logger.exception("open_sidecar: failed to unlink trade_id=%s", trade_id)
    return False


def list_open_sidecars(base: Optional[Path] = None) -> list[dict[str, Any]]:
    """Return all open-trade sidecars currently on disk (unsorted)."""
    open_dir = resolve_open_dir(base=base)
    out: list[dict[str, Any]] = []
    if not open_dir.exists():
        return out
    for path in open_dir.glob("*.json"):
        try:
            with open(path, "r") as fh:
                out.append(json.load(fh))
        except (OSError, json.JSONDecodeError):
            logger.warning("open_sidecar: skip unreadable %s", path.name)
    return out


def read_open_sidecar(trade_id: str, *, base: Optional[Path] = None) -> Optional[dict[str, Any]]:
    if not trade_id:
        return None
    open_dir = resolve_open_dir(base=base)
    path = open_dir / f"{trade_id}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.exception("open_sidecar: failed to read %s", path)
        return None
