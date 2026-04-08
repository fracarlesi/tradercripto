"""Generate fake outcomes_*.jsonl + sidecar for local dashboard smoke test.

Usage:
    HLQUANTBOT_DATA_DIR=/tmp/hlqb_fake python -m crypto_bot.frontend._make_fake_data
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> None:
    base = Path(os.environ.get("HLQUANTBOT_DATA_DIR", "/tmp/hlqb_fake"))
    log_dir = base / "trade_logs"
    fc_dir = log_dir / "forecasts"
    log_dir.mkdir(parents=True, exist_ok=True)
    fc_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    out_file = log_dir / f"outcomes_{now.strftime('%Y_%m')}.jsonl"

    rows = []
    # Modern trade with curve
    tid = str(uuid.uuid4())
    entry = 100.0
    tp_pct, sl_pct = 1.5, 1.0
    tp_price = entry * (1 + tp_pct / 100)
    sl_price = entry * (1 - sl_pct / 100)
    real_high = [entry + i * 0.1 for i in range(20)]
    real_low = [entry - 0.05 - i * 0.05 for i in range(20)]
    rows.append({
        "trade_id": tid,
        "timestamp": (now - timedelta(hours=2)).isoformat(),
        "symbol": "BTC",
        "action": "BUY",
        "action_id": 2,
        "confidence": 1.7,
        "log_prob": -0.42,
        "candles_summary": {},
        "portfolio": {},
        "entry_price": entry,
        "exit_price": tp_price,
        "pnl_usd": 1.50,
        "pnl_pct": 1.5,
        "exit_reason": "take_profit",
        "exit_reason_v2": "tp",
        "predicted_tp_pct": tp_pct,
        "predicted_sl_pct": sl_pct,
        "predicted_tp_price": tp_price,
        "predicted_sl_price": sl_price,
        "k_candles": 34,
        "candle_interval_sec": 900,
        "real_high_curve": real_high,
        "real_low_curve": real_low,
        "real_observed_k": 20,
    })
    sidecar = {
        "predicted_tp": tp_price,
        "predicted_sl": sl_price,
        "k": 34,
        "interval_sec": 900,
        "real_high": real_high,
        "real_low": real_low,
        "entry": entry,
        "exit": tp_price,
        "exit_reason_v2": "tp",
        "timestamps": [],
    }
    (fc_dir / f"{tid}.json").write_text(json.dumps(sidecar))

    # Legacy trade (no trade_id, no curve)
    rows.append({
        "timestamp": (now - timedelta(hours=5)).isoformat(),
        "symbol": "ETH",
        "action": "SELL",
        "action_id": 0,
        "confidence": 1.55,
        "log_prob": -0.6,
        "candles_summary": {},
        "portfolio": {},
        "entry_price": 2000.0,
        "exit_price": 2010.0,
        "pnl_usd": -0.30,
        "pnl_pct": -0.5,
        "exit_reason": "stop_loss",
    })

    # Expiry trade
    tid2 = str(uuid.uuid4())
    rows.append({
        "trade_id": tid2,
        "timestamp": (now - timedelta(hours=8)).isoformat(),
        "symbol": "SOL",
        "action": "BUY",
        "action_id": 2,
        "confidence": 1.6,
        "log_prob": -0.5,
        "candles_summary": {},
        "portfolio": {},
        "entry_price": 150.0,
        "exit_price": 150.4,
        "pnl_usd": 0.10,
        "pnl_pct": 0.27,
        "exit_reason_v2": "expiry",
        "predicted_tp_pct": 1.2,
        "predicted_sl_pct": 0.9,
        "predicted_tp_price": 151.8,
        "predicted_sl_price": 148.65,
        "k_candles": 34,
        "candle_interval_sec": 900,
    })

    with open(out_file, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"Wrote {len(rows)} fake trades to {out_file}")
    print(f"Modern trade_id: {tid}")


if __name__ == "__main__":
    main()
