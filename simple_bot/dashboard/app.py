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


@app.route("/api/cooldown-status")
def api_cooldown_status():
    """Get current cooldown status."""
    async def _get_data():
        try:
            db = await get_db()
            cooldown = await db.get_active_cooldown()
            if cooldown:
                # Parse details JSON if string
                details = cooldown.get("details", {})
                if isinstance(details, str):
                    import json
                    details = json.loads(details)
                
                # Calculate remaining time
                cooldown_until = cooldown.get("cooldown_until")
                if cooldown_until:
                    now = datetime.now(timezone.utc)
                    if cooldown_until.tzinfo is None:
                        cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
                    remaining_seconds = max(0, (cooldown_until - now).total_seconds())
                else:
                    remaining_seconds = 0
                
                return {
                    "active": remaining_seconds > 0,
                    "reason": cooldown.get("reason"),
                    "triggered_at": cooldown.get("triggered_at").isoformat() if cooldown.get("triggered_at") else None,
                    "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
                    "remaining_seconds": int(remaining_seconds),
                    "remaining_minutes": int(remaining_seconds // 60),
                    "details": details,
                }
            return {"active": False}
        except Exception as e:
            print(f"[Dashboard] Error fetching cooldown status: {e}")
            return {"active": False, "error": str(e)}

    cooldown = safe_run_async(_get_data(), default={"active": False})
    return jsonify(cooldown)


@app.route("/api/cooldown-history")
def api_cooldown_history():
    """Get cooldown history."""
    async def _get_data():
        try:
            db = await get_db()
            history = await db.get_cooldown_history(limit=20)
            result = []
            for row in history:
                details = row.get("details", {})
                if isinstance(details, str):
                    import json
                    details = json.loads(details)
                result.append({
                    "id": row.get("id"),
                    "reason": row.get("reason"),
                    "triggered_at": row.get("triggered_at").isoformat() if row.get("triggered_at") else None,
                    "cooldown_until": row.get("cooldown_until").isoformat() if row.get("cooldown_until") else None,
                    "details": details,
                })
            return result
        except Exception as e:
            print(f"[Dashboard] Error fetching cooldown history: {e}")
            return []

    history = safe_run_async(_get_data(), default=[])
    return jsonify(history)


@app.route("/api/protections")
def api_protections():
    """Get active protections from the protection system."""
    async def _get_data():
        try:
            db = await get_db()
            # Query active protections (protected_until > NOW())
            rows = await db.fetch("""
                SELECT 
                    id,
                    protection_name,
                    protected_until,
                    trigger_details,
                    created_at
                FROM protections
                WHERE protected_until > NOW()
                ORDER BY created_at DESC
            """)
            
            result = []
            now = datetime.now(timezone.utc)
            
            for row in rows:
                details = row.get("trigger_details", {})
                if isinstance(details, str):
                    import json
                    details = json.loads(details)
                
                protected_until = row.get("protected_until")
                if protected_until and protected_until.tzinfo is None:
                    protected_until = protected_until.replace(tzinfo=timezone.utc)
                
                remaining_seconds = max(0, (protected_until - now).total_seconds()) if protected_until else 0
                
                result.append({
                    "id": row.get("id"),
                    "protection_name": row.get("protection_name"),
                    "protected_until": protected_until.isoformat() if protected_until else None,
                    "remaining_seconds": int(remaining_seconds),
                    "remaining_minutes": int(remaining_seconds // 60),
                    "trigger_details": details,
                    "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
                })
            
            return {
                "active_protections": result,
                "count": len(result),
            }
        except Exception as e:
            print(f"[Dashboard] Error fetching protections: {e}")
            return {"active_protections": [], "count": 0, "error": str(e)}

    protections = safe_run_async(_get_data(), default={"active_protections": [], "count": 0})
    return jsonify(protections)


@app.route("/api/protections/history")
def api_protections_history():
    """Get protection trigger history."""
    async def _get_data():
        try:
            db = await get_db()
            rows = await db.fetch("""
                SELECT 
                    id,
                    protection_name,
                    protected_until,
                    trigger_details,
                    created_at
                FROM protections
                ORDER BY created_at DESC
                LIMIT 50
            """)
            
            result = []
            for row in rows:
                details = row.get("trigger_details", {})
                if isinstance(details, str):
                    import json
                    details = json.loads(details)
                
                result.append({
                    "id": row.get("id"),
                    "protection_name": row.get("protection_name"),
                    "protected_until": row.get("protected_until").isoformat() if row.get("protected_until") else None,
                    "trigger_details": details,
                    "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
                })
            
            return result
        except Exception as e:
            print(f"[Dashboard] Error fetching protection history: {e}")
            return []

    history = safe_run_async(_get_data(), default=[])
    return jsonify(history)


@app.route("/api/protections/<int:protection_id>/clear", methods=["POST"])
def api_clear_protection(protection_id: int):
    """Manually clear a protection (admin override)."""
    async def _clear():
        try:
            db = await get_db()
            await db.execute("""
                UPDATE protections
                SET protected_until = NOW()
                WHERE id = $1
            """, protection_id)
            return {"success": True, "message": f"Protection {protection_id} cleared"}
        except Exception as e:
            print(f"[Dashboard] Error clearing protection: {e}")
            return {"success": False, "error": str(e)}

    result = safe_run_async(_clear(), default={"success": False, "error": "Unknown error"})
    return jsonify(result)


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
    from decimal import Decimal as Dec
    import math
    import statistics
    from collections import defaultdict

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

    async def _get_metrics():
        """Calculate risk-adjusted performance metrics."""
        try:
            db = await get_db()

            # Get initial equity and current equity
            account = await db.get_account()
            current_equity = Dec(str(account.get("equity", 100))) if account else Dec("100")

            # Get initial equity (fallback to current minus total PnL)
            pnl_row = await db.fetchrow(
                """
                SELECT COALESCE(SUM(net_pnl), 0) as total_pnl
                FROM trades
                WHERE is_closed = true
                """
            )
            total_pnl_from_db = Dec(str(pnl_row["total_pnl"])) if pnl_row else Dec("0")
            initial_equity = current_equity - total_pnl_from_db
            if initial_equity <= 0:
                initial_equity = current_equity

            # Get all closed trades
            all_trades = await db.fetch(
                """
                SELECT
                    trade_id, symbol, side, size,
                    entry_price, entry_time,
                    exit_price, exit_time,
                    gross_pnl, fees, net_pnl,
                    strategy, duration_seconds, notes
                FROM trades
                WHERE is_closed = true
                ORDER BY exit_time ASC
                """
            )
            all_trades = [dict(row) for row in all_trades]

            # Return None if no trades
            if not all_trades:
                return None

            # Separate winning and losing trades
            winning_trades = [t for t in all_trades if (t.get("net_pnl") or 0) > 0]
            losing_trades = [t for t in all_trades if (t.get("net_pnl") or 0) < 0]

            total_trades = len(all_trades)
            winning_count = len(winning_trades)
            losing_count = len(losing_trades)

            win_rate = winning_count / total_trades if total_trades > 0 else 0

            # PnL metrics
            total_pnl = sum(Dec(str(t.get("net_pnl") or 0)) for t in all_trades)
            total_fees = sum(Dec(str(t.get("fees") or 0)) for t in all_trades)
            total_pnl_pct = (total_pnl / initial_equity * 100) if initial_equity > 0 else Dec("0")

            gross_profit = sum(Dec(str(t.get("net_pnl") or 0)) for t in winning_trades)
            gross_loss = sum(Dec(str(t.get("net_pnl") or 0)) for t in losing_trades)

            avg_win = gross_profit / winning_count if winning_count > 0 else Dec("0")
            avg_loss = gross_loss / losing_count if losing_count > 0 else Dec("0")

            avg_win_loss_ratio = abs(float(avg_win / avg_loss)) if avg_loss != 0 else None

            pnls = [Dec(str(t.get("net_pnl") or 0)) for t in all_trades]
            largest_win = max(pnls) if pnls else Dec("0")
            largest_loss = min(pnls) if pnls else Dec("0")

            durations = [t.get("duration_seconds") for t in all_trades if t.get("duration_seconds")]
            avg_duration = int(sum(durations) / len(durations)) if durations else None

            # Calculate daily returns
            daily_pnl = defaultdict(lambda: Dec("0"))
            for trade in all_trades:
                exit_time = trade.get("exit_time")
                if exit_time:
                    date_key = exit_time.date().isoformat()
                    daily_pnl[date_key] += Dec(str(trade.get("net_pnl") or 0))

            daily_returns = []
            running_equity = initial_equity
            for date_key in sorted(daily_pnl.keys()):
                pnl = daily_pnl[date_key]
                if running_equity > 0:
                    daily_returns.append(float(pnl / running_equity))
                    running_equity += pnl

            # Sharpe Ratio
            sharpe_ratio = None
            if len(daily_returns) >= 2:
                try:
                    mean_return = statistics.mean(daily_returns)
                    std_return = statistics.stdev(daily_returns)
                    if std_return > 0:
                        risk_free_daily = 0.03 / 365
                        sharpe = (mean_return - risk_free_daily) / std_return * math.sqrt(365)
                        sharpe_ratio = round(sharpe, 2)
                except Exception:
                    pass

            # Sortino Ratio
            sortino_ratio = None
            if len(daily_returns) >= 2:
                try:
                    downside_returns = [r for r in daily_returns if r < 0]
                    if len(downside_returns) >= 2:
                        mean_return = statistics.mean(daily_returns)
                        downside_std = statistics.stdev(downside_returns)
                        if downside_std > 0:
                            risk_free_daily = 0.03 / 365
                            sortino = (mean_return - risk_free_daily) / downside_std * math.sqrt(365)
                            sortino_ratio = round(sortino, 2)
                except Exception:
                    pass

            # Equity curve and drawdown
            equity_curve = [(initial_equity, None)]
            running_eq = initial_equity
            for trade in all_trades:
                net_pnl = Dec(str(trade.get("net_pnl") or 0))
                running_eq += net_pnl
                equity_curve.append((running_eq, trade.get("exit_time")))

            peak = equity_curve[0][0]
            max_dd_pct = Dec("0")
            max_dd_abs = Dec("0")
            for eq, _ in equity_curve:
                if eq > peak:
                    peak = eq
                dd_abs = peak - eq
                dd_pct = (dd_abs / peak * 100) if peak > 0 else Dec("0")
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct
                    max_dd_abs = dd_abs

            current_peak = max(eq for eq, _ in equity_curve)
            current_eq = equity_curve[-1][0]
            current_dd_pct = ((current_peak - current_eq) / current_peak * 100) if current_peak > 0 else Dec("0")

            # Calmar Ratio
            calmar_ratio = None
            if max_dd_pct > 0 and total_pnl_pct != 0 and len(all_trades) >= 2:
                first_time = all_trades[0].get("entry_time")
                last_time = all_trades[-1].get("exit_time") or all_trades[-1].get("entry_time")
                if first_time and last_time:
                    trading_days = max(1, (last_time - first_time).days)
                    annual_return_pct = (float(total_pnl_pct) / trading_days) * 365
                    calmar_ratio = round(annual_return_pct / float(max_dd_pct), 2)

            # Profit Factor
            profit_factor = None
            abs_loss = abs(gross_loss)
            if abs_loss > 0:
                profit_factor = round(float(gross_profit / abs_loss), 2)

            # Expectancy
            expectancy = None
            if avg_loss != 0:
                loss_rate = 1 - win_rate
                expectancy = round(float(avg_win) * win_rate - float(abs(avg_loss)) * loss_rate, 2)

            # SQN
            sqn = None
            if len(pnls) >= 2:
                try:
                    float_pnls = [float(p) for p in pnls]
                    mean_pnl = statistics.mean(float_pnls)
                    std_pnl = statistics.stdev(float_pnls)
                    if std_pnl > 0:
                        sqn = round((mean_pnl / std_pnl) * math.sqrt(len(float_pnls)), 2)
                except Exception:
                    pass

            return {
                "timestamp": datetime.now(timezone.utc),
                "equity": float(current_equity),
                "initial_equity": float(initial_equity),
                "total_pnl": float(total_pnl),
                "total_pnl_pct": float(total_pnl_pct),
                "sharpe_ratio": sharpe_ratio,
                "sortino_ratio": sortino_ratio,
                "calmar_ratio": calmar_ratio,
                "max_drawdown_pct": float(max_dd_pct),
                "max_drawdown_abs": float(max_dd_abs),
                "current_drawdown_pct": float(current_dd_pct),
                "profit_factor": profit_factor,
                "win_rate": round(win_rate, 4),
                "avg_win": float(avg_win),
                "avg_loss": float(avg_loss),
                "avg_win_loss_ratio": avg_win_loss_ratio,
                "expectancy": expectancy,
                "sqn": sqn,
                "total_trades": total_trades,
                "winning_trades": winning_count,
                "losing_trades": losing_count,
                "total_fees": float(total_fees),
                "avg_trade_duration_seconds": avg_duration,
                "largest_win": float(largest_win),
                "largest_loss": float(largest_loss),
            }
        except Exception as e:
            print(f"[Dashboard] Error calculating metrics: {e}")
            import traceback
            traceback.print_exc()
            return None

    result = safe_run_async(_get_data(), default=({"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "total_fees": 0}, [], []))
    stats, trades, summaries = result

    metrics = safe_run_async(_get_metrics(), default=None)

    return render_template(
        "partials/performance_table.html",
        stats=stats,
        trades=trades,
        summaries=summaries,
        metrics=metrics,
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
        cooldown = None

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

        # Get cooldown status
        try:
            cooldown = await db.get_active_cooldown()
        except Exception as e:
            print(f"[Dashboard] Error fetching cooldown: {e}")

        # Get active protections
        active_protections = []
        try:
            import json as _json
            rows = await db.fetch("""
                SELECT 
                    protection_name,
                    protected_until,
                    trigger_details
                FROM protections
                WHERE protected_until > NOW()
                ORDER BY created_at DESC
            """)
            now = datetime.now(timezone.utc)
            for row in rows:
                protected_until = row.get("protected_until")
                if protected_until and protected_until.tzinfo is None:
                    protected_until = protected_until.replace(tzinfo=timezone.utc)
                remaining_seconds = max(0, (protected_until - now).total_seconds()) if protected_until else 0
                details = row.get("trigger_details", {})
                if isinstance(details, str):
                    details = _json.loads(details)
                active_protections.append({
                    "protection_name": row.get("protection_name"),
                    "protected_until": protected_until.strftime("%Y-%m-%d %H:%M UTC") if protected_until else None,
                    "remaining_minutes": int(remaining_seconds // 60),
                    "trigger_details": details,
                })
        except Exception as e:
            print(f"[Dashboard] Error fetching active protections: {e}")

        return {
            "account": account,
            "positions": positions,
            "services": services,
            "rankings_data": rankings_data,
            "stats": stats,
            "decisions": decisions,
            "market_regime": market_regime,
            "cooldown": cooldown,
            "active_protections": active_protections,
        }

    data = safe_run_async(_get_data(), default={
        "account": None,
        "positions": [],
        "services": [],
        "rankings_data": None,
        "stats": {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "total_fees": 0},
        "decisions": [],
        "market_regime": None,
        "cooldown": None,
        "active_protections": [],
    })

    # Process data
    account = data["account"] or {}
    positions = data["positions"]
    services = data["services"]
    rankings_data = data["rankings_data"] or {}
    stats = data["stats"]
    decisions = data["decisions"]
    cooldown_data = data.get("cooldown")

    # Process cooldown data
    cooldown_active = False
    cooldown_info = None
    if cooldown_data:
        cooldown_until = cooldown_data.get("cooldown_until")
        if cooldown_until:
            now = datetime.now(timezone.utc)
            if cooldown_until.tzinfo is None:
                cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
            remaining = max(0, (cooldown_until - now).total_seconds())
            cooldown_active = remaining > 0
            if cooldown_active:
                import json
                details = cooldown_data.get("details", {})
                if isinstance(details, str):
                    details = json.loads(details)
                cooldown_info = {
                    "reason": cooldown_data.get("reason"),
                    "cooldown_until": cooldown_until,
                    "remaining_minutes": int(remaining // 60),
                    "remaining_hours": round(remaining / 3600, 1),
                    "details": details,
                }

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

    # Get active protections from data
    active_protections = data.get("active_protections", [])

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
        cooldown_active=cooldown_active,
        cooldown=cooldown_info,
        active_protections=active_protections,
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


@app.route("/api/performance-metrics")
def api_performance_metrics():
    """
    Risk-adjusted performance metrics endpoint.

    Returns comprehensive trading performance metrics including:
    - Sharpe Ratio, Sortino Ratio, Calmar Ratio
    - Max Drawdown, Current Drawdown
    - Profit Factor, Win Rate, Expectancy
    - System Quality Number (SQN)
    """
    from decimal import Decimal as Dec
    import math
    import statistics
    from collections import defaultdict

    async def _calculate_metrics():
        db = await get_db()

        # Get initial equity and current equity
        account = await db.get_account()
        current_equity = Dec(str(account.get("equity", 100))) if account else Dec("100")

        # Get initial equity (fallback to current minus total PnL)
        pnl_row = await db.fetchrow(
            """
            SELECT COALESCE(SUM(net_pnl), 0) as total_pnl
            FROM trades
            WHERE is_closed = true
            """
        )
        total_pnl_from_db = Dec(str(pnl_row["total_pnl"])) if pnl_row else Dec("0")
        initial_equity = current_equity - total_pnl_from_db
        if initial_equity <= 0:
            initial_equity = current_equity

        # Get all closed trades
        trades = await db.fetch(
            """
            SELECT
                trade_id, symbol, side, size,
                entry_price, entry_time,
                exit_price, exit_time,
                gross_pnl, fees, net_pnl,
                strategy, duration_seconds, notes
            FROM trades
            WHERE is_closed = true
            ORDER BY exit_time ASC
            """
        )
        trades = [dict(row) for row in trades]

        # Return empty metrics if no trades
        if not trades:
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "equity": float(current_equity),
                "initial_equity": float(initial_equity),
                "total_pnl": 0,
                "total_pnl_pct": 0,
                "sharpe_ratio": None,
                "sortino_ratio": None,
                "calmar_ratio": None,
                "max_drawdown_pct": 0,
                "max_drawdown_abs": 0,
                "current_drawdown_pct": 0,
                "profit_factor": None,
                "win_rate": 0,
                "avg_win": 0,
                "avg_loss": 0,
                "avg_win_loss_ratio": None,
                "expectancy": None,
                "sqn": None,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_fees": 0,
                "avg_trade_duration_seconds": None,
                "largest_win": 0,
                "largest_loss": 0,
            }

        # Separate winning and losing trades
        winning_trades = [t for t in trades if (t.get("net_pnl") or 0) > 0]
        losing_trades = [t for t in trades if (t.get("net_pnl") or 0) < 0]

        total_trades = len(trades)
        winning_count = len(winning_trades)
        losing_count = len(losing_trades)

        win_rate = winning_count / total_trades if total_trades > 0 else 0

        # PnL metrics
        total_pnl = sum(Dec(str(t.get("net_pnl") or 0)) for t in trades)
        total_fees = sum(Dec(str(t.get("fees") or 0)) for t in trades)
        total_pnl_pct = (total_pnl / initial_equity * 100) if initial_equity > 0 else Dec("0")

        gross_profit = sum(Dec(str(t.get("net_pnl") or 0)) for t in winning_trades)
        gross_loss = sum(Dec(str(t.get("net_pnl") or 0)) for t in losing_trades)

        avg_win = gross_profit / winning_count if winning_count > 0 else Dec("0")
        avg_loss = gross_loss / losing_count if losing_count > 0 else Dec("0")

        avg_win_loss_ratio = abs(float(avg_win / avg_loss)) if avg_loss != 0 else None

        pnls = [Dec(str(t.get("net_pnl") or 0)) for t in trades]
        largest_win = max(pnls) if pnls else Dec("0")
        largest_loss = min(pnls) if pnls else Dec("0")

        durations = [t.get("duration_seconds") for t in trades if t.get("duration_seconds")]
        avg_duration = int(sum(durations) / len(durations)) if durations else None

        # Calculate daily returns
        daily_pnl = defaultdict(lambda: Dec("0"))
        for trade in trades:
            exit_time = trade.get("exit_time")
            if exit_time:
                date_key = exit_time.date().isoformat()
                daily_pnl[date_key] += Dec(str(trade.get("net_pnl") or 0))

        daily_returns = []
        running_equity = initial_equity
        for date_key in sorted(daily_pnl.keys()):
            pnl = daily_pnl[date_key]
            if running_equity > 0:
                daily_returns.append(float(pnl / running_equity))
                running_equity += pnl

        # Sharpe Ratio
        sharpe_ratio = None
        if len(daily_returns) >= 2:
            try:
                mean_return = statistics.mean(daily_returns)
                std_return = statistics.stdev(daily_returns)
                if std_return > 0:
                    risk_free_daily = 0.03 / 365
                    sharpe = (mean_return - risk_free_daily) / std_return * math.sqrt(365)
                    sharpe_ratio = round(sharpe, 2)
            except Exception:
                pass

        # Sortino Ratio
        sortino_ratio = None
        if len(daily_returns) >= 2:
            try:
                downside_returns = [r for r in daily_returns if r < 0]
                if len(downside_returns) >= 2:
                    mean_return = statistics.mean(daily_returns)
                    downside_std = statistics.stdev(downside_returns)
                    if downside_std > 0:
                        risk_free_daily = 0.03 / 365
                        sortino = (mean_return - risk_free_daily) / downside_std * math.sqrt(365)
                        sortino_ratio = round(sortino, 2)
            except Exception:
                pass

        # Equity curve and drawdown
        equity_curve = [(initial_equity, None)]
        running_eq = initial_equity
        for trade in trades:
            net_pnl = Dec(str(trade.get("net_pnl") or 0))
            running_eq += net_pnl
            equity_curve.append((running_eq, trade.get("exit_time")))

        peak = equity_curve[0][0]
        max_dd_pct = Dec("0")
        max_dd_abs = Dec("0")
        for eq, _ in equity_curve:
            if eq > peak:
                peak = eq
            dd_abs = peak - eq
            dd_pct = (dd_abs / peak * 100) if peak > 0 else Dec("0")
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_abs = dd_abs

        current_peak = max(eq for eq, _ in equity_curve)
        current_eq = equity_curve[-1][0]
        current_dd_pct = ((current_peak - current_eq) / current_peak * 100) if current_peak > 0 else Dec("0")

        # Calmar Ratio
        calmar_ratio = None
        if max_dd_pct > 0 and total_pnl_pct != 0 and len(trades) >= 2:
            first_time = trades[0].get("entry_time")
            last_time = trades[-1].get("exit_time") or trades[-1].get("entry_time")
            if first_time and last_time:
                trading_days = max(1, (last_time - first_time).days)
                annual_return_pct = (float(total_pnl_pct) / trading_days) * 365
                calmar_ratio = round(annual_return_pct / float(max_dd_pct), 2)

        # Profit Factor
        profit_factor = None
        abs_loss = abs(gross_loss)
        if abs_loss > 0:
            profit_factor = round(float(gross_profit / abs_loss), 2)

        # Expectancy
        expectancy = None
        if avg_loss != 0:
            loss_rate = 1 - win_rate
            expectancy = round(float(avg_win) * win_rate - float(abs(avg_loss)) * loss_rate, 2)

        # SQN
        sqn = None
        if len(pnls) >= 2:
            try:
                float_pnls = [float(p) for p in pnls]
                mean_pnl = statistics.mean(float_pnls)
                std_pnl = statistics.stdev(float_pnls)
                if std_pnl > 0:
                    sqn = round((mean_pnl / std_pnl) * math.sqrt(len(float_pnls)), 2)
            except Exception:
                pass

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "equity": float(current_equity),
            "initial_equity": float(initial_equity),
            "total_pnl": float(total_pnl),
            "total_pnl_pct": float(total_pnl_pct),
            "sharpe_ratio": sharpe_ratio,
            "sortino_ratio": sortino_ratio,
            "calmar_ratio": calmar_ratio,
            "max_drawdown_pct": float(max_dd_pct),
            "max_drawdown_abs": float(max_dd_abs),
            "current_drawdown_pct": float(current_dd_pct),
            "profit_factor": profit_factor,
            "win_rate": round(win_rate, 4),
            "avg_win": float(avg_win),
            "avg_loss": float(avg_loss),
            "avg_win_loss_ratio": avg_win_loss_ratio,
            "expectancy": expectancy,
            "sqn": sqn,
            "total_trades": total_trades,
            "winning_trades": winning_count,
            "losing_trades": losing_count,
            "total_fees": float(total_fees),
            "avg_trade_duration_seconds": avg_duration,
            "largest_win": float(largest_win),
            "largest_loss": float(largest_loss),
        }

    try:
        metrics = run_async(_calculate_metrics())
        return jsonify(metrics)
    except Exception as e:
        import traceback
        traceback.print_exc()
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


@app.route("/api/bot-activity")
def api_bot_activity():
    """
    Get current bot activity status for the dashboard.
    Returns: current activity, last scan time, next scan time, active monitors.
    """
    async def _get_data():
        try:
            db = await get_db()
            
            # Get latest service heartbeats to determine bot state
            services = await db.get_service_health()
            
            # Get latest market state timestamp (indicates last scan)
            last_scan_row = await db.fetchrow("""
                SELECT MAX(timestamp) as last_scan
                FROM market_states
            """)
            last_scan = last_scan_row["last_scan"] if last_scan_row else None
            
            # Get active positions count
            positions = await db.get_positions()
            active_monitors = len(positions)
            
            # Get scan interval from service metadata (default 15 min)
            scan_interval_minutes = 15
            for s in services:
                if s.get("service_name") == "market_state" and s.get("metadata"):
                    meta = s["metadata"]
                    if isinstance(meta, str):
                        import json
                        meta = json.loads(meta)
                    scan_interval_minutes = meta.get("scan_interval_minutes", 15)
                    break
            
            # Calculate next scan time
            next_scan = None
            if last_scan:
                from datetime import timedelta
                next_scan = last_scan + timedelta(minutes=scan_interval_minutes)
            
            # Determine current activity based on services
            market_state_service = None
            execution_service = None
            llm_service = None
            
            for s in services:
                name = s.get("service_name", "")
                if "market_state" in name:
                    market_state_service = s
                elif "execution" in name:
                    execution_service = s
                elif "llm" in name:
                    llm_service = s
            
            # Determine activity state
            now = datetime.now(timezone.utc)
            
            activity = "idle"
            activity_detail = "Waiting for next scan"
            
            # Check if any service is actively processing
            if market_state_service:
                heartbeat = market_state_service.get("last_heartbeat")
                if heartbeat:
                    if heartbeat.tzinfo is None:
                        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
                    age = (now - heartbeat).total_seconds()
                    
                    if age < 10:  # Recent heartbeat
                        activity = "scanning"
                        activity_detail = "Scanning markets..."
            
            if llm_service:
                heartbeat = llm_service.get("last_heartbeat")
                if heartbeat:
                    if heartbeat.tzinfo is None:
                        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
                    age = (now - heartbeat).total_seconds()
                    
                    if age < 5:  # Very recent LLM activity
                        activity = "evaluating"
                        activity_detail = "Evaluating setup with LLM..."
            
            if execution_service:
                heartbeat = execution_service.get("last_heartbeat")
                if heartbeat:
                    if heartbeat.tzinfo is None:
                        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
                    age = (now - heartbeat).total_seconds()
                    
                    if age < 5:  # Very recent execution activity
                        activity = "executing"
                        activity_detail = "Executing trade..."
            
            # Check for recent setups to show evaluation state
            recent_setup = await db.fetchrow("""
                SELECT timestamp, symbol, llm_approved
                FROM trade_setups
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            
            if recent_setup and recent_setup["timestamp"]:
                setup_time = recent_setup["timestamp"]
                if setup_time.tzinfo is None:
                    setup_time = setup_time.replace(tzinfo=timezone.utc)
                setup_age = (now - setup_time).total_seconds()
                
                if setup_age < 60 and recent_setup["llm_approved"] is None:
                    activity = "evaluating"
                    activity_detail = f"Evaluating {recent_setup['symbol']} setup..."
            
            # If no services running, show offline
            healthy_services = sum(1 for s in services if s.get("status") == "healthy")
            if len(services) == 0 or healthy_services == 0:
                activity = "offline"
                activity_detail = "Bot not running"
            
            return {
                "activity": activity,
                "activity_detail": activity_detail,
                "last_scan": last_scan.isoformat() if last_scan else None,
                "next_scan": next_scan.isoformat() if next_scan else None,
                "scan_interval_minutes": scan_interval_minutes,
                "active_monitors": active_monitors,
                "healthy_services": healthy_services,
                "total_services": len(services),
            }
        except Exception as e:
            print(f"[Dashboard] Error fetching bot activity: {e}")
            return {
                "activity": "error",
                "activity_detail": str(e),
                "last_scan": None,
                "next_scan": None,
                "active_monitors": 0,
            }

    data = safe_run_async(_get_data(), default={
        "activity": "unknown",
        "activity_detail": "Unable to fetch status",
        "last_scan": None,
        "next_scan": None,
        "active_monitors": 0,
    })

    return render_template("partials/bot_activity.html", **data)


@app.route("/api/recent-setups")
def api_recent_setups():
    """
    Get recent trade setups with LLM/risk decisions.
    Shows the last 10 setups generated.
    """
    async def _get_data():
        try:
            db = await get_db()
            
            # Get recent setups from trade_setups table
            rows = await db.fetch("""
                SELECT 
                    setup_id,
                    timestamp,
                    symbol,
                    setup_type,
                    direction,
                    regime,
                    entry_price,
                    stop_price,
                    stop_distance_pct,
                    setup_quality,
                    confidence,
                    llm_approved,
                    llm_confidence,
                    llm_reason,
                    was_executed,
                    final_pnl
                FROM trade_setups
                ORDER BY timestamp DESC
                LIMIT 10
            """)
            
            setups = []
            for row in rows:
                setup = dict(row)
                
                # Determine status
                if setup.get("was_executed"):
                    status = "executed"
                    status_icon = "chart"
                    status_class = "info"
                elif setup.get("llm_approved") is True:
                    status = "approved"
                    status_icon = "check"
                    status_class = "success"
                elif setup.get("llm_approved") is False:
                    status = "rejected_llm"
                    status_icon = "x"
                    status_class = "danger"
                else:
                    status = "pending"
                    status_icon = "clock"
                    status_class = "warning"
                
                setup["status"] = status
                setup["status_icon"] = status_icon
                setup["status_class"] = status_class
                
                # Convert Decimals to float
                for key in ["entry_price", "stop_price", "stop_distance_pct", 
                           "setup_quality", "confidence", "llm_confidence", "final_pnl"]:
                    if setup.get(key) is not None:
                        setup[key] = float(setup[key])
                
                setups.append(setup)
            
            return setups
        except Exception as e:
            print(f"[Dashboard] Error fetching recent setups: {e}")
            import traceback
            traceback.print_exc()
            return []

    setups = safe_run_async(_get_data(), default=[])
    
    return render_template("partials/recent_setups.html", setups=setups)


@app.route("/api/llm-activity")
def api_llm_activity():
    """
    Get recent LLM veto decisions with details.
    Shows the last 5 LLM decisions for the activity feed.
    """
    async def _get_data():
        try:
            db = await get_db()
            
            # Get recent LLM decisions
            rows = await db.fetch("""
                SELECT 
                    setup_id,
                    timestamp,
                    decision,
                    confidence,
                    reason,
                    symbol,
                    regime,
                    setup_type,
                    was_correct
                FROM llm_decisions
                ORDER BY timestamp DESC
                LIMIT 5
            """)
            
            decisions = []
            for row in rows:
                decision = dict(row)
                
                # Convert confidence to float
                if decision.get("confidence"):
                    decision["confidence"] = float(decision["confidence"])
                
                decisions.append(decision)
            
            # Get LLM stats
            stats_row = await db.fetchrow("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN decision = 'ALLOW' THEN 1 ELSE 0 END) as allow_count,
                    SUM(CASE WHEN decision = 'DENY' THEN 1 ELSE 0 END) as deny_count
                FROM llm_decisions
                WHERE timestamp > NOW() - INTERVAL '24 hours'
            """)
            
            stats = {
                "total_today": stats_row["total"] or 0,
                "allow_today": stats_row["allow_count"] or 0,
                "deny_today": stats_row["deny_count"] or 0,
            }
            
            return {"decisions": decisions, "stats": stats}
        except Exception as e:
            print(f"[Dashboard] Error fetching LLM activity: {e}")
            return {"decisions": [], "stats": {"total_today": 0, "allow_today": 0, "deny_today": 0}}

    data = safe_run_async(_get_data(), default={
        "decisions": [], 
        "stats": {"total_today": 0, "allow_today": 0, "deny_today": 0}
    })
    
    return render_template("partials/llm_activity.html", **data)


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
