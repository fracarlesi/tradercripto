"""Main bot orchestrator."""

import asyncio
import logging
import signal
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from .config.settings import Settings, get_settings
from .core.models import ProposedTrade, ApprovedOrder, AccountState, Bar, MarketContext
from .core.enums import StrategyId, TimeFrame, AlertSeverity, MarketRegime, ExitReason, Side
from .data.market_data import MarketDataLayer
from .strategies import BaseStrategy
from .strategies.hft import (
    MMRHFTStrategy,
    MicroBreakoutStrategy,
    PairTradingStrategy,
    LiquidationSnipingStrategy,
    MomentumScalpingStrategy,
)
from .risk.risk_engine import RiskEngine
from .execution.execution_engine import ExecutionEngine
from .ai.regime_detector import RegimeDetector
from .ai.param_tuner import ParameterTuner
from .ai.aggression_controller import AggressionController
from .persistence.database import Database
from .monitoring.telegram_alerter import TelegramAlerter
from .monitoring.logger import setup_logging
from .monitoring.health_server import HealthServer
from .monitoring.hft_metrics import get_metrics_collector, HFTMetricsCollector


logger = logging.getLogger(__name__)


class HLQuantBot:
    """
    Main bot orchestrator.

    Coordinates all components:
    - Market data
    - Strategies
    - Risk engine
    - Execution engine
    - AI layer
    - Persistence
    - Monitoring
    """

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()

        # Components (initialized in start())
        self.market_data: Optional[MarketDataLayer] = None
        self.strategies: List[BaseStrategy] = []
        self.risk_engine: Optional[RiskEngine] = None
        self.execution: Optional[ExecutionEngine] = None
        self.regime_detector: Optional[RegimeDetector] = None
        self.param_tuner: Optional[ParameterTuner] = None
        self.aggression_controller: Optional[AggressionController] = None
        self.database: Optional[Database] = None
        self.telegram: Optional[TelegramAlerter] = None
        self.health_server: Optional[HealthServer] = None
        self.hft_metrics: HFTMetricsCollector = get_metrics_collector()

        # State
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._last_snapshot_time: Optional[datetime] = None
        self._last_regime_check: Optional[datetime] = None
        self._last_metrics_calc: Optional[datetime] = None

        # Intervals
        self._main_loop_interval = 1.0  # seconds
        self._snapshot_interval = 60  # seconds
        self._regime_check_interval = self.settings.openai.regime_detection_interval_minutes * 60
        self._metrics_calc_interval = 3600  # 1 hour

    async def start(self):
        """Start the bot."""
        logger.info("=" * 60)
        logger.info("Starting HLQuantBot...")
        logger.info(f"Environment: {'TESTNET' if self.settings.is_testnet else 'PRODUCTION'}")
        logger.info(f"Symbols: {self.settings.active_symbols}")
        logger.info("=" * 60)

        try:
            # Initialize components
            await self._init_components()

            # Setup signal handlers
            self._setup_signal_handlers()

            # Send startup notification
            if self.telegram:
                await self.telegram.send_startup_message()

            # Start main loop
            self._running = True
            await self._main_loop()

        except Exception as e:
            logger.exception(f"Fatal error: {e}")
            if self.telegram:
                await self.telegram.send_error_alert(str(e), "Bot startup")
            raise

        finally:
            await self.stop()

    async def stop(self, reason: str = "Normal shutdown"):
        """Stop the bot."""
        logger.info(f"Stopping HLQuantBot: {reason}")
        self._running = False
        self._shutdown_event.set()

        # Send shutdown notification
        if self.telegram:
            await self.telegram.send_shutdown_message(reason)

        # Cleanup components
        if self.health_server:
            await self.health_server.stop()
        if self.execution:
            await self.execution.stop_timeout_monitor()
            await self.execution.stop_tpsl_monitor()  # REQUISITO 4.3: Stop TP/SL monitor
        if self.market_data:
            await self.market_data.stop()
        if self.database:
            await self.database.close()
        if self.telegram:
            await self.telegram.close()

        logger.info("HLQuantBot stopped")

    async def _init_components(self):
        """Initialize all components."""
        logger.info("Initializing components...")

        # Database
        self.database = Database(self.settings)
        await self.database.connect()

        # Telegram
        self.telegram = TelegramAlerter(self.settings)

        # Market data
        self.market_data = MarketDataLayer(self.settings)
        await self.market_data.start()

        # Wait for initial data
        logger.info("Waiting for market data...")
        if not await self.market_data.wait_for_data(timeout=30):
            logger.warning("Timeout waiting for market data, continuing anyway")

        # HFT Strategies (Phase C)
        self.strategies = [
            MMRHFTStrategy(self.settings),
            MicroBreakoutStrategy(self.settings),
            PairTradingStrategy(self.settings),
            LiquidationSnipingStrategy(self.settings),
            MomentumScalpingStrategy(self.settings),
        ]
        logger.info(f"Loaded {len(self.strategies)} HFT strategies")

        # Risk engine
        self.risk_engine = RiskEngine(self.settings)
        account = self.market_data.get_account_state()
        if account:
            await self.risk_engine.initialize(account)

        # Wire up circuit breaker alerts
        self.risk_engine.circuit_breaker.on_alert(self._on_circuit_breaker_alert)

        # Execution engine
        self.execution = ExecutionEngine(self.settings)
        self.execution.on_alert(self._on_execution_alert)

        # Wire up order manager callbacks
        self.execution.order_manager.on_fill(self._on_order_fill)
        self.execution.order_manager.on_position_close(self._on_position_close)

        # Wire up WebSocket fill events to execution engine
        self.market_data.on_fill(self.execution._on_fill_event)

        # Start execution engine's timeout monitor for HFT orders
        await self.execution.start_timeout_monitor()

        # REQUISITO 4.3: Start bot-side TP/SL monitor
        await self.execution.start_tpsl_monitor()

        # AI layer
        self.regime_detector = RegimeDetector(self.settings)
        self.param_tuner = ParameterTuner(self.settings)

        # Aggression Controller (da specifica consigli.md - sezione 5)
        # Influenza rischio e leva in base a win rate, P&L, regime
        self.aggression_controller = AggressionController(self.settings)
        self.risk_engine.set_aggression_controller(self.aggression_controller)
        logger.info(f"AggressionController initialized, level: {self.aggression_controller.current_state.level.value}")

        # Health server for Docker/K8s monitoring
        self.health_server = HealthServer(self, port=8080)
        await self.health_server.start()

        logger.info("All components initialized")

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""
        loop = asyncio.get_event_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(self._handle_signal(s))
            )

    async def _handle_signal(self, sig):
        """Handle shutdown signal."""
        logger.info(f"Received signal {sig.name}")
        await self.stop(f"Signal {sig.name}")

    # -------------------------------------------------------------------------
    # Main Loop
    # -------------------------------------------------------------------------
    async def _main_loop(self):
        """Main trading loop."""
        logger.info("Starting main trading loop")
        loop_count = 0

        while self._running:
            try:
                loop_start = datetime.now(timezone.utc)
                loop_count += 1

                # Debug log EVERY iteration initially for troubleshooting
                logger.info(f"Main loop iteration {loop_count}")

                # Start HFT metrics measurement
                with self.hft_metrics.measure_loop() as metrics:
                    logger.info(f"Iter {loop_count}: Entered measure_loop")

                    # Check circuit breaker
                    if self.risk_engine.circuit_breaker.is_triggered:
                        logger.warning("Circuit breaker triggered - trading halted")
                        await asyncio.sleep(10)
                        continue

                    # Get current state
                    account = self.market_data.get_account_state()
                    if not account:
                        logger.warning("No account state available")
                        await asyncio.sleep(self._main_loop_interval)
                        continue

                    logger.info(f"Iter {loop_count}: Account equity={account.equity}")

                    # Update circuit breaker
                    await self.risk_engine.circuit_breaker.update(account.equity)

                    # Run regime detection periodically (with timing)
                    logger.info(f"Iter {loop_count}: Starting regime detection")
                    metrics.regime_start()
                    await self._maybe_detect_regime(account)
                    metrics.regime_end()
                    logger.info(f"Iter {loop_count}: Regime detection done")

                    # Get market data
                    contexts = self.market_data.get_all_market_contexts()
                    bars_by_symbol = self._get_bars_for_strategies()
                    prices = {s: ctx.mid_price for s, ctx in contexts.items()}
                    atrs = self._calculate_atrs(bars_by_symbol)

                    # Update positions with current prices
                    for symbol, price in prices.items():
                        await self.execution.order_manager.update_position_price(symbol, price)

                    # Check TP/SL for existing positions (bot-side monitoring)
                    await self._check_position_exits()

                    # Evaluate strategies (with timing)
                    metrics.strategy_start()
                    proposals = await self._evaluate_strategies(bars_by_symbol, contexts, account)
                    metrics.strategy_end()

                    # Filter out proposals for symbols with pending orders (prevent spam)
                    if proposals:
                        pending_orders = await self.execution.get_pending_hft_orders()
                        pending_symbols = {
                            p.order.symbol for p in pending_orders
                        }
                        if pending_symbols:
                            original_count = len(proposals)
                            proposals = [
                                p for p in proposals
                                if p.symbol not in pending_symbols
                            ]
                            filtered_count = original_count - len(proposals)
                            if filtered_count > 0:
                                logger.debug(
                                    f"Filtered {filtered_count} proposals for symbols "
                                    f"with pending orders: {pending_symbols}"
                                )

                    if proposals:
                        self.hft_metrics.record_signal()

                    # Process through risk engine (with timing)
                    metrics.risk_start()
                    approved_orders = []
                    if proposals:
                        approved_orders = await self.risk_engine.process_proposals(
                            proposals, account, prices, atrs
                        )
                    metrics.risk_end()

                    # Execute approved orders (with timing)
                    metrics.execution_start()
                    for order in approved_orders:
                        # Check dry-run mode
                        if self.hft_metrics.is_dry_run:
                            self.hft_metrics.record_dry_run_order({
                                "symbol": order.symbol,
                                "side": order.side.value,
                                "size": float(order.size),
                                "strategy": order.strategy_id.value,
                            })
                            logger.info(f"[DRY-RUN] Would execute: {order.symbol} {order.side.value} {order.size}")
                        else:
                            price = prices.get(order.symbol, Decimal(0))
                            spread = self.market_data.get_spread(order.symbol)
                            await self.execution.execute_order(order, price, spread)
                            self.hft_metrics.record_order()
                    metrics.execution_end()

                    # Save account snapshot periodically
                    await self._maybe_save_snapshot(account)

                    # Calculate metrics periodically
                    await self._maybe_calculate_metrics()

                # Sleep for remaining interval
                elapsed = (datetime.now(timezone.utc) - loop_start).total_seconds()
                sleep_time = max(0, self._main_loop_interval - elapsed)
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in main loop: {e}")
                if self.telegram:
                    await self.telegram.send_error_alert(str(e), "Main loop")
                await asyncio.sleep(5)  # Back off on error

    async def _evaluate_strategies(
        self,
        bars_by_symbol: Dict[str, List[Bar]],
        contexts: Dict[str, MarketContext],
        account: AccountState,
    ) -> List[ProposedTrade]:
        """Evaluate all strategies and collect proposals."""
        all_proposals = []

        # Get current regime
        regime = self.regime_detector.get_current_regime()

        # DEBUG: Log strategy evaluation
        logger.info(f"Evaluating {len(self.strategies)} strategies, regime={regime}")

        for strategy in self.strategies:
            if not strategy.is_enabled:
                logger.info(f"Strategy {strategy.name} is DISABLED, skipping")
                continue

            logger.info(f"Evaluating strategy {strategy.name} (symbols={strategy.symbols})")

            # Set regime in strategy
            strategy.set_regime(regime)

            try:
                proposals = await strategy.evaluate_all(bars_by_symbol, contexts, account)
                all_proposals.extend(proposals)

            except Exception as e:
                logger.error(f"Error evaluating {strategy.name}: {e}")

        return all_proposals

    def _get_bars_for_strategies(self) -> Dict[str, Dict[TimeFrame, List[Bar]]]:
        """Get bars for all symbols across multiple timeframes."""
        bars_by_symbol: Dict[str, Dict[TimeFrame, List[Bar]]] = {}

        for symbol in self.settings.active_symbols:
            bars_by_symbol[symbol] = {}

            # Get bars for each timeframe
            for tf in [TimeFrame.M1, TimeFrame.M5, TimeFrame.M15]:
                bars = self.market_data.get_bars(symbol, tf, count=100)
                if bars:
                    bars_by_symbol[symbol][tf] = bars

        # Debug log periodically (every ~100 iterations)
        if hash(str(datetime.now(timezone.utc).timestamp())[:10]) % 100 == 0:
            bar_counts = {
                s: {tf.value: len(b) for tf, b in tfs.items()}
                for s, tfs in bars_by_symbol.items()
            }
            logger.info(f"Bars available for strategies: {bar_counts}")

        return bars_by_symbol

    def _calculate_atrs(self, bars_by_symbol: Dict[str, Dict[TimeFrame, List[Bar]]]) -> Dict[str, Decimal]:
        """Calculate ATR for all symbols using M15 bars."""
        atrs = {}
        for symbol, tf_bars in bars_by_symbol.items():
            # Use M15 bars for ATR calculation (most stable)
            bars = tf_bars.get(TimeFrame.M15, [])
            if len(bars) >= 15:
                atr = BaseStrategy.calculate_atr(bars, period=14)
                atrs[symbol] = atr
        return atrs

    async def _check_position_exits(self):
        """
        Check all positions for TP/SL hits (bot-side monitoring).

        This provides an additional safety layer for TP/SL execution,
        complementing exchange-side orders. Useful when exchange orders
        might not fill or in volatile conditions.
        """
        if not self.execution:
            return

        for symbol in self.settings.active_symbols:
            position = self.execution.order_manager.get_position(symbol)
            if not position:
                continue

            tick = self.market_data.get_tick(symbol)
            if not tick:
                continue

            current_price = tick.mid_price

            # Check SL
            if position.stop_loss_price and position.stop_loss_price > 0:
                sl_hit = (
                    (position.side == Side.LONG and current_price <= position.stop_loss_price) or
                    (position.side == Side.SHORT and current_price >= position.stop_loss_price)
                )
                if sl_hit:
                    logger.warning(
                        f"[BOT-SL] Stop loss hit: {symbol} @ {current_price} "
                        f"(SL: {position.stop_loss_price})"
                    )
                    await self.execution.close_position(symbol, ExitReason.STOP_LOSS)
                    continue

            # Check TP
            if position.take_profit_price and position.take_profit_price > 0:
                tp_hit = (
                    (position.side == Side.LONG and current_price >= position.take_profit_price) or
                    (position.side == Side.SHORT and current_price <= position.take_profit_price)
                )
                if tp_hit:
                    logger.info(
                        f"[BOT-TP] Take profit hit: {symbol} @ {current_price} "
                        f"(TP: {position.take_profit_price})"
                    )
                    await self.execution.close_position(symbol, ExitReason.TAKE_PROFIT)

    # -------------------------------------------------------------------------
    # Periodic Tasks
    # -------------------------------------------------------------------------
    async def _maybe_detect_regime(self, account: AccountState):
        """Run regime detection if needed."""
        now = datetime.now(timezone.utc)

        if self._last_regime_check:
            elapsed = (now - self._last_regime_check).total_seconds()
            if elapsed < self._regime_check_interval:
                return

        self._last_regime_check = now
        contexts = self.market_data.get_all_market_contexts()

        # Gather bars data for technical analysis
        bars_data = {}
        for symbol in self.settings.active_symbols:
            try:
                bars = self.market_data.get_bars(symbol, TimeFrame.M5, 120)  # 10 hours of 5m bars
                if bars and len(bars) > 15:
                    bars_data[symbol] = bars
            except Exception as e:
                logger.debug(f"Could not get bars for {symbol}: {e}")

        try:
            analysis = await self.regime_detector.detect_regime(contexts, account, bars_data=bars_data)

            # Record regime detection in HFT metrics
            self.hft_metrics.record_regime_detection()

            # Update strategies with new regime
            for strategy in self.strategies:
                strategy.set_regime(analysis.regime)

            # Save to database only if this is a new analysis (not cached)
            # Check by comparing timestamps to avoid duplicates
            if self.database:
                last_saved = getattr(self, '_last_saved_regime_timestamp', None)
                if last_saved != analysis.timestamp:
                    await self.database.save_regime_analysis(analysis)
                    self._last_saved_regime_timestamp = analysis.timestamp

            # Aggiorna AggressionController con regime e metriche (da specifica)
            if self.aggression_controller and self.database:
                try:
                    win_rate = await self.database.get_win_rate_last_n_trades(100)
                    daily_pnl = await self.database.get_daily_pnl()
                    daily_pnl_pct = float(daily_pnl / account.equity) if account.equity > 0 else 0

                    # Check circuit breaker level
                    cb_level = self.risk_engine.circuit_breaker.get_current_level() if hasattr(self.risk_engine.circuit_breaker, 'get_current_level') else 0

                    await self.aggression_controller.update(
                        regime=analysis.regime,
                        win_rate=win_rate,
                        recent_pnl_pct=daily_pnl_pct,
                        circuit_breaker_triggered=cb_level >= 2,
                        circuit_breaker_level=cb_level,
                    )

                    win_rate_str = f"{win_rate:.2%}" if win_rate else "N/A"
                    logger.info(
                        f"AggressionController updated: level={self.aggression_controller.current_state.level.value}, "
                        f"win_rate={win_rate_str}, daily_pnl={daily_pnl_pct:.2%}"
                    )
                except Exception as e:
                    logger.debug(f"Could not update aggression controller: {e}")

        except Exception as e:
            logger.error(f"Regime detection failed: {e}")

    async def _maybe_save_snapshot(self, account: AccountState):
        """Save account snapshot periodically."""
        now = datetime.now(timezone.utc)

        if self._last_snapshot_time:
            elapsed = (now - self._last_snapshot_time).total_seconds()
            if elapsed < self._snapshot_interval:
                return

        self._last_snapshot_time = now

        try:
            if self.database:
                await self.database.save_account_snapshot(account)
        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")

    async def _maybe_calculate_metrics(self):
        """Calculate and save strategy metrics periodically."""
        now = datetime.now(timezone.utc)

        if self._last_metrics_calc:
            elapsed = (now - self._last_metrics_calc).total_seconds()
            if elapsed < self._metrics_calc_interval:
                return

        self._last_metrics_calc = now

        try:
            if self.database:
                end_time = now
                start_time = end_time - timedelta(days=1)

                for strategy_id in StrategyId:
                    metrics = await self.database.calculate_strategy_metrics(
                        strategy_id, start_time, end_time
                    )
                    await self.database.save_strategy_metrics(metrics, "daily")

        except Exception as e:
            logger.error(f"Failed to calculate metrics: {e}")

    # -------------------------------------------------------------------------
    # Callbacks
    # -------------------------------------------------------------------------
    async def _on_circuit_breaker_alert(self, message: str, severity: AlertSeverity):
        """Handle circuit breaker alert."""
        if self.telegram:
            account = self.market_data.get_account_state()
            if account and severity == AlertSeverity.EMERGENCY:
                await self.telegram.send_circuit_breaker_alert(message, account)
            else:
                await self.telegram.send_alert(message, severity)

        if self.database:
            await self.database.save_alert(severity.value, message, "circuit_breaker")

    async def _on_execution_alert(self, message: str, severity: AlertSeverity):
        """Handle execution alert."""
        if self.telegram:
            await self.telegram.send_alert(message, severity)
        if self.database:
            await self.database.save_alert(severity.value, message, "execution")

    async def _on_order_fill(self, order: ApprovedOrder):
        """Handle order fill."""
        logger.info(f"Order filled: {order.symbol} {order.side.value} {order.filled_size}")

        # Notify via Telegram
        position = self.execution.order_manager.get_position(order.symbol)
        if position and self.telegram:
            await self.telegram.send_position_alert(position, "opened")

    async def _on_position_close(self, trade):
        """Handle position close."""
        logger.info(
            f"Position closed: {trade.symbol} P&L: ${trade.pnl:.2f} ({trade.pnl_pct:.2%})"
        )

        # Save trade to database
        if self.database:
            await self.database.save_trade(trade)

        # Notify via Telegram
        if self.telegram:
            await self.telegram.send_trade_alert(trade)

    # -------------------------------------------------------------------------
    # Emergency Controls
    # -------------------------------------------------------------------------
    async def emergency_close_all(self):
        """Close all positions immediately."""
        logger.warning("EMERGENCY: Closing all positions")

        if self.telegram:
            await self.telegram.send_alert(
                "Emergency close all positions triggered",
                AlertSeverity.CRITICAL
            )

        closed = await self.execution.close_all_positions()
        logger.info(f"Closed {closed} positions")

        return closed

    async def trigger_circuit_breaker(self, reason: str):
        """Manually trigger circuit breaker."""
        await self.risk_engine.circuit_breaker.manual_trigger(reason)


async def run_bot():
    """Run the bot (entry point)."""
    # Setup logging
    settings = get_settings()
    setup_logging(settings, log_level="INFO", log_file="logs/bot.log")

    # Create and run bot
    bot = HLQuantBot(settings)
    await bot.start()


if __name__ == "__main__":
    asyncio.run(run_bot())
