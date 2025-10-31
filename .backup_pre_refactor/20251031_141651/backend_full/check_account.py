#!/usr/bin/env python3
"""Script to check account status"""

from sqlalchemy.orm import Session
from database.connection import SessionLocal
from database.models import Account, User

def check_account():
    """Check the current account configuration"""
    db: Session = SessionLocal()
    try:
        # Get all users
        users = db.query(User).all()
        print(f"\n{'='*60}")
        print(f"UTENTI NEL SISTEMA: {len(users)}")
        print(f"{'='*60}")

        for user in users:
            print(f"\n👤 User: {user.username}")

            # Get accounts for this user
            accounts = db.query(Account).filter(Account.user_id == user.id).all()
            print(f"   Accounts: {len(accounts)}")

            for acc in accounts:
                print(f"\n   📊 Account: {acc.name}")
                print(f"      Type: {acc.account_type}")
                print(f"      Model: {acc.model}")
                print(f"      Base URL: {acc.base_url}")
                print(f"      API Key: {'✅ Configurata' if acc.api_key and acc.api_key != 'default-key-please-update-in-settings' else '❌ Non configurata'}")
                print(f"      Capital Iniziale: €{acc.initial_capital:,.2f}")
                print(f"      Cash Disponibile: €{acc.current_cash:,.2f}")
                print(f"      Cash Bloccato: €{acc.frozen_cash:,.2f}")
                print(f"      Attivo: {'✅ Sì' if acc.is_active == 'true' else '❌ No'}")

        print(f"\n{'='*60}\n")

    except Exception as e:
        print(f"❌ Errore: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    check_account()
