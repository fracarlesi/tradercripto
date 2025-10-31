#!/usr/bin/env python3
"""Test sync trades function"""

import sys

from dotenv import load_dotenv

load_dotenv()

from database.connection import SessionLocal
from database.models import Account
from services.hyperliquid_trading_service import hyperliquid_trading_service

db = SessionLocal()
try:
    # Get account 1
    account = db.query(Account).filter(Account.id == 1).first()
    if not account:
        print("Account not found")
        sys.exit(1)

    print(f"Testing sync for account: {account.name}")
    print(f"Hyperliquid service enabled: {hyperliquid_trading_service.enabled}")
    print()

    if hyperliquid_trading_service.enabled:
        # Test sync
        result = hyperliquid_trading_service.sync_account_to_database(db, account)

        print("Sync result:")
        for key, value in result.items():
            print(f"  {key}: {value}")

    else:
        print("Hyperliquid service is not enabled")

finally:
    db.close()
