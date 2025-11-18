#!/usr/bin/env python3
"""Analyze trading history to identify patterns in losing trades."""

import sqlite3
from datetime import datetime
from collections import defaultdict

def analyze_trades(db_path: str = "/app/data/data.db"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, symbol, side, price, quantity, commission, trade_time, leverage
        FROM trades WHERE account_id = 1 ORDER BY trade_time ASC
    """)

    trades = [dict(row) for row in cursor.fetchall()]
    print(f"Total trades: {len(trades)}")

    # Match BUY/SELL pairs
    open_positions = defaultdict(list)
    completed = []

    for trade in trades:
        symbol = trade["symbol"]
        side = trade["side"].upper()
        price = float(trade["price"])
        quantity = float(trade["quantity"])
        commission = float(trade["commission"]) if trade["commission"] else 0
        leverage = int(trade["leverage"]) if trade["leverage"] else 1

        if side == "BUY":
            open_positions[symbol].append({
                "entry": price, "qty": quantity, "time": trade["trade_time"],
                "lev": leverage, "com": commission
            })
        elif side == "SELL" and open_positions[symbol]:
            pos = open_positions[symbol].pop(0)
            net_pnl = (price - pos["entry"]) * pos["qty"] - pos["com"] - commission
            try:
                entry_dt = datetime.fromisoformat(pos["time"].replace("Z", "+00:00"))
                exit_dt = datetime.fromisoformat(trade["trade_time"].replace("Z", "+00:00"))
                dur = (exit_dt - entry_dt).total_seconds() / 60
            except:
                dur = 0
            completed.append({
                "symbol": symbol, "entry": pos["entry"], "exit": price,
                "qty": pos["qty"], "pnl": net_pnl, "dur": dur,
                "lev": pos["lev"], "com": pos["com"] + commission
            })

    print(f"Completed: {len(completed)}")

    wins = [t for t in completed if t["pnl"] > 0]
    losses = [t for t in completed if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in completed)

    print(f"\nP&L: ${total:.2f}")
    print(f"Win Rate: {len(wins)/len(completed)*100:.1f}% ({len(wins)}/{len(completed)})")
    if wins:
        print(f"Avg Win: ${sum(t['pnl'] for t in wins)/len(wins):.2f}")
    if losses:
        print(f"Avg Loss: ${sum(t['pnl'] for t in losses)/len(losses):.2f}")

    # Worst trades
    print("\n=== 15 WORST TRADES ===")
    for i, t in enumerate(sorted(completed, key=lambda x: x["pnl"])[:15], 1):
        pct = ((t["exit"] - t["entry"]) / t["entry"]) * 100
        print(f"{i:2}. {t['symbol']:8} ${t['pnl']:>6.2f} | {pct:>+5.2f}% | {t['dur']:.0f}min | {t['lev']}x")

    # Best trades
    print("\n=== 10 BEST TRADES ===")
    for i, t in enumerate(reversed(sorted(completed, key=lambda x: x["pnl"])[-10:]), 1):
        pct = ((t["exit"] - t["entry"]) / t["entry"]) * 100
        print(f"{i:2}. {t['symbol']:8} ${t['pnl']:>6.2f} | {pct:>+5.2f}% | {t['dur']:.0f}min | {t['lev']}x")

    # Duration analysis
    print("\n=== DURATION ANALYSIS ===")
    for label, lo, hi in [("<30min", 0, 30), ("30-120min", 30, 120), (">120min", 120, 99999)]:
        g = [t for t in completed if lo <= t["dur"] < hi]
        if g:
            w = len([t for t in g if t["pnl"] > 0])
            print(f"{label}: {len(g)} trades, WR: {w/len(g)*100:.1f}%, P&L: ${sum(t['pnl'] for t in g):.2f}")

    # Symbol analysis
    print("\n=== SYMBOL PERFORMANCE (worst first) ===")
    sym_stats = {}
    for t in completed:
        if t["symbol"] not in sym_stats:
            sym_stats[t["symbol"]] = {"count": 0, "wins": 0, "pnl": 0}
        sym_stats[t["symbol"]]["count"] += 1
        sym_stats[t["symbol"]]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            sym_stats[t["symbol"]]["wins"] += 1

    for sym, s in sorted(sym_stats.items(), key=lambda x: x[1]["pnl"])[:15]:
        wr = s["wins"]/s["count"]*100 if s["count"] > 0 else 0
        print(f"{sym:8} {s['count']:3} trades | WR: {wr:5.1f}% | P&L: ${s['pnl']:>7.2f}")

    # Leverage analysis
    print("\n=== LEVERAGE ANALYSIS ===")
    lev_stats = {}
    for t in completed:
        lev = t["lev"]
        if lev not in lev_stats:
            lev_stats[lev] = {"count": 0, "wins": 0, "pnl": 0}
        lev_stats[lev]["count"] += 1
        lev_stats[lev]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            lev_stats[lev]["wins"] += 1

    for lev in sorted(lev_stats.keys()):
        s = lev_stats[lev]
        wr = s["wins"]/s["count"]*100 if s["count"] > 0 else 0
        print(f"{lev}x: {s['count']:3} trades | WR: {wr:5.1f}% | P&L: ${s['pnl']:>7.2f}")

    # Pattern analysis
    print("\n=== KEY PATTERNS ===")
    very_short = [t for t in completed if t["dur"] < 5]
    if very_short:
        loss_rate = len([t for t in very_short if t["pnl"] <= 0])/len(very_short)*100
        print(f"Very short (<5min): {len(very_short)} trades, Loss rate: {loss_rate:.0f}%")

    tiny = [t for t in completed if abs((t["exit"]-t["entry"])/t["entry"]) < 0.005]
    if tiny:
        print(f"Tiny moves (<0.5%): {len(tiny)} trades (lose to commissions!)")

    avg_com = sum(t["com"] for t in completed) / len(completed)
    print(f"Avg commission/trade: ${avg_com:.3f}")

    conn.close()

if __name__ == "__main__":
    analyze_trades()
