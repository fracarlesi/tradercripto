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
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .base import BaseService
from .message_bus import Message, MessageBus
from ..core.enums import Topic
from ..core.models import (
    Setup, TradeIntent, RiskParams, Direction, CooldownState, CooldownReason, PerformanceMetrics
)


logger = logging.getLogger(__name__)


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
        db: Optional[Any] = None,
        config: Optional[RiskConfig] = None,
        client: Optional[Any] = None,
        telegram: Optional[Any] = None,
    ) -> None:
        """Initialize RiskManagerService."""
        super().__init__(
            name=name,
            bus=bus,
            db=db,
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
            self._logger.info("Subscribed to SETUPS topic")

        # Fetch initial equity from exchange
        await self._update_equity()

        # Sync existing positions from exchange
        await self._sync_positions_from_exchange()
        self._logger.info(
            "Synced %d positions from exchange", len(self._open_positions)
        )

        # Load any active cooldown from database
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

                # Only track positions with actual size
                if abs(size) > 0.0001:
                    synced_symbols.add(symbol)
                    entry_price = pos.get("entryPrice", 0)
                    notional = abs(size) * entry_price

                    self._open_positions[symbol] = {
                        "symbol": symbol,
                        "side": "long" if size > 0 else "short",
                        "size": abs(size),
                        "entry_price": entry_price,
                        "notional": notional,
                        "mark_price": pos.get("markPrice", entry_price),
                        "unrealized_pnl": pos.get("unrealizedPnl", 0),
                    }
                    self._logger.debug(
                        "Synced position: %s %s %.4f @ %.2f",
                        "LONG" if size > 0 else "SHORT",
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

        # Check position limits (including pending intents!)
        total_position_count = len(self._open_positions) + len(self._pending_intents)
        if total_position_count >= cfg.max_positions:
            return RiskParams(
                risk_amount=Decimal("0"),
                position_size=Decimal("0"),
                notional_value=Decimal("0"),
                stop_price=setup.stop_price,
                stop_distance_pct=setup.stop_distance_pct,
                exposure_pct=Decimal("0"),
                total_exposure_pct=self._get_total_exposure_pct(),
                size_approved=False,
                rejection_reason=f"Max positions reached: {cfg.max_positions} (open={len(self._open_positions)}, pending={len(self._pending_intents)})",
            )

        # Check if already in position or pending for this symbol
        if setup.symbol in self._open_positions:
            return RiskParams(
                risk_amount=Decimal("0"),
                position_size=Decimal("0"),
                notional_value=Decimal("0"),
                stop_price=setup.stop_price,
                stop_distance_pct=setup.stop_distance_pct,
                exposure_pct=Decimal("0"),
                total_exposure_pct=self._get_total_exposure_pct(),
                size_approved=False,
                rejection_reason=f"Already in position: {setup.symbol}",
            )

        if setup.symbol in self._pending_intents:
            return RiskParams(
                risk_amount=Decimal("0"),
                position_size=Decimal("0"),
                notional_value=Decimal("0"),
                stop_price=setup.stop_price,
                stop_distance_pct=setup.stop_distance_pct,
                exposure_pct=Decimal("0"),
                total_exposure_pct=self._get_total_exposure_pct(),
                size_approved=False,
                rejection_reason=f"Already pending intent: {setup.symbol}",
            )

        # Calculate risk amount
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
            prefer_limit=True,
            max_slippage_pct=Decimal(str(self._config.max_slippage_pct)),
        )

    async def _publish_intent(self, intent: TradeIntent) -> None:
        """Publish trade intent to message bus."""
        if not self.bus:
            return

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
        """Get trades from the last N hours."""
        if not self.db:
            return []

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            # Query closed trades from the last N hours
            query = """
                SELECT trade_id, symbol, side, net_pnl, exit_time, notes
                FROM trades
                WHERE is_closed = true
                  AND exit_time >= $1
                ORDER BY exit_time DESC
            """
            rows = await self.db.fetch(query, cutoff)
            return [dict(row) for row in rows]
        except Exception as e:
            self._logger.warning("Failed to get recent trades: %s", e)
            return []

    async def _get_today_trade_count(self) -> int:
        """
        Get the number of trades opened today (since UTC midnight).

        Used to enforce max_daily_trades limit to prevent overtrading.
        Resets automatically at UTC midnight.

        Returns:
            Number of trades opened today
        """
        if not self.db:
            return 0

        try:
            # Calculate UTC midnight today
            now = datetime.now(timezone.utc)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

            # Query trades opened today (not just closed)
            query = """
                SELECT COUNT(*) as count
                FROM trades
                WHERE entry_time >= $1
            """
            row = await self.db.fetchrow(query, today_start)
            count = row["count"] if row else 0
            self._logger.debug("Daily trade count: %d (since %s)", count, today_start.isoformat())
            return count
        except Exception as e:
            self._logger.warning("Failed to get today's trade count: %s", e)
            return 0

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
        """Trigger cooldown and persist to database."""
        now = datetime.now(timezone.utc)
        cooldown_until = now + timedelta(hours=duration_hours)

        self._cooldown_state = CooldownState(
            active=True,
            reason=reason,
            triggered_at=now,
            cooldown_until=cooldown_until,
            trigger_details=details
        )

        # Persist to database
        if self.db:
            try:
                await self.db.fetch(
                    """
                    INSERT INTO cooldowns (reason, triggered_at, cooldown_until, details)
                    VALUES ($1, $2, $3, $4)
                    """,
                    reason.value,
                    now,
                    cooldown_until,
                    json.dumps(details)
                )
            except Exception as e:
                self._logger.error("Failed to persist cooldown to DB: %s", e)

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
        """Load any active cooldown from database on startup."""
        if not self.db:
            return

        try:
            row = await self.db.fetchrow(
                """
                SELECT reason, triggered_at, cooldown_until, details
                FROM cooldowns
                WHERE cooldown_until > NOW()
                ORDER BY triggered_at DESC
                LIMIT 1
                """
            )

            if row:
                self._cooldown_state = CooldownState(
                    active=True,
                    reason=CooldownReason(row["reason"]),
                    triggered_at=row["triggered_at"],
                    cooldown_until=row["cooldown_until"],
                    trigger_details=json.loads(row["details"]) if row["details"] else {}
                )
                self._logger.warning(
                    "Loaded active cooldown: %s until %s",
                    self._cooldown_state.reason.value,
                    self._cooldown_state.cooldown_until
                )
        except Exception as e:
            self._logger.warning("Failed to load active cooldown: %s", e)

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


    # =========================================================================
    # Performance Metrics Calculation
    # =========================================================================

    async def calculate_performance_metrics(
        self,
        risk_free_rate: Decimal = Decimal("0.03")
    ) -> PerformanceMetrics:
        """
        Calculate comprehensive performance metrics from trade history.

        This method aggregates all closed trades and calculates:
        - Risk-adjusted returns (Sharpe, Sortino, Calmar)
        - Drawdown metrics
        - Trade quality metrics (profit factor, win rate, expectancy)
        - System Quality Number (SQN)

        Args:
            risk_free_rate: Annual risk-free rate for Sharpe/Sortino (default 3%)

        Returns:
            PerformanceMetrics object with all calculated values
        """
        # Fetch all closed trades
        closed_trades = await self._get_all_closed_trades()
        initial_equity = await self._get_initial_equity()
        current_equity = self._current_equity

        # Return empty metrics if no trades
        if not closed_trades:
            return PerformanceMetrics.empty_metrics(current_equity, initial_equity)

        # Separate winning and losing trades
        winning_trades = [t for t in closed_trades if (t.get("net_pnl") or 0) > 0]
        losing_trades = [t for t in closed_trades if (t.get("net_pnl") or 0) < 0]

        # Calculate basic stats
        total_trades = len(closed_trades)
        winning_count = len(winning_trades)
        losing_count = len(losing_trades)

        win_rate = Decimal(str(winning_count / total_trades)) if total_trades > 0 else Decimal("0")

        # Calculate PnL metrics
        total_pnl = sum(Decimal(str(t.get("net_pnl") or 0)) for t in closed_trades)
        total_fees = sum(Decimal(str(t.get("fees") or 0)) for t in closed_trades)

        total_pnl_pct = Decimal("0")
        if initial_equity > 0:
            total_pnl_pct = (total_pnl / initial_equity) * 100

        # Gross profit and loss
        gross_profit = sum(Decimal(str(t.get("net_pnl") or 0)) for t in winning_trades)
        gross_loss = sum(Decimal(str(t.get("net_pnl") or 0)) for t in losing_trades)

        # Average win/loss
        avg_win = gross_profit / winning_count if winning_count > 0 else Decimal("0")
        avg_loss = gross_loss / losing_count if losing_count > 0 else Decimal("0")

        # Avg win/loss ratio
        avg_win_loss_ratio = None
        if avg_loss != 0:
            avg_win_loss_ratio = abs(avg_win / avg_loss)

        # Largest win and loss
        pnls = [Decimal(str(t.get("net_pnl") or 0)) for t in closed_trades]
        largest_win = max(pnls) if pnls else Decimal("0")
        largest_loss = min(pnls) if pnls else Decimal("0")

        # Average trade duration
        durations = [t.get("duration_seconds") for t in closed_trades if t.get("duration_seconds")]
        avg_duration = int(sum(durations) / len(durations)) if durations else None

        # Calculate daily returns for Sharpe/Sortino
        daily_returns = await self._calculate_daily_returns(closed_trades, initial_equity)

        # Risk-adjusted metrics
        sharpe_ratio = PerformanceMetrics.calculate_sharpe_ratio(daily_returns, risk_free_rate)
        sortino_ratio = PerformanceMetrics.calculate_sortino_ratio(daily_returns, risk_free_rate)

        # Drawdown metrics
        equity_curve = await self._build_equity_curve(closed_trades, initial_equity)
        max_dd_pct, max_dd_abs, current_dd_pct = PerformanceMetrics.calculate_max_drawdown(equity_curve)

        # Calmar ratio (annualized return / max drawdown)
        calmar_ratio = None
        if max_dd_pct > 0 and total_pnl_pct != 0:
            # Estimate annualized return based on trading period
            trading_days = await self._calculate_trading_days(closed_trades)
            if trading_days > 0:
                annual_return_pct = (total_pnl_pct / trading_days) * 365
                calmar_ratio = PerformanceMetrics.calculate_calmar_ratio(annual_return_pct, max_dd_pct)

        # Trade quality metrics
        profit_factor = PerformanceMetrics.calculate_profit_factor(gross_profit, gross_loss)
        expectancy = PerformanceMetrics.calculate_expectancy(avg_win, avg_loss, win_rate)
        sqn = PerformanceMetrics.calculate_sqn(pnls)

        return PerformanceMetrics(
            timestamp=datetime.now(timezone.utc),
            equity=current_equity,
            initial_equity=initial_equity,
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl_pct,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            calmar_ratio=calmar_ratio,
            max_drawdown_pct=max_dd_pct,
            max_drawdown_abs=max_dd_abs,
            current_drawdown_pct=current_dd_pct,
            profit_factor=profit_factor,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            avg_win_loss_ratio=avg_win_loss_ratio,
            expectancy=expectancy,
            sqn=sqn,
            total_trades=total_trades,
            winning_trades=winning_count,
            losing_trades=losing_count,
            total_fees=total_fees,
            avg_trade_duration_seconds=avg_duration,
            largest_win=largest_win,
            largest_loss=largest_loss,
        )

    async def _get_all_closed_trades(self) -> List[Dict]:
        """Get all closed trades from database."""
        if not self.db:
            return []

        try:
            rows = await self.db.fetch(
                """
                SELECT
                    trade_id, symbol, side, size,
                    entry_price, entry_time,
                    exit_price, exit_time,
                    gross_pnl, fees, net_pnl,
                    strategy, duration_seconds, notes
                FROM trades
                WHERE is_closed = true
                ORDER BY exit_time ASC
                """
            )
            return [dict(row) for row in rows]
        except Exception as e:
            self._logger.error("Failed to fetch closed trades: %s", e)
            return []

    async def _get_initial_equity(self) -> Decimal:
        """Get initial equity (earliest live_account record or config)."""
        if not self.db:
            return self._current_equity

        try:
            # Try to get the earliest equity record
            row = await self.db.fetchrow(
                """
                SELECT equity FROM live_account
                ORDER BY updated_at ASC
                LIMIT 1
                """
            )

            if row and row["equity"]:
                return Decimal(str(row["equity"]))

            # Fallback: calculate from current equity minus total PnL
            pnl_row = await self.db.fetchrow(
                """
                SELECT COALESCE(SUM(net_pnl), 0) as total_pnl
                FROM trades
                WHERE is_closed = true
                """
            )

            if pnl_row:
                total_pnl = Decimal(str(pnl_row["total_pnl"]))
                return self._current_equity - total_pnl

            return self._current_equity

        except Exception as e:
            self._logger.warning("Failed to get initial equity: %s", e)
            return self._current_equity

    async def _calculate_daily_returns(
        self,
        trades: List[Dict],
        initial_equity: Decimal
    ) -> List[Decimal]:
        """
        Calculate daily returns from trade history.

        Groups trades by exit date and calculates daily return %.
        """
        if not trades:
            return []

        from collections import defaultdict

        # Group PnL by exit date
        daily_pnl: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

        for trade in trades:
            exit_time = trade.get("exit_time")
            if exit_time:
                date_key = exit_time.date().isoformat()
                daily_pnl[date_key] += Decimal(str(trade.get("net_pnl") or 0))

        if not daily_pnl:
            return []

        # Calculate daily returns as % of running equity
        running_equity = initial_equity
        returns = []

        for date_key in sorted(daily_pnl.keys()):
            pnl = daily_pnl[date_key]
            if running_equity > 0:
                daily_return = pnl / running_equity
                returns.append(daily_return)
                running_equity += pnl

        return returns

    async def _build_equity_curve(
        self,
        trades: List[Dict],
        initial_equity: Decimal
    ) -> List[tuple[datetime, Decimal]]:
        """
        Build equity curve from trade history.

        Returns list of (timestamp, equity) tuples.
        """
        if not trades:
            return [(datetime.now(timezone.utc), initial_equity)]

        curve = [(trades[0].get("entry_time") or datetime.now(timezone.utc), initial_equity)]
        running_equity = initial_equity

        for trade in trades:
            exit_time = trade.get("exit_time")
            net_pnl = Decimal(str(trade.get("net_pnl") or 0))

            if exit_time:
                running_equity += net_pnl
                curve.append((exit_time, running_equity))

        return curve

    async def _calculate_trading_days(self, trades: List[Dict]) -> int:
        """Calculate number of calendar days between first and last trade."""
        if not trades:
            return 0

        first_trade = trades[0]
        last_trade = trades[-1]

        first_time = first_trade.get("entry_time")
        last_time = last_trade.get("exit_time") or last_trade.get("entry_time")

        if not first_time or not last_time:
            return 0

        delta = last_time - first_time
        return max(1, delta.days)


# =============================================================================
# Factory
# =============================================================================

def create_risk_manager(
    bus: Optional[MessageBus] = None,
    db: Optional[Any] = None,
    config: Optional[RiskConfig] = None,
    client: Optional[Any] = None,
    telegram: Optional[Any] = None,
) -> RiskManagerService:
    """Factory function to create RiskManagerService."""
    return RiskManagerService(
        name="risk_manager",
        bus=bus,
        db=db,
        config=config,
        client=client,
        telegram=telegram,
    )
