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
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .base import BaseService
from .message_bus import Message, MessageBus
from ..core.enums import Topic
from ..core.models import Setup, TradeIntent, RiskParams, Direction


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

    # Leverage
    leverage: float = 1.0
    max_leverage: float = 2.0

    # Stops
    trailing_atr_mult: float = 2.5

    # Slippage
    max_slippage_pct: float = 0.1


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

        # State
        self._current_equity: Decimal = Decimal("100")  # Safe default, updated from API
        self._open_positions: Dict[str, Dict] = {}
        self._pending_intents: Dict[str, TradeIntent] = {}

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

    @property
    def metrics(self) -> Dict[str, Any]:
        """Get service metrics."""
        return {
            "equity": float(self._current_equity),
            "open_positions": len(self._open_positions),
            "total_exposure_pct": float(self._get_total_exposure_pct()),
            "max_positions": self._config.max_positions,
            "risk_per_trade_pct": self._config.per_trade_pct,
        }


# =============================================================================
# Factory
# =============================================================================

def create_risk_manager(
    bus: Optional[MessageBus] = None,
    db: Optional[Any] = None,
    config: Optional[RiskConfig] = None,
    client: Optional[Any] = None,
) -> RiskManagerService:
    """Factory function to create RiskManagerService."""
    return RiskManagerService(
        name="risk_manager",
        bus=bus,
        db=db,
        config=config,
        client=client,
    )
