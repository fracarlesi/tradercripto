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
        self._regime_detection_task: Optional[asyncio.Task] = None  # Background regime detection
        self._watchdog_task: Optional[asyncio.Task] = None  # Watchdog to detect freezes
        self._last_loop_iteration: Optional[datetime] = None  # Track last successful loop iteration
        self._watchdog_timeout = 60  # Kill bot if no loop iteration for 60 seconds

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

        # Cancel watchdog task first (to prevent forced exit during shutdown)
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            logger.info("Watchdog task stopped")

        # Cancel background regime detection task
        if self._regime_detection_task and not self._regime_detection_task.done():
            self._regime_detection_task.cancel()
            try:
                await self._regime_detection_task
            except asyncio.CancelledError:
                pass
            logger.info("Background regime detection task stopped")

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

        # Sync positions from exchange at startup
        # This recovers state if bot crashed with open positions
        await self._sync_positions_from_exchange()

        # AI layer
        self.regime_detector = RegimeDetector(self.settings)
        self.param_tuner = ParameterTuner(self.settings)

        # Aggression Controller (da specifica consigli.md - sezione 5)
        # Influenza rischio e leva in base a win rate, P&L, regime
        self.aggression_controller = AggressionController(self.settings)
        self.risk_engine.set_aggression_controller(self.aggression_controller)
        self.risk_engine.set_database(self.database)  # For signal tracking
        logger.info(f"AggressionController initialized, level: {self.aggression_controller.current_state.level.value}")

        # Health server for Docker/K8s monitoring
        self.health_server = HealthServer(self, port=8080)
        await self.health_server.start()

        # Start background regime detection task (non-blocking)
        # This prevents deepseek-reasoner (30-60s) from blocking the main loop
        self._regime_detection_task = asyncio.create_task(self._regime_detection_loop())
        logger.info("Background regime detection task started")

        # Start watchdog task to detect main loop freezes
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info("Watchdog task started (timeout: 60s)")

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

                    # Check for externally closed positions (every 30 iterations ~30s)
                    if loop_count % 30 == 0:
                        await self._detect_external_closes(account)

                    # Get current regime (non-blocking - detection runs in background task)
                    # This prevents deepseek-reasoner (30-60s) from blocking the main loop
                    metrics.regime_start()
                    current_regime = self.regime_detector.get_current_regime()
                    metrics.regime_end()
                    if loop_count % 60 == 1:  # Log regime every ~60 iterations
                        logger.info(f"Iter {loop_count}: Current regime={current_regime.value}")

                    # Get market data with ATR enrichment
                    contexts = self.market_data.get_all_market_contexts_with_atr()
                    bars_by_symbol = self._get_bars_for_strategies()
                    prices = {s: ctx.mid_price for s, ctx in contexts.items()}
                    # ATR now comes from context (calculated in market_data)
                    atrs = {s: ctx.atr_14 for s, ctx in contexts.items() if ctx.atr_14}

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
                        # Set current regime for signal tracking
                        self.risk_engine.set_current_regime(current_regime.value)
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

                # Update watchdog timestamp (proves loop is running)
                self._last_loop_iteration = datetime.now(timezone.utc)

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
    async def _watchdog_loop(self):
        """
        Watchdog task to detect main loop freeze.

        If main loop doesn't update _last_loop_iteration for 60 seconds,
        this will force exit the process and let Docker restart it.
        This prevents the bot from staying frozen with open positions
        while the market moves against us.
        """
        logger.info("Watchdog loop started - monitoring main loop health")

        # Initial grace period for startup
        await asyncio.sleep(30)

        while self._running:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds

                if self._last_loop_iteration:
                    elapsed = (datetime.now(timezone.utc) - self._last_loop_iteration).total_seconds()

                    if elapsed > self._watchdog_timeout:
                        logger.critical(
                            f"WATCHDOG TRIGGERED: Main loop frozen for {elapsed:.0f}s "
                            f"(threshold: {self._watchdog_timeout}s). Forcing exit for Docker restart."
                        )

                        # Send alert if possible
                        if self.telegram:
                            try:
                                await asyncio.wait_for(
                                    self.telegram.send_alert(
                                        f"WATCHDOG: Bot frozen for {elapsed:.0f}s - forcing restart",
                                        AlertSeverity.CRITICAL
                                    ),
                                    timeout=5.0
                                )
                            except:
                                pass

                        # Force exit - Docker will restart the container
                        import os
                        os._exit(1)

                    elif elapsed > self._watchdog_timeout / 2:
                        logger.warning(
                            f"Watchdog warning: Main loop slow - {elapsed:.0f}s since last iteration"
                        )

            except asyncio.CancelledError:
                logger.info("Watchdog loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in watchdog loop: {e}")
                await asyncio.sleep(10)

        logger.info("Watchdog loop stopped")

    async def _regime_detection_loop(self):
        """
        Background loop for regime detection - does NOT block main loop.

        Runs every regime_check_interval (5 min by default).
        Supports deepseek-reasoner which can take 30-60 seconds to respond.
        """
        logger.info("Regime detection background loop started")

        # Initial delay to let market data warm up
        await asyncio.sleep(10)

        while self._running:
            try:
                # Get current account state
                account = self.market_data.get_account_state()
                if not account:
                    logger.debug("No account state for regime detection, waiting...")
                    await asyncio.sleep(30)
                    continue

                # Run regime detection (can take 30-90s with deepseek-reasoner)
                logger.info("Background regime detection starting...")
                await self._maybe_detect_regime(account)
                logger.info("Background regime detection completed")

                # Wait for next interval
                await asyncio.sleep(self._regime_check_interval)

            except asyncio.CancelledError:
                logger.info("Regime detection loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in regime detection loop: {e}")
                await asyncio.sleep(60)  # Back off on error

    async def _maybe_detect_regime(self, account: AccountState):
        """Run regime detection if needed."""
        now = datetime.now(timezone.utc)

        if self._last_regime_check:
            elapsed = (now - self._last_regime_check).total_seconds()
            if elapsed < self._regime_check_interval:
                return

        self._last_regime_check = now
        contexts = self.market_data.get_all_market_contexts_with_atr()

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

        # Save open trade to database (with exit_time=NULL)
        # This ensures we track positions even if bot crashes before close
        if self.database:
            try:
                await self.database.save_open_trade(order)
                logger.info(f"Open trade saved to DB: {order.symbol} {order.side.value}")
            except Exception as e:
                logger.error(f"Failed to save open trade to DB: {e}")

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
    # Position Sync and External Close Detection
    # -------------------------------------------------------------------------
    async def _sync_positions_from_exchange(self):
        """
        Sync positions from Hyperliquid at startup.

        This recovers state if the bot crashed with open positions.
        It compares exchange positions with DB open trades and reconciles.
        """
        logger.info("Syncing positions from exchange...")

        try:
            # Get positions from Hyperliquid
            account = self.market_data.get_account_state()
            if not account:
                logger.warning("No account state available for position sync")
                return

            exchange_positions = {p.symbol: p for p in account.positions}
            logger.info(f"Found {len(exchange_positions)} positions on exchange")

            # Get open trades from DB
            if self.database:
                open_trades = await self.database.get_open_trades()
                db_symbols = {t["symbol"] for t in open_trades}
                logger.info(f"Found {len(open_trades)} open trades in DB")

                # Check for positions closed externally (in DB but not on exchange)
                for trade in open_trades:
                    symbol = trade["symbol"]
                    if symbol not in exchange_positions:
                        # Position was closed externally - close the DB trade
                        logger.warning(
                            f"Position {symbol} was closed externally, updating DB"
                        )
                        # We don't have exact exit price, use entry as estimate
                        # The P&L will be inaccurate but at least the trade is closed
                        entry_price = Decimal(str(trade["entry_price"]))
                        await self.database.close_orphan_trade(
                            symbol=symbol,
                            exit_price=entry_price,  # Best estimate
                            pnl=Decimal(0),
                            pnl_pct=Decimal(0),
                            exit_reason="external_close_at_startup",
                        )

            # Log current positions
            for symbol, pos in exchange_positions.items():
                logger.info(
                    f"Position: {symbol} {pos.side.value} {pos.size} @ {pos.entry_price}"
                )

        except Exception as e:
            logger.error(f"Error syncing positions: {e}")

    async def _detect_external_closes(self, account: 'AccountState'):
        """
        Detect positions closed externally (not by the bot).

        This is called during the main loop to catch manual closes.
        """
        if not self.database:
            return

        try:
            # Get current exchange positions
            exchange_symbols = {p.symbol for p in account.positions}

            # Get open trades from DB
            open_trades = await self.database.get_open_trades()

            # Check each open trade
            for trade in open_trades:
                symbol = trade["symbol"]
                if symbol not in exchange_symbols:
                    # Position was closed externally
                    entry_price = Decimal(str(trade["entry_price"]))
                    entry_side = trade["side"]

                    # Try to get current price for P&L estimate
                    ctx = self.market_data.get_context(symbol)
                    if ctx and ctx.mid_price > 0:
                        exit_price = ctx.mid_price
                        if entry_side == "LONG":
                            pnl = (exit_price - entry_price) * Decimal(str(trade["size"]))
                        else:
                            pnl = (entry_price - exit_price) * Decimal(str(trade["size"]))
                        pnl_pct = pnl / (entry_price * Decimal(str(trade["size"]))) if entry_price > 0 else Decimal(0)
                    else:
                        exit_price = entry_price
                        pnl = Decimal(0)
                        pnl_pct = Decimal(0)

                    logger.warning(
                        f"EXTERNAL CLOSE DETECTED: {symbol} | "
                        f"Estimated P&L: ${pnl:.2f} ({pnl_pct:.2%})"
                    )

                    await self.database.close_orphan_trade(
                        symbol=symbol,
                        exit_price=exit_price,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        exit_reason="external_close",
                    )

                    # Send alert
                    if self.telegram:
                        await self.telegram.send_alert(
                            f"Position {symbol} closed externally\n"
                            f"Estimated P&L: ${pnl:.2f} ({pnl_pct:.2%})",
                            AlertSeverity.WARNING
                        )

        except Exception as e:
            logger.error(f"Error detecting external closes: {e}")

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
