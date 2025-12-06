"""Portfolio-level risk management engine with HFT and temporal risk support."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Dict, Optional, Tuple

from ..core.models import (
    ProposedTrade,
    ApprovedOrder,
    AccountState,
    Position,
    RiskLimits,
)
from ..core.enums import Side, OrderType, OrderStatus, StrategyId, AlertSeverity
from ..core.exceptions import RiskLimitExceededError
from ..config.settings import Settings
from .position_sizer import PositionSizer, HFT_STRATEGIES
from .circuit_breaker import CircuitBreaker


logger = logging.getLogger(__name__)


# Strategy priority mapping (higher = more priority)
# HFT strategies have higher priority for faster execution
STRATEGY_PRIORITY = {
    # Legacy strategies (low priority, will be removed)
    StrategyId.FUNDING_BIAS: 1,
    StrategyId.LIQUIDATION_CLUSTER: 2,
    StrategyId.VOLATILITY_EXPANSION: 2,
    # HFT strategies (high priority)
    StrategyId.MMR_HFT: 10,
    StrategyId.MICRO_BREAKOUT: 9,
    StrategyId.PAIR_TRADING: 8,
    StrategyId.LIQUIDATION_SNIPING: 10,  # Very time sensitive
}


class RiskEngine:
    """
    Portfolio-level risk management engine with HFT support.

    Responsibilities:
    - Validate proposed trades against risk limits
    - Calculate position sizes (standard and HFT fee-aware)
    - Handle strategy allocation
    - Resolve conflicts between strategies
    - Enforce portfolio-level constraints
    - Temporal kill-switch management (soft cooldowns)
    - HFT profitability validation (fee-aware)
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.risk_config = settings.risk

        # Components
        self.position_sizer = PositionSizer(settings)
        self.circuit_breaker = CircuitBreaker(settings)

        # Use global priority mapping
        self.strategy_priority = STRATEGY_PRIORITY

        # Correlation matrix (simplified - in production would be calculated dynamically)
        self.correlations = {
            ("BTC", "ETH"): Decimal("0.85"),
            ("BTC", "SOL"): Decimal("0.75"),
            ("ETH", "SOL"): Decimal("0.80"),
        }

        # HFT tracking
        self._hft_trade_count = 0
        self._last_hft_reset = datetime.now(timezone.utc)

    async def initialize(self, account: AccountState):
        """Initialize risk engine with current account state."""
        await self.circuit_breaker.initialize(account.equity)

    async def process_proposals(
        self,
        proposals: List[ProposedTrade],
        account: AccountState,
        prices: Dict[str, Decimal],
        atrs: Optional[Dict[str, Decimal]] = None,
    ) -> List[ApprovedOrder]:
        """
        Process proposed trades and return approved orders.

        Args:
            proposals: List of trade proposals from strategies
            account: Current account state
            prices: Current prices for all symbols
            atrs: ATR values for volatility adjustment

        Returns:
            List of approved orders ready for execution
        """
        # Update circuit breaker (includes temporal kill-switch)
        can_trade = await self.circuit_breaker.update(account.equity)
        if not can_trade:
            # Check if it's a temporal cooldown vs hard circuit breaker
            if self.circuit_breaker.is_temporal_cooldown():
                cooldown_remaining = self.circuit_breaker.get_temporal_cooldown_remaining()
                logger.warning(
                    f"Temporal kill-switch active - cooldown {cooldown_remaining}s remaining. "
                    "Rejecting new proposals."
                )
            else:
                logger.warning("Hard circuit breaker triggered - rejecting all proposals")
            return []

        # Check position count limit
        if account.position_count >= self.risk_config.max_open_positions:
            logger.warning(
                f"Max positions ({self.risk_config.max_open_positions}) reached - "
                "rejecting new proposals"
            )
            return []

        if not proposals:
            return []

        atrs = atrs or {}

        # 1. Filter out invalid proposals
        valid_proposals = self._filter_valid_proposals(proposals, prices)

        # 2. Resolve conflicts (same symbol, opposite directions)
        resolved_proposals = self._resolve_conflicts(valid_proposals, account)

        # 3. Sort by priority
        sorted_proposals = sorted(
            resolved_proposals,
            key=lambda p: self.strategy_priority.get(p.strategy_id, 0),
            reverse=True,
        )

        # 4. Process each proposal
        approved_orders = []
        simulated_account = self._clone_account(account)

        for proposal in sorted_proposals:
            try:
                order = await self._process_single_proposal(
                    proposal,
                    simulated_account,
                    prices,
                    atrs,
                )
                if order:
                    approved_orders.append(order)
                    # Update simulated account
                    self._update_simulated_account(simulated_account, order, prices)

            except RiskLimitExceededError as e:
                logger.warning(f"Proposal rejected - {e.limit_type}: {e}")
            except Exception as e:
                logger.error(f"Error processing proposal: {e}")

        logger.info(f"Processed {len(proposals)} proposals -> {len(approved_orders)} approved")
        return approved_orders

    def _filter_valid_proposals(
        self,
        proposals: List[ProposedTrade],
        prices: Dict[str, Decimal],
    ) -> List[ProposedTrade]:
        """Filter out invalid proposals."""
        valid = []
        for p in proposals:
            # Check symbol has price
            if p.symbol not in prices:
                logger.warning(f"No price for {p.symbol}, skipping")
                continue

            # Check side is actionable
            if p.side == Side.FLAT:
                continue

            # Check strategy is enabled
            strategy_config = self.settings.get_strategy_config(p.strategy_id)
            if not strategy_config.enabled:
                continue

            # Check symbol is in strategy's allowed symbols
            if p.symbol not in strategy_config.symbols:
                continue

            valid.append(p)

        return valid

    def _resolve_conflicts(
        self,
        proposals: List[ProposedTrade],
        account: AccountState,
    ) -> List[ProposedTrade]:
        """
        Resolve conflicts between proposals.

        If two strategies propose opposite directions on same symbol,
        higher priority wins.
        """
        # Group by symbol
        by_symbol: Dict[str, List[ProposedTrade]] = {}
        for p in proposals:
            if p.symbol not in by_symbol:
                by_symbol[p.symbol] = []
            by_symbol[p.symbol].append(p)

        resolved = []
        for symbol, symbol_proposals in by_symbol.items():
            if len(symbol_proposals) == 1:
                resolved.append(symbol_proposals[0])
                continue

            # Check for conflicts
            longs = [p for p in symbol_proposals if p.side == Side.LONG]
            shorts = [p for p in symbol_proposals if p.side == Side.SHORT]

            if longs and shorts:
                # Conflict! Pick highest priority
                all_proposals = longs + shorts
                all_proposals.sort(
                    key=lambda p: (
                        self.strategy_priority.get(p.strategy_id, 0),
                        p.confidence,
                    ),
                    reverse=True,
                )
                winner = all_proposals[0]
                logger.info(
                    f"Conflict on {symbol}: {winner.strategy_id.value} "
                    f"({winner.side.value}) wins over others"
                )
                resolved.append(winner)
            else:
                # No conflict, but might have multiple same-direction
                # Take highest confidence
                symbol_proposals.sort(key=lambda p: p.confidence, reverse=True)
                resolved.append(symbol_proposals[0])

        return resolved

    async def _process_single_proposal(
        self,
        proposal: ProposedTrade,
        account: AccountState,
        prices: Dict[str, Decimal],
        atrs: Dict[str, Decimal],
    ) -> Optional[ApprovedOrder]:
        """Process a single proposal into an approved order."""
        current_price = prices[proposal.symbol]
        atr = atrs.get(proposal.symbol)
        is_hft = self.position_sizer.is_hft_strategy(proposal.strategy_id)

        # Check existing position
        existing_pos = account.get_position(proposal.symbol)

        # If we have a position in opposite direction, this is a close or flip
        if existing_pos:
            if existing_pos.side != proposal.side:
                # Need to close first (or flip)
                return self._create_close_order(existing_pos, proposal)

            # Same direction - check if we should add
            # For now, don't add to existing positions
            logger.info(f"Already have {existing_pos.side.value} position on {proposal.symbol}")
            return None

        # HFT-specific validation: check profitability after fees
        if is_hft:
            if not self.position_sizer.validate_hft_profitability(proposal, current_price):
                logger.warning(
                    f"HFT trade {proposal.symbol} rejected: TP too small to cover fees"
                )
                return None

        # 1. Check strategy allocation
        remaining = self.position_sizer.get_remaining_allocation(
            proposal.strategy_id,
            account,
        )
        if remaining <= 0:
            raise RiskLimitExceededError(
                f"Strategy {proposal.strategy_id.value} at max allocation",
                limit_type="strategy_allocation",
                current_value=float(account.equity - remaining),
                limit_value=float(account.equity),
            )

        # 2. Calculate position size (use HFT sizing for HFT strategies)
        if is_hft:
            size = self.position_sizer.calculate_hft_size(
                proposal,
                account,
                current_price,
            )
        else:
            size = self.position_sizer.calculate_size(
                proposal,
                account,
                current_price,
                atr=atr,
            )

        if size <= 0:
            logger.warning(f"Calculated size is 0 for {proposal.symbol}")
            return None

        # 3. Adjust for correlation
        size = self.position_sizer.adjust_for_correlation(
            size,
            proposal,
            account,
            self._get_correlations_for_symbol(proposal.symbol),
        )

        # 4. Check portfolio leverage
        if not self.position_sizer.check_portfolio_leverage(size, current_price, account):
            raise RiskLimitExceededError(
                "Would exceed max portfolio leverage",
                limit_type="portfolio_leverage",
                current_value=float(account.current_leverage),
                limit_value=float(self.risk_config.max_portfolio_leverage),
            )

        # 5. Check asset concentration
        asset_exposure = self._get_asset_exposure(proposal.symbol, account)
        new_exposure = asset_exposure + (size * current_price)
        max_exposure = account.equity * self.risk_config.max_exposure_per_asset_pct

        if new_exposure > max_exposure:
            # Reduce size to fit
            available_exposure = max_exposure - asset_exposure
            if available_exposure <= 0:
                raise RiskLimitExceededError(
                    f"Max exposure for {proposal.symbol} reached",
                    limit_type="asset_concentration",
                    current_value=float(asset_exposure),
                    limit_value=float(max_exposure),
                )
            size = available_exposure / current_price

        # 6. Calculate leverage used (use HFT leverage for HFT strategies)
        if is_hft:
            leverage = self.position_sizer.get_hft_leverage(proposal.strategy_id)
            max_leverage = self.position_sizer.get_max_hft_leverage(proposal.strategy_id)
            leverage = min(leverage, max_leverage)
        else:
            leverage = self.position_sizer.calculate_leverage(
                size, current_price, account, proposal.symbol
            )

        # 7. Calculate risk amount
        if proposal.stop_loss_price:
            stop_distance = abs(current_price - proposal.stop_loss_price)
            risk_amount = size * stop_distance
        else:
            # Estimate 2% as default stop
            risk_amount = size * current_price * Decimal("0.02")

        # 8. Create approved order
        order = ApprovedOrder(
            order_id=str(uuid.uuid4())[:8],
            strategy_id=proposal.strategy_id,
            symbol=proposal.symbol,
            side=proposal.side,
            size=size,
            order_type=proposal.entry_type,
            price=proposal.entry_price,
            stop_loss_price=proposal.stop_loss_price,
            take_profit_price=proposal.take_profit_price,
            status=OrderStatus.PENDING,
            leverage_used=leverage,
            risk_amount=risk_amount,
        )

        logger.info(
            f"Approved: {order.side.value} {order.size} {order.symbol} "
            f"@ {current_price} (leverage: {leverage:.1f}x, risk: ${risk_amount:.2f})"
        )

        return order

    def _create_close_order(
        self,
        position: Position,
        proposal: ProposedTrade,
    ) -> ApprovedOrder:
        """Create an order to close an existing position."""
        # Close in opposite direction
        close_side = Side.SHORT if position.side == Side.LONG else Side.LONG

        return ApprovedOrder(
            order_id=str(uuid.uuid4())[:8],
            strategy_id=proposal.strategy_id,
            symbol=position.symbol,
            side=close_side,
            size=position.size,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )

    def _get_correlations_for_symbol(self, symbol: str) -> Dict[str, Decimal]:
        """Get correlations for a symbol with other assets."""
        result = {}
        for (s1, s2), corr in self.correlations.items():
            if s1 == symbol:
                result[s2] = corr
            elif s2 == symbol:
                result[s1] = corr
        return result

    def _get_asset_exposure(self, symbol: str, account: AccountState) -> Decimal:
        """Get total exposure to an asset."""
        exposure = Decimal(0)
        for pos in account.positions:
            if pos.symbol == symbol:
                exposure += pos.notional_value
        return exposure

    def _clone_account(self, account: AccountState) -> AccountState:
        """Create a copy of account state for simulation."""
        return AccountState(
            timestamp=account.timestamp,
            equity=account.equity,
            available_balance=account.available_balance,
            total_margin_used=account.total_margin_used,
            positions=account.positions.copy(),
            total_unrealized_pnl=account.total_unrealized_pnl,
            daily_pnl=account.daily_pnl,
            daily_pnl_pct=account.daily_pnl_pct,
            total_position_value=account.total_position_value,
            current_leverage=account.current_leverage,
        )

    def _update_simulated_account(
        self,
        account: AccountState,
        order: ApprovedOrder,
        prices: Dict[str, Decimal],
    ):
        """Update simulated account after approving an order."""
        price = prices.get(order.symbol, Decimal(0))
        notional = order.size * price

        account.total_position_value += notional
        if account.equity > 0:
            account.current_leverage = account.total_position_value / account.equity

        # Add simulated position
        account.positions.append(Position(
            symbol=order.symbol,
            side=order.side,
            size=order.size,
            entry_price=price,
            current_price=price,
            strategy_id=order.strategy_id,
        ))

    # -------------------------------------------------------------------------
    # Utility Methods
    # -------------------------------------------------------------------------
    def get_portfolio_metrics(self, account: AccountState) -> Dict:
        """Get current portfolio risk metrics."""
        return {
            "equity": float(account.equity),
            "total_position_value": float(account.total_position_value),
            "current_leverage": float(account.current_leverage),
            "max_leverage": float(self.risk_config.max_portfolio_leverage),
            "position_count": account.position_count,
            "unrealized_pnl": float(account.total_unrealized_pnl),
            "daily_pnl_pct": float(account.daily_pnl_pct),
            "circuit_breaker": self.circuit_breaker.get_risk_metrics(),
        }

    def get_strategy_exposure(self, account: AccountState) -> Dict[str, float]:
        """Get exposure per strategy."""
        exposure = {}
        for strategy_id in StrategyId:
            strategy_exposure = Decimal(0)
            for pos in account.positions:
                if pos.strategy_id == strategy_id:
                    strategy_exposure += pos.notional_value
            exposure[strategy_id.value] = float(strategy_exposure)
        return exposure

    async def close_all_positions(self, account: AccountState) -> List[ApprovedOrder]:
        """Generate orders to close all positions (emergency)."""
        orders = []
        for pos in account.positions:
            close_side = Side.SHORT if pos.side == Side.LONG else Side.LONG
            orders.append(ApprovedOrder(
                order_id=str(uuid.uuid4())[:8],
                strategy_id=pos.strategy_id or StrategyId.FUNDING_BIAS,
                symbol=pos.symbol,
                side=close_side,
                size=pos.size,
                order_type=OrderType.MARKET,
                status=OrderStatus.PENDING,
            ))
        return orders

    # -------------------------------------------------------------------------
    # Temporal Risk Management
    # -------------------------------------------------------------------------
    def is_in_temporal_cooldown(self) -> bool:
        """Check if temporal kill-switch is active."""
        return self.circuit_breaker.is_temporal_cooldown()

    def get_temporal_status(self) -> Dict:
        """Get detailed temporal risk status."""
        return self.circuit_breaker.get_temporal_status()

    def force_reset_temporal_killswitch(self, current_equity: Decimal):
        """Force reset temporal kill-switch (manual intervention)."""
        self.circuit_breaker.force_reset_temporal(current_equity)
        logger.warning("Temporal kill-switch manually reset")

    # -------------------------------------------------------------------------
    # HFT Specific Methods
    # -------------------------------------------------------------------------
    def is_hft_strategy(self, strategy_id: StrategyId) -> bool:
        """Check if strategy is HFT type."""
        return strategy_id in HFT_STRATEGIES

    def get_hft_trade_count(self) -> int:
        """Get HFT trade count since last reset."""
        return self._hft_trade_count

    def increment_hft_trade_count(self):
        """Increment HFT trade counter."""
        self._hft_trade_count += 1

    def reset_hft_trade_count(self):
        """Reset HFT trade counter (called daily)."""
        self._hft_trade_count = 0
        self._last_hft_reset = datetime.now(timezone.utc)

    def get_hft_metrics(self) -> Dict:
        """Get HFT-specific metrics."""
        return {
            "hft_trade_count": self._hft_trade_count,
            "last_hft_reset": self._last_hft_reset.isoformat(),
            "hft_strategies_enabled": [
                s.value for s in HFT_STRATEGIES
                if self.settings.get_strategy_config(s).enabled
            ],
        }

    def validate_hft_proposal(
        self,
        proposal: ProposedTrade,
        current_price: Decimal,
    ) -> Tuple[bool, str]:
        """
        Validate HFT proposal before processing.

        Returns (is_valid, reason) tuple.
        """
        if not self.is_hft_strategy(proposal.strategy_id):
            return True, "Not HFT strategy"

        # Check profitability
        if not self.position_sizer.validate_hft_profitability(proposal, current_price):
            return False, "TP too small to cover fees"

        # Check strategy is enabled
        strategy_config = self.settings.get_strategy_config(proposal.strategy_id)
        if not strategy_config.enabled:
            return False, f"Strategy {proposal.strategy_id.value} disabled"

        return True, "Valid"
