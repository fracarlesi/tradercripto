#!/usr/bin/env python3
"""Fix account balance to match Hyperliquid real balance"""

from sqlalchemy.orm import Session
from database.connection import SessionLocal
from database.models import Account

# Real Hyperliquid balance
REAL_BALANCE = 52.28

db: Session = SessionLocal()
try:
    # Get DeepSeek account
    account = db.query(Account).filter(
        Account.name == "DeepSeek"
    ).first()

    if not account:
        print("❌ DeepSeek account not found!")
        exit(1)

    print(f"\n📊 Before:")
    print(f"   Initial Capital: ${account.initial_capital}")
    print(f"   Current Cash: ${account.current_cash}")

    # Update to real balance
    account.initial_capital = REAL_BALANCE
    account.current_cash = REAL_BALANCE
    account.frozen_cash = 0.0

    db.commit()

    print(f"\n✅ Updated to real Hyperliquid balance!")
    print(f"   New Initial Capital: ${account.initial_capital}")
    print(f"   New Current Cash: ${account.current_cash}")
    print(f"\nNow DeepSeek will trade with the correct $52.28 balance! 🚀")

except Exception as e:
    print(f"❌ Error: {e}")
    db.rollback()
finally:
    db.close()
