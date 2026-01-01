"""
Simple Bot Dashboard - Flask + HTMX
Visualizza lo stato del database in tempo reale
Con P&L per strategia
"""

import os
import sys
from datetime import datetime
from decimal import Decimal

from flask import Flask, render_template_string
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()

app = Flask(__name__)

# Database connection - use local PostgreSQL Docker
DATABASE_URL = "postgresql://trader:trader_password@localhost:5432/trading_db"


def get_db():
    """Get database connection."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def format_decimal(value, decimals=2):
    """Format decimal for display."""
    if value is None:
        return "-"
    return f"{float(value):,.{decimals}f}"


def format_pnl(value):
    """Format PnL with color class."""
    if value is None:
        return "-", ""
    val = float(value)
    color = "text-green-400" if val >= 0 else "text-red-400"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.4f}", color


# HTML Template with TailwindCSS
TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Multi-Strategy Bot Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <style>
        body { background-color: #0f172a; }
        .card { background-color: #1e293b; border-radius: 0.5rem; }
    </style>
</head>
<body class="text-gray-200 p-4">
    <div class="max-w-7xl mx-auto">
        <h1 class="text-2xl font-bold mb-4 text-white">Multi-Strategy Bot Dashboard</h1>
        <p class="text-gray-400 mb-6">Database: {{ db_url[:50] }}...</p>

        <!-- Auto-refresh every 5 seconds -->
        <div hx-get="/data" hx-trigger="every 5s" hx-swap="innerHTML">
            {% include 'data_content' %}
        </div>
    </div>
</body>
</html>
"""

