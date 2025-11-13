#!/usr/bin/env python3
"""Production system status checker"""
import sqlite3
from datetime import datetime, timedelta

def main():
    conn = sqlite3.connect("/app/data/data.db")
    cursor = conn.cursor()

    print("=" * 60)
    print("TRADING SYSTEM STATUS REPORT")
    print("=" * 60)

    # Portfolio value
    print("\n📊 CURRENT PORTFOLIO")
    cursor.execute("""
        SELECT total_assets, total_margin_used, withdrawable
        FROM portfolio_snapshots
        ORDER BY snapshot_time DESC
        LIMIT 1
    """)
    row = cursor.fetchone()
    if row:
        print(f"Total Assets: ${float(row[0]):.2f}")
        print(f"Margin Used: ${float(row[1]):.2f}")
        print(f"Withdrawable: ${float(row[2]):.2f}")

    # Recent orders
    print("\n📈 LAST 10 ORDERS")
    cursor.execute("""
        SELECT
            datetime(created_at),
            symbol,
            side,
            quantity,
            price,
            status
        FROM orders
        ORDER BY created_at DESC
        LIMIT 10
    """)
    for row in cursor.fetchall():
        emoji = "🟢" if row[2] == "BUY" else "🔴"
        value = float(row[3]) * float(row[4])
        print(f"{emoji} {row[0]} | {row[1]:6} | {row[2]:4} {float(row[3]):8.2f} @ ${float(row[4]):8.4f} = ${value:8.2f} | {row[5]}")

    # Statistics
    print("\n📊 TRADING STATISTICS")
    cursor.execute("SELECT COUNT(*) FROM orders WHERE status='FILLED'")
    print(f"Total filled orders: {cursor.fetchone()[0]}")

    cursor.execute("SELECT COUNT(*) FROM orders WHERE created_at >= datetime('now', '-24 hours')")
    print(f"Orders (24h): {cursor.fetchone()[0]}")

    cursor.execute("SELECT COUNT(*) FROM orders WHERE created_at >= datetime('now', '-1 hour')")
    print(f"Orders (1h): {cursor.fetchone()[0]}")

    cursor.execute("SELECT COUNT(DISTINCT symbol) FROM orders WHERE created_at >= datetime('now', '-24 hours')")
    print(f"Symbols traded (24h): {cursor.fetchone()[0]}")

    # AI Learning
    print("\n🧠 AI LEARNING SYSTEM")
    cursor.execute("SELECT COUNT(*) FROM decision_snapshots")
    print(f"Total AI decisions: {cursor.fetchone()[0]}")

    cursor.execute("SELECT COUNT(*) FROM decision_snapshots WHERE exit_price_24h IS NOT NULL")
    print(f"Counterfactuals calculated: {cursor.fetchone()[0]}")

    # Timeline
    print("\n⏰ ACTIVITY TIMELINE")
    cursor.execute("SELECT datetime(created_at) FROM orders ORDER BY created_at DESC LIMIT 1")
    row = cursor.fetchone()
    if row:
        print(f"Last order: {row[0]}")

    cursor.execute("SELECT datetime(snapshot_time) FROM portfolio_snapshots ORDER BY snapshot_time DESC LIMIT 1")
    row = cursor.fetchone()
    if row:
        print(f"Last snapshot: {row[0]}")

    cursor.execute("SELECT datetime(timestamp) FROM decision_snapshots ORDER BY timestamp DESC LIMIT 1")
    row = cursor.fetchone()
    if row:
        print(f"Last AI decision: {row[0]}")

    # Performance last 24h
    print("\n💰 PERFORMANCE (Last 24 hours)")
    cursor.execute("""
        SELECT
            MIN(total_assets) as min_assets,
            MAX(total_assets) as max_assets
        FROM portfolio_snapshots
        WHERE snapshot_time >= datetime('now', '-24 hours')
    """)
    row = cursor.fetchone()
    if row and row[0]:
        min_val = float(row[0])
        max_val = float(row[1])
        variation = max_val - min_val
        variation_pct = (variation / min_val) * 100
        print(f"Min: ${min_val:.2f}")
        print(f"Max: ${max_val:.2f}")
        print(f"Variation: ${variation:.2f} ({variation_pct:+.2f}%)")

    conn.close()
    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()
