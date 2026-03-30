"""
IB Bot Main Orchestrator
=========================

Session-phase state machine for Opening Range Breakout trading.

Lifecycle:
  PRE_MARKET -> OPENING_RANGE (9:30-9:45) -> ACTIVE_TRADING (9:45-11:30)
  -> AFTERNOON (manage existing) -> EOD_FLATTEN (15:45) -> CLOSED

Entry point: python -m ib_bot.main
"""

import asyncio
import logging
import logging.handlers

import signal
import sys
from datetime import datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .config.loader import load_config, TradingConfig
from .core.enums import Direction, SessionPhase, SetupType, Topic
from .core.models import FuturesMarketState, ORBRange
from .services.message_bus import MessageBus
from .services.ib_client import IBClient
from .services.market_data import MarketDataService
from .services.execution_engine import ExecutionEngine
from .services.risk_manager import RiskManager
from .services.kill_switch import KillSwitchService
from .services.notifications import NotificationService
from .services.atr_filter import ATRFilter
from .services.regime_detector import RegimeDetector
from .services.trade_journal import TradeJournal
from .services.scorecard import Scorecard
from .strategies.registry import create_strategy, create_rsi_mean_reversion, create_rsi2_connors
from .strategies.etf_rotation import (
    ETFRotationStrategy,
    fetch_etf_bars,
    load_state as load_etf_state,
    RotationAction,
)
from .strategies.options_spreads import CreditSpreadStrategy

# Scanner + LLM (optional — only loaded if scanner_universal.enabled)
from .scanner.scanner_service import ScannerService
from .flag_trader.model_router import LLMModelRouter
from .flag_trader.agent import IBFlagTraderAgent, IBFlagTraderConfig
from .flag_trader.equity_model import EquityFlagTraderModel
from .flag_trader.equity_prompt import EquityPromptBuilder
from .strategies.llm_equity import LLMEquityStrategy

logger = logging.getLogger(__name__)

# Next-phase lookup for time-until-transition calculation
_NEXT_PHASE_ET = {
    SessionPhase.PRE_MARKET: ("09:30", SessionPhase.OPENING_RANGE),
    SessionPhase.OPENING_RANGE: ("09:45", SessionPhase.ACTIVE_TRADING),
    SessionPhase.ACTIVE_TRADING: ("11:30", SessionPhase.AFTERNOON),
    SessionPhase.AFTERNOON: ("15:45", SessionPhase.EOD_FLATTEN),
    SessionPhase.EOD_FLATTEN: ("16:00", SessionPhase.CLOSED),
}


def setup_logging(level: str = "INFO", log_file: str = "logs/ib_bot.log") -> None:
    """Configure structured logging with console + rotating file handler.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_file: Path to the log file.
    """
    log_dir = Path(log_file).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    log_level = getattr(logging, level.upper(), logging.INFO)
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(log_level)

    # Rotating file handler: 10 MB max, keep 5 backups
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5,
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(log_level)

    root = logging.getLogger()
    root.setLevel(log_level)
    # Clear any existing handlers to avoid duplicates on reload
    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)


