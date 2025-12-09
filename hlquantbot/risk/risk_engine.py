"""Portfolio-level risk management engine with HFT and temporal risk support.

UPDATED per requisiti 3.1-3.3:
- Nuovi limiti di rischio: 1.2% per trade, 5x leverage default, 8x max portfolio, 65% max exposure
- Circuit breaker 4 livelli con stati RUNNING/PAUSED/STOP_TRADING/HARD_STOP
- Position sizing dinamico con formula obbligatoria: risk_amount = equity × (risk_pct × aggression_factor)
- Fee-awareness obbligatoria: TP_lordo - fee_roundtrip >= 0.20%
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Dict, Optional, Tuple
from enum import Enum

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
from .position_sizer import PositionSizer, HFT_STRATEGIES, MAKER_FEE_PCT
from .circuit_breaker import CircuitBreaker


logger = logging.getLogger(__name__)


# Stati del main loop per il nuovo Circuit Breaker 4 livelli
class TradingState(str, Enum):
    """Stati del trading loop."""
    RUNNING = "running"                # Trading attivo
    PAUSED = "paused"                  # Pausa temporanea (livelli 1-2)
    STOP_TRADING = "stop_trading"      # Stop fino al giorno successivo (livello 3)
    HARD_STOP = "hard_stop"            # Stop definitivo (livello 4)


# Fee roundtrip minimo richiesto per HFT (0.20%)
MIN_NET_PROFIT_AFTER_FEES_PCT = Decimal("0.0020")  # 0.20%


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

    UPDATED per requisiti 3.1-3.3:

    Responsibilities:
    - Validate proposed trades against risk limits (1.2% per trade, 65% max exposure, 8x max leverage)
    - Calculate position sizes con formula dinamica: risk_amount = equity × (risk_pct × aggression_factor)
    - Position sizing dinamico con clamp per max_exposure_per_asset e max_portfolio_leverage
    - Fee-awareness obbligatoria: TP_lordo - fee_roundtrip >= 0.20% (altrimenti reject)
    - Handle strategy allocation
    - Resolve conflicts between strategies
    - Enforce portfolio-level constraints
    - Circuit breaker 4 livelli con stati RUNNING/PAUSED/STOP_TRADING/HARD_STOP
    - HFT profitability validation (fee-aware)
    """

    def __init__(self, settings: Settings, aggression_controller=None):
        self.settings = settings
        self.risk_config = settings.risk

        # Components
        self.position_sizer = PositionSizer(settings)
        self.circuit_breaker = CircuitBreaker(settings)

        # Aggression controller (opzionale, per dynamic risk adjustment)
        self._aggression_controller = aggression_controller

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

        # Trading state (per nuovo Circuit Breaker 4 livelli)
        self._trading_state = TradingState.RUNNING

        logger.info(
            f"RiskEngine initialized - max_risk_per_trade={self.risk_config.max_risk_per_trade_pct:.2%}, "
            f"default_leverage={self.risk_config.default_leverage}x, "
            f"max_portfolio_leverage={self.risk_config.max_portfolio_leverage}x, "
            f"max_exposure_per_asset={self.risk_config.max_exposure_per_asset_pct:.1%}"
        )

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

        UPDATED per requisiti 3.2-3.3:
        - Verifica trading state (RUNNING/PAUSED/STOP_TRADING/HARD_STOP)
        - Fee-awareness obbligatoria: TP_lordo - fee_roundtrip >= 0.20%
        - Position sizing dinamico con aggression_factor

        Args:
            proposals: List of trade proposals from strategies
            account: Current account state
            prices: Current prices for all symbols
            atrs: ATR values for volatility adjustment

        Returns:
            List of approved orders ready for execution
        """
        # 1. Update circuit breaker (includes temporal kill-switch + nuovo 4-livelli)
        can_trade = await self.circuit_breaker.update(account.equity)

        # Determina trading state dal circuit breaker
        self._update_trading_state(account)

        # Se non possiamo fare trading, rigetta tutte le proposte
        if not can_trade or self._trading_state != TradingState.RUNNING:
            state_msg = self._get_state_message()
            logger.warning(f"Trading not allowed - {state_msg}. Rejecting all proposals.")
            return []

        # 2. Check position count limit
        if account.position_count >= self.risk_config.max_open_positions:
            logger.warning(
                f"Max positions ({self.risk_config.max_open_positions}) reached - "
                "rejecting new proposals"
            )
            return []

        if not proposals:
            return []

        atrs = atrs or {}

        # 3. Filter out invalid proposals
        valid_proposals = self._filter_valid_proposals(proposals, prices)

        # 4. Fee-awareness obbligatoria: filtra proposte con TP insufficiente
        fee_valid_proposals = self._filter_fee_aware_proposals(valid_proposals, prices)

        # 5. Resolve conflicts (same symbol, opposite directions)
        resolved_proposals = self._resolve_conflicts(fee_valid_proposals, account)

        # 6. Sort by priority
        sorted_proposals = sorted(
            resolved_proposals,
            key=lambda p: self.strategy_priority.get(p.strategy_id, 0),
            reverse=True,
        )

        # 7. Process each proposal con nuovo position sizing dinamico
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

        logger.info(
            f"Processed {len(proposals)} proposals -> {len(approved_orders)} approved "
            f"(state: {self._trading_state.value})"
        )
        return approved_orders

    def _update_trading_state(self, account: AccountState):
        """
        Aggiorna trading state basandosi sul circuit breaker.

        Nuovo modello 4 livelli:
        - RUNNING: tutto OK, trading attivo
        - PAUSED: livello 1-2 attivo (pausa temporanea)
        - STOP_TRADING: livello 3 attivo (stop fino al giorno successivo)
        - HARD_STOP: livello 4 attivo (stop definitivo, -35% drawdown)
        """
        cb_state = self.circuit_breaker.state

        # Livello 4: Hard stop (-35% total drawdown)
        if cb_state.total_drawdown_pct >= self.risk_config.max_total_drawdown_pct:
            self._trading_state = TradingState.HARD_STOP
            return

        # Livello 3: Stop trading (-10% in 4h)
        if cb_state.temporal_kill_switch_active and cb_state.active_kill_switch_level:
            level_name = cb_state.active_kill_switch_level.value
            if level_name == "level_3":
                self._trading_state = TradingState.STOP_TRADING
                return

        # Livelli 1-2: Pausa temporanea
        if cb_state.temporal_kill_switch_active:
            self._trading_state = TradingState.PAUSED
            return

        # Tutto OK
        self._trading_state = TradingState.RUNNING

    def _get_state_message(self) -> str:
        """Ottieni messaggio descrittivo dello stato attuale."""
        if self._trading_state == TradingState.RUNNING:
            return "Trading active"
        elif self._trading_state == TradingState.PAUSED:
            cooldown = self.circuit_breaker.get_cooldown_remaining()
            return f"Paused (cooldown: {cooldown}s)"
        elif self._trading_state == TradingState.STOP_TRADING:
            return "Stop trading until next day (level 3 triggered)"
        elif self._trading_state == TradingState.HARD_STOP:
            return "Hard stop (level 4: -35% drawdown)"
        return "Unknown state"

    def _filter_fee_aware_proposals(
        self,
        proposals: List[ProposedTrade],
        prices: Dict[str, Decimal],
    ) -> List[ProposedTrade]:
        """
        Filtra proposte con TP insufficiente per coprire le fee.

        Fee-awareness obbligatoria per requisito 3.3:
        TP_lordo - fee_roundtrip >= 0.20%

        Altrimenti il trade è rigettato dal Risk Engine.
        """
        valid = []
        for p in proposals:
            if not p.take_profit_price or p.take_profit_price <= 0:
                logger.warning(f"Proposal {p.symbol} rejected: no TP price")
                continue

            current_price = prices.get(p.symbol)
            if not current_price or current_price <= 0:
                continue

            # Calcola TP lordo in %
            if p.side == Side.LONG:
                tp_gross_pct = (p.take_profit_price - current_price) / current_price
            else:
                tp_gross_pct = (current_price - p.take_profit_price) / current_price

            # Fee roundtrip = 2 × maker_fee (assumiamo maker-only ALO)
            fee_roundtrip_pct = MAKER_FEE_PCT * 2  # 0.0002 * 2 = 0.0004 (0.04%)

            # TP netto dopo fee
            tp_net_pct = tp_gross_pct - fee_roundtrip_pct

            # Verifica soglia minima: 0.20%
            if tp_net_pct < MIN_NET_PROFIT_AFTER_FEES_PCT:
                logger.warning(
                    f"Proposal {p.symbol} rejected by fee-awareness: "
                    f"TP_net={tp_net_pct:.4%} < min_required={MIN_NET_PROFIT_AFTER_FEES_PCT:.2%} "
                    f"(TP_gross={tp_gross_pct:.4%}, fees={fee_roundtrip_pct:.4%})"
                )
                continue

            valid.append(p)

        logger.debug(
            f"Fee-aware filter: {len(proposals)} -> {len(valid)} proposals "
            f"(rejected {len(proposals) - len(valid)} with insufficient TP)"
        )
        return valid

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
        """
        Process a single proposal into an approved order.

        UPDATED per requisito 3.3 - Position sizing dinamico:
        Formula obbligatoria: risk_amount = equity × (risk_per_trade_pct × aggression_factor)
        Con clamp per max_exposure_per_asset e max_portfolio_leverage.
        """
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

            # Same direction - allow pyramid scaling (scale into winning positions)
            # NOTE: Pyramid scaling enabled - allows adding to existing same-direction positions
            # This can increase profits in trending markets but also amplifies risk
            logger.info(
                f"Already have {existing_pos.side.value} position on {proposal.symbol} - "
                f"allowing pyramid scaling for additional entry"
            )
            # Continue processing to allow the trade (removed return None)

        # HFT-specific validation: check profitability after fees (già fatto in _filter_fee_aware_proposals)
        # Ma double-check per sicurezza
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

        # 2. NUOVO: Calcola aggression_factor dall'aggression controller (se disponibile)
        aggression_factor = Decimal("1.0")
        if self._aggression_controller:
            aggression_factor = self._aggression_controller.get_strategy_risk_multiplier(proposal.strategy_id)
            logger.debug(f"Using aggression_factor={aggression_factor} for {proposal.strategy_id.value}")

        # 3. NUOVO: Position sizing dinamico con formula obbligatoria
        # risk_amount = equity × (risk_per_trade_pct × aggression_factor)
        base_risk_pct = self.risk_config.max_risk_per_trade_pct
        adjusted_risk_pct = base_risk_pct * aggression_factor
        risk_amount = account.equity * adjusted_risk_pct

        # 4. Calcola SL percentage
        if proposal.stop_loss_price and proposal.stop_loss_price > 0:
            sl_pct = abs(current_price - proposal.stop_loss_price) / current_price
        else:
            # Default 0.5% SL per HFT (max_sl_pct dalla config)
            trade_params = getattr(self.settings, 'trade_params', None)
            if trade_params:
                sl_pct = trade_params.max_sl_pct
            else:
                sl_pct = Decimal("0.005")  # 0.5%

        # 5. NUOVO: Calcola notional con formula: notional = risk_amount / SL_pct
        if sl_pct > 0:
            notional = risk_amount / sl_pct
        else:
            logger.warning(f"SL_pct is 0 for {proposal.symbol}, using fallback sizing")
            notional = account.equity * Decimal("0.02")  # Fallback: 2% of equity

        # 6. CLAMP: Applica max_exposure_per_asset
        max_exposure = account.equity * self.risk_config.max_exposure_per_asset_pct
        asset_exposure = self._get_asset_exposure(proposal.symbol, account)
        available_exposure = max_exposure - asset_exposure

        if available_exposure <= 0:
            raise RiskLimitExceededError(
                f"Max exposure for {proposal.symbol} reached",
                limit_type="asset_concentration",
                current_value=float(asset_exposure),
                limit_value=float(max_exposure),
            )

        # Clamp notional to available exposure
        notional = min(notional, available_exposure)

        # 7. CLAMP: Applica max_portfolio_leverage
        # Calcola leverage implicito
        portfolio_notional = account.total_position_value + notional
        if account.equity > 0:
            implied_leverage = portfolio_notional / account.equity
        else:
            implied_leverage = Decimal(0)

        if implied_leverage > self.risk_config.max_portfolio_leverage:
            # Riduci notional per rispettare max portfolio leverage
            max_additional_notional = (
                account.equity * self.risk_config.max_portfolio_leverage - account.total_position_value
            )
            if max_additional_notional <= 0:
                raise RiskLimitExceededError(
                    "Would exceed max portfolio leverage",
                    limit_type="portfolio_leverage",
                    current_value=float(account.current_leverage),
                    limit_value=float(self.risk_config.max_portfolio_leverage),
                )
            notional = min(notional, max_additional_notional)

        # 8. Calcola size da notional
        size = notional / current_price

        # 9. Adjust for correlation
        size = self.position_sizer.adjust_for_correlation(
            size,
            proposal,
            account,
            self._get_correlations_for_symbol(proposal.symbol),
        )

        # 10. Apply confidence scaling
        size *= proposal.confidence

        # 11. Round to symbol decimals
        symbol_config = self.settings.symbols.get(proposal.symbol)
        if symbol_config:
            from decimal import ROUND_DOWN
            decimals = symbol_config.size_decimals
            quantizer = Decimal(10) ** -decimals
            size = size.quantize(quantizer, rounding=ROUND_DOWN)

            # Check minimum size
            if size < symbol_config.min_size:
                logger.warning(
                    f"Calculated size {size} below minimum {symbol_config.min_size} for {proposal.symbol}"
                )
                return None

        if size <= 0:
            logger.warning(f"Calculated size is 0 for {proposal.symbol}")
            return None

        # 12. Calculate leverage used
        final_notional = size * current_price
        if account.equity > 0:
            leverage = final_notional / account.equity
        else:
            leverage = self.risk_config.default_leverage

        # Cap leverage at configured limits
        max_leverage = self.risk_config.default_leverage
        if is_hft:
            hft_config = self.position_sizer.get_hft_config(proposal.strategy_id)
            if hft_config:
                max_leverage = getattr(hft_config, 'default_leverage', max_leverage)

        leverage = min(leverage, max_leverage)

        # 13. Calculate final risk amount based on actual size
        if proposal.stop_loss_price:
            stop_distance = abs(current_price - proposal.stop_loss_price)
            final_risk_amount = size * stop_distance
        else:
            final_risk_amount = size * current_price * sl_pct

        # 14. Create approved order
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
            risk_amount=final_risk_amount,
        )

        logger.info(
            f"Approved: {order.side.value} {order.size} {order.symbol} @ {current_price} | "
            f"leverage={leverage:.1f}x, risk=${final_risk_amount:.2f} ({adjusted_risk_pct:.2%}), "
            f"aggression={aggression_factor}, notional=${final_notional:.2f}"
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
    def get_trading_state(self) -> TradingState:
        """Get current trading state."""
        return self._trading_state

    def can_trade(self) -> bool:
        """Check if trading is allowed based on current state."""
        return self._trading_state == TradingState.RUNNING

    def get_portfolio_metrics(self, account: AccountState) -> Dict:
        """
        Get current portfolio risk metrics.

        UPDATED per requisito 3.1-3.2: include nuovi limiti e trading state.
        """
        return {
            "equity": float(account.equity),
            "total_position_value": float(account.total_position_value),
            "current_leverage": float(account.current_leverage),
            "max_leverage": float(self.risk_config.max_portfolio_leverage),
            "default_leverage": float(self.risk_config.default_leverage),
            "max_risk_per_trade_pct": float(self.risk_config.max_risk_per_trade_pct),
            "max_exposure_per_asset_pct": float(self.risk_config.max_exposure_per_asset_pct),
            "position_count": account.position_count,
            "max_open_positions": self.risk_config.max_open_positions,
            "unrealized_pnl": float(account.total_unrealized_pnl),
            "daily_pnl_pct": float(account.daily_pnl_pct),
            "circuit_breaker": self.circuit_breaker.get_risk_metrics(),
            "trading_state": self._trading_state.value,
            "trading_state_message": self._get_state_message(),
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

        UPDATED per requisito 3.3: include fee-awareness check (0.20% min net profit).

        Returns (is_valid, reason) tuple.
        """
        if not self.is_hft_strategy(proposal.strategy_id):
            return True, "Not HFT strategy"

        # Check profitability after fees (0.20% min)
        if not proposal.take_profit_price or proposal.take_profit_price <= 0:
            return False, "No TP price"

        # Calcola TP netto
        if proposal.side == Side.LONG:
            tp_gross_pct = (proposal.take_profit_price - current_price) / current_price
        else:
            tp_gross_pct = (current_price - proposal.take_profit_price) / current_price

        fee_roundtrip_pct = MAKER_FEE_PCT * 2
        tp_net_pct = tp_gross_pct - fee_roundtrip_pct

        if tp_net_pct < MIN_NET_PROFIT_AFTER_FEES_PCT:
            return False, f"TP_net {tp_net_pct:.4%} < 0.20% (after fees)"

        # Check strategy is enabled
        strategy_config = self.settings.get_strategy_config(proposal.strategy_id)
        if not strategy_config.enabled:
            return False, f"Strategy {proposal.strategy_id.value} disabled"

        return True, "Valid"

    def set_aggression_controller(self, aggression_controller):
        """Set aggression controller for dynamic risk adjustment."""
        self._aggression_controller = aggression_controller
        logger.info("Aggression controller connected to RiskEngine")
