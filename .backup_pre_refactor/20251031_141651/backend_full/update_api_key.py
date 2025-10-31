#!/usr/bin/env python3
"""Script to update DeepSeek API key for the default account"""

import sys
from sqlalchemy.orm import Session
from database.connection import SessionLocal
from database.models import Account

def update_api_key(api_key: str):
    """Update the API key for the default DeepSeek account"""
    db: Session = SessionLocal()
    try:
        # Find the default account
        account = db.query(Account).filter(
            Account.name == "DeepSeek"
        ).first()

        if not account:
            # Fallback: try to find any AI account
            account = db.query(Account).filter(
                Account.account_type == "AI"
            ).first()

        if account:
            account.api_key = api_key
            db.commit()
            print(f"✅ API key updated successfully for account: {account.name}")
            print(f"   Model: {account.model}")
            print(f"   Base URL: {account.base_url}")
            print(f"   Initial Capital: €{account.initial_capital}")
            print(f"   Current Cash: €{account.current_cash}")
            return True
        else:
            print("❌ No AI account found in database")
            return False

    except Exception as e:
        print(f"❌ Error updating API key: {e}")
        db.rollback()
        return False
    finally:
        db.close()

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python update_api_key.py <api_key>")
        sys.exit(1)

    api_key = sys.argv[1]
    success = update_api_key(api_key)
    sys.exit(0 if success else 1)
