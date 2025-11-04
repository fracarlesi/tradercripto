"""
Trading Commands Service - Handles order execution and trading logic
"""

import logging
import random
from decimal import Decimal

from sqlalchemy.orm import Session

from database.connection import SessionLocal
from database.models import Account, Position

# Paper trading removed - only real trading on Hyperliquid
from services.ai_decision_service import (
    SUPPORTED_SYMBOLS,
    _get_portfolio_data,
    call_ai_for_decision,
    get_active_ai_accounts,
    save_ai_decision,
)
from services.market_data.hyperliquid_market_data import get_all_prices_from_hyperliquid

# Import Hyperliquid trading service for real trading
from services.hyperliquid_trading_service import hyperliquid_trading_service
from services.market_data import get_last_price

logger = logging.getLogger(__name__)

# Real trading only - paper trading removed
# All orders are executed on Hyperliquid DEX
logger.info("🔴 REAL TRADING MODE - All orders executed on Hyperliquid DEX")


# Load all available crypto symbols dynamically from Hyperliquid
def _load_trading_symbols() -> list[str]:
    """Load all available crypto symbols from Hyperliquid"""
    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        info = Info(constants.MAINNET_API_URL)
        meta = info.meta()

        symbols = []
        for asset in meta.get("universe", []):
            symbol = asset.get("name")
            if symbol:
                symbols.append(symbol)

        logger.info(f"Loaded {len(symbols)} crypto symbols for trading")
        return symbols
    except Exception as e:
        logger.error(f"Failed to load symbols: {e}", exc_info=True)
        # Fallback to basic list
        return ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]


AI_TRADING_SYMBOLS: list[str] = _load_trading_symbols()


def _sync_balance_from_hyperliquid(db: Session, account: Account) -> None:
    """
    Sync account balance and positions from Hyperliquid after real trades.
    Uses the new comprehensive sync function that treats Hyperliquid as source of truth.
    """
    try:
        if not hyperliquid_trading_service.enabled:
            return

        result = hyperliquid_trading_service.sync_account_to_database(db, account)

        if result.get("success"):
            logger.info(f"💰 Post-trade sync completed: ${result['available']:.2f} available")
        else:
            logger.warning(f"Post-trade sync failed: {result.get('error', result.get('reason'))}")

    except Exception as e:
        logger.error(f"Failed to sync balance from Hyperliquid: {e}", exc_info=True)


def _get_market_prices(symbols: list[str]) -> dict[str, float]:
    """Get latest prices for given symbols from Hyperliquid in ONE efficient API call"""
    try:
        # Get ALL prices in ONE API call using all_mids() endpoint
        all_prices = get_all_prices_from_hyperliquid()

        # Filter to only requested symbols
        prices = {symbol: price for symbol, price in all_prices.items() if symbol in symbols}

        logger.info(f"Fetched {len(prices)}/{len(symbols)} prices in ONE API call")
        return prices
    except Exception as err:
        logger.error(f"Failed to get market prices: {err}", exc_info=True)
        return {}


