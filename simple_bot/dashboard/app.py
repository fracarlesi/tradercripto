"""
HLQuantBot v2.0 Dashboard - Flask Application
==============================================

Main Flask application with routes for the HLQuantBot v2.0 dashboard.

Run:
    python app.py

Or:
    from simple_bot.dashboard import run_dashboard
    run_dashboard()
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, render_template, request

# Add project root to path for database import
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from database.db import Database

# =============================================================================
# Flask App Configuration
# =============================================================================

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static"
)

app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "hlquantbot-v2-secret")
app.config["JSON_SORT_KEYS"] = False

# =============================================================================
# Async Helpers - Thread-safe implementation
# =============================================================================

import threading
import atexit

# Dedicated background event loop for all async operations
_bg_loop: Optional[asyncio.AbstractEventLoop] = None
_bg_thread: Optional[threading.Thread] = None
_db_instance: Optional[Database] = None
_db_async_lock: Optional[asyncio.Lock] = None  # Created lazily in event loop


def _start_background_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Run event loop in background thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def get_background_loop() -> asyncio.AbstractEventLoop:
    """Get or create the background event loop."""
    global _bg_loop, _bg_thread

    if _bg_loop is None or _bg_loop.is_closed():
        _bg_loop = asyncio.new_event_loop()
        _bg_thread = threading.Thread(target=_start_background_loop, args=(_bg_loop,), daemon=True)
        _bg_thread.start()

    return _bg_loop


def run_async(coro):
    """Run async coroutine in the background event loop (thread-safe)."""
    loop = get_background_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=30)  # 30 second timeout


def safe_run_async(coro, default=None):
    """Run async coroutine with error handling."""
    try:
        return run_async(coro)
    except Exception as e:
        import traceback
        print(f"[Dashboard] Error running async: {type(e).__name__}: {e}")
        traceback.print_exc()
        return default


async def get_db() -> Database:
    """Get or create database connection (singleton in background loop)."""
    global _db_instance, _db_async_lock

    # Create async lock lazily in the event loop context
    if _db_async_lock is None:
        _db_async_lock = asyncio.Lock()

    async with _db_async_lock:
        if _db_instance is None:
            _db_instance = Database()
            await _db_instance.connect(min_size=1, max_size=5)
        elif _db_instance.pool is None:
            await _db_instance.connect(min_size=1, max_size=5)

    return _db_instance


def cleanup_db():
    """Cleanup database connection on shutdown."""
    global _db_instance, _bg_loop
    if _db_instance and _bg_loop and not _bg_loop.is_closed():
        try:
            future = asyncio.run_coroutine_threadsafe(_db_instance.disconnect(), _bg_loop)
            future.result(timeout=5)
        except Exception:
            pass


atexit.register(cleanup_db)


# =============================================================================
# Template Filters
# =============================================================================

@app.template_filter("to_time")
def to_time_filter(value):
    """Format datetime to time string."""
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except:
            return value
    return value.strftime("%H:%M:%S")


@app.template_filter("to_datetime")
def to_datetime_filter(value):
    """Format datetime to date+time string."""
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except:
            return value
    return value.strftime("%Y-%m-%d %H:%M:%S")


@app.template_filter("ago")
def ago_filter(value):
    """Format datetime as relative time ago."""
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except:
            return value

    now = datetime.now(timezone.utc)
    if value.tzinfo:
        now = datetime.now(value.tzinfo)

    diff = now - value
    seconds = diff.total_seconds()

    if seconds < 60:
        return f"{int(seconds)}s ago"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    elif seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    else:
        return f"{int(seconds // 86400)}d ago"


@app.template_filter("format_pnl")
def format_pnl_filter(value):
    """Format PnL with sign and color class."""
    if value is None:
        return "-"
    try:
        value = float(value)
        sign = "+" if value >= 0 else ""
        return f"{sign}${value:.2f}"
    except:
        return str(value)


@app.template_filter("format_percent")
def format_percent_filter(value):
    """Format percentage with sign."""
    if value is None:
        return "-"
    try:
        value = float(value)
        sign = "+" if value >= 0 else ""
        return f"{sign}{value:.2f}%"
    except:
        return str(value)


# =============================================================================
# Context Processors
# =============================================================================

@app.context_processor
def inject_globals():
    """Inject global variables into templates."""
    return {
        "now": datetime.now(timezone.utc),
        "environment": os.getenv("HLQUANTBOT_ENV", "MAINNET"),
        "version": "2.0.0",
    }


# =============================================================================
# Main Page Routes
# =============================================================================

@app.route("/")
def index():
    """Overview dashboard."""
    return render_template("index.html", page="overview")


@app.route("/services")
def services():
    """Services status page."""
    return render_template("services.html", page="services")


@app.route("/opportunities")
def opportunities():
    """Opportunities ranking page."""
    return render_template("opportunities.html", page="opportunities")


@app.route("/signals")
def signals():
    """Strategy signals page."""
    return render_template("signals.html", page="signals")


@app.route("/positions")
def positions():
    """Active positions page."""
    return render_template("positions.html", page="positions")


@app.route("/performance")
def performance():
    """Performance analytics page."""
    return render_template("performance.html", page="performance")


@app.route("/learning")
def learning():
    """Learning/optimization page."""
    return render_template("learning.html", page="learning")


# =============================================================================
# Conservative Refactor Routes
# =============================================================================

@app.route("/market-state")
def market_state():
    """Market state page - BTC/ETH with indicators and regime."""
    return render_template("market_state.html", page="market_state")


@app.route("/risk-monitor")
def risk_monitor():
    """Risk monitor page - Kill switch and drawdown tracking."""
    return render_template("risk_monitor.html", page="risk_monitor")


@app.route("/trade-history")
def trade_history():
    """Trade history page - Closed trades and performance."""
    return render_template("trade_history.html", page="trade_history")


@app.route("/llm-decisions")
def llm_decisions():
    """LLM decisions page - Veto history and accuracy."""
    return render_template("llm_decisions.html", page="llm_decisions")


# =============================================================================
# Conservative Refactor API Routes
# =============================================================================

@app.route("/api/market-states")
def api_market_states():
    """Get current market states for tracked assets."""
    async def _get_data():
        try:
            db = await get_db()
            # Get latest market state for each symbol
            result = await db.fetch("""
                SELECT DISTINCT ON (symbol)
                    symbol, timeframe, timestamp,
                    open, high, low, close, volume,
                    atr, atr_pct, adx, rsi, ema50, ema200, ema200_slope,
                    choppiness, bb_lower, bb_mid, bb_upper,
                    regime, trend_direction
                FROM market_states
                ORDER BY symbol, timestamp DESC
            """)
            return {row["symbol"]: dict(row) for row in result}
        except Exception as e:
            print(f"[Dashboard] Error fetching market states: {e}")
            return {}

    states = safe_run_async(_get_data(), default={})

    # Convert Decimal to float for JSON
    for symbol, state in states.items():
        for key, value in state.items():
            if isinstance(value, Decimal):
                state[key] = float(value)
            elif hasattr(value, 'isoformat'):
                state[key] = value.isoformat()

    return render_template("partials/market_state_cards.html", states=states)


@app.route("/api/kill-switch")
def api_kill_switch():
    """Get kill switch status."""
    async def _get_data():
        try:
            db = await get_db()
            # Get latest equity snapshot
            result = await db.fetchrow("""
                SELECT equity, peak_equity, drawdown_pct,
                       daily_pnl_pct, weekly_pnl_pct,
                       kill_switch_status, positions_count
                FROM equity_curve
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            if result:
                return {
                    "status": result["kill_switch_status"] or "ok",
                    "equity": float(result["equity"]) if result["equity"] else 0,
                    "peak_equity": float(result["peak_equity"]) if result["peak_equity"] else 0,
                    "drawdown_pct": float(result["drawdown_pct"]) if result["drawdown_pct"] else 0,
                    "daily_pnl_pct": float(result["daily_pnl_pct"]) if result["daily_pnl_pct"] else 0,
                    "weekly_pnl_pct": float(result["weekly_pnl_pct"]) if result["weekly_pnl_pct"] else 0,
                    "positions_count": result["positions_count"] or 0,
                    "resume_time": None,
                }
            return {"status": "ok", "resume_time": None}
        except Exception as e:
            print(f"[Dashboard] Error fetching kill switch: {e}")
            return {"status": "unknown", "error": str(e)}

    kill_switch = safe_run_async(_get_data(), default={"status": "unknown"})
    return render_template("partials/kill_switch_status.html", kill_switch=kill_switch)


