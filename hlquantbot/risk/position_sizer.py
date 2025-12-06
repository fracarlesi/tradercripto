"""Position sizing calculations with HFT fee-aware logic."""

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict

from ..core.models import ProposedTrade, AccountState, Position
from ..core.enums import Side, StrategyId
from ..config.settings import Settings, SymbolConfig


logger = logging.getLogger(__name__)


# Hyperliquid fee structure
MAKER_FEE_PCT = Decimal("0.0002")  # 0.02%
TAKER_FEE_PCT = Decimal("0.0005")  # 0.05%

# HFT strategies use different sizing logic
HFT_STRATEGIES = {
    StrategyId.MMR_HFT,
    StrategyId.MICRO_BREAKOUT,
    StrategyId.PAIR_TRADING,
    StrategyId.LIQUIDATION_SNIPING,
    StrategyId.MOMENTUM_SCALPING,
}


class PositionSizer:
    """
    Calculate position sizes based on risk parameters.

    Supports:
    - Fixed fractional risk per trade (0.7% for HFT)
    - Volatility-adjusted sizing (ATR-based)
    - Kelly criterion (optional)
    - Strategy allocation limits
    - HFT fee-aware sizing (maker/taker consideration)
    - Per-strategy leverage limits
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.risk_config = settings.risk

    def is_hft_strategy(self, strategy_id: StrategyId) -> bool:
        """Check if strategy is HFT type."""
        return strategy_id in HFT_STRATEGIES

    def get_hft_config(self, strategy_id: StrategyId) -> Optional[Dict]:
        """Get HFT-specific config for a strategy."""
        if not self.is_hft_strategy(strategy_id):
            return None

        hft_config = getattr(self.settings.strategies, 'hft', None)
        if not hft_config:
            return None

        strategy_map = {
            StrategyId.MMR_HFT: 'mmr_hft',
            StrategyId.MICRO_BREAKOUT: 'micro_breakout',
            StrategyId.PAIR_TRADING: 'pair_trading',
            StrategyId.LIQUIDATION_SNIPING: 'liquidation_sniping',
            StrategyId.MOMENTUM_SCALPING: 'momentum_scalping',
        }

        config_name = strategy_map.get(strategy_id)
        if config_name:
            return getattr(hft_config, config_name, None)
        return None

    def calculate_size(
        self,
        trade: ProposedTrade,
        account: AccountState,
        current_price: Decimal,
        atr: Optional[Decimal] = None,
        win_rate: Optional[Decimal] = None,
        avg_rr: Optional[Decimal] = None,
    ) -> Decimal:
        """
        Calculate position size in asset units.

        Args:
            trade: The proposed trade
            account: Current account state
            current_price: Current price of the asset
            atr: Average True Range (for volatility adjustment)
            win_rate: Historical win rate (for Kelly)
            avg_rr: Average risk/reward ratio (for Kelly)

        Returns:
            Size in asset units
        """
        equity = account.equity

        # 1. Get base allocation for this strategy
        strategy_allocation = self._get_strategy_allocation(trade.strategy_id)
        strategy_equity = equity * strategy_allocation

        # 2. Calculate max risk amount for this trade
        max_risk_per_trade = equity * self.risk_config.max_risk_per_trade_pct
        risk_amount = min(trade.risk_per_trade, max_risk_per_trade)

        # 3. Calculate size based on stop loss distance
        if trade.stop_loss_price and trade.stop_loss_price > 0:
            stop_distance = abs(current_price - trade.stop_loss_price)
            if stop_distance > 0:
                size_from_risk = risk_amount / stop_distance
            else:
                size_from_risk = Decimal(0)
        elif atr and atr > 0:
            # Use ATR as proxy for stop distance
            stop_distance = atr * Decimal("1.5")
            size_from_risk = risk_amount / stop_distance
        else:
            # Fallback: use 2% of price as stop distance
            stop_distance = current_price * Decimal("0.02")
            size_from_risk = risk_amount / stop_distance

        # 4. Calculate size from notional limit
        symbol_config = self.settings.symbols.get(trade.symbol)
        max_position_pct = Decimal("0.5")  # Default 50%
        if symbol_config:
            max_position_pct = symbol_config.max_position_pct

        max_notional = equity * max_position_pct
        size_from_notional = max_notional / current_price

        # 5. Calculate size from strategy allocation
        size_from_strategy = strategy_equity / current_price

        # 6. Apply Kelly criterion if data available
        kelly_fraction = Decimal("1.0")
        if win_rate and avg_rr and win_rate > 0:
            kelly_fraction = self._calculate_kelly(win_rate, avg_rr)
            kelly_fraction = min(kelly_fraction, Decimal("0.5"))  # Cap at 50%
            kelly_fraction = max(kelly_fraction, Decimal("0.1"))  # Floor at 10%

        # 7. Apply volatility adjustment if ATR available
        vol_adjustment = Decimal("1.0")
        if atr and current_price > 0:
            vol_pct = atr / current_price
            # Reduce size in high volatility
            if vol_pct > Decimal("0.03"):  # > 3% ATR
                vol_adjustment = Decimal("0.03") / vol_pct

        # 8. Take minimum of all constraints
        raw_size = min(
            size_from_risk,
            size_from_notional,
            size_from_strategy,
        )

        # Apply adjustments
        adjusted_size = raw_size * kelly_fraction * vol_adjustment

        # Apply confidence scaling
        adjusted_size *= trade.confidence

        # 9. Round to symbol decimals
        if symbol_config:
            decimals = symbol_config.size_decimals
            quantizer = Decimal(10) ** -decimals
            adjusted_size = adjusted_size.quantize(quantizer, rounding=ROUND_DOWN)

        # 10. Ensure minimum size
        min_size = Decimal("0.0001")
        if symbol_config:
            min_size = symbol_config.min_size

        if adjusted_size < min_size:
            logger.warning(
                f"Calculated size {adjusted_size} below minimum {min_size} for {trade.symbol}"
            )
            return Decimal(0)

        return adjusted_size

    def calculate_hft_size(
        self,
        trade: ProposedTrade,
        account: AccountState,
        current_price: Decimal,
        aggression_factor: Decimal = Decimal("1.0"),
    ) -> Decimal:
        """
        Calculate position size for HFT strategies with fee-aware logic.

        HFT sizing with aggression factor for dynamic risk adjustment:
        - Uses max_position_pct from strategy config
        - Applies leverage from strategy config, adjusted by aggression
        - Uses SL-based sizing for proper risk management
        - Ensures profitability after fees

        Args:
            trade: The proposed trade
            account: Current account state
            current_price: Current price of the asset
            aggression_factor: Risk multiplier from aggression controller (0.5-2.0)
        """
        hft_config = self.get_hft_config(trade.strategy_id)
        if not hft_config:
            # Fallback to standard sizing
            return self.calculate_size(trade, account, current_price)

        equity = account.equity

        # Get HFT-specific limits
        max_position_pct = getattr(hft_config, 'max_position_pct', Decimal("0.01"))
        default_leverage = getattr(hft_config, 'default_leverage', Decimal("10"))
        sl_pct = getattr(hft_config, 'stop_loss_pct', Decimal("0.002"))

        # Get trade_params config for SL limits
        trade_params = getattr(self.settings, 'trade_params', None)
        if trade_params:
            # Use trade's SL pct if provided, otherwise calculate from price or use strategy default
            trade_sl_pct = getattr(trade, 'stop_loss_pct', None)
            if trade_sl_pct:
                sl_pct = min(trade_sl_pct, trade_params.max_sl_pct)
            elif trade.stop_loss_price and trade.entry_price and trade.entry_price > 0:
                # Calculate SL pct from price
                trade_sl_pct = abs(trade.entry_price - trade.stop_loss_price) / trade.entry_price
                sl_pct = min(trade_sl_pct, trade_params.max_sl_pct)
            else:
                sl_pct = min(sl_pct, trade_params.max_sl_pct)

        # Apply aggression to risk parameters
        base_risk_pct = self.risk_config.max_risk_per_trade_pct
        adjusted_risk_pct = base_risk_pct * aggression_factor
        risk_amount = equity * adjusted_risk_pct

        # SL-based sizing: notional = risk_amount / sl_pct
        if sl_pct > 0:
            notional_from_sl = risk_amount / sl_pct
        else:
            notional_from_sl = equity * max_position_pct * default_leverage

        # Max exposure per asset
        max_exposure = equity * self.risk_config.max_exposure_per_asset_pct
        notional = min(notional_from_sl, max_exposure)

        # Apply leverage limits with aggression adjustment
        effective_leverage = default_leverage * aggression_factor
        max_leverage = min(
            self.risk_config.max_portfolio_leverage,
            getattr(hft_config, 'max_leverage', Decimal("25"))
        )
        effective_leverage = min(effective_leverage, max_leverage)

        # Cap notional by leveraged equity
        max_notional = equity * max_position_pct * effective_leverage
        notional = min(notional, max_notional)

        # Calculate size
        size = notional / current_price

        # Apply trading mode size multiplier if configured
        trading_mode = getattr(self.settings, 'trading_mode', None)
        if trading_mode:
            size *= trading_mode.size_multiplier

        # Apply confidence scaling
        size *= trade.confidence

        # Round to symbol decimals
        symbol_config = self.settings.symbols.get(trade.symbol)
        if symbol_config:
            decimals = symbol_config.size_decimals
            quantizer = Decimal(10) ** -decimals
            size = size.quantize(quantizer, rounding=ROUND_DOWN)

        # Ensure minimum size
        min_size = Decimal("0.0001")
        if symbol_config:
            min_size = symbol_config.min_size

        if size < min_size:
            logger.warning(
                f"HFT size {size} below minimum {min_size} for {trade.symbol}"
            )
            return Decimal(0)

        logger.debug(
            f"HFT sizing: {trade.symbol} risk={adjusted_risk_pct:.3%} "
            f"sl={sl_pct:.3%} leverage={effective_leverage:.1f}x "
            f"aggression={aggression_factor} size={size}"
        )

        return size

    def calculate_net_profit_after_fees(
        self,
        entry_price: Decimal,
        exit_price: Decimal,
        size: Decimal,
        side: Side,
        use_maker: bool = True,
    ) -> Decimal:
        """
        Calculate net profit after fees.

        CRITICAL for HFT: Must use maker orders to be profitable.
        With maker fees (0.02%) and typical TP of 0.02-0.05%,
        taker fees (0.05%) would eliminate profits.

        Args:
            entry_price: Entry price
            exit_price: Exit price (TP or SL)
            size: Position size
            side: LONG or SHORT
            use_maker: If True, use maker fee (0.02%), else taker (0.05%)

        Returns:
            Net profit after entry + exit fees
        """
        fee_pct = MAKER_FEE_PCT if use_maker else TAKER_FEE_PCT

        # Entry fee
        entry_notional = entry_price * size
        entry_fee = entry_notional * fee_pct

        # Exit fee
        exit_notional = exit_price * size
        exit_fee = exit_notional * fee_pct

        # Gross P&L
        if side == Side.LONG:
            gross_pnl = (exit_price - entry_price) * size
        else:
            gross_pnl = (entry_price - exit_price) * size

        # Net P&L
        total_fees = entry_fee + exit_fee
        net_pnl = gross_pnl - total_fees

        return net_pnl

    def calculate_minimum_profitable_tp(
        self,
        entry_price: Decimal,
        side: Side,
        use_maker: bool = True,
    ) -> Decimal:
        """
        Calculate minimum TP to be profitable after fees.

        For HFT, this is critical: TP must exceed 2x fee %.

        With maker (0.02%): min TP = 0.04% (entry + exit fee)
        With taker (0.05%): min TP = 0.10% (entry + exit fee)

        Returns:
            Minimum TP percentage (as decimal, e.g., 0.0004 = 0.04%)
        """
        fee_pct = MAKER_FEE_PCT if use_maker else TAKER_FEE_PCT

        # Need to cover entry + exit fee
        min_tp_pct = fee_pct * 2

        # Add small buffer for slippage (0.01%)
        buffer = Decimal("0.0001")
        min_tp_pct += buffer

        return min_tp_pct

    def validate_hft_profitability(
        self,
        trade: ProposedTrade,
        current_price: Decimal,
    ) -> bool:
        """
        Validate that an HFT trade can be profitable after fees.

        Returns False if TP is too small to cover fees.
        """
        if not trade.take_profit_price or trade.take_profit_price <= 0:
            return False

        # Calculate expected profit %
        if trade.side == Side.LONG:
            tp_pct = (trade.take_profit_price - current_price) / current_price
        else:
            tp_pct = (current_price - trade.take_profit_price) / current_price

        # Get minimum profitable TP
        min_tp_pct = self.calculate_minimum_profitable_tp(
            current_price, trade.side, use_maker=True
        )

        if tp_pct < min_tp_pct:
            logger.warning(
                f"HFT trade {trade.symbol} TP {tp_pct:.4%} below minimum "
                f"profitable {min_tp_pct:.4%}. Trade rejected."
            )
            return False

        return True

    def get_hft_leverage(self, strategy_id: StrategyId) -> Decimal:
        """Get leverage for HFT strategy."""
        hft_config = self.get_hft_config(strategy_id)
        if hft_config:
            return getattr(hft_config, 'default_leverage', Decimal("10"))
        return self.risk_config.default_leverage

    def get_max_hft_leverage(self, strategy_id: StrategyId) -> Decimal:
        """Get max leverage for HFT strategy."""
        hft_config = self.get_hft_config(strategy_id)
        if hft_config:
            return getattr(hft_config, 'max_leverage', Decimal("20"))
        return self.risk_config.max_position_leverage

    def _get_strategy_allocation(self, strategy_id: StrategyId) -> Decimal:
        """Get allocation percentage for a strategy."""
        strategy_config = self.settings.get_strategy_config(strategy_id)
        return strategy_config.allocation_pct

    def _calculate_kelly(self, win_rate: Decimal, avg_rr: Decimal) -> Decimal:
        """
        Calculate Kelly fraction.

        Kelly % = W - (1-W)/R
        Where:
            W = Win rate
            R = Average Risk/Reward ratio
        """
        if avg_rr <= 0:
            return Decimal("0.1")

        kelly = win_rate - ((1 - win_rate) / avg_rr)

        # Kelly can be negative if edge is negative
        if kelly <= 0:
            return Decimal("0.1")  # Minimum allocation

        return kelly

    def calculate_leverage(
        self,
        size: Decimal,
        current_price: Decimal,
        account: AccountState,
        symbol: str,
    ) -> Decimal:
        """Calculate leverage for a position."""
        notional = size * current_price
        if account.equity <= 0:
            return Decimal(0)

        leverage = notional / account.equity

        # Cap at symbol max
        symbol_config = self.settings.symbols.get(symbol)
        max_leverage = self.risk_config.max_position_leverage
        if symbol_config:
            max_leverage = min(max_leverage, symbol_config.max_leverage)

        return min(leverage, max_leverage)

    def check_portfolio_leverage(
        self,
        new_size: Decimal,
        new_price: Decimal,
        account: AccountState,
    ) -> bool:
        """Check if adding this position would exceed portfolio leverage limit."""
        new_notional = new_size * new_price
        total_notional = account.total_position_value + new_notional

        if account.equity <= 0:
            return False

        new_leverage = total_notional / account.equity
        return new_leverage <= self.risk_config.max_portfolio_leverage

    def get_remaining_allocation(
        self,
        strategy_id: StrategyId,
        account: AccountState,
    ) -> Decimal:
        """Get remaining allocation available for a strategy."""
        strategy_allocation = self._get_strategy_allocation(strategy_id)
        max_allocation = account.equity * strategy_allocation

        # Calculate current exposure for this strategy
        current_exposure = Decimal(0)
        for pos in account.positions:
            if pos.strategy_id == strategy_id:
                current_exposure += pos.notional_value

        remaining = max_allocation - current_exposure
        return max(remaining, Decimal(0))

    def adjust_for_correlation(
        self,
        size: Decimal,
        trade: ProposedTrade,
        account: AccountState,
        correlations: Dict[str, Decimal],
    ) -> Decimal:
        """
        Adjust size based on correlation with existing positions.

        If highly correlated positions exist, reduce size.
        """
        if not correlations:
            return size

        max_correlation = Decimal(0)
        for pos in account.positions:
            if pos.symbol in correlations:
                corr = abs(correlations[pos.symbol])
                # Same direction = additive risk
                if (pos.side == trade.side):
                    max_correlation = max(max_correlation, corr)

        # Reduce size if highly correlated
        if max_correlation > Decimal("0.7"):
            adjustment = 1 - (max_correlation - Decimal("0.7"))
            size *= adjustment
            logger.info(
                f"Reducing size for {trade.symbol} due to correlation "
                f"({max_correlation:.2f}): {adjustment:.2f}x"
            )

        return size
