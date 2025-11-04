"""
Asset Curve Calculator - Hyperliquid-based Algorithm
Fetches current balance from Hyperliquid, then reconstructs historical curve using trades + market prices.
NO DEPENDENCY on deprecated DB fields (initial_capital, current_cash, frozen_cash).
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from database.models import Account, Trade
from services.market_data import get_kline_data


async def get_all_asset_curves_data_new_async(db: Session, timeframe: str = "1h") -> list[dict]:
    """
    Calculate asset curve from Hyperliquid real-time data + historical trades (ASYNC).

    Algorithm:
    1. Fetch current accountValue from Hyperliquid
    2. Get historical trades from DB
    3. Get historical prices (klines)
    4. Reconstruct portfolio value at each timestamp using trades + klines

    Args:
        db: Database session
        timeframe: Time period for the curve, options: "5m", "1h", "1d"

    Returns:
        List of asset curve data points with timestamp, account info, and asset values
    """
    try:
        # Step 1: Get current balance from Hyperliquid
        from services.trading.hyperliquid_trading_service import hyperliquid_trading_service

        user_state = await hyperliquid_trading_service.get_user_state_async()

        if not user_state or 'marginSummary' not in user_state:
            logging.error("Failed to fetch user state from Hyperliquid")
            return []

        margin = user_state['marginSummary']
        current_account_value = Decimal(str(margin.get('accountValue', '0')))

        logging.info(f"Current Hyperliquid account value: ${current_account_value}")

        # Step 2: Get all active accounts
        accounts = db.query(Account).filter(Account.is_active == True).all()
        if not accounts:
            return []

        logging.info(f"Found {len(accounts)} active accounts")

        # Step 3: Get all unique symbols from all account trades
        symbols_query = db.query(Trade.symbol).distinct().all()
        unique_symbols = set()
        for (symbol,) in symbols_query:
            unique_symbols.add((symbol, "CRYPTO"))

        if not unique_symbols:
            # No trades yet, return current Hyperliquid balance
            now = datetime.now()
            return [
                {
                    "timestamp": int(now.timestamp()),
                    "datetime_str": now.isoformat(),
                    "account_id": account.id,
                    "user_id": account.user_id,
                    "username": account.name,
                    "total_assets": float(current_account_value),
                    "cash": float(current_account_value),
                    "positions_value": 0.0,
                }
                for account in accounts
            ]

        logging.info(f"Found {len(unique_symbols)} unique symbols: {unique_symbols}")

        # Step 4: Get historical close prices for all symbols
        symbol_klines = {}
        kline_count = 100 if timeframe == "1h" else 200 if timeframe == "5m" else 50
        for symbol, market in unique_symbols:
            try:
                klines = get_kline_data(symbol, market, timeframe, kline_count)
                if klines:
                    symbol_klines[(symbol, market)] = klines
                    logging.info(f"Fetched {len(klines)} klines for {symbol}.{market}")
            except Exception as e:
                logging.warning(f"Failed to fetch klines for {symbol}.{market}: {e}")

        if not symbol_klines:
            # No market data, return current balance only
            now = datetime.now()
            return [
                {
                    "timestamp": int(now.timestamp()),
                    "datetime_str": now.isoformat(),
                    "account_id": account.id,
                    "user_id": account.user_id,
                    "username": account.name,
                    "total_assets": float(current_account_value),
                    "cash": float(current_account_value),
                    "positions_value": 0.0,
                }
                for account in accounts
            ]

        # Step 5: Get common timestamps from market data
        first_klines = next(iter(symbol_klines.values()))
        timestamps = [k["timestamp"] for k in first_klines]

        logging.info(f"Processing {len(timestamps)} timestamps")

        # Step 6: Calculate asset curves for each account
        result = []

        for account in accounts:
            account_id = account.id
            logging.info(f"Processing account {account_id}: {account.name}")

            # Create timeline using Hyperliquid balance as reference
            account_timeline = await _create_account_timeline_async(
                db, account, timestamps, symbol_klines, current_account_value
            )
            result.extend(account_timeline)

        # Sort result by timestamp and account_id for consistent ordering
        result.sort(key=lambda x: (x["timestamp"], x["account_id"]))

        logging.info(f"Generated {len(result)} data points for asset curves")
        return result

    except Exception as e:
        logging.error(f"Failed to calculate asset curves: {e}", exc_info=True)
        return []


# DELETED: Sync wrapper removed to prevent event loop deadlock
# The function get_all_asset_curves_data_new() has been removed because it caused deadlock
# by creating a new event loop inside an async context.
# Use get_all_asset_curves_data_new_async() directly from async contexts.


async def _create_account_timeline_async(
    db: Session,
    account: Account,
    timestamps: list[int],
    symbol_klines: dict[tuple[str, str], list[dict]],
    current_account_value: Decimal,
) -> list[dict]:
    """
    Create historical timeline for an account using Hyperliquid current balance.

    Algorithm:
    1. Calculate net P&L from all trades (realized gains/losses)
    2. Reverse-engineer starting capital: current_balance - realized_pnl
    3. For each timestamp: starting_capital + cumulative_pnl_up_to_timestamp + position_values

    Args:
        db: Database session
        account: Account object
        timestamps: List of timestamps to calculate for
        symbol_klines: Dictionary of symbol klines data
        current_account_value: Current account value from Hyperliquid

    Returns:
        List of timeline data points for the account
    """
    account_id = account.id

    # Get all trades for this account, ordered by time
    trades = (
        db.query(Trade)
        .filter(Trade.account_id == account_id)
        .order_by(Trade.trade_time.asc())
        .all()
    )

    if not trades:
        # No trades, return current Hyperliquid balance at all timestamps
        first_klines = next(iter(symbol_klines.values()))
        return [
            {
                "timestamp": ts,
                "datetime_str": first_klines[i]["datetime_str"],
                "account_id": account.id,
                "user_id": account.user_id,
                "username": account.name,
                "total_assets": float(current_account_value),
                "cash": float(current_account_value),
                "positions_value": 0.0,
            }
            for i, ts in enumerate(timestamps)
        ]

    # Calculate total realized P&L from trades
    total_realized_pnl = Decimal("0")
    for trade in trades:
        # Commissions are always a cost
        total_realized_pnl -= Decimal(str(trade.commission))

    # Reverse-engineer starting capital
    # starting_capital = current_value - realized_pnl
    starting_capital = current_account_value - total_realized_pnl

    logging.info(
        f"Account {account.name}: current=${current_account_value}, "
        f"realized_pnl=${total_realized_pnl}, starting_capital=${starting_capital}"
    )

    # Calculate holdings and cash at each timestamp
    timeline = []
    first_klines = next(iter(symbol_klines.values()))

    for i, ts in enumerate(timestamps):
        ts_datetime = datetime.fromtimestamp(ts, tz=UTC)

        # Calculate cash changes and positions up to this timestamp
        cash_change = Decimal("0")
        position_quantities = {}

        for trade in trades:
            trade_time = trade.trade_time
            if not trade_time.tzinfo:
                trade_time = trade_time.replace(tzinfo=UTC)

            if trade_time <= ts_datetime:
                # Update cash based on trade
                trade_amount = Decimal(str(trade.price)) * Decimal(str(trade.quantity))
                commission = Decimal(str(trade.commission))

                if trade.side == "BUY":
                    cash_change -= (trade_amount + commission)
                else:  # SELL
                    cash_change += (trade_amount - commission)

                # Update position quantity
                key = (trade.symbol, "CRYPTO")
                if key not in position_quantities:
                    position_quantities[key] = Decimal("0")

                if trade.side == "BUY":
                    position_quantities[key] += Decimal(str(trade.quantity))
                else:  # SELL
                    position_quantities[key] -= Decimal(str(trade.quantity))

        # Current cash = starting capital + net cash changes from trades
        current_cash = starting_capital + cash_change

        # Calculate positions value using prices at this timestamp
        positions_value = Decimal("0")
        for (symbol, market), quantity in position_quantities.items():
            if quantity > 0 and (symbol, market) in symbol_klines:
                klines = symbol_klines[(symbol, market)]
                if i < len(klines) and klines[i]["close"]:
                    price = Decimal(str(klines[i]["close"]))
                    positions_value += price * quantity

        total_assets = current_cash + positions_value

        timeline.append(
            {
                "timestamp": ts,
                "datetime_str": first_klines[i]["datetime_str"],
                "account_id": account.id,
                "user_id": account.user_id,
                "username": account.name,
                "total_assets": float(total_assets),
                "cash": float(current_cash),
                "positions_value": float(positions_value),
            }
        )

    return timeline
