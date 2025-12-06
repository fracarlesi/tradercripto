#!/usr/bin/env python3
"""Script per chiudere tutte le posizioni aperte e verificare lo stato."""

import os
import time
from hyperliquid_trader import HyperLiquidTrader
from dotenv import load_dotenv

load_dotenv()

trader = HyperLiquidTrader(
    secret_key=os.getenv("PRIVATE_KEY"),
    account_address=os.getenv("WALLET_ADDRESS"),
    testnet=False
)

# Verifica posizioni aperte
status = trader.get_account_status()
balance = status["balance_usd"]
positions = status["open_positions"]

print("=== STATO ATTUALE ===")
print(f"Balance: ${balance:.2f}")
print(f"Posizioni aperte: {len(positions)}")

for pos in positions:
    symbol = pos["symbol"]
    side = pos["side"]
    size = pos["size"]
    entry = pos["entry_price"]
    pnl = pos["pnl_usd"]
    print(f"  {symbol}: {side} {size} @ {entry} | PnL: ${pnl:.2f}")

# Chiudi tutte le posizioni
print()
print("=== CHIUSURA POSIZIONI ===")

for pos in positions:
    symbol = pos["symbol"]
    print(f"Chiudendo {symbol}...")
    result = trader.exchange.market_close(symbol)
    print(f"  Risultato: {result}")

# Attendi e verifica finale
time.sleep(2)

final_status = trader.get_account_status()
final_balance = final_status["balance_usd"]
final_positions = final_status["open_positions"]

print()
print("=== STATO FINALE ===")
print(f"Balance: ${final_balance:.2f}")
print(f"Posizioni aperte: {len(final_positions)}")

if len(final_positions) == 0:
    print("Tutte le posizioni sono state chiuse con successo!")
else:
    print("ATTENZIONE: Alcune posizioni sono ancora aperte:")
    for pos in final_positions:
        print(f"  {pos['symbol']}: {pos['side']} {pos['size']}")
