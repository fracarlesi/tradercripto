#!/usr/bin/env python3
"""Sync account balance with Hyperliquid real balance"""

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy.orm import Session

from database.connection import SessionLocal
from database.models import Account
from services.hyperliquid_trading_service import hyperliquid_trading_service

db: Session = SessionLocal()
try:
    # Get DeepSeek account
    account = db.query(Account).filter(Account.name == "DeepSeek").first()

    if not account:
        print("❌ DeepSeek account not found!")
        exit(1)

    print("\n📊 Current database balance:")
    print(f"   Initial Capital: ${account.initial_capital}")
    print(f"   Current Cash: ${account.current_cash}")

    # Get real balance from Hyperliquid
    if hyperliquid_trading_service.enabled:
        balance = hyperliquid_trading_service.get_account_balance()

        if "error" not in balance:
            real_balance = balance["total_equity"]

            print(f"\n💰 Real Hyperliquid balance: ${real_balance:.2f}")

            # Update database to match reality
            account.initial_capital = real_balance
            account.current_cash = real_balance
            account.frozen_cash = 0.0

            db.commit()

            print("\n✅ Database updated!")
            print(f"   New Initial Capital: ${account.initial_capital}")
            print(f"   New Current Cash: ${account.current_cash}")
        else:
            print(f"❌ Error getting Hyperliquid balance: {balance['error']}")
    else:
        print("❌ Hyperliquid trading not enabled!")

except Exception as e:
    print(f"❌ Error: {e}")
    db.rollback()
finally:
    db.close()
