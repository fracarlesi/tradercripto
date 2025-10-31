#!/usr/bin/env python3
"""Check database balance"""

from sqlalchemy.orm import Session

from database.connection import SessionLocal
from database.models import Account

db: Session = SessionLocal()
try:
    account = db.query(Account).filter(Account.name == "DeepSeek").first()

    if account:
        print("\n📊 Database Balance:")
        print(f"   Initial Capital: ${account.initial_capital:.2f}")
        print(f"   Current Cash: ${account.current_cash:.2f}")
        print(f"   Frozen Cash: ${account.frozen_cash:.2f}\n")
    else:
        print("❌ Account not found!")

finally:
    db.close()
