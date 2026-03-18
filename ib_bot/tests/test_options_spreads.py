"""Tests for CreditSpreadStrategy (SPY credit put spreads)."""

import json
import pytest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ib_bot.strategies.options_spreads import (
    CreditSpreadStrategy,
    OpenSpread,
    SpreadDefinition,
    SpreadStateFile,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def default_config() -> dict:
    return {
        "enabled": True,
        "underlying": "SPY",
        "spread_width": 5.0,
        "target_delta": 0.20,
        "target_dte": 45,
        "profit_target_pct": 50.0,
        "stop_loss_mult": 2.0,
        "dte_exit": 21,
        "delta_exit": 0.30,
        "max_positions": 3,
        "entry_frequency_days": 14,
    }


@pytest.fixture
def strategy(default_config: dict, tmp_path: Path) -> CreditSpreadStrategy:
    """Create strategy with a temp state file to avoid polluting real data."""
    s = CreditSpreadStrategy(default_config)
    s._state_path = tmp_path / "spread_state.json"
    s._state = SpreadStateFile()
    return s


@pytest.fixture
def sample_spread() -> SpreadDefinition:
    return SpreadDefinition(
        underlying="SPY",
        expiry="20260501",
        short_strike=540.0,
        long_strike=535.0,
        short_delta=0.198,
        estimated_credit=1.25,
        dte=44,
    )


@pytest.fixture
def sample_open_spread() -> OpenSpread:
    return OpenSpread(
        spread_id="SPY_P540_P535_20260501",
        underlying="SPY",
        expiry="20260501",
        short_strike=540.0,
        long_strike=535.0,
        entry_date="2026-03-17",
        credit_received=125.0,  # $1.25/share x 100
    )


# ============================================================================
# should_enter Tests
# ============================================================================


class TestShouldEnter:
    """Test entry day logic."""

    def test_not_enabled(self, default_config: dict, tmp_path: Path) -> None:
        """Disabled strategy should never enter."""
        default_config["enabled"] = False
        s = CreditSpreadStrategy(default_config)
        s._state_path = tmp_path / "state.json"
        s._state = SpreadStateFile()
        # Tuesday
        assert s.should_enter(date(2026, 3, 17)) is False

    def test_not_tuesday(self, strategy: CreditSpreadStrategy) -> None:
        """Should only enter on Tuesdays."""
        # Monday
        assert strategy.should_enter(date(2026, 3, 16)) is False
        # Wednesday
        assert strategy.should_enter(date(2026, 3, 18)) is False
        # Friday
        assert strategy.should_enter(date(2026, 3, 20)) is False

    def test_tuesday_no_history(self, strategy: CreditSpreadStrategy) -> None:
        """First entry on a Tuesday should be allowed."""
        assert strategy.should_enter(date(2026, 3, 17)) is True

    def test_max_positions(self, strategy: CreditSpreadStrategy) -> None:
        """Should not enter when at max positions."""
        strategy._state.open_positions = [
            OpenSpread(
                spread_id=f"SPY_P{540-i}_P{535-i}_20260501",
                underlying="SPY",
                expiry="20260501",
                short_strike=540.0 - i,
                long_strike=535.0 - i,
                entry_date="2026-03-03",
                credit_received=100.0,
            )
            for i in range(3)
        ]
        assert strategy.should_enter(date(2026, 3, 17)) is False

    def test_frequency_too_soon(self, strategy: CreditSpreadStrategy) -> None:
        """Should not enter if less than 14 days since last entry."""
        strategy._state.last_entry_date = "2026-03-10"  # 7 days ago
        assert strategy.should_enter(date(2026, 3, 17)) is False

    def test_frequency_ok(self, strategy: CreditSpreadStrategy) -> None:
        """Should enter if 14+ days since last entry."""
        strategy._state.last_entry_date = "2026-03-03"  # 14 days ago
        assert strategy.should_enter(date(2026, 3, 17)) is True

    def test_frequency_exactly_14_days(self, strategy: CreditSpreadStrategy) -> None:
        """Boundary: exactly 14 days should allow entry."""
        strategy._state.last_entry_date = "2026-03-03"
        assert strategy.should_enter(date(2026, 3, 17)) is True

    def test_frequency_13_days(self, strategy: CreditSpreadStrategy) -> None:
        """Boundary: 13 days should not allow entry."""
        strategy._state.last_entry_date = "2026-03-04"
        assert strategy.should_enter(date(2026, 3, 17)) is False


# ============================================================================
# State Persistence Tests
# ============================================================================


class TestStatePersistence:
    """Test JSON state file loading and saving."""

    def test_save_and_load(self, strategy: CreditSpreadStrategy) -> None:
        """State should round-trip through JSON."""
        spread = OpenSpread(
            spread_id="SPY_P540_P535_20260501",
            underlying="SPY",
            expiry="20260501",
            short_strike=540.0,
            long_strike=535.0,
            entry_date="2026-03-17",
            credit_received=125.0,
        )
        strategy._state.open_positions.append(spread)
        strategy._state.last_entry_date = "2026-03-17"
        strategy._save_state()

        # Load into fresh state
        loaded = strategy._load_state()
        assert len(loaded.open_positions) == 1
        assert loaded.open_positions[0].spread_id == "SPY_P540_P535_20260501"
        assert loaded.last_entry_date == "2026-03-17"
        assert loaded.open_positions[0].credit_received == 125.0

    def test_load_missing_file(self, strategy: CreditSpreadStrategy) -> None:
        """Missing state file should return empty state."""
        strategy._state_path = Path("/tmp/nonexistent_12345.json")
        state = strategy._load_state()
        assert len(state.open_positions) == 0
        assert state.last_entry_date is None

    def test_load_corrupt_file(self, strategy: CreditSpreadStrategy) -> None:
        """Corrupt JSON should return empty state."""
        strategy._state_path.write_text("not valid json{{{")
        state = strategy._load_state()
        assert len(state.open_positions) == 0


# ============================================================================
# SpreadDefinition Model Tests
# ============================================================================


class TestSpreadDefinition:
    """Test data model validation."""

    def test_valid_spread(self, sample_spread: SpreadDefinition) -> None:
        assert sample_spread.underlying == "SPY"
        assert sample_spread.short_strike == 540.0
        assert sample_spread.long_strike == 535.0
        assert sample_spread.short_strike - sample_spread.long_strike == 5.0

    def test_spread_width_calculation(self, sample_spread: SpreadDefinition) -> None:
        width = sample_spread.short_strike - sample_spread.long_strike
        assert width == 5.0


# ============================================================================
# Exit Logic Tests (unit, no IB connection)
# ============================================================================


class TestExitLogic:
    """Test exit condition checks with mocked IB data."""

    @pytest.mark.asyncio
    async def test_dte_exit(self, strategy: CreditSpreadStrategy) -> None:
        """Should exit when DTE <= 21."""
        # Expiry 20 days from now
        from datetime import timedelta

        exp_date = date.today() + timedelta(days=20)
        pos = OpenSpread(
            spread_id="SPY_P540_P535_test",
            underlying="SPY",
            expiry=exp_date.strftime("%Y%m%d"),
            short_strike=540.0,
            long_strike=535.0,
            entry_date="2026-03-01",
            credit_received=125.0,
        )
        strategy._state.open_positions = [pos]

        # DTE check doesn't need market data
        mock_ib = AsyncMock()
        exits = await strategy.check_exits(mock_ib)

        assert len(exits) == 1
        assert "DTE exit" in exits[0][1]

    @pytest.mark.asyncio
    async def test_no_exit_when_dte_ok(self, strategy: CreditSpreadStrategy) -> None:
        """Should not DTE-exit when plenty of time remaining."""
        from datetime import timedelta

        exp_date = date.today() + timedelta(days=35)
        pos = OpenSpread(
            spread_id="SPY_P540_P535_test",
            underlying="SPY",
            expiry=exp_date.strftime("%Y%m%d"),
            short_strike=540.0,
            long_strike=535.0,
            entry_date="2026-03-01",
            credit_received=125.0,
        )
        strategy._state.open_positions = [pos]

        # Mock IB that returns options with greeks
        mock_ib = AsyncMock()

        # Mock qualified contracts
        mock_short = MagicMock()
        mock_short.conId = 1001
        mock_long = MagicMock()
        mock_long.conId = 1002
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[mock_short, mock_long])

        # Mock tickers with safe P&L (spread value less than credit)
        short_ticker = MagicMock()
        short_ticker.bid = 0.80
        short_ticker.ask = 0.90
        short_ticker.contract = mock_short
        short_ticker.modelGreeks = MagicMock()
        short_ticker.modelGreeks.delta = -0.15  # Safe delta

        long_ticker = MagicMock()
        long_ticker.bid = 0.10
        long_ticker.ask = 0.15
        long_ticker.contract = mock_long
        long_ticker.modelGreeks = MagicMock()
        long_ticker.modelGreeks.delta = -0.05

        mock_ib.reqTickersAsync = AsyncMock(return_value=[short_ticker, long_ticker])
        mock_ib.cancelMktData = MagicMock()

        exits = await strategy.check_exits(mock_ib)
        # Close cost = (0.85 - 0.125) * 100 = $72.50
        # Credit = $125 -> 72.50 <= 62.50 (50%)? No -> no profit exit
        # 72.50 >= 250 (2x)? No -> no stop loss
        # delta = 0.15 < 0.30 -> no delta exit
        assert len(exits) == 0

    @pytest.mark.asyncio
    async def test_profit_target_exit(self, strategy: CreditSpreadStrategy) -> None:
        """Should exit when cost to close <= 50% of credit."""
        from datetime import timedelta

        exp_date = date.today() + timedelta(days=35)
        pos = OpenSpread(
            spread_id="SPY_P540_P535_test",
            underlying="SPY",
            expiry=exp_date.strftime("%Y%m%d"),
            short_strike=540.0,
            long_strike=535.0,
            entry_date="2026-03-01",
            credit_received=125.0,
        )
        strategy._state.open_positions = [pos]

        mock_ib = AsyncMock()
        mock_short = MagicMock()
        mock_long = MagicMock()
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[mock_short, mock_long])

        # Short put decayed a lot: bid/ask ~0.40
        short_ticker = MagicMock()
        short_ticker.bid = 0.38
        short_ticker.ask = 0.42
        short_ticker.contract = mock_short
        short_ticker.modelGreeks = MagicMock()
        short_ticker.modelGreeks.delta = -0.08

        long_ticker = MagicMock()
        long_ticker.bid = 0.05
        long_ticker.ask = 0.07
        long_ticker.contract = mock_long
        long_ticker.modelGreeks = MagicMock()
        long_ticker.modelGreeks.delta = -0.02

        mock_ib.reqTickersAsync = AsyncMock(return_value=[short_ticker, long_ticker])
        mock_ib.cancelMktData = MagicMock()

        exits = await strategy.check_exits(mock_ib)
        # Close cost = (0.40 - 0.06) * 100 = $34.00
        # Credit = $125 -> 50% = $62.50 -> 34 <= 62.50 -> PROFIT EXIT
        assert len(exits) == 1
        assert "Profit target" in exits[0][1]

    @pytest.mark.asyncio
    async def test_delta_exit(self, strategy: CreditSpreadStrategy) -> None:
        """Should exit when short delta >= 0.30."""
        from datetime import timedelta

        exp_date = date.today() + timedelta(days=35)
        pos = OpenSpread(
            spread_id="SPY_P540_P535_test",
            underlying="SPY",
            expiry=exp_date.strftime("%Y%m%d"),
            short_strike=540.0,
            long_strike=535.0,
            entry_date="2026-03-01",
            credit_received=125.0,
        )
        strategy._state.open_positions = [pos]

        mock_ib = AsyncMock()
        mock_short = MagicMock()
        mock_long = MagicMock()
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[mock_short, mock_long])

        # Short delta has increased to 0.35 (market dropped)
        short_ticker = MagicMock()
        short_ticker.bid = 2.50
        short_ticker.ask = 2.70
        short_ticker.contract = mock_short
        short_ticker.modelGreeks = MagicMock()
        short_ticker.modelGreeks.delta = -0.35

        long_ticker = MagicMock()
        long_ticker.bid = 1.20
        long_ticker.ask = 1.40
        long_ticker.contract = mock_long
        long_ticker.modelGreeks = MagicMock()
        long_ticker.modelGreeks.delta = -0.20

        mock_ib.reqTickersAsync = AsyncMock(return_value=[short_ticker, long_ticker])
        mock_ib.cancelMktData = MagicMock()

        exits = await strategy.check_exits(mock_ib)
        # Close cost = (2.60 - 1.30) * 100 = $130
        # Credit = $125 -> $130 is not >= 250 (stop loss) but close
        # Delta = 0.35 >= 0.30 -> DELTA EXIT
        assert len(exits) == 1
        assert "Delta exit" in exits[0][1]


