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
import os
import signal
import sys
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .config.loader import load_config, TradingConfig
from .core.enums import SessionPhase, Topic
from .services.message_bus import MessageBus
from .services.ib_client import IBClient
from .services.market_data import MarketDataService
from .services.execution_engine import ExecutionEngine
from .services.risk_manager import RiskManager
from .services.kill_switch import KillSwitchService
from .services.notifications import NotificationService
from .strategies.orb import ORBStrategy

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
        self._execution = ExecutionEngine(
            ib_client=self._ib_client,
            risk_manager=self._risk_manager,
            kill_switch=self._kill_switch,
            bus=self._bus,
        )
        self._strategy = ORBStrategy(
            strategy_config=config.strategy,
            stops_config=config.stops,
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
            f"  Strategy:        {cfg.strategy.name.upper()} (breakout buffer: {cfg.strategy.breakout_buffer_ticks} ticks)",
            f"  Contracts:       {', '.join(enabled)} (disabled: {', '.join(disabled) or 'none'})",
            f"  Allow short:     {cfg.strategy.allow_short}",
            f"  VWAP confirm:    {cfg.strategy.vwap_confirmation}",
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

        elif new == SessionPhase.EOD_FLATTEN:
            # Flatten all positions
            logger.info("EOD FLATTEN: closing all positions")
            await self._execution.flatten_all()
            await self._notifications.notify_session("EOD: all positions flattened")

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
        if self._phase != SessionPhase.ACTIVE_TRADING:
            return

        if not self._kill_switch.is_trading_allowed:
            return

        payload = msg.payload if hasattr(msg, "payload") else msg  # type: ignore[union-attr]
        if not isinstance(payload, dict):
            return

        from .core.models import FuturesMarketState

        try:
            state = FuturesMarketState(**payload)
        except Exception:
            return

        # Get OR range for this symbol
        or_range = self._market_data.get_or_range(state.symbol)
        if not or_range or not or_range.valid:
            return

        # Evaluate strategy
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
