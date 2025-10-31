#!/usr/bin/env python3
"""
Script per verificare lo stato di sincronizzazione tra Hyperliquid e il database
"""
import os
from datetime import datetime, timezone
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account as EthAccount
from database.connection import SessionLocal
from database.models import Account, Position, Order, Trade
from dotenv import load_dotenv

load_dotenv()

def check_sync_status():
    print("=" * 80)
    print("VERIFICA SINCRONIZZAZIONE HYPERLIQUID <-> DATABASE")
    print("=" * 80)
    print()

    # Inizializza client Hyperliquid
    private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY")
    if not private_key:
        print("❌ HYPERLIQUID_PRIVATE_KEY non trovata")
        return

    wallet = EthAccount.from_key(private_key)
    info = Info(skip_ws=True)
    exchange = Exchange(
        wallet=wallet,
        base_url=constants.MAINNET_API_URL,
        account_address=wallet.address
    )
    address = wallet.address

    print(f"📍 Wallet Address: {address}")
    print()

    # Database session
    db = SessionLocal()

    try:
        # 1. Verifica SALDO
        print("-" * 80)
        print("1. VERIFICA SALDO")
        print("-" * 80)

        # Saldo da Hyperliquid
        user_state = info.user_state(address)
        hl_balance = float(user_state.get("marginSummary", {}).get("accountValue", 0))
        hl_withdrawable = float(user_state.get("withdrawable", 0))

        print(f"💰 Hyperliquid Balance: ${hl_balance:,.2f}")
        print(f"💸 Hyperliquid Withdrawable: ${hl_withdrawable:,.2f}")

        # Saldo da Database
        account = db.query(Account).filter(Account.is_active == "true").first()
        if not account:
            print("❌ Nessun account attivo trovato nel database")
            return

        account_id = account.id
        db_cash = float(account.current_cash)
        db_frozen = float(account.frozen_cash)

        print(f"🗄️  Database Cash: ${db_cash:,.2f}")
        print(f"🗄️  Database Frozen: ${db_frozen:,.2f}")
        print(f"🗄️  Database Total: ${db_cash + db_frozen:,.2f}")
        print(f"⚖️  Differenza: ${abs(hl_balance - (db_cash + db_frozen)):,.2f}")

        if abs(hl_balance - (db_cash + db_frozen)) > 1.0:
            print("⚠️  SALDO DISALLINEATO!")
        else:
            print("✅ Saldo allineato")

        print()

        # 2. Verifica ORDINI
        print("-" * 80)
        print("2. VERIFICA ORDINI")
        print("-" * 80)

        # Ordini da Hyperliquid (open orders)
        hl_orders = info.open_orders(address)
        print(f"📊 Hyperliquid Open Orders: {len(hl_orders)}")

        if hl_orders:
            print("\nOrdini aperti su Hyperliquid:")
            for order in hl_orders[:10]:  # Mostra solo i primi 10
                coin = order.get("coin", "?")
                side = order.get("side", "?")
                sz = order.get("sz", "?")
                limit_px = order.get("limitPx", "?")
                oid = order.get("oid", "?")
                timestamp = order.get("timestamp", "?")

                print(f"  - {coin}: {side} {sz} @ ${limit_px} (OID: {oid}, Time: {timestamp})")

        # Ordini dal Database (con status != FILLED e != CANCELLED)
        db_orders = db.query(Order).filter(
            Order.account_id == account_id,
            Order.status.notin_(["FILLED", "CANCELLED", "REJECTED"])
        ).order_by(Order.created_at.desc()).limit(20).all()

        print(f"\n🗄️  Database Open Orders: {len(db_orders)}")

        if db_orders:
            print("\nOrdini aperti nel database:")
            for order in db_orders[:10]:
                print(f"  - {order.symbol}: {order.side} {order.quantity} @ ${order.price} (Status: {order.status}, Created: {order.created_at})")

        print()

        # 3. Verifica POSIZIONI
        print("-" * 80)
        print("3. VERIFICA POSIZIONI")
        print("-" * 80)

        # Posizioni da Hyperliquid
        hl_positions = []
        if "assetPositions" in user_state:
            for pos in user_state["assetPositions"]:
                position = pos.get("position", {})
                if float(position.get("szi", 0)) != 0:
                    hl_positions.append(pos)

        print(f"📊 Hyperliquid Open Positions: {len(hl_positions)}")

        if hl_positions:
            print("\nPosizioni aperte su Hyperliquid:")
            for pos in hl_positions[:10]:
                position = pos.get("position", {})
                coin = position.get("coin", "?")
                szi = position.get("szi", "?")
                entry_px = position.get("entryPx", "?")
                unrealized_pnl = position.get("unrealizedPnl", "?")

                print(f"  - {coin}: Size={szi}, Entry=${entry_px}, PnL=${unrealized_pnl}")

        # Posizioni dal Database (con quantity > 0)
        db_positions = db.query(Position).filter(
            Position.account_id == account_id,
            Position.quantity > 0
        ).all()

        print(f"\n🗄️  Database Open Positions: {len(db_positions)}")

        if db_positions:
            print("\nPosizioni aperte nel database:")
            for pos in db_positions[:10]:
                print(f"  - {pos.symbol}: Qty={pos.quantity}, AvgCost=${pos.avg_cost}")

        print()

        # 4. Verifica FILLS recenti (Trades nel database)
        print("-" * 80)
        print("4. VERIFICA FILLS/TRADES RECENTI")
        print("-" * 80)

        # Fills da Hyperliquid
        hl_fills = info.user_fills(address)
        print(f"📊 Hyperliquid Total Fills: {len(hl_fills)}")

        if hl_fills:
            print("\nUltimi fills su Hyperliquid:")
            for fill in hl_fills[:10]:
                coin = fill.get("coin", "?")
                side = fill.get("side", "?")
                sz = fill.get("sz", "?")
                px = fill.get("px", "?")
                time = fill.get("time", "?")
                oid = fill.get("oid", "?")

                # Converti timestamp se presente
                if isinstance(time, int):
                    dt = datetime.fromtimestamp(time / 1000, tz=timezone.utc)
                    time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    time_str = str(time)

                print(f"  - {coin}: {side} {sz} @ ${px} ({time_str}) OID:{oid}")

            # Trova l'ultimo fill
            if hl_fills:
                last_fill = hl_fills[0]
                last_time = last_fill.get("time", 0)
                if isinstance(last_time, int):
                    last_dt = datetime.fromtimestamp(last_time / 1000, tz=timezone.utc)
                    print(f"\n⏰ Ultimo fill Hyperliquid: {last_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")

        # Trades dal Database
        db_trades = db.query(Trade).filter(
            Trade.account_id == account_id
        ).order_by(Trade.trade_time.desc()).limit(20).all()

        print(f"\n🗄️  Database Total Trades: {len(db_trades)}")

        if db_trades:
            print("\nUltimi trade nel database:")
            for trade in db_trades[:10]:
                print(f"  - {trade.symbol}: {trade.side} {trade.quantity} @ ${trade.price} ({trade.trade_time})")

            if db_trades:
                print(f"\n⏰ Ultimo trade Database: {db_trades[0].trade_time}")

        print()
        print("=" * 80)

    finally:
        db.close()

if __name__ == "__main__":
    check_sync_status()
