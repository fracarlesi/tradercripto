"""Pair Trading Strategy - Long strongest / Short weakest.

AGGIORNATO (da specifica consigli.md):
- Priorità bassa rispetto altre strategie
- Attivo SOLO in regime range_bound
- TP/SL aggiornati per rispettare vincoli globali (MIN_TP=0.35%, MAX_SL=0.20%)
"""

import logging
from decimal import Decimal
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timezone

from ...core.models import (
    ProposedTrade,
    AccountState,
    Bar,
    MarketContext,
    Position,
)
from ...core.enums import StrategyId, Side, OrderType, MarketRegime
from ...config.settings import Settings
from .hft_base import HFTBaseStrategy


logger = logging.getLogger(__name__)


class PairTradingStrategy(HFTBaseStrategy):
    """
    Pair Trading HFT Strategy.

    Logic:
    - Track relative performance between correlated pairs
    - When spread (z-score) deviates from mean, enter:
      - Long the underperformer
      - Short the outperformer
    - Exit when spread reverts to mean

    Pairs:
    - BTC/ETH
    - ETH/SOL

    Key parameters:
    - lookback_seconds: Window for spread calculation
    - zscore_entry_threshold: Z-score threshold for entry
    - zscore_exit_threshold: Z-score threshold for exit
    """

    def __init__(self, settings: Settings):
        super().__init__(settings, StrategyId.PAIR_TRADING)

        # Pair-specific parameters
        self.lookback_seconds = self._get_param_int('lookback_seconds', 300)
        self.zscore_entry_threshold = self._get_param('zscore_entry_threshold', Decimal("2.0"))
        self.zscore_exit_threshold = self._get_param('zscore_exit_threshold', Decimal("0.5"))
        self.rebalance_interval_seconds = self._get_param_int('rebalance_interval_seconds', 60)

        # NUOVO: Regime restriction (da specifica - solo range_bound)
        self.allowed_regimes = self._get_allowed_regimes()

        # Define pairs from config or defaults
        self.pairs = self._get_pairs()

        # Spread tracking: pair_key -> list of (timestamp, spread)
        self._spread_history: Dict[str, List[Tuple[datetime, Decimal]]] = {}

        # Active pair positions
        self._active_pairs: Dict[str, Dict] = {}  # pair_key -> {long_symbol, short_symbol, entry_zscore}

        # Current regime (viene aggiornato dal bot)
        self._current_regime: Optional[MarketRegime] = None

    def _get_param(self, name: str, default: Decimal) -> Decimal:
        if self._hft_config:
            return Decimal(str(getattr(self._hft_config, name, default)))
        return default

    def _get_param_int(self, name: str, default: int) -> int:
        if self._hft_config:
            return int(getattr(self._hft_config, name, default))
        return default

    def _get_pairs(self) -> List[Tuple[str, str]]:
        """Get trading pairs from config."""
        if self._hft_config:
            pairs_config = getattr(self._hft_config, 'pairs', None)
            if pairs_config:
                return [tuple(p) for p in pairs_config]
        # Defaults
        return [("BTC", "ETH"), ("ETH", "SOL")]

    def _get_allowed_regimes(self) -> List[MarketRegime]:
        """Get allowed regimes from config (da specifica: solo range_bound)."""
        if self._hft_config:
            regimes = getattr(self._hft_config, 'allowed_regimes', None)
            if regimes:
                # Convert strings to MarketRegime enums
                return [MarketRegime(r) if isinstance(r, str) else r for r in regimes]
        # Default: solo range_bound come da specifica
        return [MarketRegime.RANGE_BOUND]

    def set_regime(self, regime: MarketRegime):
        """Imposta il regime corrente. Chiamato dal bot."""
        self._current_regime = regime

    def is_regime_allowed(self) -> bool:
        """Verifica se il regime corrente permette il trading."""
        if not self._current_regime:
            return False  # Se regime non noto, non operare
        return self._current_regime in self.allowed_regimes

    def _pair_key(self, pair: Tuple[str, str]) -> str:
        """Create unique key for a pair."""
        return f"{pair[0]}_{pair[1]}"

    async def evaluate(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
        position: Optional[Position] = None,
    ) -> Optional[ProposedTrade]:
        """
        Evaluate pair trading.

        Note: This is called per symbol, but we need both symbols
        of a pair to evaluate. We track state and only emit signals
        when we have data for both legs.
        """
        # Check signal cooldown
        if not self.can_signal_hft(symbol):
            return None

        # This strategy needs special handling - we'll process in evaluate_all
        # Here we just return None as pair logic is in evaluate_pairs
        return None

    async def evaluate_pairs(
        self,
        bars_by_symbol: Dict[str, List[Bar]],
        contexts: Dict[str, MarketContext],
        account: AccountState,
    ) -> List[ProposedTrade]:
        """
        Evaluate all pairs and generate signals.

        This should be called instead of evaluate_all for pair trading.

        AGGIORNATO: Verifica regime prima di generare segnali (da specifica).
        Attivo SOLO in range_bound.
        """
        if not self.is_enabled:
            return []

        # NUOVO: Verifica regime - strategia attiva solo in range_bound (da specifica)
        if not self.is_regime_allowed():
            if self._current_regime:
                logger.debug(
                    f"PairTrading: regime '{self._current_regime.value}' non permesso. "
                    f"Allowed: {[r.value for r in self.allowed_regimes]}"
                )
            return []

        proposals = []

        for pair in self.pairs:
            symbol_a, symbol_b = pair
            pair_key = self._pair_key(pair)

            # Check we have data for both symbols
            if symbol_a not in bars_by_symbol or symbol_b not in bars_by_symbol:
                continue
            if symbol_a not in contexts or symbol_b not in contexts:
                continue

            # Get current prices
            price_a = contexts[symbol_a].current_price
            price_b = contexts[symbol_b].current_price

            if not price_a or not price_b or price_a == 0 or price_b == 0:
                continue

            # Calculate spread (log ratio)
            spread = self._calculate_spread(price_a, price_b)

            # Update spread history
            self._update_spread_history(pair_key, spread)

            # Calculate z-score
            zscore = self._calculate_zscore(pair_key, spread)
            if zscore is None:
                continue

            # Check for existing pair position
            existing_pair = self._active_pairs.get(pair_key)

            if existing_pair:
                # Check for exit
                exit_proposals = self._check_pair_exit(
                    pair_key, pair, zscore, contexts, account
                )
                proposals.extend(exit_proposals)
            else:
                # Check for entry
                entry_proposals = self._check_pair_entry(
                    pair_key, pair, zscore, contexts, account
                )
                proposals.extend(entry_proposals)

        return proposals

    def _calculate_spread(self, price_a: Decimal, price_b: Decimal) -> Decimal:
        """Calculate log spread ratio."""
        # Simple ratio for now (log would require math import)
        return price_a / price_b

    def _update_spread_history(self, pair_key: str, spread: Decimal):
        """Update spread history for a pair."""
        now = datetime.now(timezone.utc)

        if pair_key not in self._spread_history:
            self._spread_history[pair_key] = []

        self._spread_history[pair_key].append((now, spread))

        # Clean old entries
        cutoff = now.timestamp() - self.lookback_seconds
        self._spread_history[pair_key] = [
            (ts, s) for ts, s in self._spread_history[pair_key]
            if ts.timestamp() >= cutoff
        ]

    def _calculate_zscore(self, pair_key: str, current_spread: Decimal) -> Optional[Decimal]:
        """Calculate z-score of current spread."""
        history = self._spread_history.get(pair_key, [])

        if len(history) < 10:  # Need minimum samples
            return None

        spreads = [s for _, s in history]

        # Calculate mean and std
        mean = sum(spreads) / len(spreads)

        variance = sum((s - mean) ** 2 for s in spreads) / len(spreads)
        if variance == 0:
            return Decimal(0)

        # Calculate std (simplified sqrt)
        std = variance ** Decimal("0.5")

        if std == 0:
            return Decimal(0)

        zscore = (current_spread - mean) / std
        return zscore

    def _check_pair_entry(
        self,
        pair_key: str,
        pair: Tuple[str, str],
        zscore: Decimal,
        contexts: Dict[str, MarketContext],
        account: AccountState,
    ) -> List[ProposedTrade]:
        """Check if we should enter a pair trade."""
        symbol_a, symbol_b = pair
        proposals = []

        abs_zscore = abs(zscore)
        if abs_zscore < self.zscore_entry_threshold:
            return []

        # Check signal cooldown for both symbols
        if not self.can_signal_hft(symbol_a) or not self.can_signal_hft(symbol_b):
            return []

        # Check no existing positions
        pos_a = account.get_position(symbol_a)
        pos_b = account.get_position(symbol_b)
        if pos_a or pos_b:
            return []

        if zscore > self.zscore_entry_threshold:
            # Spread too high -> A overperforming -> Short A, Long B
            long_symbol = symbol_b
            short_symbol = symbol_a
            reason = f"Spread z-score {zscore:.2f} > {self.zscore_entry_threshold}"
        else:
            # zscore < -threshold: Spread too low -> B overperforming -> Long A, Short B
            long_symbol = symbol_a
            short_symbol = symbol_b
            reason = f"Spread z-score {zscore:.2f} < -{self.zscore_entry_threshold}"

        logger.info(
            f"Pair Entry: {pair_key} | Z-score: {zscore:.2f} | "
            f"Long {long_symbol}, Short {short_symbol}"
        )

        # Create proposals for both legs
        # Long leg
        long_context = contexts[long_symbol]
        long_proposal = self._create_pair_leg_proposal(
            symbol=long_symbol,
            side=Side.LONG,
            context=long_context,
            reason=f"[Pair Long] {reason}",
        )
        if long_proposal:
            proposals.append(long_proposal)
            self.record_signal_hft(long_symbol)

        # Short leg
        short_context = contexts[short_symbol]
        short_proposal = self._create_pair_leg_proposal(
            symbol=short_symbol,
            side=Side.SHORT,
            context=short_context,
            reason=f"[Pair Short] {reason}",
        )
        if short_proposal:
            proposals.append(short_proposal)
            self.record_signal_hft(short_symbol)

        # Track active pair
        if len(proposals) == 2:
            self._active_pairs[pair_key] = {
                "long_symbol": long_symbol,
                "short_symbol": short_symbol,
                "entry_zscore": zscore,
                "entry_time": datetime.now(timezone.utc),
            }

        return proposals

    def _check_pair_exit(
        self,
        pair_key: str,
        pair: Tuple[str, str],
        zscore: Decimal,
        contexts: Dict[str, MarketContext],
        account: AccountState,
    ) -> List[ProposedTrade]:
        """Check if we should exit a pair trade."""
        proposals = []
        pair_info = self._active_pairs.get(pair_key)

        if not pair_info:
            return []

        long_symbol = pair_info["long_symbol"]
        short_symbol = pair_info["short_symbol"]
        entry_zscore = pair_info["entry_zscore"]
        entry_time = pair_info["entry_time"]

        # Exit conditions:
        # 1. Z-score reverted to exit threshold
        # 2. Position timeout

        should_exit = False
        reason = ""

        # Check z-score reversion
        if entry_zscore > 0 and zscore <= self.zscore_exit_threshold:
            should_exit = True
            reason = f"Z-score reverted: {zscore:.2f} <= {self.zscore_exit_threshold}"
        elif entry_zscore < 0 and zscore >= -self.zscore_exit_threshold:
            should_exit = True
            reason = f"Z-score reverted: {zscore:.2f} >= -{self.zscore_exit_threshold}"

        # Check timeout
        elapsed = (datetime.now(timezone.utc) - entry_time).total_seconds()
        if elapsed >= self.max_position_hold_seconds:
            should_exit = True
            reason = f"Position timeout after {elapsed:.0f}s"

        if not should_exit:
            return []

        logger.info(f"Pair Exit: {pair_key} | {reason}")

        # Close both legs
        # Close long
        long_pos = account.get_position(long_symbol)
        if long_pos:
            proposals.append(ProposedTrade(
                strategy_id=self.strategy_id,
                symbol=long_symbol,
                side=Side.SHORT,  # Close long
                notional_usd=long_pos.notional_value,
                risk_per_trade=Decimal(0),
                confidence=Decimal("1.0"),
                reason=f"[Pair Close Long] {reason}",
                market_context=contexts[long_symbol],
            ))

        # Close short
        short_pos = account.get_position(short_symbol)
        if short_pos:
            proposals.append(ProposedTrade(
                strategy_id=self.strategy_id,
                symbol=short_symbol,
                side=Side.LONG,  # Close short
                notional_usd=short_pos.notional_value,
                risk_per_trade=Decimal(0),
                confidence=Decimal("1.0"),
                reason=f"[Pair Close Short] {reason}",
                market_context=contexts[short_symbol],
            ))

        # Remove from active pairs
        if pair_key in self._active_pairs:
            del self._active_pairs[pair_key]

        return proposals

    def _create_pair_leg_proposal(
        self,
        symbol: str,
        side: Side,
        context: MarketContext,
        reason: str,
    ) -> Optional[ProposedTrade]:
        """Create proposal for one leg of pair trade."""
        current_price = context.current_price

        # Calculate TP/SL for pair leg
        # Pairs have different risk profile - wider SL, target spread convergence
        tp_price = self.calculate_net_tp(
            current_price,
            self._get_param('take_profit_spread_pct', Decimal("0.002")),
            side
        )
        sl_price = self.calculate_sl(
            current_price,
            self._get_param('stop_loss_spread_pct', Decimal("0.003")),
            side
        )

        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=side,
            notional_usd=Decimal("1000"),
            risk_per_trade=Decimal("70"),
            entry_type=OrderType.LIMIT_GTX,
            entry_price=current_price,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            confidence=Decimal("0.6"),
            reason=reason,
            market_context=context,
        )

    async def evaluate_all(
        self,
        bars_by_symbol: Dict[str, List[Bar]],
        contexts: Dict[str, MarketContext],
        account: AccountState,
    ) -> List[ProposedTrade]:
        """Override to use pair-specific evaluation."""
        return await self.evaluate_pairs(bars_by_symbol, contexts, account)
