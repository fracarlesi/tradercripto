"""
Autonomous trade audit agent for HLQuantBot crypto_bot.

Runs as a systemd oneshot timer on the VPS (every 30 minutes). Loads recent
closed trades from the JSONL outcome logs, infers current bot state from
`docker logs hlquantbot_crypto`, applies deterministic anomaly flags, and
(only if any flag fires) asks Anthropic Sonnet for a structured severity
assessment. Critical or unrecognised patterns are escalated to Opus with a
wider log context. Findings become GitHub issues (idempotent: duplicates
within 24h become a comment on the existing issue) and ntfy notifications.

Design goals:
- No blocking I/O loops (single-shot, invoked by systemd).
- Zero runtime cost when nothing happens (no LLM call on clean windows).
- Robust to malformed JSONL lines, missing files, docker CLI failure.
- Never log secrets (API key / GH token), even partial.

This module is intentionally self-contained: stdlib + `anthropic` only.
GitHub REST calls go through `urllib.request` to avoid pulling extra deps
into the crypto_bot container image.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# -----------------------------------------------------------------------------
# Logging — structured JSON to stdout, captured by systemd -> journalctl.
# -----------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _setup_logger() -> logging.Logger:
    log = logging.getLogger("audit_trades")
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


logger = _setup_logger()


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------


@dataclass
class AuditConfig:
    anthropic_api_key: str
    github_repo: str
    github_token: Optional[str]
    ntfy_topic: Optional[str]
    trade_logs_path: Path
    heartbeat: bool
    window_minutes: int
    sonnet_model: str
    opus_model: str
    cost_log_path: Path

    @classmethod
    def from_env(cls) -> "AuditConfig":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            logger.error("ANTHROPIC_API_KEY not set")
            sys.exit(1)
        return cls(
            anthropic_api_key=api_key,
            github_repo=os.environ.get("GITHUB_REPO", "fracarlesi/tradercripto"),
            github_token=os.environ.get("GITHUB_TOKEN") or None,
            ntfy_topic=os.environ.get("NTFY_TOPIC") or None,
            trade_logs_path=Path(
                os.environ.get("HLQUANTBOT_TRADE_LOGS_PATH", "/opt/hlquantbot_trade_logs")
            ),
            heartbeat=os.environ.get("AUDIT_HEARTBEAT", "1") == "1",
            window_minutes=int(os.environ.get("AUDIT_WINDOW_MINUTES", "35")),
            sonnet_model=os.environ.get("AUDIT_SONNET_MODEL", "claude-sonnet-4-5"),
            opus_model=os.environ.get("AUDIT_OPUS_MODEL", "claude-opus-4-1"),
            cost_log_path=Path(
                os.environ.get("AUDIT_COST_LOG", "/var/lib/hlquantbot/audit_cost.jsonl")
            ),
        )


# -----------------------------------------------------------------------------
# Data loading
# -----------------------------------------------------------------------------


def load_recent_outcomes(cfg: AuditConfig) -> list[dict[str, Any]]:
    """Load outcome records from the current-month JSONL file within the window.

    Robust to malformed lines (logs a warning, continues). The current-month
    window boundary case (run at 00:05 on day 1) is handled by also peeking at
    the previous month file if it exists.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=cfg.window_minutes)

    candidates = [
        cfg.trade_logs_path / f"outcomes_{now.strftime('%Y_%m')}.jsonl",
    ]
    # Month rollover safety
    prev = (now.replace(day=1) - timedelta(days=1))
    candidates.append(cfg.trade_logs_path / f"outcomes_{prev.strftime('%Y_%m')}.jsonl")

    records: list[dict[str, Any]] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            with open(path, "r") as f:
                for lineno, raw in enumerate(f, 1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning(f"malformed JSONL line in {path.name}:{lineno}")
                        continue
                    ts_str = rec.get("timestamp")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= cutoff:
                        records.append(rec)
        except OSError as e:
            logger.warning(f"failed to read {path}: {e}")
    return records


# -----------------------------------------------------------------------------
# Bot state from docker logs (Option 2: parse MAIN LOOP heartbeat)
# -----------------------------------------------------------------------------


@dataclass
class BotState:
    n_positions: int = 0
    equity: Optional[float] = None
    margin: Optional[float] = None
    leverage: Optional[float] = None
    unprotected_symbols: list[str] = field(default_factory=list)
    # symbol -> warning occurrence count in last hour
    unprotected_counts: dict[str, int] = field(default_factory=dict)
    raw_tail: str = ""  # last 200 lines, kept for LLM escalation context


_HEARTBEAT_RE = re.compile(
    r"MAIN LOOP heartbeat.*?positions=(\d+)", re.IGNORECASE
)
_EVAL_RE = re.compile(
    r"EVAL START.*?equity=\$?([\d.]+).*?margin=\$?([\d.]+).*?leverage=([\d.]+)x.*?positions=(\d+)",
    re.IGNORECASE,
)
_UNPROTECTED_RE = re.compile(
    r"Position (\w+) missing TP/SL protection", re.IGNORECASE
)


def _run_docker_logs(args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["docker", "logs"] + args + ["hlquantbot_crypto"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # docker logs writes to stderr (json-file driver mixes streams); combine.
        return (out.stdout or "") + (out.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning(f"docker logs failed: {e}")
        return ""


def load_bot_state() -> BotState:
    state = BotState()
    tail = _run_docker_logs(["--tail", "200"])
    state.raw_tail = tail

    # Latest EVAL START wins (most complete), else latest heartbeat
    last_eval: Optional[re.Match[str]] = None
    last_hb: Optional[re.Match[str]] = None
    for line in tail.splitlines():
        m = _EVAL_RE.search(line)
        if m:
            last_eval = m
            continue
        m2 = _HEARTBEAT_RE.search(line)
        if m2:
            last_hb = m2

    if last_eval is not None:
        try:
            state.equity = float(last_eval.group(1))
            state.margin = float(last_eval.group(2))
            state.leverage = float(last_eval.group(3))
            state.n_positions = int(last_eval.group(4))
        except (ValueError, IndexError):
            pass
    elif last_hb is not None:
        try:
            state.n_positions = int(last_hb.group(1))
        except (ValueError, IndexError):
            pass

    # Unprotected warnings over the last hour — count per symbol
    hour_tail = _run_docker_logs(["--since", "1h"])
    counts: dict[str, int] = {}
    for m in _UNPROTECTED_RE.finditer(hour_tail):
        sym = m.group(1)
        counts[sym] = counts.get(sym, 0) + 1
    state.unprotected_counts = counts
    state.unprotected_symbols = sorted(counts.keys())
    return state


# -----------------------------------------------------------------------------
# Deterministic flags
# -----------------------------------------------------------------------------


SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


@dataclass
class Flag:
    kind: str
    severity: str  # LOW | MEDIUM | HIGH | CRITICAL
    symbol: Optional[str]
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def _exit_reason_canonical(rec: dict[str, Any]) -> str:
    """Resolve exit reason for STAGE A audit rules.

    Prefer ``exit_reason_v2`` ({tp, sl, expiry, manual}); fall back to legacy
    ``exit_reason`` for records written before the STAGE A rollout.
    """
    v2 = rec.get("exit_reason_v2")
    if isinstance(v2, str) and v2:
        return v2
    return str(rec.get("exit_reason") or "")


def compute_flags(outcomes: list[dict[str, Any]], state: BotState) -> list[Flag]:
    flags: list[Flag] = []

    # CRITICAL: unprotected position warning loop > 60s (>12 occurrences)
    for sym, count in state.unprotected_counts.items():
        if count > 12:
            flags.append(
                Flag(
                    kind="unprotected_position_loop",
                    severity="CRITICAL",
                    symbol=sym,
                    message=(
                        f"Position {sym} logged 'missing TP/SL protection' "
                        f"{count} times in the last hour"
                    ),
                    details={"occurrences": count},
                )
            )

    # ------------------------------------------------------------------
    # STAGE A aggregate rules (across the whole window)
    # ------------------------------------------------------------------

    # STAGE A: high expiry rate — forecast horizon K_candles is wrong.
    # If > 30% of outcomes time out at expiry, the model's implicit horizon
    # assumption is misaligned with the K_candles config.
    if len(outcomes) >= 5:
        expiries = sum(1 for r in outcomes if _exit_reason_canonical(r) == "expiry")
        if expiries / len(outcomes) > 0.30:
            flags.append(
                Flag(
                    kind="high_expiry_rate",
                    severity="HIGH",
                    symbol=None,
                    message=(
                        f"{expiries}/{len(outcomes)} trades expired "
                        f"({100*expiries/len(outcomes):.0f}%) — forecast horizon "
                        f"K_candles likely wrong"
                    ),
                    details={"expiry_count": expiries, "total": len(outcomes)},
                )
            )

    # STAGE A: loss asymmetry — if most losses hit full SL while wins are
    # partial TPs, the R/R geometry is broken (classic predicted TP too
    # tight vs SL too wide). Threshold: >= 3 losses, all SL-closed, and
    # avg loss_pnl_abs > avg win_pnl_abs * 2.
    losses = [r for r in outcomes if isinstance(r.get("pnl_usd"), (int, float)) and r["pnl_usd"] < 0]
    wins = [r for r in outcomes if isinstance(r.get("pnl_usd"), (int, float)) and r["pnl_usd"] > 0]
    if len(losses) >= 3 and len(wins) >= 1:
        sl_losses = sum(1 for r in losses if _exit_reason_canonical(r) == "sl")
        if sl_losses / len(losses) >= 0.8:
            avg_loss = sum(abs(r["pnl_usd"]) for r in losses) / len(losses)
            avg_win = sum(r["pnl_usd"] for r in wins) / len(wins)
            if avg_loss > avg_win * 2:
                flags.append(
                    Flag(
                        kind="rr_geometry_asymmetric",
                        severity="HIGH",
                        symbol=None,
                        message=(
                            f"R/R broken: avg_loss=${avg_loss:.2f} is "
                            f"{avg_loss/avg_win:.1f}x avg_win=${avg_win:.2f} "
                            f"({sl_losses}/{len(losses)} losses are SL-hit)"
                        ),
                        details={
                            "avg_loss": avg_loss,
                            "avg_win": avg_win,
                            "sl_loss_ratio": sl_losses / len(losses),
                        },
                    )
                )

    # Per-outcome scans
    per_symbol: dict[str, list[dict[str, Any]]] = {}
    for rec in outcomes:
        sym = str(rec.get("symbol") or "?")
        per_symbol.setdefault(sym, []).append(rec)

        hold = rec.get("hold_duration_minutes")
        exit_reason = _exit_reason_canonical(rec)
        pnl_usd = rec.get("pnl_usd")
        pnl_pct = rec.get("pnl_pct")
        confidence = rec.get("confidence")
        predicted_tp = rec.get("predicted_tp_pct") or rec.get("tp_pct")
        predicted_sl = rec.get("predicted_sl_pct") or rec.get("sl_pct")

        # STAGE A churn: any exit in STAGE A is planned (tp/sl/expiry/manual
        # via trigger orders on the exchange). A <2min hold with exit_reason_v2
        # in {tp, sl} is a planned hit — not churn. Only manual/unknown
        # ultra-fast closes are flagged now.
        if (
            isinstance(hold, (int, float))
            and hold < 2.0
            and exit_reason not in ("tp", "sl", "expiry", "manual",
                                    # legacy planned exits for backward compat
                                    "take_profit", "stop_loss", "roi_target",
                                    "max_hold_time", "external_close")
        ):
            flags.append(
                Flag(
                    kind="churn",
                    severity="HIGH",
                    symbol=sym,
                    message=f"{sym} closed after {hold:.2f}min with unknown exit '{exit_reason}'",
                    details={"hold_minutes": hold, "exit_reason": exit_reason,
                             "pnl_usd": pnl_usd},
                )
            )

        # STAGE A: zero-confidence trade — model fell back to rule-based or
        # feature pipeline degraded. Distinct from a small confidence drop.
        if isinstance(confidence, (int, float)) and confidence == 0.0:
            flags.append(
                Flag(
                    kind="zero_confidence",
                    severity="MEDIUM",
                    symbol=sym,
                    message=f"{sym} trade taken with confidence=0.0 (model fallback / feature degraded)",
                    details={"confidence": confidence, "exit_reason": exit_reason,
                             "pnl_usd": pnl_usd},
                )
            )

        # STAGE A: per-trade R/R asymmetry — the model predicted TP/SL with
        # R/R worse than 1:1.5 (e.g. tp=0.5% sl=2.0% → ratio 0.25). Even if
        # the single trade wins, the ratio itself is a policy signal.
        if (
            isinstance(predicted_tp, (int, float))
            and isinstance(predicted_sl, (int, float))
            and predicted_tp > 0
            and predicted_sl > 0
        ):
            rr = predicted_tp / predicted_sl
            if rr < 1.0 / 1.5:  # < 1:1.5
                flags.append(
                    Flag(
                        kind="rr_prediction_asymmetric",
                        severity="MEDIUM",
                        symbol=sym,
                        message=(
                            f"{sym} model predicted tp={predicted_tp:.2f}% "
                            f"sl={predicted_sl:.2f}% (R/R={rr:.2f}, < 1:1.5)"
                        ),
                        details={"predicted_tp_pct": predicted_tp,
                                 "predicted_sl_pct": predicted_sl, "rr": rr},
                    )
                )

        # MEDIUM: outlier loss
        if isinstance(pnl_pct, (int, float)) and pnl_pct < -3.0:
            flags.append(
                Flag(
                    kind="outlier_loss",
                    severity="MEDIUM",
                    symbol=sym,
                    message=f"{sym} closed at {pnl_pct:.2f}% (outlier)",
                    details={"pnl_pct": pnl_pct, "pnl_usd": pnl_usd},
                )
            )

        # LOW: fee-only loss
        if (
            isinstance(pnl_usd, (int, float))
            and pnl_usd < 0
            and abs(pnl_usd) < 0.05
        ):
            flags.append(
                Flag(
                    kind="fee_only_loss",
                    severity="LOW",
                    symbol=sym,
                    message=f"{sym} closed at ${pnl_usd:.4f} (fee-dominated)",
                    details={"pnl_usd": pnl_usd},
                )
            )

    # Aggregates per symbol
    for sym, recs in per_symbol.items():
        # sort chronologically for streak
        def _ts(r: dict[str, Any]) -> str:
            return str(r.get("timestamp") or "")

        recs_sorted = sorted(recs, key=_ts)

        # MEDIUM: 3+ consecutive losses
        streak = 0
        for r in recs_sorted:
            pnl = r.get("pnl_usd")
            if isinstance(pnl, (int, float)) and pnl < 0:
                streak += 1
            else:
                streak = 0
            if streak >= 3:
                flags.append(
                    Flag(
                        kind="negative_streak",
                        severity="MEDIUM",
                        symbol=sym,
                        message=f"{sym} has {streak} consecutive losing trades",
                        details={"streak": streak},
                    )
                )
                break

        # MEDIUM: reversal storm (4+ outcomes on same symbol)
        if len(recs_sorted) >= 4:
            flags.append(
                Flag(
                    kind="reversal_storm",
                    severity="MEDIUM",
                    symbol=sym,
                    message=f"{sym} closed {len(recs_sorted)} trades in window",
                    details={"count": len(recs_sorted)},
                )
            )

    return flags


def max_severity(flags: list[Flag]) -> str:
    if not flags:
        return "LOW"
    return max(flags, key=lambda f: SEVERITY_RANK[f.severity]).severity


# -----------------------------------------------------------------------------
# Anthropic client
# -----------------------------------------------------------------------------


def _anonymize_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Strip fields that could blow up the prompt or leak noise."""
    keep = (
        "timestamp", "symbol", "action", "confidence", "entry_price",
        "exit_price", "pnl_usd", "pnl_pct", "exit_reason",
        "hold_duration_minutes", "market_state_summary",
    )
    return {k: rec.get(k) for k in keep if k in rec}


def call_sonnet(
    cfg: AuditConfig,
    flags: list[Flag],
    outcomes: list[dict[str, Any]],
    state: BotState,
) -> dict[str, Any]:
    """Call Sonnet and return parsed JSON verdict.

    On any failure, return a fallback verdict using the deterministic severity.
    """
    try:
        import anthropic  # type: ignore
    except ImportError:
        logger.error("anthropic SDK not installed")
        return _fallback_verdict(flags, reason="anthropic SDK missing")

    n_wins = sum(
        1 for o in outcomes if isinstance(o.get("pnl_usd"), (int, float)) and o["pnl_usd"] > 0
    )
    n_losses = sum(
        1 for o in outcomes if isinstance(o.get("pnl_usd"), (int, float)) and o["pnl_usd"] < 0
    )
    total_pnl = sum(
        o.get("pnl_usd", 0) or 0 for o in outcomes if isinstance(o.get("pnl_usd"), (int, float))
    )

    flags_text = "\n".join(
        f"- [{f.severity}] {f.kind}: {f.message}" for f in flags
    ) or "  (none)"

    records_json = json.dumps(
        [_anonymize_record(r) for r in outcomes[-20:]], default=str, indent=2
    )

    prompt = f"""Sei un quality assurance agent per HLQuantBot trading system. Analizza questo report di anomalie.

Window: ultimi {cfg.window_minutes} minuti UTC
Stato bot: {state.n_positions} posizioni, equity ${state.equity}, margin ${state.margin}, leverage {state.leverage}x
Trade chiusi nella finestra: {len(outcomes)} (wins={n_wins} losses={n_losses} total_pnl=${total_pnl:.2f})

Anomalie deterministiche rilevate:
{flags_text}

Trade records (anonimizzati):
{records_json}

Rispondi SOLO in JSON valido con questo schema:
{{
  "severity": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
  "summary": "<una frase, max 160 char>",
  "root_cause_hypotheses": ["<ipotesi 1>", "<ipotesi 2>"],
  "suggested_fix": "<fix concreto e azionabile, 1-3 frasi: cosa modificare, in quale file/config>",
  "escalate_to_opus": true | false
}}

escalate_to_opus = true SOLO se: severity=CRITICAL, oppure pattern non riconosciuto e serve deep dive.

Context STAGE A (predict-and-place, mode_enabled=true):
- Il bot usa TP/SL predetti dal modello Qwen FLAG-Trader (campi `predicted_tp_pct`, `predicted_sl_pct` nelle decisions_*.jsonl)
- exit_reason_v2 ∈ {{tp, sl, expiry, manual}}. Vecchi valori (trailing_stop, model_reversal, min_hold_*) NON esistono più nel codepath live — se li vedi sono record legacy, non flaggare come bug.
- min_hold_minutes è stato RIMOSSO, non segnalare violazioni min-hold.
- Expiry = K_candles × 15min = 8.5h. Un alto tasso di exit_reason_v2=expiry indica forecast orizzonte sbagliato, flaggalo.
- R/R asimmetrico (predicted_tp_pct < predicted_sl_pct / 1.5) è un problema REALE da segnalare.
"""

    try:
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        resp = client.messages.create(
            model=cfg.sonnet_model,
            max_tokens=800,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text += block.text
        # Track cost approx
        _log_cost(cfg, model=cfg.sonnet_model, usage=getattr(resp, "usage", None))
        return _parse_llm_json(text, flags, model=cfg.sonnet_model)
    except Exception as e:  # noqa: BLE001
        logger.error(f"sonnet call failed: {type(e).__name__}")
        return _fallback_verdict(flags, reason=f"sonnet error: {type(e).__name__}")


def call_opus(
    cfg: AuditConfig,
    verdict: dict[str, Any],
    flags: list[Flag],
    outcomes: list[dict[str, Any]],
    state: BotState,
) -> str:
    """Deep-dive call to Opus. Returns a markdown analysis body."""
    try:
        import anthropic  # type: ignore
    except ImportError:
        return "_(opus escalation skipped: anthropic SDK missing)_"

    extra_logs = _run_docker_logs(["--since", "1h"])
    # Keep last ~200 lines to stay within budget
    tail_lines = extra_logs.splitlines()[-200:]
    log_ctx = "\n".join(tail_lines)

    flags_text = "\n".join(
        f"- [{f.severity}] {f.kind} ({f.symbol}): {f.message} {f.details}"
        for f in flags
    )

    prompt = f"""Sei un senior trading systems engineer. Analizza in profondità questa anomalia.
Investiga root cause, proponi fix concreto, identifica regression risk.

Sonnet verdict: {json.dumps(verdict)}

Deterministic flags:
{flags_text}

Bot state: {state.n_positions} positions, equity ${state.equity}, margin ${state.margin}, leverage {state.leverage}x

Recent closed trades (last {len(outcomes)}):
{json.dumps([_anonymize_record(r) for r in outcomes[-30:]], default=str, indent=2)}

Container logs (last 200 lines):
```
{log_ctx[-12000:]}
```

Respond in markdown with sections: ## Root cause / ## Suggested fix / ## Regression risk / ## Verification steps.
"""

    try:
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        resp = client.messages.create(
            model=cfg.opus_model,
            max_tokens=2000,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text += block.text
        _log_cost(cfg, model=cfg.opus_model, usage=getattr(resp, "usage", None))
        return text or "_(empty opus response)_"
    except Exception as e:  # noqa: BLE001
        logger.error(f"opus call failed: {type(e).__name__}")
        return f"_(opus escalation failed: {type(e).__name__})_"


def _parse_llm_json(text: str, flags: list[Flag], model: str) -> dict[str, Any]:
    """Extract the first JSON object from the LLM text output."""
    text = text.strip()
    # Try direct parse first
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            v.setdefault("_model", model)
            return v
    except json.JSONDecodeError:
        pass
    # Fallback: find {...} block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            v = json.loads(m.group(0))
            if isinstance(v, dict):
                v.setdefault("_model", model)
                return v
        except json.JSONDecodeError:
            pass
    logger.warning("LLM returned non-JSON output, using fallback verdict")
    return _fallback_verdict(flags, reason="LLM output not JSON")


def _fallback_verdict(flags: list[Flag], reason: str) -> dict[str, Any]:
    return {
        "severity": max_severity(flags),
        "summary": f"Deterministic verdict (LLM unavailable: {reason})",
        "root_cause_hypotheses": [f.message for f in flags[:5]],
        "escalate_to_opus": False,
        "_model": "fallback",
    }


# -----------------------------------------------------------------------------
# Cost tracking
# -----------------------------------------------------------------------------

# Very rough public-pricing anchors (USD per 1M tokens). Kept as constants so
# the audit script doesn't need the Anthropic billing API.
_PRICING_PER_MTOK = {
    "sonnet": (3.0, 15.0),   # (input, output)
    "opus":   (15.0, 75.0),
}


def _log_cost(cfg: AuditConfig, model: str, usage: Any) -> None:
    try:
        cfg.cost_log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    family = "opus" if "opus" in model.lower() else "sonnet"
    in_rate, out_rate = _PRICING_PER_MTOK[family]
    in_tok = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
    out_tok = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
    cost = (in_tok / 1_000_000) * in_rate + (out_tok / 1_000_000) * out_rate
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_estimate_usd": round(cost, 6),
    }
    logger.info(f"cost_estimate_usd={cost:.6f} model={model}")
    try:
        with open(cfg.cost_log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        logger.warning(f"failed to write cost log: {e}")


# -----------------------------------------------------------------------------
# GitHub integration (urllib, idempotent)
# -----------------------------------------------------------------------------


def _gh_request(
    cfg: AuditConfig, method: str, path: str, body: Optional[dict[str, Any]] = None
) -> tuple[int, Any]:
    if not cfg.github_token:
        return 0, None
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {cfg.github_token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "hlquantbot-audit/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        logger.warning(f"github {method} {path} -> {e.code}")
        return e.code, None
    except (urllib.error.URLError, TimeoutError) as e:
        logger.warning(f"github {method} {path} failed: {e}")
        return 0, None


def _fingerprint(flags: list[Flag]) -> str:
    top = sorted({(f.kind, f.symbol or "-") for f in flags})
    return "|".join(f"{k}:{s}" for k, s in top)


def find_existing_issue(cfg: AuditConfig, fingerprint: str) -> Optional[int]:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    q = urllib.parse.urlencode(
        {"labels": "bot-anomaly", "state": "open", "since": since, "per_page": "50"}
    )
    status, data = _gh_request(cfg, "GET", f"/repos/{cfg.github_repo}/issues?{q}")
    if status != 200 or not isinstance(data, list):
        return None
    tag = f"[fp:{fingerprint}]"
    for issue in data:
        body = issue.get("body") or ""
        if tag in body:
            return int(issue.get("number") or 0) or None
    return None


def create_or_comment_issue(
    cfg: AuditConfig,
    title: str,
    body: str,
    labels: list[str],
    fingerprint: str,
) -> Optional[str]:
    """Return html_url of the (new or existing) issue, or None on failure."""
    if not cfg.github_token:
        logger.info("GITHUB_TOKEN not set — skipping issue creation")
        return None
    body_full = body + f"\n\n<!-- [fp:{fingerprint}] -->\n"
    existing = find_existing_issue(cfg, fingerprint)
    if existing:
        comment = {"body": f"Recurring anomaly detected.\n\n{body_full}"}
        status, data = _gh_request(
            cfg, "POST", f"/repos/{cfg.github_repo}/issues/{existing}/comments", comment
        )
        if status in (200, 201) and isinstance(data, dict):
            logger.info(f"commented on existing issue #{existing}")
            return str(data.get("html_url") or f"#{existing}")
        return None
    payload = {"title": title, "body": body_full, "labels": labels}
    status, data = _gh_request(cfg, "POST", f"/repos/{cfg.github_repo}/issues", payload)
    if status in (200, 201) and isinstance(data, dict):
        url = str(data.get("html_url") or "")
        logger.info(f"created new issue: {url}")
        return url
    return None


# -----------------------------------------------------------------------------
# ntfy notifications
# -----------------------------------------------------------------------------


def ntfy(cfg: AuditConfig, message: str, priority: str = "default", title: str = "HLQuantBot audit") -> None:
    if not cfg.ntfy_topic:
        return
    url = f"https://ntfy.sh/{cfg.ntfy_topic}"
    req = urllib.request.Request(url, data=message.encode(), method="POST")
    req.add_header("Title", title)
    req.add_header("Priority", priority)
    req.add_header("Tags", "robot")
    try:
        with urllib.request.urlopen(req, timeout=10) as _:  # noqa: S310
            pass
    except (urllib.error.URLError, TimeoutError) as e:
        logger.warning(f"ntfy failed: {e}")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def _format_issue_body(
    verdict: dict[str, Any],
    flags: list[Flag],
    outcomes: list[dict[str, Any]],
    state: BotState,
    deep_analysis: Optional[str],
) -> str:
    lines = [
        "## Summary",
        f"**Severity:** {verdict.get('severity')}",
        f"**Model:** {verdict.get('_model')}",
        "",
        verdict.get("summary") or "",
        "",
        "## Bot state",
        f"- positions: {state.n_positions}",
        f"- equity: ${state.equity}",
        f"- margin: ${state.margin}",
        f"- leverage: {state.leverage}x",
        "",
        "## Flags",
    ]
    for f in flags:
        lines.append(f"- **[{f.severity}] {f.kind}** ({f.symbol}): {f.message}")
    lines += ["", "## Root cause hypotheses"]
    for h in verdict.get("root_cause_hypotheses") or []:
        lines.append(f"- {h}")
    suggested_fix = verdict.get("suggested_fix")
    if suggested_fix:
        lines += ["", "## Suggested fix", str(suggested_fix)]
    lines += [
        "",
        "## Recent outcomes",
        "```json",
        json.dumps([_anonymize_record(r) for r in outcomes[-10:]], default=str, indent=2),
        "```",
    ]
    if deep_analysis:
        lines += ["", "## Deep analysis (Opus)", deep_analysis]
    return "\n".join(lines)


def _labels_for(severity: str) -> list[str]:
    base = ["bot-anomaly"]
    if severity == "CRITICAL":
        return base + ["critical", "needs-deep-review"]
    if severity == "HIGH":
        return base + ["high-priority", "high"]
    if severity == "MEDIUM":
        return base + ["medium"]
    return base + ["low"]


def _persist_verdict(
    cfg: "AuditConfig",
    verdict: dict,
    flags: list,
    outcomes: list,
    state: "BotState",
    deep_analysis: Optional[str],
    title: str,
    body: str,
    fingerprint: str,
    severity: str,
) -> Optional[Path]:
    """Durable local snapshot of every flagged verdict.

    Written BEFORE any GitHub/ntfy I/O so that a 403 or network error never
    loses the Sonnet diagnosis. File name includes timestamp + severity so
    the latest findings are easy to locate:
    ``<trade_logs_path>/audit_verdicts/2026-04-08T09-17-33Z_MEDIUM_churn-XPL.json``
    """
    try:
        out_dir = cfg.trade_logs_path / "audit_verdicts"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        safe_fp = "".join(c if c.isalnum() or c in "-_" else "-" for c in fingerprint)[:40]
        name = f"{ts}_{severity}_{safe_fp or 'unknown'}.json"
        path = out_dir / name
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": severity,
            "fingerprint": fingerprint,
            "title": title,
            "verdict": verdict,
            "deep_analysis": deep_analysis,
            "flags": [
                {"code": getattr(f, "code", None), "symbol": getattr(f, "symbol", None),
                 "detail": getattr(f, "detail", None), "severity": getattr(f, "severity", None)}
                for f in flags
            ],
            "state": {
                "n_positions": getattr(state, "n_positions", None),
                "equity": getattr(state, "equity", None),
            },
            "n_outcomes_window": len(outcomes),
            "issue_body_preview": body[:2000],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
        logger.info(f"verdict persisted: {path}")
        return path
    except Exception as e:  # noqa: BLE001
        logger.warning(f"failed to persist verdict locally: {type(e).__name__}: {e}")
        return None


def run_audit() -> int:
    cfg = AuditConfig.from_env()
    try:
        outcomes = load_recent_outcomes(cfg)
        state = load_bot_state()
        flags = compute_flags(outcomes, state)

        # Fast path: nothing to report
        if not flags:
            status = {
                "status": "ok",
                "n_outcomes": len(outcomes),
                "n_positions": state.n_positions,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            print(json.dumps(status))
            if cfg.heartbeat:
                ntfy(cfg, f"audit ok - {len(outcomes)} trades, {state.n_positions} pos", "min")
            return 0

        # LLM verdict
        verdict = call_sonnet(cfg, flags, outcomes, state)
        severity = str(verdict.get("severity") or max_severity(flags)).upper()
        if severity not in SEVERITY_RANK:
            severity = max_severity(flags)
        verdict["severity"] = severity

        # Escalation
        deep_analysis: Optional[str] = None
        if severity == "CRITICAL" or verdict.get("escalate_to_opus") is True:
            deep_analysis = call_opus(cfg, verdict, flags, outcomes, state)

        # Build issue
        symbols = sorted({f.symbol for f in flags if f.symbol})
        title = f"[{severity}] audit: {', '.join(symbols) or 'multi'} - {verdict.get('summary', '')[:80]}"
        body = _format_issue_body(verdict, flags, outcomes, state, deep_analysis)
        fp = _fingerprint(flags)

        # Persist verdict to disk BEFORE GitHub — if the API call fails (403
        # token, network, etc.) the content is still durable and the ntfy
        # notification can link the local path.
        verdict_path = _persist_verdict(
            cfg, verdict, flags, outcomes, state, deep_analysis, title, body, fp, severity
        )

        issue_url: Optional[str] = None
        if severity != "LOW":
            issue_url = create_or_comment_issue(
                cfg, title=title, body=body, labels=_labels_for(severity), fingerprint=fp
            )

        # ntfy — promote MEDIUM to "high" priority so the notification is
        # actually visible on the phone (default priority is silent on most
        # ntfy clients).
        prio_map = {"LOW": "low", "MEDIUM": "high", "HIGH": "high", "CRITICAL": "urgent"}
        msg = f"[{severity}] {verdict.get('summary', '')}"
        if issue_url:
            msg += f"\n{issue_url}"
        elif verdict_path is not None:
            msg += f"\n(local: {verdict_path})"
        ntfy(cfg, msg, priority=prio_map.get(severity, "default"))

        print(
            json.dumps(
                {
                    "status": "flagged",
                    "severity": severity,
                    "n_flags": len(flags),
                    "n_outcomes": len(outcomes),
                    "issue_url": issue_url,
                    "fingerprint": fp,
                    "model": verdict.get("_model"),
                }
            )
        )
        return 0
    except Exception as e:  # noqa: BLE001
        logger.error(f"audit failed: {type(e).__name__}: {e}")
        try:
            ntfy(cfg, f"audit FAILED: {type(e).__name__}: {e}", priority="high")
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(run_audit())
