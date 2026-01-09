"""
Base Strategy Interface
========================

Abstract base class for all trading strategies in HLQuantBot.

Each strategy:
- Analyzes MarketState
- Generates Setup objects when conditions are met
- Works only in specific regimes
"""

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from ..core.models import MarketState, Setup, Regime, Direction


logger = logging.getLogger(__name__)


@dataclass
class StrategyResult:
    """Result of strategy evaluation."""

    has_setup: bool = False
    setup: Optional[Setup] = None
    reason: str = ""


class BaseStrategy(ABC):
    """
    Abstract base class for trading strategies.

    Strategies must implement:
    - name: Strategy identifier
    - required_regime: Which regime this strategy operates in
    - evaluate(): Analyze market state and return setup if valid
    """

    def __init__(self, config: dict = None):
        """
        Initialize strategy.

        Args:
            config: Strategy-specific configuration
        """
        self.config = config or {}
        self._logger = logging.getLogger(f"strategy.{self.name}")

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name for logging and identification."""
        pass

    @property
    @abstractmethod
    def required_regime(self) -> Regime:
        """Regime required for this strategy to be active."""
        pass

    @abstractmethod
    def evaluate(self, state: MarketState) -> StrategyResult:
        """
        Evaluate market state and generate setup if conditions met.

        Args:
            state: Current market state with indicators

        Returns:
            StrategyResult with setup if valid opportunity found
        """
        pass

    def can_trade(self, state: MarketState) -> bool:
        """
        Check if strategy can trade in current regime.

        Args:
            state: Current market state

        Returns:
            True if regime matches required regime
        """
        return state.regime == self.required_regime

    def reject(self, reason: str) -> StrategyResult:
        """
        Create a rejection result with the given reason.

        Args:
            reason: Why the setup was rejected

        Returns:
            StrategyResult with has_setup=False
        """
        return StrategyResult(has_setup=False, reason=reason)

    def generate_setup_id(self) -> str:
        """Generate unique setup ID."""
        return f"{self.name}_{uuid.uuid4().hex[:8]}"

    def calculate_stop_price(
        self,
        entry_price: Decimal,
        atr: Decimal,
        direction: Direction,
        atr_mult: float = 2.5,
    ) -> Decimal:
        """
        Calculate stop price based on ATR.

        Args:
            entry_price: Entry price
            atr: Current ATR value
            direction: Trade direction
            atr_mult: ATR multiplier for stop distance

        Returns:
            Stop price
        """
        stop_distance = atr * Decimal(str(atr_mult))

        if direction == Direction.LONG:
            return entry_price - stop_distance
        else:
            return entry_price + stop_distance

    def calculate_stop_distance_pct(
        self,
        entry_price: Decimal,
        stop_price: Decimal,
    ) -> Decimal:
        """Calculate stop distance as percentage."""
        return abs(entry_price - stop_price) / entry_price * 100
