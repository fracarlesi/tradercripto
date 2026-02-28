"""
IB Bot Main Orchestrator
=========================

Session-phase state machine for Opening Range Breakout trading.

Lifecycle:
  PRE_MARKET → OPENING_RANGE (9:30-9:45) → ACTIVE_TRADING (9:45-11:30)
  → AFTERNOON (manage existing) → EOD_FLATTEN (15:45) → CLOSED

Entry point: python -m ib_bot.main
"""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, time, timezone
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


def setup_logging(level: str = "INFO", log_file: str = "logs/ib_bot.log") -> None:
    """Configure logging for IB bot."""
    log_dir = Path(log_file).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, mode="a"),
        ],
    )


class IBBot:
    """Main orchestrator for IB Opening Range Breakout bot."""

    def __init__(self, config: TradingConfig) -> None:
        self._config = config
        self._phase = SessionPhase.CLOSED
        self._running = False

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

    async def start(self) -> None:
        """Start the bot: connect, subscribe, and run main loop."""
        logger.info("=" * 60)
        logger.info("IB Bot Starting - ORB Strategy")
        logger.info("Contracts: %s", self._symbols)
        logger.info("=" * 60)

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

        await self._execution.stop()
        await self._market_data.stop()
        await self._kill_switch.stop()
        await self._bus.stop()
        await self._ib_client.disconnect()

        await self._notifications.notify_session("IB Bot stopped")
        logger.info("IB Bot shut down complete")

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
        # Note: proper timezone handling would use pytz or zoneinfo
        # For now, phase detection is handled by MarketDataService

        new_phase = self._detect_phase(now)
        if new_phase != self._phase:
            old_phase = self._phase
            self._phase = new_phase
            await self._on_phase_change(old_phase, new_phase)

    def _detect_phase(self, now: datetime) -> SessionPhase:
        """Detect session phase from current UTC time.

        Note: This is a simplified version. In production,
        use proper ET timezone conversion.
        """
        # Approximate ET offset (UTC-5 or UTC-4 for DST)
        # Real implementation should use zoneinfo
        et_hour = (now.hour - 5) % 24  # Simplified EST
        et_minute = now.minute
        t = time(et_hour, et_minute)

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

    async def _on_phase_change(
        self, old: SessionPhase, new: SessionPhase
    ) -> None:
        """Handle session phase transitions."""
        logger.info("Phase transition: %s → %s", old.value, new.value)
        await self._notifications.notify_session(
            f"Phase: {old.value} → {new.value}"
        )

        if new == SessionPhase.PRE_MARKET:
            # Reset for new day
            self._risk_manager.reset_daily()
            self._kill_switch.reset_daily()
            self._execution.reset_daily()
            self._market_data.reset_session()
            logger.info("Daily reset complete")

        elif new == SessionPhase.EOD_FLATTEN:
            # Flatten all positions
            logger.info("EOD FLATTEN: closing all positions")
            await self._execution.flatten_all()
            await self._notifications.notify_session("EOD: all positions flattened")

    async def _on_opening_range(self, msg: object) -> None:
        """Handle Opening Range publication."""
        payload = msg.payload if hasattr(msg, "payload") else msg  # type: ignore[union-attr]
        logger.info("Opening Range received: %s", payload.get("symbol", "?") if isinstance(payload, dict) else "?")

    async def _on_market_data(self, msg: object) -> None:
        """Handle market data updates — evaluate strategy during active trading."""
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
            # Size the trade
            intent = self._risk_manager.size_trade(result.setup)
            if intent:
                await self._bus.publish(
                    Topic.ORDER,
                    intent.model_dump(),
                    source="strategy",
                )
                logger.info("Trade intent published: %s", result.setup.symbol)


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
