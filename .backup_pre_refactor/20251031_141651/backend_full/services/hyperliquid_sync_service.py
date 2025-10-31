"""
Hyperliquid Account Synchronization Service
Periodically syncs local database state with Hyperliquid (source of truth)
"""
import logging
from typing import List

from sqlalchemy.orm import Session

from database.connection import SessionLocal
from database.models import Account
from services.hyperliquid_trading_service import hyperliquid_trading_service

logger = logging.getLogger(__name__)


def sync_all_active_accounts() -> None:
    """
    Synchronize all active AI accounts with Hyperliquid.
    This is the main periodic reconciliation function.

    Best Practice: Run this every 1-2 minutes to ensure local state
    matches the authoritative on-chain state from Hyperliquid.
    """
    if not hyperliquid_trading_service.enabled:
        logger.debug("Hyperliquid sync skipped - service not enabled")
        return

    db: Session = SessionLocal()
    try:
        # Get all active AI accounts
        accounts = db.query(Account).filter(
            Account.is_active == "true",
            Account.account_type == "AI"
        ).all()

        if not accounts:
            logger.debug("No active accounts to sync")
            return

        synced_count = 0
        failed_count = 0

        for account in accounts:
            try:
                result = hyperliquid_trading_service.sync_account_to_database(db, account)

                if result.get('success'):
                    synced_count += 1
                    logger.debug(f"Synced account {account.name}: ${result['available']:.2f} available")
                else:
                    failed_count += 1
                    reason = result.get('error') or result.get('reason', 'unknown')
                    logger.warning(f"Sync failed for account {account.name}: {reason}")

            except Exception as account_err:
                failed_count += 1
                logger.error(f"Error syncing account {account.name}: {account_err}")

        if synced_count > 0:
            logger.info(f"✅ Hyperliquid sync: {synced_count} account(s) synced, {failed_count} failed")

    except Exception as err:
        logger.error(f"Error in periodic Hyperliquid sync: {err}", exc_info=True)
    finally:
        db.close()


def sync_specific_account(account_id: int) -> dict:
    """
    Sync a specific account by ID.
    Used for manual sync via API endpoint.

    Returns:
        dict with sync result
    """
    db: Session = SessionLocal()
    try:
        account = db.query(Account).filter(Account.id == account_id).first()

        if not account:
            return {'success': False, 'error': 'Account not found'}

        if not hyperliquid_trading_service.enabled:
            return {'success': False, 'error': 'Hyperliquid service not enabled'}

        result = hyperliquid_trading_service.sync_account_to_database(db, account)
        return result

    except Exception as e:
        logger.error(f"Error syncing account {account_id}: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        db.close()