# ============================================================================
# Status Report Tests
# ============================================================================


class TestStatusReport:
    """Test status report generation."""

    def test_empty_report(self, strategy: CreditSpreadStrategy) -> None:
        report = strategy.status_report()
        assert "Credit Spread Strategy" in report
        assert "0/3" in report

    def test_report_with_positions(
        self, strategy: CreditSpreadStrategy, sample_open_spread: OpenSpread
    ) -> None:
        strategy._state.open_positions = [sample_open_spread]
        strategy._state.last_entry_date = "2026-03-17"
        report = strategy.status_report()
        assert "1/3" in report
        assert "SPY_P540_P535_20260501" in report
        assert "Last entry: 2026-03-17" in report

    def test_report_with_closed_trades(self, strategy: CreditSpreadStrategy) -> None:
        closed = OpenSpread(
            spread_id="SPY_P530_P525_20260401",
            underlying="SPY",
            expiry="20260401",
            short_strike=530.0,
            long_strike=525.0,
            entry_date="2026-02-15",
            credit_received=110.0,
            status="closed",
            close_reason="Profit target",
            close_pnl=55.0,
        )
        strategy._state.closed_positions = [closed]
        report = strategy.status_report()
        assert "Closed trades: 1" in report


# ============================================================================
# Config Model Tests
# ============================================================================


