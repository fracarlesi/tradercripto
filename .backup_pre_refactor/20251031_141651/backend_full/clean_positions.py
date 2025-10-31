#!/usr/bin/env python3
"""Clean all positions and reset account for real trading"""

from sqlalchemy.orm import Session
from database.connection import SessionLocal
from database.models import Account, Position, Order, Trade

db: Session = SessionLocal()
try:
    # Get DeepSeek account
    account = db.query(Account).filter(
        Account.name == "DeepSeek"
    ).first()

    if not account:
        print("❌ DeepSeek account not found!")
        exit(1)

    print(f"\n📊 Before cleanup:")
    positions = db.query(Position).filter(Position.account_id == account.id).all()
    print(f"   Positions: {len(positions)}")
    for pos in positions:
        print(f"     - {pos.symbol}: {pos.quantity} @ ${pos.avg_cost}")

    # Delete all positions
    db.query(Position).filter(Position.account_id == account.id).delete()

    # Delete all orders
    db.query(Order).filter(Order.account_id == account.id).delete()

    # Delete all trades
    db.query(Trade).filter(Trade.account_id == account.id).delete()

    # Reset account balance to real Hyperliquid balance
    REAL_BALANCE = 52.28
    account.initial_capital = REAL_BALANCE
    account.current_cash = REAL_BALANCE
    account.frozen_cash = 0.0

    db.commit()

    print(f"\n✅ Cleanup completed!")
    print(f"   All positions deleted")
    print(f"   All orders deleted")
    print(f"   All trades deleted")
    print(f"   Account reset to: ${REAL_BALANCE}")
    print(f"\n🚀 Ready for fresh real trading!\n")

except Exception as e:
    print(f"❌ Error: {e}")
    db.rollback()
finally:
    db.close()
