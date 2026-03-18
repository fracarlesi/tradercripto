"""Tests for strategy registry factory."""

import pytest
from decimal import Decimal

from ib_bot.config.loader import TradingConfig, StrategyConfig, EMAStrategyConfig
from ib_bot.strategies.registry import create_strategy
from ib_bot.strategies.orb import ORBStrategy
from ib_bot.strategies.ema_momentum import EMAMomentumStrategy


def test_create_orb_strategy() -> None:
    """Factory returns ORBStrategy for name='orb'."""
    config = TradingConfig()
    assert config.strategy.name == "orb"
    strategy = create_strategy(config)
    assert isinstance(strategy, ORBStrategy)
    assert strategy.name == "orb"


def test_create_ema_strategy() -> None:
    """Factory returns EMAMomentumStrategy for name='ema_momentum'."""
    config = TradingConfig(
        strategy=StrategyConfig(name="ema_momentum"),
    )
    strategy = create_strategy(config)
    assert isinstance(strategy, EMAMomentumStrategy)
    assert strategy.name == "ema_momentum"


def test_unknown_strategy_raises() -> None:
    """Factory raises ValueError for unknown strategy name."""
    config = TradingConfig()
    # Bypass pydantic validation by setting name directly
    object.__setattr__(config.strategy, "name", "unknown_strategy")
    with pytest.raises(ValueError, match="Unknown strategy"):
        create_strategy(config)


def test_orb_is_default() -> None:
    """Default TradingConfig uses 'orb' strategy."""
    config = TradingConfig()
    assert config.strategy.name == "orb"


def test_ema_config_forwarded() -> None:
    """EMA strategy receives ema_strategy config from TradingConfig."""
    config = TradingConfig(
        strategy=StrategyConfig(name="ema_momentum"),
        ema_strategy=EMAStrategyConfig(ema_fast=5, ema_slow=13),
    )
    strategy = create_strategy(config)
    assert isinstance(strategy, EMAMomentumStrategy)
    assert strategy._ema_cfg.ema_fast == 5
    assert strategy._ema_cfg.ema_slow == 13
