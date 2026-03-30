"""Tests for ib_bot.scanner.universe."""

import pytest

from ib_bot.scanner.universe import (
    ETF_UNIVERSE,
    FUTURES_UNIVERSE,
    SP500_SYMBOLS,
    STOCK_SECTORS,
    get_universe,
)


class TestUniverseLists:
    """Verify that universe lists have the expected sizes."""

    def test_sp500_has_many_symbols(self) -> None:
        # May not be exactly 500 (some tickers change), but should be 350+
        assert len(SP500_SYMBOLS) >= 350
        assert len(SP500_SYMBOLS) == len(set(SP500_SYMBOLS)), "Duplicates in SP500"

    def test_etf_universe_size(self) -> None:
        assert 30 <= len(ETF_UNIVERSE) <= 50
        assert "SPY" in ETF_UNIVERSE
        assert "QQQ" in ETF_UNIVERSE
        assert "TLT" in ETF_UNIVERSE

    def test_futures_universe(self) -> None:
        assert "ES" in FUTURES_UNIVERSE
        assert "NQ" in FUTURES_UNIVERSE
        assert "MES" in FUTURES_UNIVERSE
        assert "MNQ" in FUTURES_UNIVERSE

    def test_stock_sectors_coverage(self) -> None:
        # At least 50 stocks mapped
        assert len(STOCK_SECTORS) >= 50
        # All mapped stocks are in SP500
        for symbol in STOCK_SECTORS:
            assert symbol in SP500_SYMBOLS, f"{symbol} in STOCK_SECTORS but not SP500"

    def test_no_duplicates_in_etf(self) -> None:
        assert len(ETF_UNIVERSE) == len(set(ETF_UNIVERSE))


class TestGetUniverse:
    """Test the get_universe dispatcher."""

    def test_stocks(self) -> None:
        result = get_universe("stocks")
        assert result == SP500_SYMBOLS
        # Should be a copy, not the same object
        assert result is not SP500_SYMBOLS

    def test_etf(self) -> None:
        assert get_universe("etf") == ETF_UNIVERSE

    def test_futures(self) -> None:
        assert get_universe("futures") == FUTURES_UNIVERSE

    def test_all(self) -> None:
        result = get_universe("all")
        assert len(result) == len(SP500_SYMBOLS) + len(ETF_UNIVERSE) + len(FUTURES_UNIVERSE)

    def test_case_insensitive(self) -> None:
        assert get_universe("STOCKS") == SP500_SYMBOLS
        assert get_universe("Etf") == ETF_UNIVERSE

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown asset_class"):
            get_universe("crypto")
