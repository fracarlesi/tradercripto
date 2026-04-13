"""Flask dashboard for HLQuantBot — read-only view of trade JSONL + sidecars.

Stack: Flask + Jinja2 + HTMX (CDN) + Tailwind (CDN) + uPlot (vendored).
Bind: 127.0.0.1:5611 (no auth, expose via SSH tunnel only).
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request

from crypto_bot.flag_trader.open_sidecar import list_open_sidecars
from crypto_bot.frontend.data import TradeStore

logger = logging.getLogger(__name__)

_HL_INTERVAL_MAP: dict[int, str] = {
    60: "1m", 300: "5m", 900: "15m", 3600: "1h", 14400: "4h", 86400: "1d"
}
_HL_API_URL = "https://api.hyperliquid.xyz/info"
_CONTEXT_PRE_BARS = 20
_CONTEXT_POST_BARS = 10


def _attach_context_candles(payload: dict[str, Any], trade: dict[str, Any]) -> None:
    """Fetch pre/post context candles from Hyperliquid and add to payload.

    Modifies payload in-place. Silently skips on any error (best-effort).
    """
    symbol: str | None = trade.get("symbol")
    timestamp_str: str | None = trade.get("timestamp")
    interval_sec: int = int(trade.get("candle_interval_sec") or 900)
    hold_min: float | None = trade.get("hold_duration_minutes")

    if not symbol or not timestamp_str:
        return

    interval_str = _HL_INTERVAL_MAP.get(interval_sec, "15m")
    interval_ms = interval_sec * 1000

    try:
        entry_ms = int(
            datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp() * 1000
        )
    except (ValueError, OSError):
        return

    def _fetch(start_ms: int, end_ms: int) -> list[dict] | None:
        import json as _json
        import urllib.request
        body = _json.dumps({"type": "candleSnapshot", "req": {
            "coin": symbol, "interval": interval_str,
            "startTime": start_ms, "endTime": end_ms,
        }}).encode()
        try:
            req = urllib.request.Request(
                _HL_API_URL, data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                candles = _json.loads(resp.read())
                return [
                    {"t": c["t"], "o": float(c["o"]), "h": float(c["h"]),
                     "l": float(c["l"]), "c": float(c["c"])}
                    for c in candles
                ]
        except Exception:
            pass
        return None

    # Pre-entry candles
    pre = _fetch(entry_ms - _CONTEXT_PRE_BARS * interval_ms, entry_ms)
    if pre:
        payload["pre_candles"] = pre

    # Post-exit candles (only for closed trades)
    if hold_min is not None:
        exit_ms = entry_ms + int(hold_min * 60 * 1000)
        post = _fetch(exit_ms, exit_ms + _CONTEXT_POST_BARS * interval_ms)
        if post:
            payload["post_candles"] = post


def create_app(store: TradeStore | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["TRADE_STORE"] = store or TradeStore()

    def _store() -> TradeStore:
        return app.config["TRADE_STORE"]

    # ---------------------------------------------------------------- pages
    @app.route("/")
    def dashboard() -> str:
        return render_template("dashboard.html", summary=_store().get_summary())

    @app.route("/trades")
    def trades_list() -> str:
        page = max(1, int(request.args.get("page", 1)))
        limit = min(200, int(request.args.get("limit", 50)))
        symbol = request.args.get("symbol") or None
        offset = (page - 1) * limit
        rows, total = _store().list_trades(limit=limit, offset=offset, symbol=symbol)
        return render_template(
            "trades_list.html",
            rows=rows,
            total=total,
            page=page,
            limit=limit,
            symbol=symbol or "",
            has_next=offset + limit < total,
            has_prev=page > 1,
        )

    @app.route("/trades/<trade_id>")
    def trade_detail(trade_id: str) -> str:
        trade = _store().get_trade(trade_id)
        sidecar = _store().get_sidecar(trade_id)
        return render_template(
            "trade_detail.html",
            trade=trade,
            trade_id=trade_id,
            has_curve=bool(sidecar) or bool(trade and trade.get("real_high_curve")),
        )

    # ---------------------------------------------------------------- partials
    @app.route("/partials/summary")
    def partial_summary() -> str:
        return render_template(
            "partials/summary_cards.html",
            summary=_store().get_summary(),
        )

    @app.route("/partials/trades")
    def partial_trades() -> str:
        page = max(1, int(request.args.get("page", 1)))
        limit = min(200, int(request.args.get("limit", 50)))
        symbol = request.args.get("symbol") or None
        offset = (page - 1) * limit
        rows, total = _store().list_trades(limit=limit, offset=offset, symbol=symbol)
        return render_template(
            "partials/trades_table.html",
            rows=rows,
            total=total,
            page=page,
            limit=limit,
            symbol=symbol or "",
            has_next=offset + limit < total,
            has_prev=page > 1,
        )

    # ---------------------------------------------------------------- json api
    @app.route("/api/summary")
    def api_summary() -> Response:
        return jsonify(_store().get_summary())

    @app.route("/api/trades")
    def api_trades() -> Response:
        page = max(1, int(request.args.get("page", 1)))
        limit = min(200, int(request.args.get("limit", 50)))
        symbol = request.args.get("symbol") or None
        offset = (page - 1) * limit
        rows, total = _store().list_trades(limit=limit, offset=offset, symbol=symbol)
        return jsonify({"rows": rows, "total": total, "page": page, "limit": limit})

    @app.route("/api/open_trades")
    def api_open_trades() -> Response:
        """Return all in-flight trades with predicted TP/SL sidecars.

        Each row also carries a derived ``risk_reward`` (TP distance / SL
        distance) and ``age_sec`` (seconds since ``opened_at``) for the UI.
        """
        from datetime import datetime, timezone

        rows: list[dict[str, Any]] = []
        for sc in list_open_sidecars():
            entry = sc.get("entry_price")
            tp = sc.get("predicted_tp_price")
            sl = sc.get("predicted_sl_price")
            rr: float | None = None
            try:
                if entry is not None and tp is not None and sl is not None:
                    tp_d = abs(float(tp) - float(entry))
                    sl_d = abs(float(entry) - float(sl))
                    if sl_d > 0:
                        rr = round(tp_d / sl_d, 2)
            except (TypeError, ValueError):
                rr = None
            age: float | None = None
            try:
                opened = sc.get("opened_at")
                if isinstance(opened, str) and opened:
                    ts = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
            except ValueError:
                age = None
            row = dict(sc)
            row["risk_reward"] = rr
            row["age_sec"] = age
            rows.append(row)
        rows.sort(key=lambda r: r.get("opened_at") or "", reverse=True)
        return jsonify({"rows": rows, "count": len(rows)})

    @app.route("/partials/open_trades")
    def partial_open_trades() -> str:
        resp = api_open_trades()
        data = json.loads(resp.get_data(as_text=True))
        return render_template("partials/open_trades_table.html", rows=data["rows"])

    @app.route("/api/trades/<trade_id>/curves")
    def api_trade_curves(trade_id: str) -> Response:
        trade = _store().get_trade(trade_id)
        sidecar = _store().get_sidecar(trade_id)
        payload: dict[str, Any] = {
            "trade_id": trade_id,
            "found": trade is not None,
            "has_curve": False,
        }
        if trade is not None:
            payload.update({
                "symbol": trade.get("symbol"),
                "action": trade.get("action"),
                "entry_price": trade.get("entry_price"),
                "exit_price": trade.get("exit_price"),
                "predicted_tp_price": trade.get("predicted_tp_price"),
                "predicted_sl_price": trade.get("predicted_sl_price"),
                "predicted_tp_pct": trade.get("predicted_tp_pct"),
                "predicted_sl_pct": trade.get("predicted_sl_pct"),
                "exit_reason": trade.get("exit_reason_v2") or trade.get("exit_reason"),
                "k_candles": trade.get("k_candles"),
                "candle_interval_sec": trade.get("candle_interval_sec"),
                "real_high_curve": trade.get("real_high_curve"),
                "real_low_curve": trade.get("real_low_curve"),
                "real_open_curve": trade.get("real_open_curve"),
                "real_close_curve": trade.get("real_close_curve"),
                "real_observed_k": trade.get("real_observed_k"),
                "timestamp": trade.get("timestamp"),
                "hold_duration_minutes": trade.get("hold_duration_minutes"),
                "pnl_usd": trade.get("pnl_usd"),
            })
        if sidecar is not None:
            payload["sidecar"] = sidecar
            payload["has_curve"] = True
        elif trade and trade.get("real_high_curve"):
            payload["has_curve"] = True

        # Best-effort: fetch pre/post context candles from Hyperliquid public API.
        if trade is not None:
            _attach_context_candles(payload, trade)

        body = json.dumps(payload, default=str).encode("utf-8")
        accept_enc = request.headers.get("Accept-Encoding", "")
        if "gzip" in accept_enc:
            body = gzip.compress(body)
            return Response(body, mimetype="application/json", headers={"Content-Encoding": "gzip"})
        return Response(body, mimetype="application/json")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    # Bind 0.0.0.0 inside the container; host-side access is restricted to
    # localhost by docker-compose publish `127.0.0.1:5611:5611`, so the
    # exposure stays private (SSH-tunnel only).
    app.run(host="0.0.0.0", port=5611, debug=False)


if __name__ == "__main__":
    main()
