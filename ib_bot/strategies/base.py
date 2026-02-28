"""
Base Strategy Interface
========================

Abstract base class for IB bot trading strategies.
"""

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from ..core.enums import Direction
from ..core.models import FuturesMarketState, ORBRange, ORBSetup


logger = logging.getLogger(__name__)


@dataclass
class StrategyResult:
    """Result of strategy evaluation."""

    has_setup: bool = False
    setup: Optional[ORBSetup] = None
    reason: str = ""


class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self._logger = logging.getLogger(f"ib_bot.strategy.{self.name}")

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def evaluate(
        self,
        state: FuturesMarketState,
        or_range: ORBRange,
    ) -> StrategyResult:
        """Evaluate market state against opening range for trade setup."""
        pass

    def reject(self, reason: str) -> StrategyResult:
        return StrategyResult(has_setup=False, reason=reason)

    def generate_setup_id(self) -> str:
        return f"{self.name}_{uuid.uuid4().hex[:8]}"
