"""Check recent fills to see what happened with kPEPE."""
import json
import os
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

address = os.getenv("WALLET_ADDRESS")
base_url = "https://api.hyperliquid.xyz"

resp = requests.post(f"{base_url}/info", json={"type": "userFills", "user": address})
fills = resp.json()

# Show last 10 fills
print("=== Last 10 fills ===")
for f in fills[:10]:
    coin = f.get("coin")
    side = "BUY" if f.get("side", "").upper() == "B" else "SELL"
    px = f.get("px")
    sz = f.get("sz")
    closed_pnl = f.get("closedPnl", "0")
    time_ms = f.get("time", 0)
    from datetime import datetime
    ts = datetime.fromtimestamp(time_ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if time_ms else "?"
    print(f"  [{coin}] {side} {sz} @ {px} | closedPnl={closed_pnl} | {ts}")
