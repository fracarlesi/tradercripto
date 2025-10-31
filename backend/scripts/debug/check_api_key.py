#!/usr/bin/env python3
"""Check if DeepSeek account has valid API key"""

from sqlalchemy.orm import Session

from database.connection import SessionLocal
from database.models import Account

db: Session = SessionLocal()
try:
    account = db.query(Account).filter(Account.name == "DeepSeek").first()

    if account:
        print("\n📊 DeepSeek Account Configuration:")
        print(f"   Name: {account.name}")
        print(f"   Account Type: {account.account_type}")
        print(f"   Model: {account.model}")
        print(f"   Base URL: {account.base_url}")
        print(
            f"   API Key: {account.api_key[:20]}..."
            if len(account.api_key) > 20
            else f"   API Key: {account.api_key}"
        )
        print(f"   Is Active: {account.is_active}")
        print(f"   Balance: ${account.current_cash:.2f}")

        # Check if it's a default key
        if account.api_key in ["default-key-please-update-in-settings", "default", "", None]:
            print("\n⚠️  WARNING: Using default API key! AI trading will be skipped.")
            print("   Please update the API key in the settings to enable automated trading.\n")
        else:
            print("\n✅ Valid API key detected! Automated trading is enabled.\n")
    else:
        print("❌ DeepSeek account not found!")

finally:
    db.close()
