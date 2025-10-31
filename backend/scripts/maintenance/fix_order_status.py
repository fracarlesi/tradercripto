#!/usr/bin/env python3
"""
Script per correggere gli ordini con status EXECUTED e impostarli a FILLED
"""

from database.connection import SessionLocal
from database.models import Order


def fix_order_status():
    db = SessionLocal()

    try:
        # Trova tutti gli ordini con status EXECUTED
        executed_orders = db.query(Order).filter(Order.status == "EXECUTED").all()

        print(f"Trovati {len(executed_orders)} ordini con status EXECUTED")

        if not executed_orders:
            print("Nessun ordine da correggere")
            return

        # Aggiorna tutti gli ordini da EXECUTED a FILLED
        for order in executed_orders:
            order.status = "FILLED"
            # Assicurati che filled_quantity sia impostato
            if order.filled_quantity == 0:
                order.filled_quantity = order.quantity

        db.commit()

        print(f"✅ Aggiornati {len(executed_orders)} ordini da EXECUTED a FILLED")

        # Verifica il risultato
        remaining_executed = db.query(Order).filter(Order.status == "EXECUTED").count()
        filled_count = db.query(Order).filter(Order.status == "FILLED").count()

        print("\nRisultati finali:")
        print(f"  - Ordini EXECUTED rimasti: {remaining_executed}")
        print(f"  - Ordini FILLED: {filled_count}")

    except Exception as e:
        print(f"❌ Errore: {e}")
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    fix_order_status()