class IBBot:
    """Main orchestrator for IB Opening Range Breakout bot."""

    # Heartbeat interval in seconds
    _HEARTBEAT_INTERVAL = 60.0

    def __init__(self, config: TradingConfig) -> None:
        self._config = config
        self._phase = SessionPhase.CLOSED
        self._running = False
        self._heartbeat_task: Optional[asyncio.Task[None]] = None

        # Primary timezone for session-phase detection (from first enabled contract)
        from .core.contracts import CONTRACTS
        first_symbol = config.enabled_contracts[0].symbol if config.enabled_contracts else "ES"
        spec = CONTRACTS.get(first_symbol)
        self._session_tz = ZoneInfo(spec.tz_name) if spec else ZoneInfo("America/New_York")

        # Services
        self._bus = MessageBus()
        self._ib_client = IBClient(config.ib_connection)
        self._risk_manager = RiskManager(config.risk)
        self._kill_switch = KillSwitchService(config=config.risk, bus=self._bus)
        self._notifications = NotificationService(config.notifications)

        # Get enabled contract symbols
        self._symbols = [c.symbol for c in config.enabled_contracts]

        self._market_data = MarketDataService(
            ib_client=self._ib_client,
            or_config=config.opening_range,
            symbols=self._symbols,
            bus=self._bus,
        )
        # Trade journal + scorecard (initialized before execution engine)
        self._journal = TradeJournal()
        self._scorecard = Scorecard(
            halt_dd_usd=Decimal(str(config.scorecard.halt_dd_usd)),
            halt_5s_loss_usd=Decimal(str(config.scorecard.halt_5s_loss_usd)),
            candidate_pf_20s=Decimal(str(config.scorecard.candidate_pf)),
            candidate_min_trades=config.scorecard.candidate_min_trades,
            candidate_max_dd=Decimal(str(config.scorecard.candidate_max_dd)),
            candidate_min_wr=config.scorecard.candidate_min_wr,
        )
        self._scorecard_enabled = config.scorecard.enabled

        self._execution = ExecutionEngine(
            ib_client=self._ib_client,
            risk_manager=self._risk_manager,
            kill_switch=self._kill_switch,
            bus=self._bus,
            journal=self._journal,
        )
        self._strategy = create_strategy(config)
        self._rsi_mr_strategy = create_rsi_mean_reversion(config)
        self._rsi2_strategy = create_rsi2_connors(config)
        self._atr_filter = ATRFilter(config.atr_filter)

        # Regime detector (observation-only)
        self._regime_detector = RegimeDetector(
            atr_lookback=config.regime.atr_lookback,
            price_window=config.regime.price_window,
            high_vol_mult=config.regime.high_vol_multiplier,
            low_vol_mult=config.regime.low_vol_multiplier,
        )
        self._regime_enabled = config.regime.enabled

        # ETF Rotation (VAA-G4) — monthly rebalance
        self._etf_rotation: Optional[ETFRotationStrategy] = None
        self._etf_rotation_checked_today = False
        if config.etf_rotation.enabled:
            self._etf_rotation = ETFRotationStrategy(
                offensive=config.etf_rotation.offensive,
                defensive=config.etf_rotation.defensive,
            )
            logger.info(
                "ETF Rotation enabled: offensive=%s defensive=%s check_time=%s",
                config.etf_rotation.offensive,
                config.etf_rotation.defensive,
                config.etf_rotation.check_time,
            )

        # Credit put spread strategy
        self._credit_spread: Optional[CreditSpreadStrategy] = None
        self._credit_spread_checked_today = False
        if config.options_spreads.enabled:
            self._credit_spread = CreditSpreadStrategy(
                config.options_spreads.model_dump()
            )
            logger.info(
                "Credit Spread strategy enabled: %s width=$%.0f delta=%.2f",
                config.options_spreads.underlying,
                config.options_spreads.spread_width,
                config.options_spreads.target_delta,
            )

        # Scanner + LLM equity strategy (optional)
        self._scanner: Optional[ScannerService] = None
        self._llm_equity: Optional[LLMEquityStrategy] = None
        self._scanner_checked_today = False
        self._scanner_candidates: list[dict] = []  # loaded from last EOD scan
        self._scanner_candle_cache: dict[str, list[dict[str, float]]] = {}
        self._llm_evaluated_today = False

        if config.scanner_universal.enabled:
            scanner_cfg = config.scanner_universal
            # Build LLM model router with equity model factory
            model_router = LLMModelRouter(
                confidence_threshold=scanner_cfg.confidence_threshold,
            )
            model_router.load_models({
                "equity": {
                    "model_name": "Qwen/Qwen2.5-0.5B-Instruct",
                    "device": "cpu",
                },
            })

            self._scanner = ScannerService(
                bus=self._bus,
                model_router=model_router,
                config={
                    "max_candidates": scanner_cfg.max_candidates,
                    "confidence_threshold": scanner_cfg.confidence_threshold,
                    "max_total": scanner_cfg.max_open_positions,
                },
            )

            # Build the IBFlagTraderAgent + LLMEquityStrategy
            agent_config = IBFlagTraderConfig(
                confidence_threshold=scanner_cfg.confidence_threshold,
            )
            equity_model = EquityFlagTraderModel(
                model_name="Qwen/Qwen2.5-0.5B-Instruct", device="cpu",
            )
            prompt_builder = EquityPromptBuilder()
            flag_agent = IBFlagTraderAgent(
                config=agent_config,
                model=equity_model,
                prompt_builder=prompt_builder,
            )
            self._llm_equity = LLMEquityStrategy(agent=flag_agent)

            logger.info(
                "Scanner + LLM Equity enabled: max_candidates=%d threshold=%.2f "
                "max_positions=%d scan_time=%s",
                scanner_cfg.max_candidates,
                scanner_cfg.confidence_threshold,
                scanner_cfg.max_open_positions,
                scanner_cfg.scan_time,
            )

    # =========================================================================
    # Startup Diagnostics
    # =========================================================================

    def _log_startup_banner(self) -> None:
        """Print comprehensive startup diagnostics."""
        cfg = self._config
        conn = cfg.ib_connection
        now_utc = datetime.now(timezone.utc)
        now_et = now_utc.astimezone(self._session_tz)
        current_phase = self._detect_phase(now_utc)

        # Determine paper/live mode from port
        is_paper = conn.port in (7497, 4002)
        mode_label = "PAPER TRADING" if is_paper else "LIVE TRADING"

        enabled = [c.symbol for c in cfg.enabled_contracts]
        disabled = [c.symbol for c in cfg.contracts if not c.enabled]

        lines = [
            "",
            "=" * 62,
            "  IB Bot - Opening Range Breakout Strategy",
            "=" * 62,
            "",
            f"  Mode:            {mode_label}",
            f"  Strategy:        {self._strategy.name.upper()}",
            f"  Contracts:       {', '.join(enabled)} (disabled: {', '.join(disabled) or 'none'})",
            f"  Allow short:     {cfg.strategy.allow_short}",
            f"  VWAP confirm:    {cfg.strategy.vwap_confirmation}",
            f"  RSI MR (2nd):    {'ENABLED' if self._rsi_mr_strategy else 'disabled'}",
            f"  RSI2 Connors:    {'ENABLED (daily)' if self._rsi2_strategy else 'disabled'}",
            f"  ETF Rotation:    {'ENABLED (monthly VAA-G4)' if self._etf_rotation else 'disabled'}",
            f"  Credit Spreads:  {'ENABLED (bi-weekly SPY puts)' if self._credit_spread else 'disabled'}",
            f"  LLM Scanner:    {'ENABLED (EOD scan + LLM equity)' if self._scanner else 'disabled'}",
            "",
            "  --- IB Connection ---",
            f"  Host:            {conn.host}:{conn.port}",
            f"  Client ID:       {conn.client_id}",
            f"  Readonly:        {conn.readonly}",
            f"  Timeout:         {conn.timeout}s",
            "",
            "  --- Opening Range ---",
            f"  Window:          {cfg.opening_range.or_start} - {cfg.opening_range.or_end} ET",
            f"  Range filter:    {cfg.opening_range.min_range_ticks} - {cfg.opening_range.max_range_ticks} ticks",
            f"  Max entry time:  {cfg.strategy.max_entry_time} ET",
            "",
            "  --- Risk Limits ---",
            f"  Max risk/trade:  ${cfg.risk.max_risk_per_trade_usd}",
            f"  Max daily loss:  ${cfg.risk.max_daily_loss_usd}",
            f"  Max contracts:   {cfg.risk.max_contracts_per_trade}",
            f"  Max trades/day:  {cfg.risk.max_trades_per_day}",
            f"  Consec stops:    {cfg.risk.consecutive_stops_halt} (halt)",
            "",
            "  --- Stops ---",
            f"  Stop type:       {cfg.stops.stop_type}",
            f"  R:R ratio:       1:{cfg.stops.reward_risk_ratio}",
            f"  Trailing:        {cfg.stops.trailing_enabled}",
            f"  EOD flatten:     {cfg.stops.eod_flatten_time} ET",
            "",
            "  --- ATR Filter ---",
            f"  Enabled:         {cfg.atr_filter.enabled}",
            f"  Lookback:        {cfg.atr_filter.lookback_days} days",
            f"  Range:           p{cfg.atr_filter.low_percentile:.0f} - p{cfg.atr_filter.high_percentile:.0f}",
            "",
            "  --- Session ---",
            f"  UTC time:        {now_utc.strftime('%Y-%m-%d %H:%M:%S')}",
            f"  ET time:         {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"  Current phase:   {current_phase.value}",
            "",
            "=" * 62,
        ]

        for line in lines:
            logger.info(line)

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start the bot: connect, subscribe, and run main loop."""
        self._log_startup_banner()

        self._running = True

        try:
            # Connect to IB
            await self._ib_client.connect()

            # Qualify contracts
            for symbol in self._symbols:
                await self._ib_client.qualify_contract(symbol)

            # Start services
            await self._bus.start()
            await self._kill_switch.start()
            await self._market_data.start()
            await self._execution.start()

            # Start scanner service if enabled
            if self._scanner:
                await self._scanner.start()

            # Subscribe to opening range for strategy evaluation
            await self._bus.subscribe(Topic.OPENING_RANGE, self._on_opening_range)
            await self._bus.subscribe(Topic.MARKET_DATA, self._on_market_data)

            await self._notifications.notify_session(
                f"IB Bot started: {', '.join(self._symbols)}"
            )

            # Start heartbeat task
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="heartbeat"
            )

            # Main loop
            await self._main_loop()

        except Exception as e:
            logger.critical("Fatal error: %s", e, exc_info=True)
            await self._notifications.notify_kill_switch(f"Fatal: {e}")
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down IB Bot...")
        self._running = False

        # Cancel heartbeat
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._scanner:
            await self._scanner.stop()
        await self._execution.stop()
        await self._market_data.stop()
        await self._kill_switch.stop()
        await self._bus.stop()
        await self._ib_client.disconnect()

        await self._notifications.notify_session("IB Bot stopped")
        logger.info("IB Bot shut down complete")

    # =========================================================================
    # Main Loop & Phase Detection
    # =========================================================================

    async def _main_loop(self) -> None:
        """Main session-phase state machine loop."""
        while self._running:
            try:
                await self._update_phase()
                await self._check_etf_rotation()
                await self._check_credit_spreads()
                await self._check_scanner_eod()
                await self._check_llm_equity_trading()
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Main loop error: %s", e, exc_info=True)
                await asyncio.sleep(5.0)

    async def _update_phase(self) -> None:
        """Update session phase based on current time."""
        now = datetime.now(timezone.utc)

        new_phase = self._detect_phase(now)
        if new_phase != self._phase:
            old_phase = self._phase
            self._phase = new_phase
            await self._on_phase_change(old_phase, new_phase)

    def _detect_phase(self, now: datetime) -> SessionPhase:
        """Detect session phase from current UTC time.

        Converts UTC to the exchange's local timezone (handles DST
        automatically via zoneinfo) before comparing against session times.
        """
        local_now = now.astimezone(self._session_tz)
        t = local_now.time()

        or_start = time(9, 30)
        or_end = time(9, 45)
        max_entry = time(11, 30)
        eod_flatten = time(15, 45)
        close = time(16, 0)

        if t < or_start:
            return SessionPhase.PRE_MARKET
        elif or_start <= t < or_end:
            return SessionPhase.OPENING_RANGE
        elif or_end <= t < max_entry:
            return SessionPhase.ACTIVE_TRADING
        elif max_entry <= t < eod_flatten:
            return SessionPhase.AFTERNOON
        elif eod_flatten <= t < close:
            return SessionPhase.EOD_FLATTEN
        else:
            return SessionPhase.CLOSED

    # =========================================================================
    # Phase Transition Logging
    # =========================================================================

    async def _on_phase_change(
        self, old: SessionPhase, new: SessionPhase
    ) -> None:
        """Handle session phase transitions with detailed logging."""
        now_utc = datetime.now(timezone.utc)
        now_et = now_utc.astimezone(self._session_tz)

        logger.info(
            "PHASE TRANSITION: %s -> %s at %s ET",
            old.value, new.value, now_et.strftime("%H:%M:%S"),
        )

        # Log time until next transition
        next_info = _NEXT_PHASE_ET.get(new)
        if next_info:
            next_time_str, next_phase = next_info
            logger.info(
                "  Next transition: %s -> %s at %s ET",
                new.value, next_phase.value, next_time_str,
            )

        await self._notifications.notify_session(
            f"Phase: {old.value} -> {new.value}"
        )

        if new == SessionPhase.PRE_MARKET:
            # Reset for new day
            self._risk_manager.reset_daily()
            self._kill_switch.reset_daily()
            self._execution.reset_daily()
            self._market_data.reset_session()
            self._atr_filter.reset_daily()
            self._strategy.reset_daily()
            if self._rsi_mr_strategy:
                self._rsi_mr_strategy.reset_daily()
            if self._rsi2_strategy:
                self._rsi2_strategy.reset_daily()
            if self._regime_enabled:
                self._regime_detector.reset()
            self._etf_rotation_checked_today = False
            self._credit_spread_checked_today = False
            self._scanner_checked_today = False
            self._llm_evaluated_today = False
            # Load scanner candidates from last EOD scan
            if self._scanner and self._scanner.last_decisions:
                self._scanner_candidates = [
                    {
                        "symbol": d.symbol if hasattr(d, "symbol") else str(d),
                        "asset_class": getattr(d, "asset_class", "equity"),
                        "action": getattr(d, "action", 2),
                        "action_name": getattr(d, "action_name", "BUY"),
                        "confidence": getattr(d, "confidence", 0.0),
                        "tp_pct": getattr(d, "tp_pct", 3.0),
                        "sl_pct": getattr(d, "sl_pct", 2.0),
                    }
                    for d in self._scanner.last_decisions
                ]
                logger.info(
                    "Loaded %d scanner candidates from last EOD scan: %s",
                    len(self._scanner_candidates),
                    [c["symbol"] for c in self._scanner_candidates],
                )
            else:
                self._scanner_candidates = []
            logger.info("Daily reset complete - ready for new session")

        elif new == SessionPhase.OPENING_RANGE:
            logger.info(
                "Opening Range window OPEN - collecting bars for %s",
                self._symbols,
            )

        elif new == SessionPhase.ACTIVE_TRADING:
            # Log OR ranges for all symbols at start of active trading
            for symbol in self._symbols:
                or_range = self._market_data.get_or_range(symbol)
                if or_range and or_range.valid:
                    logger.info(
                        "OR RANGE [%s]: high=%.2f low=%.2f mid=%.2f "
                        "range=%d ticks vol=%.0f vwap=%.2f",
                        symbol, float(or_range.or_high), float(or_range.or_low),
                        float(or_range.midpoint), or_range.range_ticks,
                        float(or_range.volume), float(or_range.vwap),
                    )
                elif or_range and not or_range.valid:
                    logger.warning(
                        "OR RANGE [%s]: INVALID - range=%d ticks (need %d-%d)",
                        symbol, or_range.range_ticks,
                        self._config.opening_range.min_range_ticks,
                        self._config.opening_range.max_range_ticks,
                    )
                else:
                    logger.warning("OR RANGE [%s]: not detected", symbol)

            # ATR percentile filter — check once at start of active trading
            if self._atr_filter.is_enabled and not self._atr_filter.today_checked:
                # Use ATR from the first enabled symbol as the reference
                for symbol in self._symbols:
                    atr_value = self._market_data.get_atr(symbol)
                    if atr_value > 0:
                        allowed = self._atr_filter.record_and_check(atr_value)
                        if not allowed:
                            await self._notifications.notify_session(
                                f"ATR filter: skipping today (ATR={float(atr_value):.4f})"
                            )
                        break

        elif new == SessionPhase.EOD_FLATTEN:
            # Flatten all positions
            logger.info("EOD FLATTEN: closing all positions")
            await self._execution.flatten_all()
            await self._notifications.notify_session("EOD: all positions flattened")

            # EOD scorecard evaluation
            if self._scorecard_enabled:
                try:
                    sessions = self._journal.load_sessions(days=30)
                    metrics = self._scorecard.evaluate(sessions)
                    report = Scorecard.format_report(metrics)
                    logger.info("EOD Scorecard:\n%s", report)
                    await self._notifications.notify_scorecard(report)
                except Exception as e:
                    logger.error("Scorecard evaluation failed: %s", e)

        elif new == SessionPhase.CLOSED:
            # RSI(2) Connors daily evaluation at 16:00 ET
            # Runs after market close when today's daily bar is final.
            if self._rsi2_strategy:
                await self._evaluate_rsi2_daily()

    # =========================================================================
    # Heartbeat Logging
    # =========================================================================

    async def _heartbeat_loop(self) -> None:
        """Log heartbeat every 60 seconds during ACTIVE_TRADING."""
        while self._running:
            try:
                await asyncio.sleep(self._HEARTBEAT_INTERVAL)

                if self._phase != SessionPhase.ACTIVE_TRADING:
                    continue

                now_et = datetime.now(timezone.utc).astimezone(self._session_tz)
                positions = self._ib_client.get_positions()
                open_count = sum(1 for p in positions if p.position != 0)
                risk_stats = self._risk_manager.stats
                ks_metrics = self._kill_switch.metrics

                # Calculate time until next phase transition
                next_info = _NEXT_PHASE_ET.get(self._phase)
                time_remaining = ""
                if next_info:
                    next_time_str, _ = next_info
                    h, m = map(int, next_time_str.split(":"))
                    next_t = now_et.replace(hour=h, minute=m, second=0)
                    delta = next_t - now_et
                    mins_left = int(delta.total_seconds() / 60)
                    time_remaining = f"{mins_left}m until AFTERNOON"

                logger.info(
                    "HEARTBEAT | phase=%s | positions=%d | "
                    "daily_pnl=$%.2f | trades=%d/%d | kill_switch=%s | %s",
                    self._phase.value, open_count,
                    -risk_stats["daily_loss_usd"],
                    risk_stats["daily_trade_count"],
                    risk_stats["max_trades_per_day"],
                    ks_metrics["status"],
                    time_remaining,
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Heartbeat error: %s", e)

    # =========================================================================
    # ETF Rotation (VAA-G4)
    # =========================================================================

    async def _check_etf_rotation(self) -> None:
        """Check if ETF rotation rebalance is needed.

        Runs daily at the configured check_time (default 15:50 ET).
        Only executes on the last trading day of the month.
        """
        if not self._etf_rotation:
            return

        if self._etf_rotation_checked_today:
            return

        now_et = datetime.now(timezone.utc).astimezone(self._session_tz)
        check_time_str = self._config.etf_rotation.check_time
        h, m = map(int, check_time_str.split(":"))
        check_time = time(h, m)

        # Only check after the configured time
        if now_et.time() < check_time:
            return

        # Mark as checked so we don't re-run today
        self._etf_rotation_checked_today = True
        today = now_et.date()

        if not self._etf_rotation.should_rebalance(today):
            logger.info(
                "ETF Rotation: not last trading day of month (%s), skipping",
                today,
            )
            return

        logger.info("ETF Rotation: last trading day of month — running evaluation")

        try:
            # Fetch 13 months of daily bars for all 7 ETFs
            all_symbols = self._etf_rotation.all_symbols
            bars_by_symbol = await fetch_etf_bars(self._ib_client, all_symbols)

            # Evaluate momentum and get recommendation
            result = self._etf_rotation.evaluate(bars_by_symbol, today)

            # Execute the rebalance
            await self._etf_rotation.execute_rebalance(
                result, self._ib_client, self._notifications,
            )

        except Exception as e:
            logger.error("ETF Rotation failed: %s", e, exc_info=True)
            await self._notifications.send(
                f"ETF Rotation ERROR: {e}",
                title="ETF Rotation - ERROR",
                tags="warning",
            )

    # =========================================================================
    # Credit Put Spread Strategy
    # =========================================================================

    async def _check_credit_spreads(self) -> None:
        """Check credit spread entries and exits daily at 15:50 ET.

        Runs once per day at 15:50 ET:
        1. Check exits on all open positions
        2. If it's an entry day and below max positions, find and place a new spread
        """
        if not self._credit_spread:
            return

        if self._credit_spread_checked_today:
            return

        now_et = datetime.now(timezone.utc).astimezone(self._session_tz)
        check_time = time(15, 50)

        # Only check after 15:50 ET
        if now_et.time() < check_time:
            return

        self._credit_spread_checked_today = True
        today = now_et.date()
        ib = self._ib_client.ib

        logger.info("Credit Spread: daily check at %s ET", now_et.strftime("%H:%M"))

        # --- Step 1: Check exits on open positions ---
        try:
            exits = await self._credit_spread.check_exits(ib)
            for pos, reason in exits:
                logger.info("Credit Spread EXIT: %s — %s", pos.spread_id, reason)
                closed = await self._credit_spread.close_spread(ib, pos, reason)
                if closed:
                    await self._notifications.send(
                        f"SPREAD CLOSED: {pos.spread_id}\n"
                        f"Reason: {reason}\n"
                        f"Credit received: ${pos.credit_received:.2f}",
                        title="Credit Spread EXIT",
                        tags="white_check_mark",
                    )
                else:
                    await self._notifications.send(
                        f"SPREAD CLOSE FAILED: {pos.spread_id}\n"
                        f"Reason: {reason}",
                        title="Credit Spread ERROR",
                        tags="warning",
                    )
        except Exception as e:
            logger.error("Credit Spread exit check failed: %s", e, exc_info=True)

        # --- Step 2: Check entry ---
        if not self._credit_spread.should_enter(today):
            # Log status even when not entering
            report = self._credit_spread.status_report()
            logger.info("Credit Spread status:\n%s", report)
            return

        logger.info("Credit Spread: entry day — searching for spread")

        try:
            spread = await self._credit_spread.find_spread(ib)
            if not spread:
                logger.warning("Credit Spread: no suitable spread found")
                await self._notifications.send(
                    "No suitable credit spread found today",
                    title="Credit Spread",
                    tags="mag",
                )
                return

            result = await self._credit_spread.place_spread(ib, spread)
            if result:
                await self._notifications.send(
                    f"NEW SPREAD: {result.spread_id}\n"
                    f"Short: P{spread.short_strike:.0f} ({spread.short_delta:.3f} delta)\n"
                    f"Long: P{spread.long_strike:.0f}\n"
                    f"Expiry: {spread.expiry} ({spread.dte} DTE)\n"
                    f"Credit: ${result.credit_received:.2f}\n"
                    f"Max risk: ${self._credit_spread._spread_width * 100 - result.credit_received:.2f}",
                    title="Credit Spread ENTRY",
                    tags="chart_with_upwards_trend",
                )
            else:
                logger.error("Credit Spread: order placement failed")
                await self._notifications.send(
                    "Credit spread order placement FAILED",
                    title="Credit Spread ERROR",
                    tags="warning",
                )

        except Exception as e:
            logger.error("Credit Spread entry failed: %s", e, exc_info=True)
            await self._notifications.send(
                f"Credit Spread ENTRY ERROR: {e}",
                title="Credit Spread ERROR",
                tags="warning",
            )

        # Log final status
        report = self._credit_spread.status_report()
        logger.info("Credit Spread status:\n%s", report)

    # =========================================================================
    # Scanner EOD Scan
    # =========================================================================

    async def _check_scanner_eod(self) -> None:
        """Run the EOD scanner after market close.

        Triggers once per day at the configured scan_time (default 16:15 ET).
        Collects candidates for LLM evaluation the next morning.
        """
        if not self._scanner:
            return

        if self._scanner_checked_today:
            return

        now_et = datetime.now(timezone.utc).astimezone(self._session_tz)
        scan_time_str = self._config.scanner_universal.scan_time
        h, m = map(int, scan_time_str.split(":"))
        scan_time = time(h, m)

        if now_et.time() < scan_time:
            return

        self._scanner_checked_today = True

        logger.info("Scanner EOD: starting batch scan at %s ET", now_et.strftime("%H:%M"))

        try:
            # Get current portfolio state for LLM context
            positions = self._ib_client.get_positions()
            open_positions = [
                {"symbol": p.contract.symbol, "position": p.position}
                for p in positions if p.position != 0
            ]

            decisions = await self._scanner.run_eod_scan(
                open_positions=open_positions,
            )

            logger.info(
                "Scanner EOD: %d filtered decisions: %s",
                len(decisions),
                [getattr(d, "symbol", str(d)) for d in decisions],
            )

            if decisions:
                await self._notifications.send(
                    f"EOD Scanner: {len(decisions)} candidates\n"
                    + "\n".join(
                        f"  {getattr(d, 'symbol', '?')} "
                        f"{getattr(d, 'action_name', '?')} "
                        f"conf={getattr(d, 'confidence', 0):.2f}"
                        for d in decisions[:10]
                    ),
                    title="Scanner EOD Results",
                    tags="mag",
                )

        except Exception as e:
            logger.error("Scanner EOD failed: %s", e, exc_info=True)
            await self._notifications.send(
                f"Scanner EOD ERROR: {e}",
                title="Scanner ERROR",
                tags="warning",
            )

    # =========================================================================
    # LLM Equity Trading (Active Trading)
    # =========================================================================

    async def _check_llm_equity_trading(self) -> None:
        """Evaluate scanner candidates with LLM during active trading.

        Runs once per day during ACTIVE_TRADING phase. Uses the
        LLMEquityStrategy to produce TradeSetup objects, then sizes
        and publishes them as orders.
        """
        if not self._llm_equity:
            return

        if self._llm_evaluated_today:
            return

        if self._phase != SessionPhase.ACTIVE_TRADING:
            return

        if not self._scanner_candidates:
            self._llm_evaluated_today = True
            return

        self._llm_evaluated_today = True

        logger.info(
            "LLM Equity: evaluating %d scanner candidates",
            len(self._scanner_candidates),
        )

        try:
            # Fetch fresh candle data for candidates via scanner data fetcher
            symbols = [c["symbol"] for c in self._scanner_candidates]
            if self._scanner and self._scanner.data_fetcher:
                candle_cache_raw = await self._scanner.data_fetcher.fetch_universe(
                    symbols
                )
                # Convert DataFrames to list-of-dicts format
                candle_cache: dict[str, list[dict[str, float]]] = {}
                for sym, df in candle_cache_raw.items():
                    if df is not None and not df.empty:
                        candle_cache[sym] = [
                            {
                                "open": float(row["Open"]),
                                "high": float(row["High"]),
                                "low": float(row["Low"]),
                                "close": float(row["Close"]),
                                "volume": float(row["Volume"]),
                            }
                            for _, row in df.iterrows()
                        ]
            else:
                candle_cache = self._scanner_candle_cache

            if not candle_cache:
                logger.warning("LLM Equity: no candle data available, skipping")
                return

            # Get portfolio state
            positions = self._ib_client.get_positions()
            total_value = sum(
                abs(float(p.position) * float(p.avgCost))
                for p in positions
            )
            portfolio = {
                "cash_balance": 0.0,
                "asset_position": float(total_value),
                "total_account_value": float(total_value),
            }

            # Evaluate candidates with LLM
            setups = await self._llm_equity.evaluate_candidates(
                scan_results=self._scanner_candidates,
                candle_cache=candle_cache,
                portfolio=portfolio,
            )

            if not setups:
                logger.info("LLM Equity: no actionable setups from %d candidates", len(self._scanner_candidates))
                return

            scanner_cfg = self._config.scanner_universal
            placed = 0

            for setup in setups:
                if placed >= scanner_cfg.max_open_positions:
                    break

                logger.info(
                    "LLM EQUITY SIGNAL [%s]: %s %s entry=%.2f stop=%.2f "
                    "target=%.2f conf=%.4f",
                    setup.symbol, setup.setup_type.value, setup.direction.value,
                    float(setup.entry_price), float(setup.stop_price),
                    float(setup.target_price), float(setup.confidence),
                )

                # Publish as LLM_SIGNAL for execution
                await self._bus.publish(
                    Topic.LLM_SIGNAL,
                    setup.model_dump(mode="json"),
                    source="llm_equity",
                )

                await self._notifications.send(
                    f"LLM EQUITY: {setup.direction.value} {setup.symbol}\n"
                    f"Entry: {float(setup.entry_price):.2f}\n"
                    f"Stop: {float(setup.stop_price):.2f}\n"
                    f"Target: {float(setup.target_price):.2f}\n"
                    f"Confidence: {float(setup.confidence):.4f}",
                    title=f"LLM Equity {setup.direction.value}",
                    tags="chart_with_upwards_trend" if setup.direction == Direction.LONG else "chart_with_downwards_trend",
                )
                placed += 1

            logger.info(
                "LLM Equity: published %d / %d setups",
                placed, len(setups),
            )

        except Exception as e:
            logger.error("LLM Equity evaluation failed: %s", e, exc_info=True)
            await self._notifications.send(
                f"LLM Equity ERROR: {e}",
                title="LLM Equity ERROR",
                tags="warning",
            )

    # =========================================================================
    # Event Handlers
    # =========================================================================

    async def _on_opening_range(self, msg: object) -> None:
        """Handle Opening Range publication with detailed logging."""
        payload = msg.payload if hasattr(msg, "payload") else msg  # type: ignore[union-attr]
        if isinstance(payload, dict):
            symbol = payload.get("symbol", "?")
            logger.info(
                "OR DETECTED [%s]: high=%.2f low=%.2f mid=%.2f "
                "range=%d ticks vol=%.0f valid=%s",
                symbol,
                payload.get("or_high", 0),
                payload.get("or_low", 0),
                payload.get("midpoint", 0),
                payload.get("range_ticks", 0),
                payload.get("volume", 0),
                payload.get("valid", False),
            )
        else:
            logger.info("Opening Range received: %s", payload)

    async def _on_market_data(self, msg: object) -> None:
        """Handle market data updates - evaluate strategy during active trading."""
        # Primary strategy only runs during ACTIVE_TRADING.
        # RSI Mean Reversion also runs during AFTERNOON (10:00-15:30 ET).
        active_phases = {SessionPhase.ACTIVE_TRADING, SessionPhase.AFTERNOON}
        if self._phase not in active_phases:
            return

        if not self._kill_switch.is_trading_allowed:
            return

        # ATR filter: skip if today's volatility is out of range
        if self._atr_filter.is_enabled and self._atr_filter.today_checked and not self._atr_filter.today_allowed:
            return

        payload = msg.payload if hasattr(msg, "payload") else msg  # type: ignore[union-attr]
        if not isinstance(payload, dict):
            return

        try:
            state = FuturesMarketState(**payload)
        except Exception:
            return

        # Update regime detector (observation-only)
        if self._regime_enabled:
            try:
                regime = self._regime_detector.update(
                    close=state.last_price,
                    vwap=state.vwap,
                    atr=state.atr_14,
                )
                logger.debug("Regime [%s]: %s", state.symbol, regime.value)
            except Exception:
                pass

        # Get OR range for this symbol
        or_range = self._market_data.get_or_range(state.symbol)

        # --- Primary strategy (ORB / Connors / EMA) ---
        if self._phase == SessionPhase.ACTIVE_TRADING and or_range and or_range.valid:
            result = self._strategy.evaluate(state, or_range)
            if result.has_setup and result.setup:
                setup = result.setup
                logger.info(
                    "SIGNAL [%s]: %s %s entry=%.2f stop=%.2f target=%.2f "
                    "risk=%d ticks reward=%d ticks conf=%.2f",
                    setup.symbol, setup.setup_type.value, setup.direction.value,
                    float(setup.entry_price), float(setup.stop_price),
                    float(setup.target_price), setup.risk_ticks,
                    setup.reward_ticks, float(setup.confidence),
                )

                # Size the trade
                intent = self._risk_manager.size_trade(setup)
                if intent:
                    await self._bus.publish(
                        Topic.ORDER,
                        intent.model_dump(),
                        source="strategy",
                    )
                    logger.info(
                        "TRADE INTENT [%s]: %s x%d risk=$%.2f",
                        setup.symbol, setup.direction.value,
                        intent.contracts, float(intent.risk_usd),
                    )

        # --- RSI Mean Reversion (secondary intraday strategy) ---
        await self._evaluate_rsi_mr(state, or_range)

    async def _evaluate_rsi_mr(
        self,
        state: FuturesMarketState,
        or_range: Optional[ORBRange],
    ) -> None:
        """Evaluate RSI Mean Reversion strategy as secondary intraday strategy.

        Coordination rules:
        - Does NOT enter if execution engine has any active trades
          (i.e., the primary strategy has an open position).
        - Always processes exit signals for its own open positions.
        - Uses a dummy OR range if none is available (RSI MR doesn't use it).
        """
        if not self._rsi_mr_strategy:
            return

        # Build a dummy OR range if none available (RSI MR ignores it)
        if or_range is None:
            or_range = ORBRange(
                symbol=state.symbol,
                or_high=state.last_price,
                or_low=state.last_price,
                midpoint=state.last_price,
                range_ticks=0,
                volume=Decimal("0"),
                vwap=state.last_price,
                timestamp=state.timestamp,
                valid=False,
            )

        result = self._rsi_mr_strategy.evaluate(state, or_range)

        if not result.has_setup or not result.setup:
            return

        setup = result.setup

        # Handle EXIT signals -- always process these
        if setup.setup_type in (SetupType.RSI_MR_EXIT_LONG, SetupType.RSI_MR_EXIT_SHORT):
            logger.info(
                "RSI_MR EXIT [%s]: %s price=%.2f",
                setup.symbol, setup.setup_type.value,
                float(setup.entry_price),
            )
            # Flatten the position
            await self._execution.flatten_all()
            self._rsi_mr_strategy.record_exit()
            return

        # Handle ENTRY signals -- only if no other strategy has a position
        if self._execution.has_active_trades:
            logger.debug(
                "RSI_MR entry blocked: primary strategy has active trades"
            )
            return

        # Block if RSI2 Connors has an overnight position
        if self._rsi2_strategy and self._rsi2_strategy.in_position:
            logger.debug(
                "RSI_MR entry blocked: RSI2 Connors has overnight position"
            )
            return

        logger.info(
            "RSI_MR SIGNAL [%s]: %s %s entry=%.2f stop=%.2f "
            "risk=%d ticks conf=%.2f",
            setup.symbol, setup.setup_type.value, setup.direction.value,
            float(setup.entry_price), float(setup.stop_price),
            setup.risk_ticks, float(setup.confidence),
        )

        # Size the trade
        intent = self._risk_manager.size_trade(setup)
        if intent:
            await self._bus.publish(
                Topic.ORDER,
                intent.model_dump(),
                source="rsi_mr_strategy",
            )
            self._rsi_mr_strategy.record_entry(
                direction=setup.direction,
                entry_price=setup.entry_price,
            )
            logger.info(
                "RSI_MR TRADE INTENT [%s]: %s x%d risk=$%.2f",
                setup.symbol, setup.direction.value,
                intent.contracts, float(intent.risk_usd),
            )


    # =========================================================================
    # RSI(2) Connors Daily Evaluation
    # =========================================================================

    async def _evaluate_rsi2_daily(self) -> None:
        """Evaluate RSI(2) Connors strategy on daily bars at 16:00 ET.

        Fetches 2 years of daily bars from IB, converts to DailyBar objects,
        and calls the strategy's evaluate_daily(). On entry/exit signals,
        places a market order (executes at next open).
        """
        from .strategies.rsi2_connors import DailyBar

        if self._rsi2_strategy is None:
            return

        symbol = self._rsi2_strategy._symbol
        today = datetime.now(self._session_tz).date()

        logger.info("RSI2 daily evaluation starting for %s", symbol)

        try:
            # Fetch 2 years of daily bars from IB
            contract = await self._ib_client.qualify_contract(symbol)
            ib_bars = await self._ib_client.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr="2 Y",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                keepUpToDate=False,
            )

            if not ib_bars:
                logger.warning("RSI2: no daily bars returned for %s", symbol)
                return

            # Convert IB bars to DailyBar objects
            bars: list[DailyBar] = []
            for b in ib_bars:
                bar_date = b.date.date() if isinstance(b.date, datetime) else b.date
                bars.append(DailyBar(
                    date=bar_date,
                    open=Decimal(str(b.open)),
                    high=Decimal(str(b.high)),
                    low=Decimal(str(b.low)),
                    close=Decimal(str(b.close)),
                    volume=Decimal(str(b.volume)) if b.volume else Decimal("0"),
                ))

            logger.info(
                "RSI2: fetched %d daily bars for %s (from %s to %s)",
                len(bars), symbol, bars[0].date, bars[-1].date,
            )

            # Evaluate the strategy
            result = self._rsi2_strategy.evaluate_daily(bars, today)

            if not result.has_setup or not result.setup:
                logger.info("RSI2: no signal — %s", result.reason)
                return

            setup = result.setup

            # --- ENTRY: RSI2_LONG -> place market buy (1 contract) ---
            if setup.setup_type == SetupType.RSI2_LONG:
                logger.info(
                    "RSI2 ENTRY: BUY %s x1 @ market (will fill at next open) "
                    "| stop=%.2f | RSI2 entry",
                    symbol, float(setup.stop_price),
                )
                trade = await self._ib_client.place_market_order(
                    symbol=symbol,
                    direction=Direction.LONG,
                    contracts=1,
                )
                await self._notifications.send(
                    f"RSI2 ENTRY: BUY {symbol} x1 @ market\n"
                    f"Stop: {float(setup.stop_price):.2f} ({self._rsi2_strategy._cfg.stop_points} pts)\n"
                    f"Exit: RSI(2) > {self._rsi2_strategy._cfg.rsi_exit_threshold} or {self._rsi2_strategy._cfg.max_hold_days}d max",
                    title="RSI2 Connors ENTRY",
                    tags="chart_with_upwards_trend",
                )

                # Track in execution engine's active trades
                trade_id = f"rsi2_{symbol}_{today.isoformat()}"
                self._execution._active_trades[symbol] = {
                    "trades": [trade],
                    "intent": None,
                    "entry_time": datetime.now(timezone.utc),
                    "trade_id": trade_id,
                    "source": "rsi2_connors",
                }

            # --- ENTRY: RSI2_SHORT -> place market sell (1 contract) ---
            elif setup.setup_type == SetupType.RSI2_SHORT:
                logger.info(
                    "RSI2 ENTRY: SELL SHORT %s x1 @ market (will fill at next open) "
                    "| stop=%.2f | RSI2 short entry",
                    symbol, float(setup.stop_price),
                )
                trade = await self._ib_client.place_market_order(
                    symbol=symbol,
                    direction=Direction.SHORT,
                    contracts=1,
                )
                await self._notifications.send(
                    f"RSI2 ENTRY: SELL SHORT {symbol} x1 @ market\n"
                    f"Stop: {float(setup.stop_price):.2f} ({self._rsi2_strategy._cfg.stop_points} pts above)\n"
                    f"Exit: RSI(2) < {self._rsi2_strategy._cfg.rsi_short_exit_threshold} or {self._rsi2_strategy._cfg.max_hold_days}d max",
                    title="RSI2 Connors SHORT ENTRY",
                    tags="chart_with_downwards_trend",
                )

                # Track in execution engine's active trades
                trade_id = f"rsi2_{symbol}_{today.isoformat()}"
                self._execution._active_trades[symbol] = {
                    "trades": [trade],
                    "intent": None,
                    "entry_time": datetime.now(timezone.utc),
                    "trade_id": trade_id,
                    "source": "rsi2_connors",
                }

            # --- EXIT: RSI2_EXIT -> flatten long position ---
            elif setup.setup_type == SetupType.RSI2_EXIT:
                logger.info(
                    "RSI2 EXIT: SELL %s @ market | reason: %s",
                    symbol, result.reason,
                )
                await self._ib_client.flatten_position(symbol)

                # Clear from execution engine tracking
                self._execution._active_trades.pop(symbol, None)

                await self._notifications.send(
                    f"RSI2 EXIT: SELL {symbol} @ market\n"
                    f"Reason: {result.reason}",
                    title="RSI2 Connors EXIT",
                    tags="white_check_mark",
                )

            # --- EXIT: RSI2_EXIT_SHORT -> cover short position ---
            elif setup.setup_type == SetupType.RSI2_EXIT_SHORT:
                logger.info(
                    "RSI2 EXIT SHORT: COVER %s @ market | reason: %s",
                    symbol, result.reason,
                )
                await self._ib_client.flatten_position(symbol)

                # Clear from execution engine tracking
                self._execution._active_trades.pop(symbol, None)

                await self._notifications.send(
                    f"RSI2 EXIT SHORT: COVER {symbol} @ market\n"
                    f"Reason: {result.reason}",
                    title="RSI2 Connors SHORT EXIT",
                    tags="white_check_mark",
                )

        except Exception as e:
            logger.error("RSI2 daily evaluation failed: %s", e, exc_info=True)
            await self._notifications.send(
                f"RSI2 evaluation error: {e}",
                title="RSI2 Error",
                tags="warning",
            )


async def main() -> None:
    """Entry point."""
    load_dotenv()

    config = load_config()
    setup_logging(
        level=config.logging.level,
        log_file=config.logging.file,
    )

    bot = IBBot(config)

    # Signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.stop()))

    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