DATA_TEMPLATE = """
<!-- Last Update -->
<p class="text-sm text-gray-500 mb-4">Last update: {{ now }}</p>

<!-- Strategy P&L Summary -->
<div class="card p-4 mb-4">
    <h2 class="text-lg font-semibold mb-3 text-pink-400">Strategy P&L Summary</h2>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
        {% for strat in strategy_pnl %}
        <div class="bg-gray-800 rounded-lg p-4">
            <div class="flex justify-between items-center mb-2">
                <h3 class="text-md font-bold text-white uppercase">{{ strat.strategy }}</h3>
                <span class="text-xs px-2 py-1 rounded {{ 'bg-green-900 text-green-300' if strat.has_position else 'bg-gray-700 text-gray-400' }}">
                    {{ 'Active' if strat.has_position else 'Idle' }}
                </span>
            </div>
            <div class="space-y-1 text-sm">
                <div class="flex justify-between">
                    <span class="text-gray-400">Symbol:</span>
                    <span class="font-mono">{{ strat.symbol or '-' }}</span>
                </div>
                <div class="flex justify-between">
                    <span class="text-gray-400">Trades:</span>
                    <span>{{ strat.trades }}</span>
                </div>
                <div class="flex justify-between">
                    <span class="text-gray-400">Win Rate:</span>
                    <span class="{{ 'text-green-400' if strat.win_rate >= 50 else 'text-red-400' }}">{{ strat.win_rate }}%</span>
                </div>
                <div class="flex justify-between border-t border-gray-700 pt-1 mt-1">
                    <span class="text-gray-400">Realized P&L:</span>
                    <span class="font-bold {{ strat.realized_color }}">${{ strat.realized_pnl }}</span>
                </div>
                <div class="flex justify-between">
                    <span class="text-gray-400">Unrealized:</span>
                    <span class="{{ strat.unrealized_color }}">${{ strat.unrealized_pnl }}</span>
                </div>
            </div>
        </div>
        {% endfor %}
    </div>
    <!-- Total -->
    <div class="bg-gray-900 rounded-lg p-4">
        <div class="flex justify-between items-center">
            <h3 class="text-lg font-bold text-white">TOTAL P&L</h3>
            <span class="text-2xl font-bold {{ total_pnl_color }}">${{ total_pnl }}</span>
        </div>
    </div>
</div>

<!-- DeepSeek Optimization Panel -->
{% if optimization.enabled %}
<div class="card p-4 mb-4">
    <h2 class="text-lg font-semibold mb-3 text-purple-400">DeepSeek Auto-Optimization</h2>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <!-- Current Parameters -->
        <div class="bg-gray-800 rounded-lg p-4">
            <h3 class="text-md font-bold text-white mb-2">Current Parameters (v{{ optimization.current_version }})</h3>
            <div class="space-y-1 text-sm">
                <div class="flex justify-between">
                    <span class="text-gray-400">Source:</span>
                    <span class="px-2 py-0.5 rounded text-xs
                        {% if optimization.version_source == 'llm' %}bg-purple-900 text-purple-300
                        {% elif optimization.version_source == 'manual' %}bg-blue-900 text-blue-300
                        {% elif optimization.version_source == 'rollback' %}bg-orange-900 text-orange-300
                        {% else %}bg-gray-700 text-gray-300{% endif %}">
                        {{ optimization.version_source or 'unknown' }}
                    </span>
                </div>
                <div class="flex justify-between">
                    <span class="text-gray-400">Applied:</span>
                    <span>{{ optimization.applied_at or '-' }}</span>
                </div>
                <div class="flex justify-between border-t border-gray-700 pt-1 mt-1">
                    <span class="text-gray-400">Take Profit:</span>
                    <span class="text-green-400 font-mono">{{ optimization.tp_pct|default(1.0) }}%</span>
                </div>
                <div class="flex justify-between">
                    <span class="text-gray-400">Stop Loss:</span>
                    <span class="text-red-400 font-mono">{{ optimization.sl_pct|default(0.5) }}%</span>
                </div>
                <div class="flex justify-between">
                    <span class="text-gray-400">Position Size:</span>
                    <span class="font-mono">${{ optimization.position_size|default(100) }}</span>
                </div>
                <div class="flex justify-between">
                    <span class="text-gray-400">Leverage:</span>
                    <span class="font-mono">{{ optimization.leverage|default(5) }}x</span>
                </div>
            </div>
        </div>

        <!-- Recent Optimization Runs -->
        <div class="bg-gray-800 rounded-lg p-4">
            <h3 class="text-md font-bold text-white mb-2">Recent Optimization Runs</h3>
            {% if optimization.recent_runs %}
            <div class="space-y-2 text-xs">
                {% for run in optimization.recent_runs %}
                <div class="flex items-center justify-between border-b border-gray-700 pb-1">
                    <div class="flex items-center gap-2">
                        <span class="text-gray-500">{{ run.time }}</span>
                        <span class="{{ run.status_color }}">{{ run.status }}</span>
                    </div>
                    <div class="flex items-center gap-2">
                        <span class="text-gray-400">{{ run.confidence }}</span>
                        {% if run.applied_version %}
                        <span class="text-green-400">→ v{{ run.applied_version }}</span>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <p class="text-gray-500 text-sm">No optimization runs yet</p>
            {% endif %}
        </div>
    </div>

    <!-- Parameter Version History -->
    {% if optimization.param_history %}
    <div class="mt-4">
        <h3 class="text-md font-bold text-white mb-2">Parameter Version History</h3>
        <div class="overflow-x-auto">
            <table class="w-full text-xs">
                <thead class="text-gray-400 border-b border-gray-700">
                    <tr>
                        <th class="text-left py-1">Ver</th>
                        <th class="text-left py-1">Source</th>
                        <th class="text-left py-1">Applied</th>
                        <th class="text-right py-1">TP/SL</th>
                        <th class="text-right py-1">Hours</th>
                        <th class="text-right py-1">Trades</th>
                        <th class="text-right py-1">Win%</th>
                        <th class="text-right py-1">P&L</th>
                    </tr>
                </thead>
                <tbody>
                    {% for v in optimization.param_history %}
                    <tr class="border-b border-gray-800 {{ 'opacity-50' if v.reverted else '' }}">
                        <td class="py-1 font-mono">v{{ v.version_id }}</td>
                        <td class="py-1">
                            <span class="px-1 py-0.5 rounded {{ v.source_color }}">{{ v.source }}</span>
                        </td>
                        <td class="py-1 text-gray-400">{{ v.created_at }}</td>
                        <td class="py-1 text-right font-mono">{{ v.tp_pct }}/{{ v.sl_pct }}</td>
                        <td class="py-1 text-right">{{ v.hours }}h</td>
                        <td class="py-1 text-right">{{ v.trades }}</td>
                        <td class="py-1 text-right">{{ v.win_rate }}</td>
                        <td class="py-1 text-right font-mono {{ v.pnl_color }}">{{ v.pnl }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
    {% endif %}
</div>
{% endif %}

<!-- Account Card -->
<div class="card p-4 mb-4">
    <h2 class="text-lg font-semibold mb-3 text-blue-400">Live Account</h2>
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div>
            <p class="text-gray-400 text-sm">Equity</p>
            <p class="text-xl font-bold text-white">${{ account.equity }}</p>
        </div>
        <div>
            <p class="text-gray-400 text-sm">Available</p>
            <p class="text-xl font-bold text-white">${{ account.available }}</p>
        </div>
        <div>
            <p class="text-gray-400 text-sm">Margin Used</p>
            <p class="text-xl font-bold text-yellow-400">${{ account.margin }}</p>
        </div>
        <div>
            <p class="text-gray-400 text-sm">Unrealized PnL</p>
            <p class="text-xl font-bold {{ account.pnl_color }}">${{ account.pnl }}</p>
        </div>
    </div>
</div>

<!-- Positions -->
<div class="card p-4 mb-4">
    <h2 class="text-lg font-semibold mb-3 text-green-400">Live Positions ({{ positions|length }})</h2>
    {% if positions %}
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead class="text-gray-400 border-b border-gray-700">
                <tr>
                    <th class="text-left py-2">Symbol</th>
                    <th class="text-left py-2">Side</th>
                    <th class="text-right py-2">Size</th>
                    <th class="text-right py-2">Entry</th>
                    <th class="text-right py-2">Mark</th>
                    <th class="text-right py-2">PnL</th>
                    <th class="text-right py-2">Lev</th>
                </tr>
            </thead>
            <tbody>
                {% for pos in positions %}
                <tr class="border-b border-gray-800">
                    <td class="py-2 font-mono">{{ pos.symbol }}</td>
                    <td class="py-2 {{ 'text-green-400' if pos.side == 'LONG' else 'text-red-400' }}">{{ pos.side }}</td>
                    <td class="py-2 text-right font-mono">{{ pos.size }}</td>
                    <td class="py-2 text-right font-mono">${{ pos.entry }}</td>
                    <td class="py-2 text-right font-mono">${{ pos.mark }}</td>
                    <td class="py-2 text-right font-mono {{ pos.pnl_color }}">${{ pos.pnl }}</td>
                    <td class="py-2 text-right">{{ pos.leverage }}x</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    <p class="text-gray-500">No open positions</p>
    {% endif %}
</div>

<!-- Orders -->
<div class="card p-4 mb-4">
    <h2 class="text-lg font-semibold mb-3 text-yellow-400">Live Orders ({{ orders|length }})</h2>
    {% if orders %}
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead class="text-gray-400 border-b border-gray-700">
                <tr>
                    <th class="text-left py-2">Order ID</th>
                    <th class="text-left py-2">Symbol</th>
                    <th class="text-left py-2">Side</th>
                    <th class="text-right py-2">Size</th>
                    <th class="text-right py-2">Price</th>
                    <th class="text-left py-2">Type</th>
                </tr>
            </thead>
            <tbody>
                {% for order in orders %}
                <tr class="border-b border-gray-800">
                    <td class="py-2 font-mono text-xs">{{ order.order_id }}</td>
                    <td class="py-2 font-mono">{{ order.symbol }}</td>
                    <td class="py-2 {{ 'text-green-400' if order.side == 'BUY' else 'text-red-400' }}">{{ order.side }}</td>
                    <td class="py-2 text-right font-mono">{{ order.size }}</td>
                    <td class="py-2 text-right font-mono">${{ order.price }}</td>
                    <td class="py-2">{{ order.order_type }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    <p class="text-gray-500">No open orders</p>
    {% endif %}
</div>

<!-- Recent Trades by Strategy -->
<div class="card p-4 mb-4">
    <h2 class="text-lg font-semibold mb-3 text-cyan-400">Recent Trades ({{ trades|length }})</h2>
    {% if trades %}
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead class="text-gray-400 border-b border-gray-700">
                <tr>
                    <th class="text-left py-2">Time</th>
                    <th class="text-left py-2">Strategy</th>
                    <th class="text-left py-2">Symbol</th>
                    <th class="text-left py-2">Side</th>
                    <th class="text-right py-2">Size</th>
                    <th class="text-right py-2">Entry</th>
                    <th class="text-right py-2">Exit</th>
                    <th class="text-right py-2">Net PnL</th>
                    <th class="text-left py-2">Status</th>
                </tr>
            </thead>
            <tbody>
                {% for trade in trades %}
                <tr class="border-b border-gray-800">
                    <td class="py-2 text-xs">{{ trade.entry_time }}</td>
                    <td class="py-2">
                        <span class="px-2 py-1 rounded text-xs
                            {% if trade.strategy == 'momentum' %}bg-purple-900 text-purple-300
                            {% elif trade.strategy == 'mean_reversion' %}bg-blue-900 text-blue-300
                            {% elif trade.strategy == 'breakout' %}bg-orange-900 text-orange-300
                            {% else %}bg-gray-700 text-gray-300{% endif %}">
                            {{ trade.strategy or 'unknown' }}
                        </span>
                    </td>
                    <td class="py-2 font-mono">{{ trade.symbol }}</td>
                    <td class="py-2 {{ 'text-green-400' if trade.side == 'LONG' else 'text-red-400' }}">{{ trade.side }}</td>
                    <td class="py-2 text-right font-mono">{{ trade.size }}</td>
                    <td class="py-2 text-right font-mono">${{ trade.entry }}</td>
                    <td class="py-2 text-right font-mono">{{ trade.exit if trade.exit else '-' }}</td>
                    <td class="py-2 text-right font-mono {{ trade.pnl_color }}">{{ trade.pnl }}</td>
                    <td class="py-2 {{ 'text-gray-500' if trade.is_closed else 'text-yellow-400' }}">{{ 'Closed' if trade.is_closed else 'Open' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    <p class="text-gray-500">No trades</p>
    {% endif %}
</div>

<!-- Signals -->
<div class="card p-4 mb-4">
    <h2 class="text-lg font-semibold mb-3 text-purple-400">Recent Signals ({{ signals|length }})</h2>
    {% if signals %}
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead class="text-gray-400 border-b border-gray-700">
                <tr>
                    <th class="text-left py-2">Time</th>
                    <th class="text-left py-2">Strategy</th>
                    <th class="text-left py-2">Symbol</th>
                    <th class="text-left py-2">Side</th>
                    <th class="text-left py-2">Type</th>
                    <th class="text-left py-2">Executed</th>
                    <th class="text-right py-2">Price</th>
                </tr>
            </thead>
            <tbody>
                {% for sig in signals %}
                <tr class="border-b border-gray-800">
                    <td class="py-2 text-xs">{{ sig.timestamp }}</td>
                    <td class="py-2">
                        <span class="px-2 py-1 rounded text-xs
                            {% if sig.strategy == 'momentum' %}bg-purple-900 text-purple-300
                            {% elif sig.strategy == 'mean_reversion' %}bg-blue-900 text-blue-300
                            {% elif sig.strategy == 'breakout' %}bg-orange-900 text-orange-300
                            {% else %}bg-gray-700 text-gray-300{% endif %}">
                            {{ sig.strategy or 'unknown' }}
                        </span>
                    </td>
                    <td class="py-2 font-mono">{{ sig.symbol }}</td>
                    <td class="py-2 {{ 'text-green-400' if sig.side == 'BUY' else 'text-red-400' }}">{{ sig.side }}</td>
                    <td class="py-2">{{ sig.signal_type }}</td>
                    <td class="py-2 {{ 'text-green-400' if sig.executed else 'text-gray-500' }}">{{ 'Yes' if sig.executed else 'No' }}</td>
                    <td class="py-2 text-right font-mono">{{ sig.exec_price if sig.exec_price else '-' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    <p class="text-gray-500">No signals</p>
    {% endif %}
</div>

<!-- Daily Summary -->
<div class="card p-4">
    <h2 class="text-lg font-semibold mb-3 text-orange-400">Daily Summary</h2>
    {% if daily %}
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead class="text-gray-400 border-b border-gray-700">
                <tr>
                    <th class="text-left py-2">Date</th>
                    <th class="text-right py-2">Trades</th>
                    <th class="text-right py-2">Wins</th>
                    <th class="text-right py-2">Losses</th>
                    <th class="text-right py-2">Win Rate</th>
                    <th class="text-right py-2">Net PnL</th>
                </tr>
            </thead>
            <tbody>
                {% for day in daily %}
                <tr class="border-b border-gray-800">
                    <td class="py-2">{{ day.date }}</td>
                    <td class="py-2 text-right">{{ day.trades }}</td>
                    <td class="py-2 text-right text-green-400">{{ day.wins }}</td>
                    <td class="py-2 text-right text-red-400">{{ day.losses }}</td>
                    <td class="py-2 text-right">{{ day.win_rate }}%</td>
                    <td class="py-2 text-right font-mono {{ day.pnl_color }}">${{ day.pnl }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    <p class="text-gray-500">No daily data</p>
    {% endif %}
</div>
"""


