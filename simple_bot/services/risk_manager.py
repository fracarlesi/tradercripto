"""
HLQuantBot Risk Manager Service
================================

Professional position sizing and risk management.

Features:
- Risk-based position sizing (not fixed %)
- Max positions and exposure limits
- Correlation checks between positions
- Generates TradeIntent from approved Setups

Formula:
    risk_amount = equity * risk_per_trade_pct
    position_size = risk_amount / stop_distance

Author: Francesco Carlesi
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .base import BaseService
from .message_bus import Message, MessageBus
from ..core.enums import Topic
from ..core.models import (
    Setup, TradeIntent, RiskParams, Direction, CooldownState, CooldownReason
)


logger = logging.getLogger(__name__)


# =============================================================================
# Correlation Groups
# =============================================================================

CORR_GROUPS: Dict[str, List[str]] = {
    "btc_ecosystem": ["BTC", "STX", "ORDI", "RUNE"],
    "eth_ecosystem": ["ETH", "ARB", "OP", "MATIC", "STRK", "ZK", "SCROLL", "BLAST"],
    "layer1": ["SOL", "AVAX", "SUI", "APT", "SEI", "TIA", "NEAR", "ADA", "DOT"],
    "defi": ["UNI", "AAVE", "CRV", "SNX", "DYDX", "GMX", "PENDLE", "JUP"],
    "meme": ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "MEME", "MYRO"],
    "ai": ["FET", "RNDR", "TAO", "ARKM", "WLD", "NEAR"],
}


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class RiskConfig:
    """Risk management configuration."""

    # Per-trade risk
    per_trade_pct: float = 0.5        # Risk 0.5% per trade
    max_per_trade_pct: float = 1.0    # Absolute max

    # Position limits
    max_positions: int = 2
    max_exposure_pct: float = 100.0   # Max notional as % of equity
    max_position_pct: float = 30.0    # Max 30% of capital per trade (conservative)

    # Leverage
    leverage: float = 1.0
    max_leverage: float = 2.0

    # Stops (conservative: 1.5% SL, 3% TP for 1:2 risk/reward ratio)
    stop_loss_pct: float = 1.5        # Stop loss at 1.5% from entry
    take_profit_pct: float = 3.0      # Take profit at 3% from entry (1:2 R:R)
    trailing_atr_mult: float = 2.5

    # Slippage
    max_slippage_pct: float = 0.1

    # Daily trade limit
    max_daily_trades: int = 3  # Max trades per day (resets at UTC midnight)


# =============================================================================
# Risk Manager Service
# =============================================================================

class RiskManagerService(BaseService):
    """
    Risk management and position sizing service.

    Subscribes to: Topic.SETUPS (approved setups after LLM veto)
    Publishes to: Topic.TRADE_INTENT (sized trades ready for execution)

    Key responsibilities:
    1. Calculate position size based on risk amount and stop distance
    2. Check position limits and exposure
    3. Validate correlation between positions
    4. Generate TradeIntent for execution
    """

    def __init__(
        self,
        name: str = "risk_manager",
        bus: Optional[MessageBus] = None,
        config: Optional[RiskConfig] = None,
        client: Optional[Any] = None,
        telegram: Optional[Any] = None,
    ) -> None:
        """Initialize RiskManagerService."""
        super().__init__(
            name=name,
            bus=bus,
            loop_interval_seconds=60,  # Check every minute
        )

        self._config = config or RiskConfig()
        self._client = client  # HyperliquidClient for equity updates
        self._telegram = telegram  # TelegramService for alerts

        # State
        self._current_equity: Decimal = Decimal("100")  # Safe default, updated from API
        self._open_positions: Dict[str, Dict] = {}
        self._pending_intents: Dict[str, TradeIntent] = {}

        # Cooldown state
        self._cooldown_state: Optional[CooldownState] = None

        # In-memory per-symbol cooldown (prevent re-entry within N minutes)
        self._last_trade_time: Dict[str, datetime] = {}
        self._cooldown_minutes: int = 3  # Minimum minutes between trades on same symbol

        # Extended cooldown for structural rejections (e.g. OI cap reached)
        self._rejection_cooldowns: Dict[str, datetime] = {}  # symbol -> cooldown_until
        self._rejection_cooldown_minutes: int = 30

        # In-memory daily trade counter (resets at UTC midnight)
        self._trades_today: int = 0
        self._last_trade_day: Optional[datetime] = None

        self._logger.info(
            "RiskManagerService initialized: risk=%.1f%%, max_pos=%d, max_exposure=%.0f%%",
            self._config.per_trade_pct,
            self._config.max_positions,
            self._config.max_exposure_pct,
        )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def _on_start(self) -> None:
        """Subscribe to setups topic and sync state."""
        self._logger.info("Starting RiskManagerService...")

        # CRITICAL: Clear stale pending intents from previous session
        self.clear_all_pending_intents()
        self._logger.info("Cleared stale pending intents")

        if self.bus:
            await self.subscribe(Topic.SETUPS, self._handle_setup)
            await self.subscribe(Topic.ORDERS, self._handle_order_event)
            await self.subscribe(Topic.FILLS, self._handle_fill_event)
            self._logger.info("Subscribed to SETUPS, ORDERS and FILLS topics")

        # Fetch initial equity from exchange
        await self._update_equity()

        # Sync existing positions from exchange
        await self._sync_positions_from_exchange()
        self._logger.info(
            "Synced %d positions from exchange", len(self._open_positions)
        )

        # Load any active cooldown (no-op, in-memory only)
        await self.load_active_cooldown()

    async def _on_stop(self) -> None:
        """Cleanup."""
        self._logger.info("Stopping RiskManagerService...")

    async def _run_iteration(self) -> None:
        """Periodic tasks - update equity, sync positions."""
        await self._update_equity()
        await self._sync_positions_from_exchange()

    async def _update_equity(self) -> None:
        """Fetch current equity from exchange."""
        if not self._client:
            return

        try:
            state = await self._client.get_account_state()
            equity = Decimal(str(state.get("equity", 0)))
            if equity > 0:
                self._current_equity = equity
                self._logger.debug("Equity updated: $%.2f", float(equity))
        except Exception as e:
            self._logger.warning("Failed to update equity: %s", e)

    async def _sync_positions_from_exchange(self) -> None:
        """Sync open positions from exchange to local state."""
        if not self._client:
            return

        try:
            positions = await self._client.get_positions()
            synced_symbols = set()

            for pos in positions:
                symbol = pos.get("symbol")
                size = pos.get("size", 0)

                # Only track positions with actual size and notional > $1
                # (filters dust positions left after SL closes)
                entry_price = pos.get("entryPrice", 0)
                notional = abs(size) * entry_price
                if abs(size) > 0.0001 and notional >= 1.0:
                    synced_symbols.add(symbol)

                    side = pos.get("side", "long")
                    self._open_positions[symbol] = {
                        "symbol": symbol,
                        "side": side,
                        "size": abs(size),
                        "entry_price": entry_price,
                        "notional": notional,
                        "mark_price": pos.get("markPrice", entry_price),
                        "unrealized_pnl": pos.get("unrealizedPnl", 0),
                    }
                    self._logger.debug(
                        "Synced position: %s %s %.4f @ %.2f",
                        side.upper(),
                        symbol,
                        abs(size),
                        entry_price,
                    )

            # Remove positions that no longer exist on exchange
            closed_symbols = set(self._open_positions.keys()) - synced_symbols
            for symbol in closed_symbols:
                del self._open_positions[symbol]
                self._logger.info("Position closed (removed): %s", symbol)

        except Exception as e:
            self._logger.error("Failed to sync positions: %s", e)

    async def _health_check_impl(self) -> bool:
        """Check service health."""
        return True

    # =========================================================================
    # Setup Handling
    # =========================================================================

    async def _handle_setup(self, message: Message) -> None:
        """
        Handle incoming setup from strategy/LLM veto.

        Args:
            message: Message containing setup data from message bus
        """
        try:
            # Extract payload from Message
            setup_data = message.payload

            # Reconstruct Setup
            setup = Setup(**setup_data)

            self._logger.info(
                "Received setup: %s %s @ %.2f",
                setup.direction.value,
                setup.symbol,
                float(setup.entry_price),
            )

            # Check daily trade limit before processing
            today_count = await self._get_today_trade_count()
            max_daily = self._config.max_daily_trades
            if today_count >= max_daily:
                self._logger.warning(
                    "Daily trade limit reached: %d/%d trades today. Rejecting setup: %s",
                    today_count,
                    max_daily,
                    setup.id,
                )
                return

            # Calculate risk params
            risk_params = self._calculate_risk_params(setup)

            if not risk_params.size_approved:
                self._logger.warning(
                    "Setup rejected: %s - %s",
                    setup.id,
                    risk_params.rejection_reason,
                )
                return

            # Create TradeIntent
            intent = self._create_trade_intent(setup, risk_params)

            # Publish for execution
            await self._publish_intent(intent)

        except Exception as e:
            self._logger.error("Error handling setup: %s", e, exc_info=True)

    # =========================================================================
    # Pending Intent Cleanup (Order / Fill events)
    # =========================================================================

    async def _handle_order_event(self, message: Message) -> None:
        """Clear pending intent when an order fails or is cancelled.

        The pending intent is NOT cleared on ``order_submitted`` because the
        position hasn't appeared in ``_open_positions`` yet (synced every 60s).
        Clearing too early creates a TOCTOU race where new trades bypass
        ``max_positions``.  The intent is cleared later by the
        ``position_opened`` fill event (see ``_handle_fill_event``).
        """
        try:
            payload = message.payload
            event = payload.get("event", "")

            # Extract symbol from nested signal dict or flat payload
            symbol = None
            signal = payload.get("signal")
            if isinstance(signal, dict):
                symbol = signal.get("symbol")
            if not symbol:
                symbol = payload.get("symbol")
            if not symbol:
                return

            if event in ("order_error", "order_cancelled", "slippage_rejected"):
                self.clear_pending_intent(symbol)
                self.decrement_trade_count()

                # Apply extended cooldown for structural rejections
                error_msg = payload.get("error", "")
                if "open interest is at cap" in error_msg.lower():
                    cooldown_until = datetime.now(timezone.utc) + timedelta(
                        minutes=self._rejection_cooldown_minutes
                    )
                    self._rejection_cooldowns[symbol] = cooldown_until
                    self._logger.warning(
                        "OI cap rejection for %s — cooldown %dmin until %s",
                        symbol,
                        self._rejection_cooldown_minutes,
                        cooldown_until.strftime("%H:%M UTC"),
                    )

                self._logger.info(
                    "Cleared pending intent for %s on %s event", symbol, event
                )
        except Exception as e:
            self._logger.debug("Error handling order event: %s", e)

    async def _handle_fill_event(self, message: Message) -> None:
        """Track position opens/closes from fill events.

        On ``position_opened``: immediately add to ``_open_positions`` and
        clear the pending intent.  This closes the TOCTOU gap between order
        submission and the next 60-second exchange sync.

        On ``position_closed``: remove from ``_open_positions`` and clear
        any stale pending intent.
        """
        try:
            payload = message.payload
            event = payload.get("event", "")
            symbol = payload.get("symbol")
            if not symbol:
                return

            if event == "position_opened":
                self._open_positions[symbol] = {
                    "symbol": symbol,
                    "side": payload.get("direction", "long"),
                    "size": payload.get("size", 0),
                    "entry_price": payload.get("entry_price", 0),
                    "notional": payload.get("notional", 0),
                }
                self.clear_pending_intent(symbol)
                self._logger.info("Position tracked from fill: %s", symbol)

            elif event == "position_closed":
                self._open_positions.pop(symbol, None)
                self.clear_pending_intent(symbol)

        except Exception as e:
            self._logger.debug("Error handling fill event: %s", e)

    # =========================================================================
    # Position Sizing
    # =========================================================================

    def _calculate_risk_params(self, setup: Setup) -> RiskParams:
        """
        Calculate position size and risk parameters.

        Formula:
            risk_amount = equity * per_trade_pct
            stop_distance_pct = abs(entry - stop) / entry
            position_size = risk_amount / (stop_distance_pct * entry_price)
        """
        equity = self._current_equity
        cfg = self._config

        def _reject(reason: str) -> RiskParams:
            return RiskParams(
                risk_amount=Decimal("0"),
                position_size=Decimal("0"),
                notional_value=Decimal("0"),
                stop_price=setup.stop_price,
                stop_distance_pct=setup.stop_distance_pct,
                exposure_pct=Decimal("0"),
                total_exposure_pct=self._get_total_exposure_pct(),
                size_approved=False,
                rejection_reason=reason,
            )

        # Check position limits (including pending intents!)
        total_position_count = len(self._open_positions) + len(self._pending_intents)
        if total_position_count >= cfg.max_positions:
            return _reject(
                f"Max positions reached: {cfg.max_positions} "
                f"(open={len(self._open_positions)}, pending={len(self._pending_intents)})"
            )

        # Check if already in position or pending for this symbol
        if setup.symbol in self._open_positions:
            return _reject(f"Already in position: {setup.symbol}")

        if setup.symbol in self._pending_intents:
            return _reject(f"Already pending intent: {setup.symbol}")

        # Extended rejection cooldown (e.g. OI cap reached — no point retrying)
        if setup.symbol in self._rejection_cooldowns:
            cooldown_until = self._rejection_cooldowns[setup.symbol]
            now = datetime.now(timezone.utc)
            if now < cooldown_until:
                remaining = (cooldown_until - now).total_seconds() / 60
                return _reject(f"Rejection cooldown: {remaining:.0f}min remaining for {setup.symbol}")
            else:
                del self._rejection_cooldowns[setup.symbol]

        # Per-symbol cooldown check
        if setup.symbol in self._last_trade_time:
            elapsed = (datetime.now(timezone.utc) - self._last_trade_time[setup.symbol]).total_seconds() / 60
            if elapsed < self._cooldown_minutes:
                remaining = self._cooldown_minutes - elapsed
                return _reject(f"Cooldown: {remaining:.1f}min remaining for {setup.symbol}")

        # Correlation filter: max 1 position per correlated group
        if not self._check_correlation(setup.symbol):
            return _reject(f"Correlation filter: {setup.symbol} blocked by open position in same group")

        # Kelly sizing based on setup confidence/probability
        if hasattr(setup, 'confidence') and setup.confidence and float(setup.confidence) > 0:
            risk_pct = Decimal(str(self._kelly_fraction(float(setup.confidence))))
        else:
            risk_pct = Decimal(str(cfg.per_trade_pct)) / 100
        risk_amount = equity * risk_pct

        # Calculate position size from risk
        stop_distance_pct = setup.stop_distance_pct / 100

        if stop_distance_pct <= 0:
            return RiskParams(
                risk_amount=risk_amount,
                position_size=Decimal("0"),
                notional_value=Decimal("0"),
                stop_price=setup.stop_price,
                stop_distance_pct=setup.stop_distance_pct,
                exposure_pct=Decimal("0"),
                total_exposure_pct=self._get_total_exposure_pct(),
                size_approved=False,
                rejection_reason="Invalid stop distance",
            )

        # position_size = risk_amount / (stop_distance * entry_price)
        position_size = risk_amount / (stop_distance_pct * setup.entry_price)
        notional_value = position_size * setup.entry_price

        # Ensure minimum order value for profitability
        # At $50 with 2-3% TP = $1-1.50 profit, easily covers ~$0.05 fees
        MIN_NOTIONAL = Decimal("50")  # $50 minimum for meaningful profit
        if notional_value < MIN_NOTIONAL:
            notional_value = MIN_NOTIONAL
            position_size = notional_value / setup.entry_price

        # Cap position size at max_position_pct of equity (conservative: 30%)
        max_position_value = equity * Decimal(str(cfg.max_position_pct)) / 100
        if notional_value > max_position_value:
            notional_value = max_position_value
            position_size = notional_value / setup.entry_price
            self._logger.info(
                "Position size capped at %.1f%% of equity ($%.2f)",
                float(cfg.max_position_pct),
                float(notional_value),
            )

        # Check exposure limits
        exposure_pct = (notional_value / equity) * 100
        total_exposure = self._get_total_exposure_pct() + exposure_pct

        if total_exposure > Decimal(str(cfg.max_exposure_pct)):
            # Reduce size to fit exposure limit
            available_exposure = Decimal(str(cfg.max_exposure_pct)) - self._get_total_exposure_pct()
            if available_exposure <= 0:
                return RiskParams(
                    risk_amount=risk_amount,
                    position_size=Decimal("0"),
                    notional_value=Decimal("0"),
                    stop_price=setup.stop_price,
                    stop_distance_pct=setup.stop_distance_pct,
                    exposure_pct=exposure_pct,
                    total_exposure_pct=total_exposure,
                    size_approved=False,
                    rejection_reason=f"Max exposure exceeded: {total_exposure:.1f}%",
                )

            # Reduce to available exposure
            max_notional = equity * available_exposure / 100
            notional_value = min(notional_value, max_notional)
            position_size = notional_value / setup.entry_price
            exposure_pct = (notional_value / equity) * 100

        return RiskParams(
            risk_amount=risk_amount,
            position_size=position_size,
            notional_value=notional_value,
            stop_price=setup.stop_price,
            stop_distance_pct=setup.stop_distance_pct,
            trailing_distance_atr=Decimal(str(cfg.trailing_atr_mult)),
            exposure_pct=exposure_pct,
            total_exposure_pct=self._get_total_exposure_pct() + exposure_pct,
            leverage_used=Decimal(str(cfg.leverage)),
            size_approved=True,
        )

    def _get_total_exposure_pct(self) -> Decimal:
        """Calculate total current exposure as % of equity."""
        total_notional = sum(
            Decimal(str(pos.get("notional", 0)))
            for pos in self._open_positions.values()
        )
        if self._current_equity <= 0:
            return Decimal("0")
        return (total_notional / self._current_equity) * 100

    def _kelly_fraction(self, prob: float, rr_ratio: float = 2.0) -> float:
        """Half-Kelly criterion for position sizing.

        Args:
            prob: Estimated probability of winning (P(TP))
            rr_ratio: Risk-reward ratio (TP/SL), default 2.0

        Returns:
            Fractional risk as decimal (e.g. 0.035 = 3.5%)
        """
        if prob <= 0.5:
            return 0.02  # minimum 2% risk
        q = 1.0 - prob
        kelly = (prob * rr_ratio - q) / rr_ratio
        half_kelly = kelly / 2.0
        # Clamp between 2% and max_per_trade_pct
        max_frac = float(self._config.max_per_trade_pct) / 100
        return max(0.02, min(max_frac, half_kelly))

    def _check_correlation(self, symbol: str) -> bool:
        """Check if adding this symbol would violate correlation limits.

        Max 1 position per correlated group.

        Args:
            symbol: Symbol to check

        Returns:
            True if the symbol is allowed, False if blocked
        """
        for group_name, group_symbols in CORR_GROUPS.items():
            if symbol in group_symbols:
                for open_sym in self._open_positions:
                    if open_sym in group_symbols and open_sym != symbol:
                        self._logger.info(
                            "Correlation filter: %s blocked (same group '%s' as open %s)",
                            symbol, group_name, open_sym,
                        )
                        return False
        return True

    # =========================================================================
    # Trade Intent
    # =========================================================================

    def _create_trade_intent(self, setup: Setup, risk: RiskParams) -> TradeIntent:
        """Create TradeIntent from Setup and RiskParams."""
        return TradeIntent(
            id=f"intent_{setup.id}",
            setup_id=setup.id,
            symbol=setup.symbol,
            timestamp=datetime.now(timezone.utc),
            direction=setup.direction,
            setup_type=setup.setup_type,
            entry_price=setup.entry_price,
            position_size=risk.position_size,
            notional_value=risk.notional_value,
            stop_price=risk.stop_price,
            trailing_atr_mult=risk.trailing_distance_atr,
            risk_amount=risk.risk_amount,
            risk_pct=Decimal(str(self._config.per_trade_pct)),
            atr_pct=setup.atr_pct,
            regime=setup.regime.value if setup.regime else None,
            prefer_limit=True,
            max_slippage_pct=Decimal(str(self._config.max_slippage_pct)),
        )

    async def _publish_intent(self, intent: TradeIntent) -> None:
        """Publish trade intent to message bus."""
        if not self.bus:
            return

        # Increment daily trade counter BEFORE publishing
        self.increment_trade_count()

        # Record per-symbol trade time for cooldown
        self._last_trade_time[intent.symbol] = datetime.now(timezone.utc)

        # Track pending intent to prevent duplicate orders
        self._pending_intents[intent.symbol] = intent

        await self.publish(Topic.TRADE_INTENT, intent.model_dump())

        self._logger.info(
            "Published TRADE_INTENT: %s %s, size=%.4f, risk=$%.2f",
            intent.direction.value,
            intent.symbol,
            float(intent.position_size),
            float(intent.risk_amount),
        )

    # =========================================================================
    # Public API
    # =========================================================================

    def update_equity(self, equity: Decimal) -> None:
        """Update current equity value."""
        self._current_equity = equity
        self._logger.debug("Equity updated: $%.2f", float(equity))

    def add_position(self, symbol: str, position_data: Dict) -> None:
        """Track a new open position."""
        self._open_positions[symbol] = position_data

    def remove_position(self, symbol: str) -> None:
        """Remove a closed position."""
        self._open_positions.pop(symbol, None)

    def clear_pending_intent(self, symbol: str) -> None:
        """Clear a pending intent (called when order fills or is rejected)."""
        if symbol in self._pending_intents:
            self._pending_intents.pop(symbol, None)
            self._logger.debug("Cleared pending intent for %s", symbol)

    def clear_all_pending_intents(self) -> None:
        """Clear all pending intents (e.g., on startup or after timeout)."""
        count = len(self._pending_intents)
        self._pending_intents.clear()
        if count > 0:
            self._logger.info("Cleared %d pending intents", count)

    def get_position_count(self) -> int:
        """Get count of open positions."""
        return len(self._open_positions)


    # =========================================================================
    # Cooldown System
    # =========================================================================

    async def check_cooldown_required(self) -> tuple[bool, Optional[CooldownState]]:
        """
        Check if cooldown should be triggered.

        Cooldown rules:
        1. 3+ stoploss consecutivi in 1h -> cooldown 6h
        2. Daily drawdown > 5% -> cooldown 12h
        3. 5+ losing trades in 24h con win rate < 20% -> cooldown 24h

        Returns:
            Tuple of (is_cooldown_active, cooldown_state)
        """
        # Check if already in cooldown
        if self._cooldown_state and self._cooldown_state.active:
            if not self._cooldown_state.is_expired():
                return True, self._cooldown_state
            else:
                # Cooldown expired
                await self._clear_cooldown()
                return False, None

        # Check stoploss streak (3+ in 1 hour)
        recent_trades = await self._get_recent_trades(hours=1)
        consecutive_losses = self._count_consecutive_stoplosses(recent_trades)

        if consecutive_losses >= 3:
            cooldown_state = await self._trigger_cooldown(
                reason=CooldownReason.STOPLOSS_STREAK,
                duration_hours=6,
                details={"consecutive_losses": consecutive_losses}
            )
            return True, cooldown_state

        # Check daily drawdown (> 5%)
        daily_trades = await self._get_recent_trades(hours=24)
        daily_dd_pct = self._calculate_drawdown_pct(daily_trades)

        if daily_dd_pct > Decimal("5.0"):
            cooldown_state = await self._trigger_cooldown(
                reason=CooldownReason.DAILY_DRAWDOWN,
                duration_hours=12,
                details={"drawdown_pct": float(daily_dd_pct)}
            )
            return True, cooldown_state

        # Check low performance (5+ trades with win rate < 20%)
        if len(daily_trades) >= 5:
            wins = len([t for t in daily_trades if t.get("net_pnl", 0) > 0])
            win_rate = wins / len(daily_trades)
            if win_rate < 0.20:
                cooldown_state = await self._trigger_cooldown(
                    reason=CooldownReason.LOW_PERFORMANCE,
                    duration_hours=24,
                    details={"win_rate": round(win_rate, 2), "num_trades": len(daily_trades)}
                )
                return True, cooldown_state

        return False, None

    async def _get_recent_trades(self, hours: int) -> List[Dict]:
        """Get trades from the last N hours. Stubbed - no DB trades table."""
        return []

    async def _get_today_trade_count(self) -> int:
        """
        Get the number of trades opened today (since UTC midnight).

        Uses in-memory counter that resets at UTC midnight.

        Returns:
            Number of trades opened today
        """
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Reset counter at midnight
        if self._last_trade_day is None or self._last_trade_day < today_start:
            self._trades_today = 0
            self._last_trade_day = today_start

        self._logger.debug("Daily trade count: %d (in-memory)", self._trades_today)
        return self._trades_today

    def increment_trade_count(self) -> None:
        """Increment the daily trade counter. Call when a trade is opened."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if self._last_trade_day is None or self._last_trade_day < today_start:
            self._trades_today = 0
            self._last_trade_day = today_start

        self._trades_today += 1
        self._logger.info("Daily trade count incremented to %d", self._trades_today)

    def decrement_trade_count(self) -> None:
        """Decrement the daily trade counter when an unfilled order is cancelled."""
        if self._trades_today > 0:
            self._trades_today -= 1
            self._logger.info(
                "Daily trade count decremented to %d (cancelled/expired order)",
                self._trades_today,
            )

    def _count_consecutive_stoplosses(self, trades: List[Dict]) -> int:
        """Count consecutive stoploss exits from most recent trade."""
        consecutive = 0
        for trade in trades:
            notes = trade.get("notes", "") or ""
            # Check if trade was closed by stoploss
            if "stop" in notes.lower() or "sl" in notes.lower():
                consecutive += 1
            else:
                break
        return consecutive

    def _calculate_drawdown_pct(self, trades: List[Dict]) -> Decimal:
        """Calculate total drawdown percentage from trades."""
        if not trades or self._current_equity <= 0:
            return Decimal("0")

        total_pnl = sum(
            Decimal(str(t.get("net_pnl", 0) or 0))
            for t in trades
        )

        # Only negative PnL counts as drawdown
        if total_pnl >= 0:
            return Decimal("0")

        # Calculate DD as percentage of current equity
        dd_pct = abs(total_pnl / self._current_equity) * 100
        return dd_pct

    async def _trigger_cooldown(
        self,
        reason: CooldownReason,
        duration_hours: int,
        details: dict
    ) -> CooldownState:
        """Trigger cooldown (in-memory only)."""
        now = datetime.now(timezone.utc)
        cooldown_until = now + timedelta(hours=duration_hours)

        self._cooldown_state = CooldownState(
            active=True,
            reason=reason,
            triggered_at=now,
            cooldown_until=cooldown_until,
            trigger_details=details
        )

        # Send Telegram alert
        if self._telegram:
            try:
                await self._telegram.send_custom_alert(
                    f"COOLDOWN TRIGGERED\n"
                    f"Reason: {reason.value}\n"
                    f"Duration: {duration_hours}h\n"
                    f"Details: {details}\n"
                    f"Resuming at: {cooldown_until.strftime('%Y-%m-%d %H:%M UTC')}",
                    emoji="kill_switch"
                )
            except Exception as e:
                self._logger.warning("Failed to send Telegram alert: %s", e)

        self._logger.warning(
            "COOLDOWN TRIGGERED: %s for %dh - %s",
            reason.value,
            duration_hours,
            details
        )

        return self._cooldown_state

    async def _clear_cooldown(self) -> None:
        """Clear expired cooldown."""
        if self._cooldown_state:
            self._logger.info(
                "Cooldown expired: %s, resuming trading",
                self._cooldown_state.reason.value if self._cooldown_state.reason else "unknown"
            )

            # Send Telegram notification
            if self._telegram:
                try:
                    await self._telegram.send_custom_alert(
                        "COOLDOWN EXPIRED - Trading resumed",
                        emoji="startup"
                    )
                except Exception as e:
                    self._logger.warning("Failed to send Telegram alert: %s", e)

        self._cooldown_state = None

    async def load_active_cooldown(self) -> None:
        """Load any active cooldown on startup (no-op, cooldowns are in-memory only)."""
        pass

    def get_cooldown_state(self) -> Optional[CooldownState]:
        """Get current cooldown state."""
        return self._cooldown_state

    def is_cooldown_active(self) -> bool:
        """Check if cooldown is currently active."""
        if not self._cooldown_state or not self._cooldown_state.active:
            return False
        return not self._cooldown_state.is_expired()

    @property
    def metrics(self) -> Dict[str, Any]:
        """Get service metrics."""
        cooldown_info = None
        if self._cooldown_state and self._cooldown_state.active:
            cooldown_info = {
                "active": True,
                "reason": self._cooldown_state.reason.value if self._cooldown_state.reason else None,
                "until": self._cooldown_state.cooldown_until.isoformat() if self._cooldown_state.cooldown_until else None,
                "remaining_seconds": self._cooldown_state.time_remaining(),
            }

        return {
            "equity": float(self._current_equity),
            "open_positions": len(self._open_positions),
            "total_exposure_pct": float(self._get_total_exposure_pct()),
            "max_positions": self._config.max_positions,
            "risk_per_trade_pct": self._config.per_trade_pct,
            "cooldown": cooldown_info,
        }



# =============================================================================
# Factory
# =============================================================================

def create_risk_manager(
    bus: Optional[MessageBus] = None,
    config: Optional[RiskConfig] = None,
    client: Optional[Any] = None,
    telegram: Optional[Any] = None,
) -> RiskManagerService:
    """Factory function to create RiskManagerService."""
    return RiskManagerService(
        name="risk_manager",
        bus=bus,
        config=config,
        client=client,
        telegram=telegram,
    )