def _select_side(
    db: Session, account: Account, symbol: str, max_value: float
) -> tuple[str, int] | None:
    """Select random trading side and quantity for legacy random trading"""
    market = "CRYPTO"
    try:
        price = float(get_last_price(symbol, market))
    except Exception as err:
        logger.warning("Cannot get price for %s: %s", symbol, err)
        return None

    if price <= 0:
        logger.debug("%s returned non-positive price %s", symbol, price)
        return None

    max_quantity_by_value = int(Decimal(str(max_value)) // Decimal(str(price)))
    position = (
        db.query(Position)
        .filter(
            Position.account_id == account.id, Position.symbol == symbol, Position.market == market
        )
        .first()
    )
    available_quantity = int(position.available_quantity) if position else 0

    # Balance data from Hyperliquid API, not from DB
    # This is legacy code - should fetch from Hyperliquid API instead
    choices = []

    if float(account.current_cash) >= price and max_quantity_by_value >= 1:
        choices.append(("BUY", max_quantity_by_value))

    if available_quantity > 0:
        max_sell_quantity = min(
            available_quantity,
            max_quantity_by_value if max_quantity_by_value >= 1 else available_quantity,
        )
        if max_sell_quantity >= 1:
            choices.append(("SELL", max_sell_quantity))

    if not choices:
        return None

    side, max_qty = random.choice(choices)
    quantity = random.randint(1, max_qty)
    return side, quantity


def place_ai_driven_crypto_order(max_ratio: float = 0.2) -> None:
    """Place crypto order based on AI model decision for all active accounts"""
    db = SessionLocal()
    try:
        accounts = get_active_ai_accounts(db)
        if not accounts:
            logger.debug("No available accounts, skipping AI trading")
            return

        # Get latest market prices once for all accounts
        prices = _get_market_prices(AI_TRADING_SYMBOLS)
        if not prices:
            logger.warning("Failed to fetch market prices, skipping AI trading")
            return

        # Iterate through all active accounts
        for account in accounts:
            try:
                logger.info(f"Processing AI trading for account: {account.name}")

                # Get portfolio data for this account
                portfolio = _get_portfolio_data(db, account)

                if portfolio["total_assets"] <= 0:
                    logger.debug(f"Account {account.name} has non-positive total assets, skipping")
                    continue

                # Call AI for trading decision
                decision = call_ai_for_decision(account, portfolio, prices)
                if not decision or not isinstance(decision, dict):
                    logger.warning(f"Failed to get AI decision for {account.name}, skipping")
                    continue

                operation = (
                    decision.get("operation", "").lower() if decision.get("operation") else ""
                )
                symbol = decision.get("symbol", "").upper() if decision.get("symbol") else ""
                target_portion = (
                    float(decision.get("target_portion_of_balance", 0))
                    if decision.get("target_portion_of_balance") is not None
                    else 0
                )
                reason = decision.get("reason", "No reason provided")

                logger.info(
                    f"AI decision for {account.name}: {operation} {symbol} (portion: {target_portion:.2%}) - {reason}"
                )

                # Validate decision
                if operation not in ["buy", "sell", "hold"]:
                    logger.warning(
                        f"Invalid operation '{operation}' from AI for {account.name}, skipping"
                    )
                    # Save invalid decision for debugging
                    save_ai_decision(db, account, decision, portfolio, executed=False)
                    continue

                if operation == "hold":
                    logger.info(f"AI decided to HOLD for {account.name}")
                    # Save hold decision
                    save_ai_decision(db, account, decision, portfolio, executed=True)
                    continue

                if symbol not in SUPPORTED_SYMBOLS:
                    logger.warning(
                        f"Invalid symbol '{symbol}' from AI for {account.name}, skipping"
                    )
                    # Save invalid decision for debugging
                    save_ai_decision(db, account, decision, portfolio, executed=False)
                    continue

                if target_portion <= 0 or target_portion > 1:
                    logger.warning(
                        f"Invalid target_portion {target_portion} from AI for {account.name}, skipping"
                    )
                    # Save invalid decision for debugging
                    save_ai_decision(db, account, decision, portfolio, executed=False)
                    continue

                # Get current price
                price = prices.get(symbol)
                if not price or price <= 0:
                    logger.warning(f"Invalid price for {symbol} for {account.name}, skipping")
                    # Save decision with execution failure
                    save_ai_decision(db, account, decision, portfolio, executed=False)
                    continue

                # Calculate quantity based on operation
                if operation == "buy":
                    # Balance data from Hyperliquid API, not from DB
                    # This is legacy code - should fetch from Hyperliquid API instead
                    available_cash = float(account.current_cash)
                    order_value = available_cash * target_portion

                    # Ensure minimum order size of $10 (Hyperliquid requirement)
                    MIN_ORDER_SIZE = 10.0
                    if order_value < MIN_ORDER_SIZE:
                        if available_cash >= MIN_ORDER_SIZE:
                            logger.info(
                                f"Order value ${order_value:.2f} below minimum, adjusting to ${MIN_ORDER_SIZE}"
                            )
                            order_value = MIN_ORDER_SIZE
                        else:
                            logger.info(
                                f"Insufficient funds for minimum order size (${MIN_ORDER_SIZE}) for {account.name}, skipping"
                            )
                            save_ai_decision(db, account, decision, portfolio, executed=False)
                            continue

                    # For crypto, support fractional quantities - use float instead of int
                    quantity = float(Decimal(str(order_value)) / Decimal(str(price)))

                    # Round to reasonable precision (6 decimal places for crypto)
                    quantity = round(quantity, 6)

                    if quantity <= 0:
                        logger.info(
                            f"Calculated BUY quantity <= 0 for {symbol} for {account.name}, skipping"
                        )
                        # Save decision with execution failure
                        save_ai_decision(db, account, decision, portfolio, executed=False)
                        continue

                    side = "BUY"

                elif operation == "sell":
                    # Calculate quantity based on position and target portion
                    position = (
                        db.query(Position)
                        .filter(
                            Position.account_id == account.id,
                            Position.symbol == symbol,
                            Position.market == "CRYPTO",
                        )
                        .first()
                    )

                    if not position or float(position.available_quantity) <= 0:
                        logger.info(
                            f"No position available to SELL for {symbol} for {account.name}, skipping"
                        )
                        # Save decision with execution failure
                        save_ai_decision(db, account, decision, portfolio, executed=False)
                        continue

                    available_quantity = int(position.available_quantity)
                    quantity = max(1, int(available_quantity * target_portion))

                    if quantity > available_quantity:
                        quantity = available_quantity

                    side = "SELL"

                else:
                    continue

                # Create and execute order
                name = SUPPORTED_SYMBOLS[symbol]

                # REAL TRADING on Hyperliquid (only mode supported)
                if not hyperliquid_trading_service.enabled:
                    logger.error("Hyperliquid trading not enabled - cannot execute order")
                    # Save decision as not executed
                    save_ai_decision(
                        db, account, decision, portfolio, executed=False, order_id=None
                    )
                    continue

                logger.warning(
                    f"🔴 EXECUTING REAL ORDER on Hyperliquid: {side} {symbol} quantity={quantity}"
                )

                # Calculate order value in USD
                order_value_usd = float(quantity) * price

                # Place order on Hyperliquid
                hl_result = hyperliquid_trading_service.place_market_order(
                    symbol=symbol, side=side.lower(), size_usd=order_value_usd
                )

                if hl_result and hl_result.get("success"):
                    logger.info(f"✅ REAL order executed on Hyperliquid: {hl_result}")

                    # Sync balance from Hyperliquid after successful trade
                    _sync_balance_from_hyperliquid(db, account)

                    # Save decision as executed (no order_id since order is on Hyperliquid)
                    save_ai_decision(db, account, decision, portfolio, executed=True, order_id=None)
                else:
                    logger.error(f"❌ REAL order failed on Hyperliquid: {hl_result}")
                    # Save decision as not executed
                    save_ai_decision(
                        db, account, decision, portfolio, executed=False, order_id=None
                    )

            except Exception as account_err:
                logger.error(
                    f"AI-driven order placement failed for account {account.name}: {account_err}",
                    exc_info=True,
                )
                # Continue with next account even if one fails

    except Exception as err:
        logger.error(f"AI-driven order placement failed: {err}", exc_info=True)
        db.rollback()
    finally:
        db.close()


# Legacy random order placement removed - only AI-driven trading supported
AI_TRADE_JOB_ID = "ai_crypto_trade"