def get_optimization_data(cur):
    """Get optimization status and history."""
    data = {
        "enabled": False,
        "current_version": None,
        "version_source": None,
        "applied_at": None,
        "recent_runs": [],
        "param_history": []
    }

    try:
        # Check if optimization tables exist
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'parameter_versions'
            )
        """)
        if not cur.fetchone()['exists']:
            return data

        data["enabled"] = True

        # Get current active version
        cur.execute("""
            SELECT version_id, source, applied_at, tp_pct, sl_pct, position_size_usd, leverage
            FROM parameter_versions
            WHERE is_active = TRUE
            ORDER BY created_at DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            data["current_version"] = row['version_id']
            data["version_source"] = row['source']
            data["applied_at"] = row['applied_at'].strftime("%Y-%m-%d %H:%M") if row['applied_at'] else None
            data["tp_pct"] = float(row['tp_pct']) * 100
            data["sl_pct"] = float(row['sl_pct']) * 100
            data["position_size"] = float(row['position_size_usd'])
            data["leverage"] = row['leverage']

        # Get recent optimization runs
        cur.execute("""
            SELECT run_id, started_at, status, confidence_score, reasoning_summary, applied_version
            FROM optimization_runs
            ORDER BY started_at DESC
            LIMIT 5
        """)
        for row in cur.fetchall():
            status_color = {
                "success": "text-green-400",
                "skipped": "text-yellow-400",
                "failed": "text-red-400",
                "pending": "text-blue-400",
                "rolled_back": "text-orange-400"
            }.get(row['status'], "text-gray-400")

            data["recent_runs"].append({
                "run_id": row['run_id'],
                "time": row['started_at'].strftime("%H:%M") if row['started_at'] else "",
                "status": row['status'],
                "status_color": status_color,
                "confidence": f"{row['confidence_score']*100:.0f}%" if row['confidence_score'] else "-",
                "reasoning": (row['reasoning_summary'][:80] + "...") if row['reasoning_summary'] and len(row['reasoning_summary']) > 80 else row['reasoning_summary'],
                "applied_version": row['applied_version']
            })

        # Get parameter history with performance
        cur.execute("""
            SELECT version_id, source, created_at, tp_pct, sl_pct,
                   hours_active, total_trades, win_rate, total_pnl, was_reverted
            FROM parameter_performance
            ORDER BY created_at DESC
            LIMIT 5
        """)
        for row in cur.fetchall():
            pnl = float(row['total_pnl']) if row['total_pnl'] else 0
            pnl_color = "text-green-400" if pnl >= 0 else "text-red-400"

            source_color = {
                "llm": "bg-purple-900 text-purple-300",
                "manual": "bg-blue-900 text-blue-300",
                "rollback": "bg-orange-900 text-orange-300",
                "initial": "bg-gray-700 text-gray-300"
            }.get(row['source'], "bg-gray-700 text-gray-300")

            data["param_history"].append({
                "version_id": row['version_id'],
                "source": row['source'],
                "source_color": source_color,
                "created_at": row['created_at'].strftime("%d/%m %H:%M") if row['created_at'] else "",
                "tp_pct": f"{float(row['tp_pct'])*100:.2f}%",
                "sl_pct": f"{float(row['sl_pct'])*100:.2f}%",
                "hours": row['hours_active'] or 0,
                "trades": row['total_trades'] or 0,
                "win_rate": f"{row['win_rate']:.0f}%" if row['win_rate'] else "-",
                "pnl": f"${pnl:+.2f}",
                "pnl_color": pnl_color,
                "reverted": row['was_reverted']
            })

    except Exception as e:
        print(f"Optimization data error: {e}")

    return data