class TestConfigModel:
    """Test that OptionsSpreadsConfig validates correctly."""

    def test_default_config(self) -> None:
        from ib_bot.config.loader import OptionsSpreadsConfig

        cfg = OptionsSpreadsConfig()
        assert cfg.enabled is False
        assert cfg.underlying == "SPY"
        assert cfg.spread_width == 5.0
        assert cfg.target_delta == 0.20
        assert cfg.target_dte == 45
        assert cfg.max_positions == 3

    def test_custom_config(self) -> None:
        from ib_bot.config.loader import OptionsSpreadsConfig

        cfg = OptionsSpreadsConfig(
            enabled=True,
            underlying="QQQ",
            spread_width=10.0,
            target_delta=0.15,
            target_dte=30,
            max_positions=5,
        )
        assert cfg.enabled is True
        assert cfg.underlying == "QQQ"
        assert cfg.spread_width == 10.0

    def test_validation_spread_width(self) -> None:
        from ib_bot.config.loader import OptionsSpreadsConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            OptionsSpreadsConfig(spread_width=0.5)  # Below minimum 1.0

    def test_validation_delta_range(self) -> None:
        from ib_bot.config.loader import OptionsSpreadsConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            OptionsSpreadsConfig(target_delta=0.50)  # Above max 0.40

    def test_config_in_trading_config(self) -> None:
        """OptionsSpreadsConfig should be part of TradingConfig."""
        from ib_bot.config.loader import TradingConfig

        cfg = TradingConfig()
        assert hasattr(cfg, "options_spreads")
        assert cfg.options_spreads.enabled is False
