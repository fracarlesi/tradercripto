"""Flask dashboard for HLQuantBot — read-only view of trade JSONL + sidecars.

Stack: Flask + Jinja2 + HTMX (CDN) + Tailwind (CDN) + uPlot (vendored).
Bind: 127.0.0.1:5611 (no auth, expose via SSH tunnel only).
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request

from crypto_bot.flag_trader.open_sidecar import list_open_sidecars
from crypto_bot.frontend.data import TradeStore

logger = logging.getLogger(__name__)


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
            })
        if sidecar is not None:
            payload["sidecar"] = sidecar
            payload["has_curve"] = True
        elif trade and trade.get("real_high_curve"):
            payload["has_curve"] = True

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
