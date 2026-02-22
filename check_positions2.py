"""
Deep check: dump raw openOrders vs frontendOpenOrders to find the missing kPEPE TP order.
"""
import json
import os
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

address = os.getenv("WALLET_ADDRESS")
base_url = "https://api.hyperliquid.xyz"

print("=== Standard openOrders ===")
resp = requests.post(f"{base_url}/info", json={"type": "openOrders", "user": address})
standard = resp.json()
print(json.dumps(standard, indent=2))

print("\n=== frontendOpenOrders ===")
resp2 = requests.post(f"{base_url}/info", json={"type": "frontendOpenOrders", "user": address})
frontend = resp2.json()
print(json.dumps(frontend, indent=2))

# Check: are there any orders in standard that are NOT in frontend?
standard_oids = {o.get("oid") for o in standard}
frontend_oids = {o.get("oid") for o in frontend}

print(f"\nStandard OIDs: {standard_oids}")
print(f"Frontend OIDs: {frontend_oids}")
print(f"In standard but NOT in frontend: {standard_oids - frontend_oids}")
print(f"In frontend but NOT in standard: {frontend_oids - standard_oids}")
