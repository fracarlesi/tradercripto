from hyperliquid_trader import HyperLiquidTrader
import os
from dotenv import load_dotenv
import json
import time

load_dotenv()

# -------------------------------------------------------------------
#                    CONFIG PANEL
# -------------------------------------------------------------------
TESTNET = True   # True = testnet, False = mainnet (occhio!)
VERBOSE = True    # stampa informazioni extra

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

if not PRIVATE_KEY or not WALLET_ADDRESS:
    raise RuntimeError("PRIVATE_KEY o WALLET_ADDRESS mancanti nel .env")

# -------------------------------------------------------------------
#                    INIT BOT
# -------------------------------------------------------------------
bot = HyperLiquidTrader(
    secret_key=PRIVATE_KEY,
    account_address=WALLET_ADDRESS,
    testnet=TESTNET
)

bot.debug_symbol_limits("BTC")

# Prima del test
print(f"🔧 Leva corrente per BTC: {bot.get_current_leverage('BTC')}x")

# Dopo l'apertura della posizione
status = bot.get_account_status()
if status['open_positions']:
    pos = status['open_positions'][0]
    print(f"📊 Posizione aperta: {pos['size']} {pos['symbol']} con leva {pos.get('leverage', 'N/A')}")

def pretty(obj):
    return json.dumps(obj, indent=2)

print(bot.get_account_status())
print("\n---------------------------------------------------")
print("🔄 Testing HyperLiquidTrader")
print("---------------------------------------------------\n")

# -------------------------------------------------------------------
#                    TEST 1 — OPEN ORDER
# -------------------------------------------------------------------
signal_open = {
    "operation": "close",
    "symbol": "BNB",
    "direction": "long",
    "target_portion_of_balance": 0.05,
    "leverage": 2,
    "reason": "Test apertura posizione long"
}

print("📌 TEST 1 — OPEN ORDER (BTC LONG)")
try:
    result_open = bot.execute_signal(signal_open)
    print("Risultato OPEN:\n", pretty(result_open))
except Exception as e:
    print("❌ ERRORE durante apertura:", e)

print(bot.get_account_status())
# # aspetta un attimo per evitare race
# time.sleep(5)

# # -------------------------------------------------------------------
# #                    TEST 2 — STATUS CHECK
# # -------------------------------------------------------------------
# print("\n📌 TEST 2 — ACCOUNT STATUS")
# try:
#     status = bot.get_account_status()
#     print("Stato account:\n", pretty(status))
# except Exception as e:
#     print("❌ ERRORE durante status check:", e)

# # -------------------------------------------------------------------
# #                    TEST 3 — HOLD (should do nothing)
# # -------------------------------------------------------------------
# signal_hold = {
#     "operation": "hold",
#     "symbol": "BTC",
#     "direction": "long",
#     "target_portion_of_balance": 0.1,
#     "leverage": 1,
#     "reason": "Test hold"
# }

# print("\n📌 TEST 3 — HOLD")
# try:
#     result_hold = bot.execute_signal(signal_hold)
#     print("Risultato HOLD:\n", pretty(result_hold))
# except Exception as e:
#     print("❌ ERRORE durante HOLD:", e)

# # -------------------------------------------------------------------
# #                    TEST 4 — CLOSE POSITION
# # -------------------------------------------------------------------
# signal_close = {
#     "operation": "close",
#     "symbol": "BTC",
#     "direction": "long",
#     "target_portion_of_balance": 0.2,
#     "leverage": 1,
#     "reason": "Test chiusura posizione"
# }

# print("\n📌 TEST 4 — CLOSE ORDER (BTC)")
# try:
#     result_close = bot.execute_signal(signal_close)
#     print("Risultato CLOSE:\n", pretty(result_close))
# except Exception as e:
#     print("❌ ERRORE durante close:", e)

# # -------------------------------------------------------------------
# #                    FINAL STATUS
# # -------------------------------------------------------------------
# time.sleep(2)
# print("\n📌 STATUS FINALE")
# try:
#     final_status = bot.get_account_status()
#     print("Stato finale:\n", pretty(final_status))
# except Exception as e:
#     print("❌ ERRORE durante final status:", e)

# print("\n---------------------------------------------------")
# print("🏁 Testing completato.")
# print("---------------------------------------------------\n")
