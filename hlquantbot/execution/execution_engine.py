"""
Smart execution engine for order management with HFT optimizations.

OTTIMIZZAZIONI IMPLEMENTATE (dalla specifica):

4.1 ALO SMART ROUTING:
    - Timeout ridotto da 1.5s a 150-250ms (default: 200ms)
    - Se ordine non fillato entro timeout:
      * Cancella ordine immediatamente
      * Strategy può ricalcolare prezzo e ripiazzare se segnale ancora valido
    - Monitoraggio ogni 20ms per reattività massima

4.2 STALE FILL PROTECTION:
    - Cancella ordini maker se abs(current_mid - order_price) >= 0.05%
    - Verifica continua ogni 20ms insieme al timeout monitor
    - Previene fill a prezzi sfavorevoli in mercati veloci

4.3 TP/SL BOT-SIDE:
    - TP/SL gestiti principalmente dal bot, non dall'exchange
    - Monitoraggio continuo del price feed ogni 100ms
    - Quando TP/SL raggiunto:
      * Cancella ordini TP/SL exchange (se esistono)
      * Chiude con aggressive limit order (IOC) per minimizzare fees
      * Fallback a market order solo se limit fallisce
    - Ordini TP/SL exchange restano solo come "hard stop" di emergenza
      (per protezione in caso di crash bot o disconnessione)

VANTAGGI:
    - Fill più rapidi (200ms vs 1.5s)
    - Protezione da slippage in mercati veloci
    - Fees ridotte usando limit orders per TP/SL (0.02% vs 0.05%)
    - Controllo più preciso delle uscite
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

import eth_account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from ..core.models import ApprovedOrder, Position
from ..core.enums import OrderType, OrderStatus, Side, ExitReason, AlertSeverity, StrategyId
from ..core.exceptions import ExecutionError, RateLimitError
from ..config.settings import Settings
from .order_manager import OrderManager
from .rate_limiter import OrderRateLimiter


logger = logging.getLogger(__name__)


# HFT strategies require maker orders to be profitable
HFT_STRATEGIES = {
    StrategyId.MMR_HFT,
    StrategyId.MICRO_BREAKOUT,
    StrategyId.PAIR_TRADING,
    StrategyId.LIQUIDATION_SNIPING,
}

# Default timeouts by strategy type
# REQUISITO 4.1: ALO timeout ridotto a 150-250ms (non più 1.5s)
DEFAULT_HFT_TIMEOUT_MS = 200  # 200ms (centro del range 150-250ms)
DEFAULT_HFT_TIMEOUT_SECONDS = 0.2  # 200ms in secondi
DEFAULT_STANDARD_TIMEOUT_SECONDS = 30

# Stale fill protection
# REQUISITO 4.2: Cancellare ordine maker se price deviation >= 0.05%
STALE_FILL_THRESHOLD_MS = 500  # Fills older than 500ms are considered stale
MAX_PRICE_DEVIATION_PCT = 0.0005  # 0.05% max price deviation (era 0.2%, ora più stringente)


@dataclass
class PendingOrder:
    """Track pending orders with timeout."""
    order: ApprovedOrder
    hl_order_id: Optional[str]
    submitted_at: datetime
    timeout_seconds: float
    is_hft: bool = False

    @property
    def is_expired(self) -> bool:
        """Check if order has timed out."""
        elapsed = (datetime.now(timezone.utc) - self.submitted_at).total_seconds()
        return elapsed >= self.timeout_seconds

    @property
    def time_remaining(self) -> float:
        """Get remaining time before timeout."""
        elapsed = (datetime.now(timezone.utc) - self.submitted_at).total_seconds()
        return max(0, self.timeout_seconds - elapsed)


class ExecutionEngine:
    """
    Smart execution engine for Hyperliquid.

    Features:
    - Rate limiting
    - Order chunking for large orders
    - Smart order type selection (market vs aggressive limit)
    - Spread checking
    - SL/TP order management
    - Retry logic with backoff
    """

    def __init__(self, settings: Settings):
        self.settings = settings

        # Initialize Hyperliquid clients
        base_url = (
            constants.TESTNET_API_URL
            if settings.is_testnet
            else constants.MAINNET_API_URL
        )

        account: LocalAccount = eth_account.Account.from_key(settings.hl_private_key)

        self.info = Info(base_url, skip_ws=True)
        self.exchange = Exchange(
            account,
            base_url,
            account_address=settings.hl_wallet_address,
        )

        # Get meta for symbol info
        self._meta = self.info.meta()
        self._symbol_info = {p["name"]: p for p in self._meta.get("universe", [])}

        # Components
        self.rate_limiter = OrderRateLimiter(
            max_orders_per_second=settings.hyperliquid.max_orders_per_second,
            max_requests_per_minute=settings.hyperliquid.max_requests_per_minute,
        )
        self.order_manager = OrderManager()

        # Configuration
        self.max_spread_bps = Decimal("10")  # Max spread to use market orders
        self.chunk_size_usd = Decimal("50000")  # Split orders larger than this
        self.max_retries = 3
        self.retry_delay = 1.0

        # HFT Configuration (Context Pack 2.0: 150-250ms timeout)
        self.hft_timeout_seconds = DEFAULT_HFT_TIMEOUT_SECONDS  # 1.5s
        self.hft_timeout_ms = DEFAULT_HFT_TIMEOUT_MS  # 1500ms
        self.standard_timeout_seconds = DEFAULT_STANDARD_TIMEOUT_SECONDS
        self.use_maker_for_hft = True  # Always use maker orders for HFT
        self.stale_fill_threshold_ms = STALE_FILL_THRESHOLD_MS

        # Pending orders tracking (for timeout management)
        self._pending_orders: Dict[str, PendingOrder] = {}
        self._pending_orders_lock = asyncio.Lock()
        self._timeout_check_task: Optional[asyncio.Task] = None

        # REQUISITO 4.3: TP/SL bot-side monitoring
        self._tpsl_monitor_task: Optional[asyncio.Task] = None
        self._tpsl_check_interval = 0.1  # 100ms per reattività HFT

        # Alert callback
        self._alert_callback = None

    def on_alert(self, callback):
        """Register alert callback."""
        self._alert_callback = callback

    async def _send_alert(self, message: str, severity: AlertSeverity):
        """Send alert."""
        if self._alert_callback:
            try:
                await self._alert_callback(message, severity)
            except Exception:
                pass

    async def _on_fill_event(self, fill_data: dict):
        """
        Handle fill event from WebSocket.

        Fill data format:
        {
            "coin": "BTC",
            "oid": 123456,
            "px": "98765.4",
            "sz": "0.01",
            "side": "B" or "A",
            "time": 1234567890000,
            "fee": "0.02"
        }
        """
        try:
            hl_order_id = str(fill_data.get("oid"))
            filled_price = Decimal(str(fill_data.get("px", 0)))
            filled_size = Decimal(str(fill_data.get("sz", 0)))
            fee_str = fill_data.get("fee", "0")
            fees = Decimal(str(fee_str)) if fee_str else Decimal(0)
            fill_time = fill_data.get("time", 0)  # Unix timestamp in ms
            symbol = fill_data.get("coin", "")

            # Context Pack 2.0: Stale fill protection
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            fill_age_ms = now_ms - fill_time if fill_time else 0

            if fill_age_ms > STALE_FILL_THRESHOLD_MS:
                # Check if this is an HFT order
                pending = None
                for order_id, p in self._pending_orders.items():
                    if p.hl_order_id == hl_order_id:
                        pending = p
                        break

                if pending and pending.is_hft:
                    # Get current market price
                    try:
                        mids = self.info.all_mids()
                        current_price = Decimal(str(mids.get(symbol, 0)))

                        if current_price > 0:
                            price_deviation = abs((filled_price - current_price) / current_price)

                            if price_deviation > Decimal(str(MAX_PRICE_DEVIATION_PCT)):
                                logger.warning(
                                    f"STALE FILL DETECTED: {symbol} | "
                                    f"Fill age: {fill_age_ms}ms | "
                                    f"Price deviation: {price_deviation:.4%} | "
                                    f"Filled @ {filled_price}, Current @ {current_price}"
                                )
                                # Mark as stale but still process (for tracking)
                                # The position will need manual review
                                await self._send_alert(
                                    f"STALE FILL: {symbol} filled @ {filled_price} "
                                    f"({fill_age_ms}ms old, {price_deviation:.2%} deviation)",
                                    AlertSeverity.WARNING
                                )
                    except Exception as e:
                        logger.warning(f"Could not verify stale fill: {e}")

            logger.info(
                f"Processing fill: HL order {hl_order_id} - "
                f"{filled_size} @ {filled_price} (fee: {fees}, age: {fill_age_ms}ms)"
            )

            # Update order status in order manager
            await self.order_manager.update_order_status(
                hl_order_id,
                OrderStatus.FILLED,
                filled_size=filled_size,
                filled_price=filled_price,
                fees=fees,
            )

            # Remove from pending tracking
            # Try to find by HL order ID
            async with self._pending_orders_lock:
                for order_id, pending in list(self._pending_orders.items()):
                    if pending.hl_order_id == hl_order_id:
                        if order_id in self._pending_orders:
                            del self._pending_orders[order_id]
                        break

        except Exception as e:
            logger.error(f"Error processing fill event: {e} - data: {fill_data}")

    # -------------------------------------------------------------------------
    # HFT Timeout Management
    # -------------------------------------------------------------------------
    def is_hft_strategy(self, strategy_id: StrategyId) -> bool:
        """Check if strategy is HFT type."""
        return strategy_id in HFT_STRATEGIES

    def get_order_timeout(self, order: ApprovedOrder) -> float:
        """Get timeout for an order based on strategy type."""
        if self.is_hft_strategy(order.strategy_id):
            # Check for strategy-specific timeout in config
            hft_config = getattr(self.settings.strategies, 'hft', None)
            if hft_config:
                strategy_map = {
                    StrategyId.MMR_HFT: 'mmr_hft',
                    StrategyId.MICRO_BREAKOUT: 'micro_breakout',
                    StrategyId.PAIR_TRADING: 'pair_trading',
                    StrategyId.LIQUIDATION_SNIPING: 'liquidation_sniping',
                }
                config_name = strategy_map.get(order.strategy_id)
                if config_name:
                    strategy_cfg = getattr(hft_config, config_name, None)
                    if strategy_cfg:
                        return getattr(strategy_cfg, 'order_timeout_seconds', self.hft_timeout_seconds)
            return self.hft_timeout_seconds
        return self.standard_timeout_seconds

    async def start_timeout_monitor(self):
        """Start the timeout monitoring task."""
        if self._timeout_check_task is None or self._timeout_check_task.done():
            self._timeout_check_task = asyncio.create_task(self._timeout_monitor_loop())
            logger.info("Order timeout monitor started")

    async def stop_timeout_monitor(self):
        """Stop the timeout monitoring task."""
        if self._timeout_check_task and not self._timeout_check_task.done():
            self._timeout_check_task.cancel()
            try:
                await self._timeout_check_task
            except asyncio.CancelledError:
                pass
            logger.info("Order timeout monitor stopped")

    async def start_tpsl_monitor(self):
        """
        REQUISITO 4.3: Start bot-side TP/SL monitoring.

        Monitora continuamente i prezzi e chiude posizioni quando
        TP o SL vengono raggiunti, usando limit orders con fallback a market.
        """
        if self._tpsl_monitor_task is None or self._tpsl_monitor_task.done():
            self._tpsl_monitor_task = asyncio.create_task(self._tpsl_monitor_loop())
            logger.info("Bot-side TP/SL monitor started")

    async def stop_tpsl_monitor(self):
        """Stop the TP/SL monitoring task."""
        if self._tpsl_monitor_task and not self._tpsl_monitor_task.done():
            self._tpsl_monitor_task.cancel()
            try:
                await self._tpsl_monitor_task
            except asyncio.CancelledError:
                pass
            logger.info("Bot-side TP/SL monitor stopped")

    async def _timeout_monitor_loop(self):
        """
        Background loop to check and cancel timed-out orders.

        REQUISITO 4.1: Con timeout di 150-250ms, monitoriamo ogni 20ms
        per garantire cancellazione tempestiva degli ordini scaduti.
        """
        while True:
            try:
                # Check ogni 20ms per reattività con timeout di 200ms
                await asyncio.sleep(0.02)
                await self._check_pending_timeouts()
                # REQUISITO 4.2: Controllo stale fill protection
                await self._check_stale_orders()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in timeout monitor: {e}")
                await asyncio.sleep(1)

    async def _check_pending_timeouts(self):
        """Check all pending orders for timeout and cancel if expired."""
        expired_orders = []

        async with self._pending_orders_lock:
            for order_id, pending in list(self._pending_orders.items()):
                if pending.is_expired:
                    expired_orders.append((order_id, pending))

        for order_id, pending in expired_orders:
            await self._handle_order_timeout(order_id, pending)

    async def _check_stale_orders(self):
        """
        REQUISITO 4.2: Stale Fill Protection
        Cancella immediatamente ordini maker se price deviation >= 0.05%
        """
        to_cancel = []

        async with self._pending_orders_lock:
            for order_id, pending in list(self._pending_orders.items()):
                if not pending.is_hft or not pending.hl_order_id:
                    continue

                # Ottieni prezzo corrente
                try:
                    mids = self.info.all_mids()
                    current_mid = Decimal(str(mids.get(pending.order.symbol, 0)))

                    if current_mid <= 0 or not pending.order.price:
                        continue

                    # Calcola deviazione dal prezzo dell'ordine
                    order_price = pending.order.price
                    deviation = abs(current_mid - order_price) / current_mid

                    # Se deviazione >= 0.05%, cancella immediatamente
                    if deviation >= Decimal(str(MAX_PRICE_DEVIATION_PCT)):
                        logger.warning(
                            f"STALE ORDER DETECTED: {pending.order.symbol} | "
                            f"Order @ {order_price}, Mid @ {current_mid} | "
                            f"Deviation: {deviation:.4%} >= {MAX_PRICE_DEVIATION_PCT:.4%}"
                        )
                        to_cancel.append((order_id, pending, current_mid, deviation))

                except Exception as e:
                    logger.debug(f"Error checking stale order {order_id}: {e}")

        # Cancella ordini stale
        for order_id, pending, current_mid, deviation in to_cancel:
            await self._handle_stale_order(order_id, pending, current_mid, deviation)

    async def _handle_stale_order(
        self,
        order_id: str,
        pending: PendingOrder,
        current_mid: Decimal,
        deviation: Decimal
    ):
        """Handle a stale order (price deviated too much)."""
        # Cancella l'ordine
        if pending.hl_order_id:
            cancelled = await self.cancel_order(pending.order.symbol, pending.hl_order_id)
            if cancelled:
                logger.info(
                    f"Stale order cancelled: {pending.hl_order_id} "
                    f"(deviation: {deviation:.4%})"
                )
            else:
                logger.warning(f"Failed to cancel stale order: {pending.hl_order_id}")

        # Update order status
        pending.order.status = OrderStatus.CANCELLED

        # Remove from pending
        async with self._pending_orders_lock:
            if order_id in self._pending_orders:
                del self._pending_orders[order_id]

        # Update order manager
        await self.order_manager.update_order_status(
            order_id,
            OrderStatus.CANCELLED,
        )

        # Alert
        await self._send_alert(
            f"STALE ORDER: {pending.order.symbol} cancelled "
            f"(price moved {deviation:.4%})",
            AlertSeverity.INFO
        )

    async def _handle_order_timeout(self, order_id: str, pending: PendingOrder):
        """
        REQUISITO 4.1: ALO Smart Routing
        Se ordine timeout:
        1. Cancella ordine
        2. Ricalcola prezzo
        3. Ripiazza se segnale ancora valido
        """
        logger.warning(
            f"Order timeout: {pending.order.symbol} {pending.order.side.value} "
            f"after {pending.timeout_seconds}s (HFT: {pending.is_hft})"
        )

        # 1. Cancella l'ordine
        if pending.hl_order_id:
            cancelled = await self.cancel_order(pending.order.symbol, pending.hl_order_id)
            if cancelled:
                logger.info(f"Timed-out order cancelled: {pending.hl_order_id}")
            else:
                logger.warning(f"Failed to cancel timed-out order: {pending.hl_order_id}")

        # Update order status
        pending.order.status = OrderStatus.EXPIRED

        # Remove from pending
        async with self._pending_orders_lock:
            if order_id in self._pending_orders:
                del self._pending_orders[order_id]

        # Update order manager
        await self.order_manager.update_order_status(
            order_id,
            OrderStatus.EXPIRED,
        )

        # 2-3. Per ordini HFT: valuta se ripiazzare con prezzo aggiornato
        if pending.is_hft:
            await self._retry_hft_order_if_valid(pending)

    async def _retry_hft_order_if_valid(self, pending: PendingOrder):
        """
        REQUISITO 4.1: Ripiazza ordine HFT con prezzo aggiornato se segnale ancora valido.

        Nota: Il bot deve determinare se il segnale è ancora valido.
        Per ora loggiamo l'evento - la logica di retry può essere implementata
        dalla strategy che aveva generato il segnale.
        """
        try:
            # Ottieni prezzo corrente
            mids = self.info.all_mids()
            current_mid = Decimal(str(mids.get(pending.order.symbol, 0)))

            if current_mid <= 0:
                return

            logger.info(
                f"HFT order timeout: {pending.order.symbol} | "
                f"Original price: {pending.order.price} | "
                f"Current mid: {current_mid} | "
                f"Strategy can re-evaluate signal and resubmit if still valid"
            )

            # Alert per monitoraggio
            await self._send_alert(
                f"HFT timeout: {pending.order.symbol} @ {current_mid}\n"
                f"Original: {pending.order.price}\n"
                f"Strategy: {pending.order.strategy_id.value}",
                AlertSeverity.INFO
            )

        except Exception as e:
            logger.error(f"Error in retry evaluation: {e}")

    # -------------------------------------------------------------------------
    # REQUISITO 4.3: Bot-side TP/SL Monitoring
    # -------------------------------------------------------------------------
    async def _tpsl_monitor_loop(self):
        """
        Loop di monitoraggio TP/SL bot-side.

        Controlla continuamente i prezzi delle posizioni aperte e
        chiude quando TP o SL vengono raggiunti.
        """
        while True:
            try:
                await asyncio.sleep(self._tpsl_check_interval)
                await self._check_tpsl_triggers()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in TP/SL monitor: {e}")
                await asyncio.sleep(1)

    async def _check_tpsl_triggers(self):
        """
        Controlla se TP o SL sono stati raggiunti per le posizioni aperte.

        REQUISITO 4.3:
        - Monitora price feed continuo
        - Se TP/SL raggiunti: chiude con limit order al best price
        - Fallback a market solo se necessario
        """
        positions = self.order_manager.get_all_positions()

        if not positions:
            return

        # Ottieni tutti i prezzi correnti in una sola chiamata
        try:
            mids = self.info.all_mids()
        except Exception as e:
            logger.debug(f"Error fetching mids for TP/SL check: {e}")
            return

        for position in positions:
            try:
                current_price = Decimal(str(mids.get(position.symbol, 0)))
                if current_price <= 0:
                    continue

                # Update position price
                await self.order_manager.update_position_price(position.symbol, current_price)

                # Check SL
                if position.stop_loss_price and self._is_stop_loss_hit(position, current_price):
                    logger.warning(
                        f"STOP LOSS HIT: {position.symbol} | "
                        f"Current: {current_price} | SL: {position.stop_loss_price}"
                    )
                    await self._close_position_at_tpsl(
                        position,
                        current_price,
                        is_stop_loss=True
                    )
                    continue

                # Check TP
                if position.take_profit_price and self._is_take_profit_hit(position, current_price):
                    logger.info(
                        f"TAKE PROFIT HIT: {position.symbol} | "
                        f"Current: {current_price} | TP: {position.take_profit_price}"
                    )
                    await self._close_position_at_tpsl(
                        position,
                        current_price,
                        is_stop_loss=False
                    )

            except Exception as e:
                logger.error(f"Error checking TP/SL for {position.symbol}: {e}")

    def _is_stop_loss_hit(self, position: Position, current_price: Decimal) -> bool:
        """Check if stop loss is hit."""
        if not position.stop_loss_price:
            return False

        if position.side == Side.LONG:
            # Long: SL è sotto entry, triggera se prezzo <= SL
            return current_price <= position.stop_loss_price
        else:
            # Short: SL è sopra entry, triggera se prezzo >= SL
            return current_price >= position.stop_loss_price

    def _is_take_profit_hit(self, position: Position, current_price: Decimal) -> bool:
        """Check if take profit is hit."""
        if not position.take_profit_price:
            return False

        if position.side == Side.LONG:
            # Long: TP è sopra entry, triggera se prezzo >= TP
            return current_price >= position.take_profit_price
        else:
            # Short: TP è sotto entry, triggera se prezzo <= TP
            return current_price <= position.take_profit_price

    async def _close_position_at_tpsl(
        self,
        position: Position,
        current_price: Decimal,
        is_stop_loss: bool
    ):
        """
        REQUISITO 4.3: Chiude posizione al TP/SL usando limit order con fallback a market.

        Processo:
        1. Cancella eventuali ordini TP/SL exchange (se esistono)
        2. Prova chiusura con limit order al miglior prezzo disponibile
        3. Se limit fallisce dopo timeout breve, usa market order
        """
        reason = ExitReason.STOP_LOSS if is_stop_loss else ExitReason.TAKE_PROFIT
        action = "SL" if is_stop_loss else "TP"

        logger.info(
            f"Closing position at {action}: {position.symbol} | "
            f"Size: {position.size} | Current: {current_price}"
        )

        # 1. Cancella ordini TP/SL exchange se esistono
        managed_pos = self.order_manager.get_managed_position(position.symbol)
        if managed_pos:
            if managed_pos.stop_order_id:
                await self.cancel_order(position.symbol, managed_pos.stop_order_id)
            if managed_pos.tp_order_id:
                await self.cancel_order(position.symbol, managed_pos.tp_order_id)

        # 2. Prova chiusura con aggressive limit order
        close_success = await self._try_limit_close(position, current_price)

        # 3. Fallback a market se limit fallisce
        if not close_success:
            logger.warning(
                f"Limit close failed for {position.symbol}, using market order fallback"
            )
            await self._try_market_close(position, reason)

    async def _try_limit_close(
        self,
        position: Position,
        current_price: Decimal,
        timeout_seconds: float = 0.5
    ) -> bool:
        """
        Tenta chiusura con aggressive limit order.

        Returns True se completata, False altrimenti.
        """
        if not await self.rate_limiter.acquire(timeout=10):
            return False

        try:
            # Ordine opposto alla posizione per chiudere
            is_buy = position.side == Side.SHORT
            size = self._round_size(position.symbol, position.size)

            # Aggressive limit: prezzo leggermente peggiore per fill rapido
            if is_buy:
                # Comprare per chiudere short: prezzo leggermente sopra mid
                limit_price = self._round_price(
                    position.symbol,
                    current_price * Decimal("1.0005")  # +0.05% per fill rapido
                )
            else:
                # Vendere per chiudere long: prezzo leggermente sotto mid
                limit_price = self._round_price(
                    position.symbol,
                    current_price * Decimal("0.9995")  # -0.05% per fill rapido
                )

            # Piazza aggressive limit order con IOC (Immediate or Cancel)
            result = self.exchange.order(
                name=position.symbol,
                is_buy=is_buy,
                sz=size,
                limit_px=limit_price,
                order_type={"limit": {"tif": "Ioc"}},
                reduce_only=True,
            )

            if result.get("status") == "ok":
                response_data = result.get("response", {})
                if "data" in response_data:
                    statuses = response_data["data"].get("statuses", [])
                    if statuses and "filled" in statuses[0]:
                        logger.info(
                            f"Position closed via limit order: {position.symbol} @ {limit_price}"
                        )
                        return True

            return False

        except Exception as e:
            logger.error(f"Error in limit close for {position.symbol}: {e}")
            return False

    async def _try_market_close(
        self,
        position: Position,
        reason: ExitReason
    ) -> bool:
        """
        Fallback: chiusura market order.

        NOTA: Usare solo come ultimo resort perché taker fee è 0.05% vs 0.02% maker.
        """
        if not await self.rate_limiter.acquire(timeout=10):
            return False

        try:
            result = self.exchange.market_close(position.symbol)

            if result.get("status") == "ok":
                logger.info(
                    f"Position closed via MARKET order: {position.symbol} "
                    f"(fallback, taker fee applied)"
                )

                # Get current price from result or fetch
                mids = self.info.all_mids()
                current_price = Decimal(str(mids.get(position.symbol, 0)))

                # Update order manager
                await self.order_manager.update_order_status(
                    f"close_{position.symbol}_{reason.value}",
                    OrderStatus.FILLED,
                    filled_size=position.size,
                    filled_price=current_price,
                )

                await self._send_alert(
                    f"Position closed at {reason.value}: {position.symbol}\n"
                    f"Method: MARKET (fallback)\n"
                    f"Price: {current_price}",
                    AlertSeverity.INFO
                )

                return True
            else:
                logger.error(f"Market close failed for {position.symbol}: {result}")
                await self._send_alert(
                    f"CRITICAL: Failed to close {position.symbol} at {reason.value}",
                    AlertSeverity.CRITICAL
                )
                return False

        except Exception as e:
            logger.error(f"Error in market close for {position.symbol}: {e}")
            await self._send_alert(
                f"CRITICAL: Error closing {position.symbol}: {e}",
                AlertSeverity.CRITICAL
            )
            return False

    async def _track_pending_order(
        self,
        order: ApprovedOrder,
        hl_order_id: Optional[str],
        timeout_seconds: float,
    ):
        """Add an order to pending tracking."""
        is_hft = self.is_hft_strategy(order.strategy_id)
        pending = PendingOrder(
            order=order,
            hl_order_id=hl_order_id,
            submitted_at=datetime.now(timezone.utc),
            timeout_seconds=timeout_seconds,
            is_hft=is_hft,
        )
        async with self._pending_orders_lock:
            self._pending_orders[order.order_id] = pending

    async def _untrack_pending_order(self, order_id: str):
        """Remove an order from pending tracking."""
        async with self._pending_orders_lock:
            if order_id in self._pending_orders:
                del self._pending_orders[order_id]

    def get_pending_orders_count(self) -> int:
        """Get count of pending orders."""
        return len(self._pending_orders)

    async def get_pending_hft_orders(self) -> List[PendingOrder]:
        """Get list of pending HFT orders."""
        async with self._pending_orders_lock:
            return [p for p in self._pending_orders.values() if p.is_hft]

    # -------------------------------------------------------------------------
    # Order Execution
    # -------------------------------------------------------------------------
    async def execute_order(
        self,
        order: ApprovedOrder,
        current_price: Decimal,
        spread: Optional[Decimal] = None,
    ) -> bool:
        """
        Execute an approved order.

        Returns True if successful.
        """
        try:
            # Check if order should be split
            notional = order.size * current_price
            if notional > self.chunk_size_usd:
                return await self._execute_chunked(order, current_price, spread)

            # Single order execution
            return await self._execute_single(order, current_price, spread)

        except Exception as e:
            logger.error(f"Execution error for {order.order_id}: {e}")
            order.status = OrderStatus.REJECTED
            await self._send_alert(
                f"Order execution failed: {order.symbol} {order.side.value}\n{e}",
                AlertSeverity.WARNING
            )
            return False

    async def _execute_single(
        self,
        order: ApprovedOrder,
        current_price: Decimal,
        spread: Optional[Decimal] = None,
    ) -> bool:
        """Execute a single order with HFT-aware order type selection."""
        is_hft = self.is_hft_strategy(order.strategy_id)

        # Determine order type based on strategy and spread
        # HFT MUST use maker orders to be profitable (fee: 0.02% vs 0.05% taker)
        use_market = order.order_type == OrderType.MARKET

        if is_hft and self.use_maker_for_hft:
            # HFT always uses maker (post-only) orders
            use_market = False
            use_maker_only = True
            logger.debug(f"HFT order for {order.symbol}: forcing maker order")
        elif use_market and spread:
            spread_bps = (spread / current_price) * 10000
            if spread_bps > self.max_spread_bps:
                # Convert to aggressive limit
                logger.info(
                    f"Spread too wide ({spread_bps:.1f} bps), "
                    f"using aggressive limit for {order.symbol}"
                )
                use_market = False
            use_maker_only = False
        else:
            use_maker_only = False

        # Set leverage before placing order
        await self._set_leverage(order.symbol, int(order.leverage_used))

        # Acquire rate limit
        if not await self.rate_limiter.acquire(timeout=30):
            raise ExecutionError("Rate limit timeout", order_id=order.order_id)

        # Get timeout for this order
        timeout_seconds = self.get_order_timeout(order)

        # Place order
        try:
            order.status = OrderStatus.SUBMITTED

            if use_market:
                result = await self._place_market_order(order)
            elif use_maker_only:
                # Use post-only (GTX) limit order for HFT
                result = await self._place_maker_order(order, current_price)
            else:
                result = await self._place_limit_order(order, current_price)

            # Handle result
            if result.get("status") == "ok":
                response_data = result.get("response", {})

                # Extract order ID from response
                hl_order_id = None
                if "data" in response_data:
                    statuses = response_data["data"].get("statuses", [])
                    if statuses and "resting" in statuses[0]:
                        hl_order_id = str(statuses[0]["resting"]["oid"])
                        # Track pending order for timeout
                        await self._track_pending_order(order, hl_order_id, timeout_seconds)
                    elif statuses and "filled" in statuses[0]:
                        hl_order_id = str(statuses[0]["filled"]["oid"])
                        order.status = OrderStatus.FILLED
                        order.filled_size = order.size
                        order.filled_price = current_price
                        order.executed_at = datetime.now(timezone.utc)
                    elif statuses and "error" in statuses[0]:
                        # Post-only order rejected (would have been taker)
                        error_msg = statuses[0].get("error", "Unknown error")
                        if "cross" in error_msg.lower() or "would take" in error_msg.lower():
                            logger.warning(
                                f"Maker order rejected (would cross spread): {order.symbol}"
                            )
                            order.status = OrderStatus.REJECTED
                            return False
                        raise ExecutionError(error_msg, order_id=order.order_id)

                await self.order_manager.add_order(order, hl_order_id, result)

                logger.info(
                    f"Order placed: {order.side.value} {order.size} {order.symbol} "
                    f"(HL ID: {hl_order_id}, HFT: {is_hft}, timeout: {timeout_seconds}s)"
                )

                # Place SL/TP if specified and filled immediately
                if order.status == OrderStatus.FILLED:
                    await self._untrack_pending_order(order.order_id)
                    await self._place_sl_tp(order, current_price)

                return True

            else:
                error_msg = result.get("response", {}).get("error", str(result))
                raise ExecutionError(error_msg, order_id=order.order_id, response=result)

        except Exception as e:
            order.status = OrderStatus.REJECTED
            raise ExecutionError(str(e), order_id=order.order_id)

    async def _execute_chunked(
        self,
        order: ApprovedOrder,
        current_price: Decimal,
        spread: Optional[Decimal] = None,
    ) -> bool:
        """Execute a large order in chunks."""
        total_size = order.size
        chunk_size = self.chunk_size_usd / current_price

        filled_size = Decimal(0)
        chunk_num = 0

        logger.info(
            f"Splitting {order.symbol} order into chunks: "
            f"{total_size} / {chunk_size:.4f} per chunk"
        )

        while filled_size < total_size:
            chunk_num += 1
            remaining = total_size - filled_size
            this_chunk = min(remaining, chunk_size)

            # Create chunk order
            chunk_order = ApprovedOrder(
                order_id=f"{order.order_id}_c{chunk_num}",
                strategy_id=order.strategy_id,
                symbol=order.symbol,
                side=order.side,
                size=this_chunk,
                order_type=order.order_type,
                price=order.price,
                leverage_used=order.leverage_used,
            )

            # Execute chunk
            success = await self._execute_single(chunk_order, current_price, spread)
            if not success:
                logger.warning(f"Chunk {chunk_num} failed, stopping")
                break

            filled_size += this_chunk

            # Small delay between chunks
            if filled_size < total_size:
                await asyncio.sleep(0.5)

        # Update original order
        order.filled_size = filled_size
        order.status = (
            OrderStatus.FILLED
            if filled_size >= total_size
            else OrderStatus.PARTIALLY_FILLED
        )

        if filled_size > 0:
            order.filled_price = current_price
            order.executed_at = datetime.now(timezone.utc)
            await self._place_sl_tp(order, current_price)

        return filled_size > 0

    async def _place_market_order(self, order: ApprovedOrder) -> dict:
        """Place a market order."""
        is_buy = order.side == Side.LONG
        size = self._round_size(order.symbol, order.size)

        return self.exchange.market_open(
            name=order.symbol,
            is_buy=is_buy,
            sz=size,
            px=None,
            slippage=0.01,  # 1% slippage tolerance
        )

    async def _place_limit_order(
        self,
        order: ApprovedOrder,
        current_price: Decimal,
    ) -> dict:
        """Place a limit order (aggressive, at current price)."""
        is_buy = order.side == Side.LONG
        size = self._round_size(order.symbol, order.size)

        # Use current best price for aggressive limit
        if order.price:
            price = self._round_price(order.symbol, order.price)
        else:
            # Aggressive limit: slightly better than market
            if is_buy:
                price = self._round_price(
                    order.symbol,
                    current_price * Decimal("1.001")
                )
            else:
                price = self._round_price(
                    order.symbol,
                    current_price * Decimal("0.999")
                )

        return self.exchange.order(
            name=order.symbol,
            is_buy=is_buy,
            sz=size,
            limit_px=price,
            order_type={"limit": {"tif": "Ioc"}},  # Immediate or cancel
        )

    async def _place_maker_order(
        self,
        order: ApprovedOrder,
        current_price: Decimal,
    ) -> dict:
        """
        Place a maker-only (post-only) limit order for HFT.

        Post-only orders are rejected if they would cross the spread
        and become taker orders. This ensures we only pay maker fees (0.02%).

        For profitability:
        - Maker fee: 0.02%
        - Min TP must be > 0.04% (entry + exit fees)
        """
        is_buy = order.side == Side.LONG
        size = self._round_size(order.symbol, order.size)

        # For post-only, price should be at or slightly inside the spread
        # This increases chances of fill while staying maker
        if order.price:
            price = self._round_price(order.symbol, order.price)
        else:
            # Place at the current bid/ask to be at front of queue
            # Slightly inside for better fill probability
            if is_buy:
                # Buy at slightly below current price (maker bid)
                price = self._round_price(
                    order.symbol,
                    current_price * Decimal("0.9999")  # 0.01% below
                )
            else:
                # Sell at slightly above current price (maker ask)
                price = self._round_price(
                    order.symbol,
                    current_price * Decimal("1.0001")  # 0.01% above
                )

        # Use ALO (Add Liquidity Only / post-only) order type
        # This order will be rejected if it would cross the spread
        return self.exchange.order(
            name=order.symbol,
            is_buy=is_buy,
            sz=size,
            limit_px=price,
            order_type={"limit": {"tif": "Alo"}},  # Add Liquidity Only (post-only)
        )

    async def _place_sl_tp(self, order: ApprovedOrder, current_price: Decimal):
        """
        Place stop loss and take profit orders on exchange.

        REQUISITO 4.3: Questi ordini exchange sono solo "hard stop" di emergenza.
        La gestione primaria di TP/SL è bot-side tramite _tpsl_monitor_loop(),
        che monitora continuamente i prezzi e chiude con limit orders.

        Gli ordini exchange servono come backup in caso di:
        - Crash del bot
        - Disconnessione temporanea
        - Altri eventi imprevisti
        """
        try:
            # Place stop loss (hard stop di emergenza)
            if order.stop_loss_price:
                sl_result = await self._place_stop_order(
                    order.symbol,
                    order.side,
                    order.filled_size,
                    order.stop_loss_price,
                    is_stop_loss=True,
                )
                if sl_result:
                    await self.order_manager.set_stop_order_id(order.symbol, sl_result)
                    logger.info(
                        f"Emergency SL placed for {order.symbol} @ {order.stop_loss_price} "
                        f"(backup only, primary is bot-side)"
                    )

            # Place take profit (hard stop di emergenza)
            if order.take_profit_price:
                tp_result = await self._place_stop_order(
                    order.symbol,
                    order.side,
                    order.filled_size,
                    order.take_profit_price,
                    is_stop_loss=False,
                )
                if tp_result:
                    await self.order_manager.set_tp_order_id(order.symbol, tp_result)
                    logger.info(
                        f"Emergency TP placed for {order.symbol} @ {order.take_profit_price} "
                        f"(backup only, primary is bot-side)"
                    )

        except Exception as e:
            logger.error(f"Failed to place emergency SL/TP for {order.symbol}: {e}")
            await self._send_alert(
                f"Emergency SL/TP placement failed for {order.symbol}: {e}",
                AlertSeverity.WARNING
            )

    async def _place_stop_order(
        self,
        symbol: str,
        position_side: Side,
        size: Decimal,
        trigger_price: Decimal,
        is_stop_loss: bool,
    ) -> Optional[str]:
        """Place a stop order (SL or TP)."""
        # Stop orders close the position, so opposite side
        is_buy = position_side == Side.SHORT

        if not await self.rate_limiter.acquire(timeout=10):
            return None

        size = self._round_size(symbol, size)
        price = self._round_price(symbol, trigger_price)

        # Hyperliquid uses trigger orders
        result = self.exchange.order(
            name=symbol,
            is_buy=is_buy,
            sz=size,
            limit_px=price,
            order_type={
                "trigger": {
                    "triggerPx": str(price),
                    "isMarket": True,
                    "tpsl": "sl" if is_stop_loss else "tp",
                }
            },
            reduce_only=True,
        )

        if result.get("status") == "ok":
            response_data = result.get("response", {})
            if "data" in response_data:
                statuses = response_data["data"].get("statuses", [])
                if statuses and "resting" in statuses[0]:
                    return str(statuses[0]["resting"]["oid"])
        return None

    # -------------------------------------------------------------------------
    # Position Close
    # -------------------------------------------------------------------------
    async def close_position(
        self,
        symbol: str,
        size: Optional[Decimal] = None,
        reason: ExitReason = ExitReason.SIGNAL_EXIT,
    ) -> bool:
        """Close a position (or partial)."""
        position = self.order_manager.get_position(symbol)
        if not position:
            logger.warning(f"No position to close for {symbol}")
            return False

        close_size = size or position.size
        is_buy = position.side == Side.SHORT

        if not await self.rate_limiter.acquire(timeout=10):
            return False

        try:
            result = self.exchange.market_close(symbol)

            if result.get("status") == "ok":
                logger.info(f"Position closed: {symbol}")

                # Get current price from result or fetch
                mids = self.info.all_mids()
                current_price = Decimal(str(mids.get(symbol, 0)))

                # Update order manager
                await self.order_manager.update_order_status(
                    f"close_{symbol}",
                    OrderStatus.FILLED,
                    filled_size=close_size,
                    filled_price=current_price,
                )

                return True
            else:
                logger.error(f"Failed to close {symbol}: {result}")
                return False

        except Exception as e:
            logger.error(f"Error closing {symbol}: {e}")
            return False

    async def close_all_positions(self) -> int:
        """Close all positions (emergency)."""
        positions = self.order_manager.get_all_positions()
        closed = 0

        for position in positions:
            if await self.close_position(position.symbol, reason=ExitReason.CIRCUIT_BREAKER):
                closed += 1

        return closed

    # -------------------------------------------------------------------------
    # Order Cancellation
    # -------------------------------------------------------------------------
    async def cancel_order(self, symbol: str, hl_order_id: str) -> bool:
        """Cancel an order."""
        if not await self.rate_limiter.acquire(timeout=10):
            return False

        try:
            result = self.exchange.cancel(symbol, int(hl_order_id))
            if result.get("status") == "ok":
                await self.order_manager.update_order_status(
                    hl_order_id,
                    OrderStatus.CANCELLED,
                )
                return True
            return False
        except Exception as e:
            logger.error(f"Error cancelling order {hl_order_id}: {e}")
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """Cancel all orders for a symbol or all symbols."""
        cancelled = 0

        try:
            if symbol:
                result = self.exchange.cancel_all_orders(symbol)
            else:
                # Cancel for all active symbols
                for s in self._symbol_info.keys():
                    try:
                        self.exchange.cancel_all_orders(s)
                        cancelled += 1
                    except Exception:
                        pass
                return cancelled

            if result.get("status") == "ok":
                cancelled += 1

        except Exception as e:
            logger.error(f"Error cancelling orders: {e}")

        return cancelled

    # -------------------------------------------------------------------------
    # Leverage Management
    # -------------------------------------------------------------------------
    async def _set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol."""
        try:
            result = self.exchange.update_leverage(
                leverage=leverage,
                name=symbol,
                is_cross=True,
            )
            return result.get("status") == "ok"
        except Exception as e:
            logger.warning(f"Failed to set leverage for {symbol}: {e}")
            return False

    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------
    def _round_size(self, symbol: str, size: Decimal) -> float:
        """Round size to symbol's decimals."""
        info = self._symbol_info.get(symbol, {})
        decimals = info.get("szDecimals", 4)
        quantizer = Decimal(10) ** -decimals
        rounded = size.quantize(quantizer, rounding=ROUND_DOWN)
        return float(rounded)

    def _round_price(self, symbol: str, price: Decimal) -> float:
        """Round price to symbol's tick size (5 significant figures)."""
        # Hyperliquid uses 5 significant figures for prices
        # e.g., 1234.5 is valid, but 1234.56 is not
        price_float = float(price)
        if price_float == 0:
            return 0.0

        # Calculate the order of magnitude
        from math import log10, floor
        magnitude = floor(log10(abs(price_float)))

        # Round to 5 significant figures
        scale = 10 ** (magnitude - 4)  # 5 sig figs = magnitude - 4
        rounded = round(price_float / scale) * scale

        # Ensure we don't have more than 6 decimal places
        if rounded < 1:
            # For small prices, limit decimal places
            return round(rounded, 6)
        else:
            # For larger prices, the significant figures rule applies
            return round(rounded, max(0, 4 - magnitude))

    def get_min_size(self, symbol: str) -> Decimal:
        """Get minimum order size for a symbol."""
        info = self._symbol_info.get(symbol, {})
        return Decimal(str(info.get("minSz", "0.001")))

    async def refresh_meta(self):
        """Refresh symbol metadata."""
        self._meta = self.info.meta()
        self._symbol_info = {p["name"]: p for p in self._meta.get("universe", [])}