def get_strategy_pnl(cur):
    """Get P&L breakdown by strategy."""
    strategies = ["momentum", "mean_reversion", "breakout"]
    results = []

    for strategy in strategies:
        # Get realized P&L (closed trades)
        cur.execute("""
            SELECT
                COUNT(*) as trades,
                COUNT(*) FILTER (WHERE net_pnl > 0) as wins,
                COALESCE(SUM(net_pnl), 0) as realized_pnl
            FROM trades
            WHERE strategy = %s AND is_closed = TRUE
        """, (strategy,))
        row = cur.fetchone()

        trades = row['trades'] if row else 0
        wins = row['wins'] if row else 0
        realized_pnl = float(row['realized_pnl']) if row and row['realized_pnl'] else 0

        # Get unrealized P&L (open trades)
        cur.execute("""
            SELECT symbol FROM trades
            WHERE strategy = %s AND is_closed = FALSE
            ORDER BY entry_time DESC LIMIT 1
        """, (strategy,))
        open_trade = cur.fetchone()

        unrealized_pnl = 0
        symbol = None
        has_position = False

        if open_trade:
            symbol = open_trade['symbol']
            # Get position for this symbol
            cur.execute("""
                SELECT unrealized_pnl FROM live_positions WHERE symbol = %s
            """, (symbol,))
            pos = cur.fetchone()
            if pos:
                unrealized_pnl = float(pos['unrealized_pnl']) if pos['unrealized_pnl'] else 0
                has_position = True

        win_rate = round(wins / trades * 100, 1) if trades > 0 else 0

        realized_color = "text-green-400" if realized_pnl >= 0 else "text-red-400"
        unrealized_color = "text-green-400" if unrealized_pnl >= 0 else "text-red-400"

        results.append({
            "strategy": strategy,
            "symbol": symbol,
            "trades": trades,
            "wins": wins,
            "win_rate": win_rate,
            "realized_pnl": f"{realized_pnl:+.2f}",
            "realized_color": realized_color,
            "unrealized_pnl": f"{unrealized_pnl:+.2f}",
            "unrealized_color": unrealized_color,
            "has_position": has_position
        })

    return results