@app.route("/api/kill-switch-events")
def api_kill_switch_events():
    """Get recent kill switch events."""
    async def _get_data():
        try:
            db = await get_db()
            result = await db.fetch("""
                SELECT timestamp, trigger_type, trigger_value,
                       threshold, action_taken, equity_at_trigger, message
                FROM kill_switch_log
                ORDER BY timestamp DESC
                LIMIT 20
            """)
            return [dict(row) for row in result]
        except Exception as e:
            print(f"[Dashboard] Error fetching kill switch events: {e}")
            return []

    events = safe_run_async(_get_data(), default=[])

    if not events:
        return '<div class="empty-state">No kill switch events recorded</div>'

    return render_template("partials/kill_switch_events.html", events=events)


@app.route("/api/llm-stats")
def api_llm_stats():
    """Get LLM decision statistics."""
    async def _get_data():
        try:
            db = await get_db()
            result = await db.fetchrow("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN decision = 'ALLOW' THEN 1 ELSE 0 END) as allow_count,
                    SUM(CASE WHEN decision = 'DENY' THEN 1 ELSE 0 END) as deny_count,
                    AVG(CASE WHEN was_correct THEN 1 ELSE 0 END) as accuracy
                FROM llm_decisions
            """)
            if result:
                total = result["total"] or 0
                return {
                    "total": total,
                    "allow_count": result["allow_count"] or 0,
                    "deny_count": result["deny_count"] or 0,
                    "allow_rate": (result["allow_count"] or 0) / total * 100 if total > 0 else 0,
                    "accuracy": (result["accuracy"] or 0) * 100,
                }
            return {"total": 0, "allow_count": 0, "deny_count": 0, "allow_rate": 0, "accuracy": 0}
        except Exception as e:
            print(f"[Dashboard] Error fetching LLM stats: {e}")
            return {"total": 0, "error": str(e)}

    stats = safe_run_async(_get_data(), default={"total": 0})

    html = f'''
    <div class="card"><div class="card-body">
        <div class="stat-label">Total Decisions</div>
        <div class="stat-value">{stats.get("total", 0)}</div>
    </div></div>
    <div class="card"><div class="card-body">
        <div class="stat-label">Allowed</div>
        <div class="stat-value text-green-400">{stats.get("allow_count", 0)}</div>
    </div></div>
    <div class="card"><div class="card-body">
        <div class="stat-label">Denied</div>
        <div class="stat-value text-red-400">{stats.get("deny_count", 0)}</div>
    </div></div>
    <div class="card"><div class="card-body">
        <div class="stat-label">Allow Rate</div>
        <div class="stat-value">{stats.get("allow_rate", 0):.1f}%</div>
    </div></div>
    <div class="card"><div class="card-body">
        <div class="stat-label">Accuracy</div>
        <div class="stat-value">{stats.get("accuracy", 0):.1f}%</div>
    </div></div>
    '''
    return html


@app.route("/api/llm-decisions")
def api_llm_decisions():
    """Get recent LLM decisions."""
    async def _get_data():
        try:
            db = await get_db()
            result = await db.fetch("""
                SELECT setup_id, timestamp, decision, confidence, reason,
                       symbol, regime, setup_type, was_correct
                FROM llm_decisions
                ORDER BY timestamp DESC
                LIMIT 50
            """)
            return [dict(row) for row in result]
        except Exception as e:
            print(f"[Dashboard] Error fetching LLM decisions: {e}")
            return []

    decisions = safe_run_async(_get_data(), default=[])

    # Convert types
    for d in decisions:
        if isinstance(d.get("confidence"), Decimal):
            d["confidence"] = float(d["confidence"])
        if hasattr(d.get("timestamp"), "isoformat"):
            d["timestamp"] = d["timestamp"].strftime("%Y-%m-%d %H:%M")

    return render_template("partials/llm_decisions_table.html", decisions=decisions)


@app.route("/api/trade-history")
def api_trade_history():
    """Get closed trades history."""
    async def _get_data():
        try:
            db = await get_db()
            # Get from trades table
            result = await db.fetch("""
                SELECT
                    t.symbol, t.side as direction, t.entry_time, t.exit_time,
                    t.entry_price, t.exit_price, t.size, t.net_pnl as pnl,
                    ts.setup_type as strategy,
                    CASE WHEN t.net_pnl > 0 THEN t.net_pnl / NULLIF(ABS(t.entry_price - ts.stop_price) * t.size, 0)
                         ELSE t.net_pnl / NULLIF(ABS(t.entry_price - ts.stop_price) * t.size, 0)
                    END as r_multiple
                FROM trades t
                LEFT JOIN trade_setups ts ON t.setup_id = ts.setup_id
                WHERE t.status = 'closed'
                ORDER BY t.exit_time DESC
                LIMIT 100
            """)
            trades = []
            for row in result:
                trade = dict(row)
                # Calculate duration
                if trade.get("entry_time") and trade.get("exit_time"):
                    delta = trade["exit_time"] - trade["entry_time"]
                    hours = delta.total_seconds() / 3600
                    if hours < 24:
                        trade["duration"] = f"{hours:.1f}h"
                    else:
                        trade["duration"] = f"{hours/24:.1f}d"
                else:
                    trade["duration"] = "-"
                trades.append(trade)
            return trades
        except Exception as e:
            print(f"[Dashboard] Error fetching trade history: {e}")
            return []

    trades = safe_run_async(_get_data(), default=[])

    # Convert types
    for t in trades:
        for key in ["entry_price", "exit_price", "size", "pnl", "r_multiple"]:
            if isinstance(t.get(key), Decimal):
                t[key] = float(t[key])
        for key in ["entry_time", "exit_time"]:
            if hasattr(t.get(key), "strftime"):
                t[key] = t[key].strftime("%Y-%m-%d %H:%M")

    return render_template("partials/trade_history_table.html", trades=trades)


@app.route("/api/kill-switch/resume", methods=["POST"])
def api_kill_switch_resume():
    """Manual resume from kill switch stop."""
    # This would call the KillSwitchService.manual_resume()
    # For now, just update the database
    async def _resume():
        try:
            db = await get_db()
            await db.execute("""
                UPDATE equity_curve
                SET kill_switch_status = 'ok'
                WHERE id = (SELECT id FROM equity_curve ORDER BY timestamp DESC LIMIT 1)
            """)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    result = safe_run_async(_resume(), default={"success": False, "error": "Unknown error"})
    return jsonify(result)


# =============================================================================
# HTMX Partial Routes
# =============================================================================

@app.route("/partials/services")
def partial_services():
    """Service health partial."""
    async def _get_data():
        try:
            db = await get_db()
            services = await db.get_service_health()
            return services
        except Exception as e:
            print(f"[Dashboard] Error fetching services: {e}")
            return []

    services = safe_run_async(_get_data(), default=[])

    # Parse metadata if it's a string (asyncpg may not auto-parse jsonb)
    import json
    for s in services:
        if s.get("metadata") and isinstance(s["metadata"], str):
            try:
                s["metadata"] = json.loads(s["metadata"])
            except (json.JSONDecodeError, TypeError):
                s["metadata"] = None

    # Check for stale heartbeats (> 30 seconds)
    now = datetime.now(timezone.utc)
    for s in services:
        if s.get("last_heartbeat"):
            heartbeat = s["last_heartbeat"]
            # Ensure both are tz-aware for comparison
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            age = (now - heartbeat).total_seconds()
            s["is_stale"] = age > 30
            s["age_seconds"] = int(age)
        else:
            s["is_stale"] = True
            s["age_seconds"] = None

    return render_template(
        "partials/service_list.html",
        services=services,
        updated_at=datetime.now(timezone.utc)
    )


@app.route("/partials/opportunities")
def partial_opportunities():
    """Opportunities ranking partial."""
    async def _get_data():
        try:
            db = await get_db()
            data = await db.get_latest_rankings()
            return data
        except Exception as e:
            print(f"[Dashboard] Error fetching opportunities: {e}")
            return None

    data = safe_run_async(_get_data(), default=None)

    if data:
        rankings = data.get("rankings", [])
        market_regime = data.get("market_regime", "unknown")
        timestamp = data.get("timestamp")
    else:
        rankings = []
        market_regime = "unknown"
        timestamp = None

    return render_template(
        "partials/opportunity_table.html",
        rankings=rankings[:20],
        market_regime=market_regime,
        data_timestamp=timestamp,
        updated_at=datetime.now(timezone.utc)
    )


@app.route("/partials/signals")
def partial_signals():
    """Strategy signals partial."""
    limit = request.args.get("limit", 50, type=int)

    async def _get_data():
        try:
            db = await get_db()
            decisions = await db.get_strategy_decisions(limit=limit)
            return decisions
        except Exception as e:
            print(f"[Dashboard] Error fetching signals: {e}")
            return []

    decisions = safe_run_async(_get_data(), default=[])

    # Calculate strategy distribution
    strategy_counts = {}
    for d in decisions:
        strategy = d.get("selected_strategy", "unknown")
        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1

    return render_template(
        "partials/signal_list.html",
        decisions=decisions,
        strategy_counts=strategy_counts,
        updated_at=datetime.now(timezone.utc)
    )


@app.route("/partials/positions")
def partial_positions():
    """Active positions partial."""
    async def _get_data():
        try:
            db = await get_db()
            positions = await db.get_positions()
            return positions
        except Exception as e:
            print(f"[Dashboard] Error fetching positions: {e}")
            return []

    positions = safe_run_async(_get_data(), default=[])

    # Calculate totals
    total_unrealized = sum(float(p.get("unrealized_pnl", 0) or 0) for p in positions)
    total_margin = sum(float(p.get("margin_used", 0) or 0) for p in positions)

    return render_template(
        "partials/position_table.html",
        positions=positions,
        total_unrealized=total_unrealized,
        total_margin=total_margin,
        updated_at=datetime.now(timezone.utc)
    )


@app.route("/partials/performance")
def partial_performance():
    """Performance metrics partial."""
    async def _get_data():
        try:
            db = await get_db()

            # Get stats
            stats = await db.get_stats()

            # Get recent trades
            trades = await db.get_trades(is_closed=True, limit=20)

            # Get daily summaries
            summaries = await db.get_daily_summaries(days=7)

            return stats, trades, summaries
        except Exception as e:
            print(f"[Dashboard] Error fetching performance: {e}")
            return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "total_fees": 0}, [], []

    result = safe_run_async(_get_data(), default=({"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "total_fees": 0}, [], []))
    stats, trades, summaries = result

    return render_template(
        "partials/performance_table.html",
        stats=stats,
        trades=trades,
        summaries=summaries,
        updated_at=datetime.now(timezone.utc)
    )


@app.route("/partials/account")
def partial_account():
    """Account summary partial."""
    async def _get_data():
        try:
            db = await get_db()
            account = await db.get_account()
            positions = await db.get_positions()
            stats = await db.get_stats()
            return account, positions, stats
        except Exception as e:
            print(f"[Dashboard] Error fetching account: {e}")
            return None, [], {}

    result = safe_run_async(_get_data(), default=(None, [], {}))
    account, positions, stats = result

    # Calculate position summary
    position_count = len(positions)
    total_unrealized = sum(float(p.get("unrealized_pnl", 0) or 0) for p in positions)

    return render_template(
        "partials/account_summary.html",
        account=account,
        position_count=position_count,
        total_unrealized=total_unrealized,
        stats=stats,
        updated_at=datetime.now(timezone.utc)
    )


@app.route("/partials/overview")
def partial_overview():
    """Overview summary partial."""
    async def _get_data():
        db = await get_db()

        # Fetch each piece of data separately to avoid one failure breaking all
        account = None
        positions = []
        services = []
        rankings_data = None
        stats = {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "total_fees": 0}
        decisions = []
        market_regime = None

        try:
            account = await db.get_account()
        except Exception as e:
            print(f"[Dashboard] Error fetching account: {e}")

        try:
            positions = await db.get_positions()
        except Exception as e:
            print(f"[Dashboard] Error fetching positions: {e}")

        try:
            services = await db.get_service_health()
        except Exception as e:
            print(f"[Dashboard] Error fetching services: {e}")

        try:
            rankings_data = await db.get_latest_rankings()
        except Exception as e:
            # This is expected for conservative system (no opportunity_rankings table)
            pass

        try:
            stats = await db.get_stats()
        except Exception as e:
            print(f"[Dashboard] Error fetching stats: {e}")

        try:
            decisions = await db.get_strategy_decisions(limit=10)
        except Exception as e:
            print(f"[Dashboard] Error fetching decisions: {e}")

        # Get market regime from market_states (conservative system)
        try:
            btc_states = await db.fetch("""
                SELECT regime FROM market_states
                WHERE symbol = 'BTC'
                ORDER BY timestamp DESC LIMIT 1
            """)
            if btc_states and len(btc_states) > 0:
                market_regime = btc_states[0]["regime"]
        except Exception as e:
            print(f"[Dashboard] Error fetching market regime: {e}")

        return {
            "account": account,
            "positions": positions,
            "services": services,
            "rankings_data": rankings_data,
            "stats": stats,
            "decisions": decisions,
            "market_regime": market_regime,
        }

    data = safe_run_async(_get_data(), default={
        "account": None,
        "positions": [],
        "services": [],
        "rankings_data": None,
        "stats": {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "total_fees": 0},
        "decisions": [],
        "market_regime": None,
    })

    # Process data
    account = data["account"] or {}
    positions = data["positions"]
    services = data["services"]
    rankings_data = data["rankings_data"] or {}
    stats = data["stats"]
    decisions = data["decisions"]

    # Service health summary
    healthy_count = sum(1 for s in services if s.get("status") == "healthy")
    total_services = len(services)

    # Market regime (from market_states or fallback to rankings_data)
    market_regime = data.get("market_regime") or rankings_data.get("market_regime")

    # If still no regime, try a separate fetch
    if not market_regime:
        async def _get_regime():
            try:
                db = await get_db()
                result = await db.fetch("""
                    SELECT regime FROM market_states
                    WHERE symbol = 'BTC'
                    ORDER BY timestamp DESC LIMIT 1
                """)
                if result and len(result) > 0:
                    return result[0]["regime"]
            except Exception:
                pass
            return "unknown"
        market_regime = safe_run_async(_get_regime(), default="unknown")

    # Daily PnL (from stats or calculate)
    daily_pnl = float(stats.get("total_pnl", 0))

    return render_template(
        "partials/overview_summary.html",
        account=account,
        positions=positions,
        services=services,
        healthy_count=healthy_count,
        total_services=total_services,
        market_regime=market_regime,
        stats=stats,
        decisions=decisions,
        daily_pnl=daily_pnl,
        updated_at=datetime.now(timezone.utc)
    )


# =============================================================================
# API Routes (JSON)
# =============================================================================

@app.route("/api/health")
def api_health():
    """Health check endpoint."""
    async def _check():
        db = await get_db()
        db_healthy = await db.health_check()
        return db_healthy

    try:
        db_healthy = run_async(_check())
        return jsonify({
            "status": "healthy" if db_healthy else "degraded",
            "database": db_healthy,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }), 500


@app.route("/api/metrics")
def api_metrics():
    """System metrics endpoint."""
    async def _get_metrics():
        db = await get_db()

        account = await db.get_account()
        positions = await db.get_positions()
        services = await db.get_service_health()
        stats = await db.get_stats()

        return {
            "account": {
                "equity": float(account.get("equity", 0)) if account else 0,
                "available_balance": float(account.get("available_balance", 0)) if account else 0,
                "margin_used": float(account.get("margin_used", 0)) if account else 0,
                "unrealized_pnl": float(account.get("unrealized_pnl", 0)) if account else 0,
            },
            "positions": {
                "count": len(positions),
                "total_margin": sum(float(p.get("margin_used", 0) or 0) for p in positions),
                "total_unrealized": sum(float(p.get("unrealized_pnl", 0) or 0) for p in positions),
            },
            "services": {
                "total": len(services),
                "healthy": sum(1 for s in services if s.get("status") == "healthy"),
                "degraded": sum(1 for s in services if s.get("status") == "degraded"),
                "unhealthy": sum(1 for s in services if s.get("status") == "unhealthy"),
            },
            "trading": {
                "total_trades": stats.get("total_trades", 0),
                "win_rate": stats.get("win_rate", 0),
                "total_pnl": float(stats.get("total_pnl", 0)),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    try:
        metrics = run_async(_get_metrics())
        return jsonify(metrics)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bot-status")
def api_bot_status():
    """Bot status for header indicator."""
    async def _get_status():
        db = await get_db()
        services = await db.get_service_health()
        return services

    try:
        services = run_async(_get_status())

        # Determine overall status
        healthy = sum(1 for s in services if s.get("status") == "healthy")
        total = len(services)

        if total == 0:
            status = "offline"
            status_class = "offline"
        elif healthy == total:
            status = "online"
            status_class = "online"
        elif healthy > 0:
            status = "degraded"
            status_class = "warning"
        else:
            status = "offline"
            status_class = "offline"

        # Return HTML for HTMX
        return f'''
        <span class="status-dot {status_class}"></span>
        <span>{status.capitalize()}</span>
        <span style="font-size: 0.7rem; color: var(--text-muted);">({healthy}/{total})</span>
        '''
    except Exception as e:
        return '''
        <span class="status-dot offline"></span>
        <span>Error</span>
        '''


# =============================================================================
# Error Handlers
# =============================================================================

@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors."""
    return render_template("error.html", error="Page not found", code=404), 404


@app.errorhandler(500)
def server_error(e):
    """Handle 500 errors."""
    return render_template("error.html", error="Internal server error", code=500), 500


# =============================================================================
# Main Entry Point
# =============================================================================

def run_dashboard(host: str = "0.0.0.0", port: int = 5611, debug: bool = None):
    """Run the dashboard server."""
    # Default to False in production, can enable via FLASK_DEBUG=1
    if debug is None:
        debug = os.getenv("FLASK_DEBUG", "0") == "1"

    print(f"Starting HLQuantBot v2.0 Dashboard on http://{host}:{port}")
    # use_reloader=False prevents Flask from forking which breaks background event loop
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    run_dashboard()
