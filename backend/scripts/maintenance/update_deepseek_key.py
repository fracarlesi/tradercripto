#!/usr/bin/env python3
"""Update DeepSeek API key in database"""

from sqlalchemy.orm import Session

from database.connection import SessionLocal
from database.models import Account

# Your DeepSeek API key
NEW_API_KEY = "sk-11361edf6ed1405fbef0b980ac11f164"

db: Session = SessionLocal()
try:
    # Find DeepSeek account
    account = db.query(Account).filter(Account.name == "DeepSeek").first()

    if account:
        print(f"Found account: {account.name}")
        print(f"Old API key: {account.api_key[:20]}...")

        # Update API key
        account.api_key = NEW_API_KEY
        db.commit()

        print("✅ API key updated successfully!")
        print(f"New API key: {NEW_API_KEY[:20]}...")
    else:
        print("❌ DeepSeek account not found!")

except Exception as e:
    print(f"❌ Error: {e}")
    db.rollback()
finally:
    db.close()