def get_data():
    """Fetch all data from database."""
    data = {
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "account": {"equity": "0", "available": "0", "margin": "0", "pnl": "0", "pnl_color": ""},
        "positions": [],
        "orders": [],
        "signals": [],
        "trades": [],
        "daily": [],
        "strategy_pnl": [],
        "total_pnl": "0.00",
        "total_pnl_color": "",
        "optimization": {}
    }

    try:
        conn = get_db()
        cur = conn.cursor()

        # Optimization data
        data["optimization"] = get_optimization_data(cur)

        # Strategy P&L
        data["strategy_pnl"] = get_strategy_pnl(cur)

        # Calculate total P&L
        total = sum(
            float(s["realized_pnl"].replace("+", "")) + float(s["unrealized_pnl"].replace("+", ""))
            for s in data["strategy_pnl"]
        )
        data["total_pnl"] = f"{total:+.2f}"
        data["total_pnl_color"] = "text-green-400" if total >= 0 else "text-red-400"

        # Account
        cur.execute("SELECT * FROM live_account LIMIT 1")
        row = cur.fetchone()
        if row:
            pnl_val, pnl_color = format_pnl(row['unrealized_pnl'])
            data["account"] = {
                "equity": format_decimal(row['equity']),
                "available": format_decimal(row['available_balance']),
                "margin": format_decimal(row['margin_used']),
                "pnl": pnl_val,
                "pnl_color": pnl_color
            }

        # Positions
        cur.execute("SELECT * FROM live_positions ORDER BY symbol")
        for row in cur.fetchall():
            pnl_val, pnl_color = format_pnl(row['unrealized_pnl'])
            data["positions"].append({
                "symbol": row['symbol'],
                "side": row['side'],
                "size": format_decimal(row['size'], 4),
                "entry": format_decimal(row['entry_price']),
                "mark": format_decimal(row['mark_price']),
                "pnl": pnl_val,
                "pnl_color": pnl_color,
                "leverage": row['leverage']
            })

        # Orders
        cur.execute("SELECT * FROM live_orders ORDER BY created_at DESC LIMIT 20")
        for row in cur.fetchall():
            data["orders"].append({
                "order_id": row['order_id'],
                "symbol": row['symbol'],
                "side": row['side'],
                "size": format_decimal(row['size'], 4),
                "price": format_decimal(row['price']),
                "order_type": row['order_type']
            })

        # Signals
        cur.execute("SELECT * FROM signals ORDER BY timestamp DESC LIMIT 20")
        for row in cur.fetchall():
            data["signals"].append({
                "timestamp": row['timestamp'].strftime("%H:%M:%S") if row['timestamp'] else "",
                "symbol": row['symbol'],
                "strategy": row['strategy'],
                "side": row['side'],
                "signal_type": row['signal_type'],
                "executed": row['executed'],
                "exec_price": f"${format_decimal(row['execution_price'])}" if row['execution_price'] else None
            })

        # Trades
        cur.execute("SELECT * FROM trades ORDER BY entry_time DESC LIMIT 20")
        for row in cur.fetchall():
            pnl_val, pnl_color = format_pnl(row['net_pnl']) if row['net_pnl'] else ("-", "")
            data["trades"].append({
                "entry_time": row['entry_time'].strftime("%d/%m %H:%M") if row['entry_time'] else "",
                "symbol": row['symbol'],
                "strategy": row['strategy'],
                "side": row['side'],
                "size": format_decimal(row['size'], 4),
                "entry": format_decimal(row['entry_price']),
                "exit": f"${format_decimal(row['exit_price'])}" if row['exit_price'] else None,
                "pnl": pnl_val,
                "pnl_color": pnl_color,
                "is_closed": row['is_closed']
            })

        # Daily Summary
        cur.execute("SELECT * FROM daily_summary ORDER BY date DESC LIMIT 7")
        for row in cur.fetchall():
            pnl_val, pnl_color = format_pnl(row['net_pnl'])
            win_rate = (row['winning_trades'] / row['total_trades'] * 100) if row['total_trades'] > 0 else 0
            data["daily"].append({
                "date": row['date'].strftime("%Y-%m-%d") if row['date'] else "",
                "trades": row['total_trades'],
                "wins": row['winning_trades'],
                "losses": row['losing_trades'],
                "win_rate": f"{win_rate:.1f}",
                "pnl": pnl_val,
                "pnl_color": pnl_color
            })

        conn.close()
    except Exception as e:
        print(f"Database error: {e}")

    return data


@app.route("/")
def index():
    """Main dashboard page."""
    data = get_data()
    return render_template_string(
        TEMPLATE.replace("{% include 'data_content' %}", DATA_TEMPLATE),
        db_url=DATABASE_URL,
        **data
    )


@app.route("/data")
def data():
    """HTMX endpoint for data refresh."""
    data = get_data()
    return render_template_string(DATA_TEMPLATE, **data)


if __name__ == "__main__":
    print("=" * 60)
    print("Multi-Strategy Bot Dashboard")
    print("=" * 60)
    print(f"Database: {DATABASE_URL[:50]}...")
    print(f"Open: http://localhost:5555")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5555, debug=True)
