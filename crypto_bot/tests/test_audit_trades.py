"""Tests for crypto_bot/scripts/audit_trades.py.

All external calls (anthropic, github, docker, ntfy) are mocked. The script
is imported by file path so pytest doesn't require the full crypto_bot
package import chain.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


# -----------------------------------------------------------------------------
# Import the audit script by path to bypass crypto_bot/__init__.py side-effects
# (it pulls pandas/hyperliquid which we don't need for these tests).
# -----------------------------------------------------------------------------

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "audit_trades.py"
)
_spec = importlib.util.spec_from_file_location("audit_trades_under_test", _SCRIPT)
assert _spec and _spec.loader
audit = importlib.util.module_from_spec(_spec)
sys.modules["audit_trades_under_test"] = audit
_spec.loader.exec_module(audit)  # type: ignore[union-attr]


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _iso(minutes_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _make_cfg(tmp_path: Path) -> "audit.AuditConfig":
    return audit.AuditConfig(
        anthropic_api_key="test-key",
        github_repo="fracarlesi/tradercripto",
        github_token="gh-test",
        ntfy_topic=None,
        trade_logs_path=tmp_path,
        heartbeat=False,
        window_minutes=35,
        sonnet_model="claude-sonnet-4-5",
        opus_model="claude-opus-4-1",
        cost_log_path=tmp_path / "audit_cost.jsonl",
    )


def _write_outcomes(tmp_path: Path, records: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    fname = tmp_path / f"outcomes_{now.strftime('%Y_%m')}.jsonl"
    with open(fname, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _empty_state() -> "audit.BotState":
    return audit.BotState(n_positions=0, equity=1000.0, margin=100.0, leverage=1.0)


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_no_recent_trades_returns_ok(tmp_path, capsys):
    """Empty outcomes file -> no flags -> clean exit 0, no LLM call."""
    cfg = _make_cfg(tmp_path)
    _write_outcomes(tmp_path, [])  # empty file

    env = {
        "ANTHROPIC_API_KEY": "test-key",
        "HLQUANTBOT_TRADE_LOGS_PATH": str(tmp_path),
        "AUDIT_HEARTBEAT": "0",
        "AUDIT_COST_LOG": str(cfg.cost_log_path),
    }

    with patch.object(audit, "load_bot_state", return_value=_empty_state()), \
         patch.object(audit, "call_sonnet") as mock_sonnet, \
         patch.object(audit, "ntfy"), \
         patch.dict(os.environ, env, clear=False):
        rc = audit.run_audit()

    assert rc == 0
    mock_sonnet.assert_not_called()
    out = capsys.readouterr().out
    assert '"status": "ok"' in out


def test_churn_flag_detected():
    """Outcome with hold < 2min on a non-target exit -> HIGH churn flag."""
    outcomes = [
        {
            "timestamp": _iso(5),
            "symbol": "ETH",
            "action": "BUY",
            "hold_duration_minutes": 1.5,
            "exit_reason": "momentum_fade",
            "pnl_usd": 0.30,
            "pnl_pct": 0.02,
        }
    ]
    flags = audit.compute_flags(outcomes, _empty_state())
    churn = [f for f in flags if f.kind == "churn"]
    assert len(churn) == 1
    assert churn[0].severity == "HIGH"
    assert churn[0].symbol == "ETH"


def test_churn_not_flagged_when_take_profit():
    """Fast take_profit exit (hold < 2min) must NOT be flagged as churn.

    Regression test for XPL incident 2026-04-07 01:19: a 32s trade closed
    at take_profit (+$0.18) was falsely flagged as churn. Target-based
    exits are planned closures, not churning.
    """
    outcomes = [
        {
            "timestamp": _iso(5),
            "symbol": "XPL",
            "action": "BUY",
            "hold_duration_minutes": 0.5,
            "exit_reason": "take_profit",
            "pnl_usd": 0.18,
            "pnl_pct": 0.12,
        }
    ]
    flags = audit.compute_flags(outcomes, _empty_state())
    churn = [f for f in flags if f.kind == "churn"]
    assert len(churn) == 0


def test_churn_flagged_when_model_reversal():
    """Fast exit via model_reversal (hold < 2min) IS churn (regression guard)."""
    outcomes = [
        {
            "timestamp": _iso(5),
            "symbol": "BTC",
            "action": "BUY",
            "hold_duration_minutes": 0.5,
            "exit_reason": "model_reversal",
            "pnl_usd": -0.25,
            "pnl_pct": -0.15,
        }
    ]
    flags = audit.compute_flags(outcomes, _empty_state())
    churn = [f for f in flags if f.kind == "churn"]
    assert len(churn) == 1
    assert churn[0].symbol == "BTC"


def test_zero_confidence_detected():
    """STAGE A: a trade taken with confidence=0 flags as model fallback."""
    outcomes = [
        {
            "timestamp": _iso(5),
            "symbol": "SOL",
            "action": "BUY",
            "confidence": 0.0,
            "hold_duration_minutes": 60.0,
            "exit_reason_v2": "sl",
            "pnl_usd": -0.80,
            "pnl_pct": -0.5,
        }
    ]
    flags = audit.compute_flags(outcomes, _empty_state())
    kinds = {f.kind for f in flags}
    assert "zero_confidence" in kinds
    zc = next(f for f in flags if f.kind == "zero_confidence")
    assert zc.severity == "MEDIUM"


def test_rr_prediction_asymmetric_flagged():
    """STAGE A: predicted tp/sl with R/R < 1:1.5 flags as asymmetric."""
    outcomes = [
        {
            "timestamp": _iso(5),
            "symbol": "ZEC",
            "action": "SELL",
            "confidence": 1.8,
            "hold_duration_minutes": 65.0,
            "exit_reason_v2": "sl",
            "predicted_tp_pct": 0.5,
            "predicted_sl_pct": 2.0,
            "pnl_usd": -0.63,
            "pnl_pct": -1.68,
        }
    ]
    flags = audit.compute_flags(outcomes, _empty_state())
    kinds = {f.kind for f in flags}
    assert "rr_prediction_asymmetric" in kinds


def test_rr_geometry_asymmetric_aggregate():
    """STAGE A: 3+ SL-closed losses with avg loss >> avg win = broken R/R."""
    outcomes = [
        {"timestamp": _iso(60), "symbol": "BTC", "pnl_usd": -1.0,
         "hold_duration_minutes": 60, "exit_reason_v2": "sl"},
        {"timestamp": _iso(50), "symbol": "ETH", "pnl_usd": -1.2,
         "hold_duration_minutes": 60, "exit_reason_v2": "sl"},
        {"timestamp": _iso(40), "symbol": "SOL", "pnl_usd": -1.5,
         "hold_duration_minutes": 60, "exit_reason_v2": "sl"},
        {"timestamp": _iso(30), "symbol": "ADA", "pnl_usd": 0.25,
         "hold_duration_minutes": 30, "exit_reason_v2": "tp"},
    ]
    flags = audit.compute_flags(outcomes, _empty_state())
    kinds = {f.kind for f in flags}
    assert "rr_geometry_asymmetric" in kinds


def test_high_expiry_rate_flagged():
    """STAGE A: > 30% expiry rate flags forecast horizon mismatch."""
    outcomes = [
        {"timestamp": _iso(60), "symbol": "BTC", "pnl_usd": 0.1,
         "exit_reason_v2": "expiry", "hold_duration_minutes": 510},
        {"timestamp": _iso(50), "symbol": "ETH", "pnl_usd": -0.1,
         "exit_reason_v2": "expiry", "hold_duration_minutes": 510},
        {"timestamp": _iso(40), "symbol": "SOL", "pnl_usd": 0.1,
         "exit_reason_v2": "tp", "hold_duration_minutes": 30},
        {"timestamp": _iso(30), "symbol": "ADA", "pnl_usd": -0.1,
         "exit_reason_v2": "sl", "hold_duration_minutes": 60},
        {"timestamp": _iso(20), "symbol": "XRP", "pnl_usd": 0.1,
         "exit_reason_v2": "tp", "hold_duration_minutes": 30},
    ]
    flags = audit.compute_flags(outcomes, _empty_state())
    kinds = {f.kind for f in flags}
    assert "high_expiry_rate" in kinds


def test_negative_streak_detected():
    """3+ consecutive losing trades on same symbol -> MEDIUM streak."""
    outcomes = [
        {"timestamp": _iso(30), "symbol": "BTC", "pnl_usd": -1.0, "hold_duration_minutes": 150},
        {"timestamp": _iso(20), "symbol": "BTC", "pnl_usd": -2.0, "hold_duration_minutes": 150},
        {"timestamp": _iso(10), "symbol": "BTC", "pnl_usd": -1.5, "hold_duration_minutes": 150},
    ]
    flags = audit.compute_flags(outcomes, _empty_state())
    streaks = [f for f in flags if f.kind == "negative_streak"]
    assert len(streaks) == 1
    assert streaks[0].severity == "MEDIUM"
    assert streaks[0].details.get("streak") == 3


def test_unprotected_position_critical():
    """> 12 'missing TP/SL' warnings on same symbol -> CRITICAL flag."""
    state = audit.BotState(
        n_positions=1, equity=1000.0, margin=100.0, leverage=1.0,
        unprotected_counts={"ETH": 15},
        unprotected_symbols=["ETH"],
    )
    flags = audit.compute_flags([], state)
    crits = [f for f in flags if f.kind == "unprotected_position_loop"]
    assert len(crits) == 1
    assert crits[0].severity == "CRITICAL"
    assert crits[0].symbol == "ETH"
    assert audit.max_severity(flags) == "CRITICAL"


def test_idempotent_issue_creation(tmp_path):
    """If an existing open issue has the same fingerprint, comment instead of create."""
    cfg = _make_cfg(tmp_path)

    # Mock _gh_request: first call (GET issues) returns an issue with a
    # matching [fp:...] tag; second call (POST comment) returns 201.
    flags = [
        audit.Flag(kind="churn", severity="HIGH", symbol="ETH", message="ETH churn")
    ]
    fp = audit._fingerprint(flags)
    existing_issue = {
        "number": 42,
        "body": f"old body\n<!-- [fp:{fp}] -->",
        "html_url": "https://github.com/fracarlesi/tradercripto/issues/42",
    }

    call_log: list[tuple] = []

    def fake_gh(cfg_, method, path, body=None):
        call_log.append((method, path))
        if method == "GET" and "/issues?" in path:
            return 200, [existing_issue]
        if method == "POST" and "/issues/42/comments" in path:
            return 201, {"html_url": "https://github.com/fracarlesi/tradercripto/issues/42#c1"}
        return 0, None

    with patch.object(audit, "_gh_request", side_effect=fake_gh):
        url = audit.create_or_comment_issue(
            cfg, title="t", body="b", labels=["bot-anomaly", "high"], fingerprint=fp
        )

    assert url is not None and "#c1" in url
    # Ensure we did NOT POST a new issue
    posts_to_issues_root = [
        c for c in call_log if c[0] == "POST" and c[1].endswith("/issues")
    ]
    assert posts_to_issues_root == []


def test_cost_tracking_appended(tmp_path):
    """_log_cost appends a JSONL entry to the configured cost log path."""
    cfg = _make_cfg(tmp_path)
    usage = SimpleNamespace(input_tokens=1000, output_tokens=500)
    audit._log_cost(cfg, model="claude-sonnet-4-5", usage=usage)
    audit._log_cost(cfg, model="claude-opus-4-1", usage=usage)

    assert cfg.cost_log_path.exists()
    lines = cfg.cost_log_path.read_text().strip().splitlines()
    assert len(lines) == 2
    entries = [json.loads(ln) for ln in lines]
    assert entries[0]["model"] == "claude-sonnet-4-5"
    assert entries[0]["input_tokens"] == 1000
    assert entries[0]["output_tokens"] == 500
    assert entries[0]["cost_estimate_usd"] > 0
    # Opus should cost strictly more than Sonnet for identical usage
    assert entries[1]["cost_estimate_usd"] > entries[0]["cost_estimate_usd"]


def test_load_recent_outcomes_filters_by_window(tmp_path):
    """Old records outside the window must be dropped, bad JSON skipped."""
    now_iso = _iso(5)
    old_iso = _iso(600)  # 10h ago
    path = tmp_path / f"outcomes_{datetime.now(timezone.utc).strftime('%Y_%m')}.jsonl"
    with open(path, "w") as f:
        f.write(json.dumps({"timestamp": now_iso, "symbol": "A", "pnl_usd": 1.0}) + "\n")
        f.write("this is not json\n")
        f.write(json.dumps({"timestamp": old_iso, "symbol": "B", "pnl_usd": 2.0}) + "\n")

    cfg = _make_cfg(tmp_path)
    recs = audit.load_recent_outcomes(cfg)
    assert len(recs) == 1
    assert recs[0]["symbol"] == "A"
