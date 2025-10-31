"""
Hyperliquid Real Trading Service
Handles real trading operations on Hyperliquid DEX
"""
import logging
import os
from decimal import Decimal
from typing import Dict, Optional
from dotenv import load_dotenv
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account as EthAccount

# Load environment variables before anything else
load_dotenv()

logger = logging.getLogger(__name__)

class HyperliquidTradingService:
    def __init__(self):
        self.private_key = None
        self.wallet_address = None
        self.exchange = None
        self.info = None
        self.max_capital = 53.0  # $53 USD limit
        self.min_order_size = 10.0  # Minimum $10 per order (Hyperliquid requirement)
        self.enabled = False
        self.asset_specs = {}  # Cache for asset specifications (szDecimals)

        self._load_config()
        if self.enabled:
            self._initialize_client()
            self._load_asset_specs()

    def _load_config(self):
        """Load configuration from environment variables"""
        try:
            # Load from .env file
            private_key = os.getenv('HYPERLIQUID_PRIVATE_KEY')
            if not private_key:
                logger.warning("HYPERLIQUID_PRIVATE_KEY not found in environment")
                return

            # Ensure private key has 0x prefix
            if not private_key.startswith('0x'):
                private_key = '0x' + private_key

            self.private_key = private_key
            self.wallet_address = os.getenv('HYPERLIQUID_WALLET_ADDRESS')

            # Check if real trading is enabled
            enable_trading = os.getenv('ENABLE_REAL_TRADING', 'false').lower()
            self.enabled = enable_trading in ['true', '1', 'yes']

            # Load max capital limit
            max_capital_str = os.getenv('MAX_CAPITAL_USD', '53.0')
            self.max_capital = float(max_capital_str)

            logger.info(f"Hyperliquid trading {'ENABLED' if self.enabled else 'DISABLED'}")
            logger.info(f"Max capital limit: ${self.max_capital}")
            logger.info(f"Wallet address: {self.wallet_address}")

        except Exception as e:
            logger.error(f"Error loading Hyperliquid config: {e}")
            self.enabled = False

    def _initialize_client(self):
        """Initialize Hyperliquid client"""
        try:
            if not self.private_key:
                raise ValueError("Private key not configured")

            # Initialize Exchange and Info clients
            # Use mainnet (testnet=False)
            self.exchange = Exchange(
                wallet=EthAccount.from_key(self.private_key),
                base_url=constants.MAINNET_API_URL,
                account_address=self.wallet_address
            )

            self.info = Info(constants.MAINNET_API_URL)

            logger.info("Hyperliquid client initialized successfully")

            # Log current balance
            self._log_account_info()

        except Exception as e:
            logger.error(f"Failed to initialize Hyperliquid client: {e}")
            self.enabled = False
            raise

    def _log_account_info(self):
        """Log current account information"""
        try:
            user_state = self.info.user_state(self.wallet_address)

            if user_state and 'marginSummary' in user_state:
                margin = user_state['marginSummary']
                account_value = float(margin.get('accountValue', 0))
                total_margin_used = float(margin.get('totalMarginUsed', 0))

                logger.info(f"Account value: ${account_value:.2f}")
                logger.info(f"Margin used: ${total_margin_used:.2f}")
                logger.info(f"Available: ${account_value - total_margin_used:.2f}")

        except Exception as e:
            logger.error(f"Error fetching account info: {e}")

    def _load_asset_specs(self):
        """Load asset specifications (szDecimals) from Hyperliquid"""
        try:
            meta = self.info.meta()
            for asset in meta.get('universe', []):
                symbol = asset.get('name')
                sz_decimals = asset.get('szDecimals', 2)
                if symbol:
                    self.asset_specs[symbol] = {
                        'szDecimals': sz_decimals,
                        'maxLeverage': asset.get('maxLeverage', 1)
                    }
            logger.info(f"Loaded specs for {len(self.asset_specs)} assets")
        except Exception as e:
            logger.error(f"Error loading asset specs: {e}")

    def get_account_balance(self) -> Dict:
        """Get current account balance"""
        try:
            if not self.enabled:
                return {'error': 'Trading not enabled'}

            user_state = self.info.user_state(self.wallet_address)

            if not user_state or 'marginSummary' not in user_state:
                return {'error': 'Could not fetch account state'}

            margin = user_state['marginSummary']
            account_value = float(margin.get('accountValue', 0))
            total_margin_used = float(margin.get('totalMarginUsed', 0))

            return {
                'total_equity': account_value,
                'margin_used': total_margin_used,
                'available': account_value - total_margin_used,
                'positions': user_state.get('assetPositions', [])
            }

        except Exception as e:
            logger.error(f"Error getting account balance: {e}")
            return {'error': str(e)}

    def sync_account_to_database(self, db, account) -> Dict:
        """
        Synchronize account balance and positions from Hyperliquid to local database.
        IMPORTANT: Hyperliquid is the source of truth - always override local state.

        Returns dict with sync status and details
        """
        try:
            if not self.enabled:
                logger.debug("Hyperliquid sync skipped - service not enabled")
                return {'success': False, 'reason': 'service_disabled'}

            # Get authoritative state from Hyperliquid
            balance_info = self.get_account_balance()

            if 'error' in balance_info:
                logger.error(f"Failed to fetch balance from Hyperliquid: {balance_info['error']}")
                return {'success': False, 'error': balance_info['error']}

            # Extract balance data
            total_equity = balance_info['total_equity']
            available_balance = balance_info['available']
            margin_used = balance_info['margin_used']

            # Update account balance in database
            old_balance = float(account.current_cash)
            account.current_cash = available_balance
            account.frozen_cash = margin_used
            # Update initial_capital to reflect total account value
            account.initial_capital = total_equity

            db.commit()

            balance_diff = available_balance - old_balance

            logger.info(f"✅ Synced account '{account.name}' from Hyperliquid:")
            logger.info(f"   Total Equity: ${total_equity:.2f}")
            logger.info(f"   Available: ${available_balance:.2f} (was ${old_balance:.2f}, diff: ${balance_diff:+.2f})")
            logger.info(f"   Margin Used: ${margin_used:.2f}")

            # Sync positions
            positions_synced = self._sync_positions_to_database(db, account, balance_info['positions'])

            # Sync recent trades/fills (last 100)
            trades_synced = self._sync_trades_to_database(db, account, limit=100)

            return {
                'success': True,
                'total_equity': total_equity,
                'available': available_balance,
                'margin_used': margin_used,
                'balance_diff': balance_diff,
                'positions_synced': positions_synced,
                'trades_synced': trades_synced
            }

        except Exception as e:
            logger.error(f"Error syncing account from Hyperliquid: {e}", exc_info=True)
            db.rollback()
            return {'success': False, 'error': str(e)}

    def _sync_positions_to_database(self, db, account, hyperliquid_positions: list) -> int:
        """
        Sync positions from Hyperliquid to database.
        Returns number of positions synced.
        """
        try:
            from database.models import Position

            # Clear all existing CRYPTO positions for this account
            # (Hyperliquid is source of truth)
            db.query(Position).filter(
                Position.account_id == account.id,
                Position.market == "CRYPTO"
            ).delete(synchronize_session=False)

            synced_count = 0

            # Add current positions from Hyperliquid
            for hl_pos in hyperliquid_positions:
                pos_data = hl_pos.get('position', {})
                symbol = pos_data.get('coin')
                size = float(pos_data.get('szi', 0))

                if size != 0 and symbol:
                    entry_price = float(pos_data.get('entryPx', 0))

                    # Create position in database
                    position = Position(
                        account_id=account.id,
                        symbol=symbol,
                        name=symbol,
                        market="CRYPTO",
                        quantity=abs(size),
                        available_quantity=abs(size),
                        avg_cost=entry_price
                    )
                    db.add(position)
                    synced_count += 1

                    logger.debug(f"   Synced position: {symbol} size={size} @ ${entry_price}")

            db.commit()

            if synced_count > 0:
                logger.info(f"   Synced {synced_count} positions from Hyperliquid")

            return synced_count

        except Exception as e:
            logger.error(f"Error syncing positions: {e}")
            db.rollback()
            return 0

    def _sync_trades_to_database(self, db, account, limit: int = 100) -> int:
        """
        Sync recent fills (executed trades) from Hyperliquid to database.
        Returns number of trades synced.

        Args:
            db: Database session
            account: Account object
            limit: Maximum number of recent fills to fetch (default: 100)
        """
        try:
            from database.models import Order, Trade
            from datetime import datetime, timezone

            # Get recent fills from Hyperliquid
            user_fills = self.info.user_fills(self.wallet_address)

            if not user_fills:
                logger.debug("No fills found on Hyperliquid")
                return 0

            # Limit to recent fills
            recent_fills = user_fills[:limit]

            synced_count = 0
            skipped_count = 0

            for fill in recent_fills:
                try:
                    # Extract fill data
                    coin = fill.get('coin')
                    side = fill.get('side')  # 'B' or 'S'
                    size = float(fill.get('sz', 0))
                    price = float(fill.get('px', 0))
                    time_ms = fill.get('time')  # milliseconds since epoch

                    # Convert to datetime
                    fill_time = datetime.fromtimestamp(time_ms / 1000.0, tz=timezone.utc)

                    # Convert side to our format
                    order_side = 'BUY' if side == 'B' else 'SELL'

                    # Check if this fill already exists in database
                    # Use time + symbol + side + size as unique identifier
                    existing_trade = db.query(Trade).filter(
                        Trade.account_id == account.id,
                        Trade.symbol == coin,
                        Trade.side == order_side,
                        Trade.quantity == size,
                        Trade.trade_time == fill_time
                    ).first()

                    if existing_trade:
                        skipped_count += 1
                        continue

                    # Generate order_no from timestamp and symbol
                    order_no = f"HL_{time_ms}_{coin}_{side}"

                    # Create order record (for history tracking)
                    # Note: These are fills from Hyperliquid, so status is FILLED
                    order = Order(
                        account_id=account.id,
                        order_no=order_no,
                        symbol=coin,
                        name=coin,
                        side=order_side,
                        order_type='MARKET',
                        price=price,
                        quantity=size,
                        filled_quantity=size,  # Set filled quantity
                        status='FILLED',  # Changed from EXECUTED to FILLED
                        market='CRYPTO'
                    )
                    db.add(order)
                    db.flush()  # Get order ID

                    # Create trade record
                    trade = Trade(
                        account_id=account.id,
                        order_id=order.id,
                        symbol=coin,
                        name=coin,
                        side=order_side,
                        price=price,
                        quantity=size,
                        commission=0,  # Hyperliquid uses funding instead
                        trade_time=fill_time,
                        market='CRYPTO'
                    )
                    db.add(trade)
                    synced_count += 1

                except Exception as fill_err:
                    logger.warning(f"Error processing fill: {fill_err}")
                    continue

            db.commit()

            if synced_count > 0:
                logger.info(f"   Synced {synced_count} trades from Hyperliquid (skipped {skipped_count} existing)")

            return synced_count

        except Exception as e:
            logger.error(f"Error syncing trades: {e}")
            db.rollback()
            return 0

    def place_market_order(self, symbol: str, side: str, size_usd: float, reduce_only: bool = False) -> Optional[Dict]:
        """
        Place a market order on Hyperliquid

        Args:
            symbol: Trading pair (e.g., 'BTC', 'ETH')
            side: 'buy' or 'sell'
            size_usd: Size in USD
            reduce_only: If True, only reduces existing position

        Returns:
            Order result dict or None if error
        """
        try:
            if not self.enabled:
                logger.warning("Real trading not enabled, order not placed")
                return None

            # Safety checks
            if size_usd < self.min_order_size:
                logger.error(f"Order size ${size_usd} below minimum ${self.min_order_size}")
                return None

            # Add safety margin to ensure rounding doesn't drop below minimum
            if size_usd < self.min_order_size * 1.01:  # Less than 1% above minimum
                size_usd = self.min_order_size * 1.01  # Add 1% margin
                logger.info(f"Adjusted order size to ${size_usd:.2f} to account for rounding")

            if size_usd > self.max_capital:
                logger.error(f"Order size ${size_usd} exceeds max capital ${self.max_capital}")
                return None

            # Get current price
            ticker = self.info.all_mids()
            if symbol not in ticker:
                logger.error(f"Symbol {symbol} not found in market data")
                return None

            current_price = float(ticker[symbol])

            # Calculate size in coins
            size_coins = size_usd / current_price

            # Round to appropriate precision based on asset's szDecimals
            sz_decimals = self.asset_specs.get(symbol, {}).get('szDecimals', 2)
            size_coins = round(size_coins, sz_decimals)

            # Ensure actual order value meets minimum after rounding
            actual_value = size_coins * current_price
            if actual_value < self.min_order_size:
                # Round up to next valid increment
                increment = 10 ** (-sz_decimals)
                size_coins = round(size_coins + increment, sz_decimals)
                actual_value = size_coins * current_price
                logger.info(f"Adjusted size to meet minimum: {size_coins} {symbol} = ${actual_value:.2f}")

            logger.info(f"Placing {side.upper()} order: {size_coins} {symbol} (~${actual_value:.2f})")

            # Place order
            is_buy = (side.lower() == 'buy')

            # market_open parameters: (coin, is_buy, sz, px=None, slippage=0.01, cloid=None)
            # px=None for market orders, slippage=0.01 (1%), cloid=None
            order_result = self.exchange.market_open(
                symbol,      # coin
                is_buy,      # is_buy
                size_coins,  # sz
                None,        # px (None for market orders)
                0.01         # slippage (1%)
            )

            logger.info(f"Order placed successfully: {order_result}")

            return {
                'success': True,
                'symbol': symbol,
                'side': side,
                'size_coins': size_coins,
                'size_usd': size_usd,
                'price': current_price,
                'result': order_result
            }

        except Exception as e:
            logger.error(f"Error placing order: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    def close_position(self, symbol: str) -> Optional[Dict]:
        """Close an existing position"""
        try:
            if not self.enabled:
                return None

            # Get current position
            user_state = self.info.user_state(self.wallet_address)
            positions = user_state.get('assetPositions', [])

            position = None
            for pos in positions:
                if pos['position']['coin'] == symbol:
                    position = pos
                    break

            if not position:
                logger.warning(f"No position found for {symbol}")
                return None

            size = abs(float(position['position']['szi']))
            is_long = float(position['position']['szi']) > 0

            # Close position (sell if long, buy if short)
            side = 'sell' if is_long else 'buy'

            logger.info(f"Closing {symbol} position: {side} {size}")

            # market_close parameters: (coin, slippage=0.01)
            order_result = self.exchange.market_close(symbol, 0.01)

            logger.info(f"Position closed: {order_result}")

            return {
                'success': True,
                'symbol': symbol,
                'side': side,
                'size': size,
                'result': order_result
            }

        except Exception as e:
            logger.error(f"Error closing position: {e}")
            return {'success': False, 'error': str(e)}

    def get_open_positions(self) -> list:
        """Get all open positions"""
        try:
            if not self.enabled:
                return []

            user_state = self.info.user_state(self.wallet_address)
            positions = user_state.get('assetPositions', [])

            open_positions = []
            for pos in positions:
                size = float(pos['position']['szi'])
                if size != 0:
                    open_positions.append({
                        'symbol': pos['position']['coin'],
                        'size': size,
                        'entry_price': float(pos['position']['entryPx']),
                        'unrealized_pnl': float(pos['position']['unrealizedPnl']),
                        'side': 'long' if size > 0 else 'short'
                    })

            return open_positions

        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []


# Global instance
hyperliquid_trading_service = HyperliquidTradingService()
