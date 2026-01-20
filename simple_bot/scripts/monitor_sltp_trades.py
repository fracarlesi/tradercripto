#!/usr/bin/env python3
"""
Monitor bot trades to verify SL/TP placement.
Counts trades that have both SL and TP orders placed.
"""
import subprocess
import re
import time
from datetime import datetime
from collections import defaultdict

TARGET_TRADES = 20
CHECK_INTERVAL = 60  # seconds

def get_bot_logs(lines=500):
    """Fetch recent bot logs from Hetzner."""
    result = subprocess.run(
        ["ssh", "root@<VPS_IP_REDACTED>",
         f"cd /opt/hlquantbot && docker compose logs bot --tail={lines} 2>&1"],
        capture_output=True, text=True, timeout=30
    )
    return result.stdout

def parse_trades_with_sltp(logs: str) -> dict:
    """
    Parse logs to find trades with both SL and TP orders.
    Returns dict of {symbol: {'has_sl': bool, 'has_tp': bool, 'timestamp': str}}
    """
    trades = defaultdict(lambda: {'has_sl': False, 'has_tp': False, 'timestamp': None})

    # Pattern: Position opened: SHORT/LONG SYMBOL SIZE @ PRICE
    position_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Position opened: (SHORT|LONG) (\w+)"

    # Pattern: SL trigger order placed
    sl_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*SL trigger order placed.*@ ([\d.]+)"

    # Pattern: TP order placed
    tp_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*TP order placed.*@ ([\d.]+)"

    # Pattern: Setting TP/SL for SYMBOL
    setting_pattern = r"Setting TP/SL for (\w+): TP=([\d.]+).*SL=([\d.]+)"

    current_trade_symbol = None

    for line in logs.split('\n'):
        # Check for position opened
        match = re.search(position_pattern, line)
        if match:
            timestamp, direction, symbol = match.groups()
            current_trade_symbol = symbol
            trades[f"{timestamp}_{symbol}"] = {
                'symbol': symbol,
                'direction': direction,
                'has_sl': False,
                'has_tp': False,
                'timestamp': timestamp
            }
            continue

        # Check for SL placed right after position
        if current_trade_symbol:
            if "SL trigger order placed" in line:
                # Find the most recent trade for this timing
                for key in reversed(list(trades.keys())):
                    if not trades[key]['has_sl']:
                        trades[key]['has_sl'] = True
                        break

            if "TP order placed" in line:
                for key in reversed(list(trades.keys())):
                    if not trades[key]['has_tp']:
                        trades[key]['has_tp'] = True
                        break

    return trades

def count_complete_trades(trades: dict) -> tuple:
    """Count trades that have both SL and TP."""
    complete = []
    incomplete = []

    for key, trade in trades.items():
        if trade.get('has_sl') and trade.get('has_tp'):
            complete.append(trade)
        elif trade.get('timestamp'):  # Only count actual trades
            incomplete.append(trade)

    return complete, incomplete

def main():
    print("=" * 60)
    print("HLQuantBot - SL/TP Trade Monitor")
    print(f"Target: {TARGET_TRADES} trades with both SL and TP")
    print(f"Started: {datetime.now().isoformat()}")
    print("=" * 60)

    seen_trades = set()
    complete_count = 0

    while complete_count < TARGET_TRADES:
        try:
            logs = get_bot_logs()
            trades = parse_trades_with_sltp(logs)
            complete, incomplete = count_complete_trades(trades)

            # Count new complete trades
            for trade in complete:
                trade_key = f"{trade['timestamp']}_{trade.get('symbol', 'unknown')}"
                if trade_key not in seen_trades:
                    seen_trades.add(trade_key)
                    complete_count += 1
                    print(f"\n✅ [{complete_count}/{TARGET_TRADES}] {trade.get('symbol', 'unknown')} "
                          f"{trade.get('direction', '')} @ {trade['timestamp']}")
                    print(f"   SL: ✓ | TP: ✓")

            # Show progress
            print(f"\r⏳ Progress: {complete_count}/{TARGET_TRADES} | "
                  f"Last check: {datetime.now().strftime('%H:%M:%S')}", end='', flush=True)

            if complete_count >= TARGET_TRADES:
                break

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n⚠️  Monitoring interrupted by user")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            time.sleep(CHECK_INTERVAL)

    print("\n")
    print("=" * 60)
    print("MONITORING COMPLETE")
    print("=" * 60)
    print(f"Total trades with SL+TP: {complete_count}")
    print(f"Target achieved: {'✅ YES' if complete_count >= TARGET_TRADES else '❌ NO'}")
    print(f"Finished: {datetime.now().isoformat()}")

if __name__ == "__main__":
    main()
