"""Futures contract specifications for IB ORB Trading Bot.

Defines tick sizes, multipliers, session times, and opening range
windows for each supported futures contract.
"""

from datetime import time
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field


class FuturesSpec(BaseModel):
    """Specification for a futures contract."""

    symbol: str = Field(..., description="Contract symbol (e.g., ES, NQ)")
    exchange: str = Field(..., description="Exchange (e.g., CME, EUREX)")
    currency: str = Field(default="USD", description="Contract currency")
    tick_size: Decimal = Field(..., gt=0, description="Minimum price increment")
    tick_value: Decimal = Field(..., gt=0, description="Dollar value per tick")
    multiplier: int = Field(..., gt=0, description="Contract multiplier")

    # Session times (local exchange time)
    session_start: time = Field(..., description="Regular session start")
    session_end: time = Field(..., description="Regular session end")
    or_start: time = Field(..., description="Opening range start (= session_start)")
    or_end: time = Field(..., description="Opening range end (e.g., 30 min after open)")

    # Timezone for this contract's exchange
    tz_name: str = Field(..., description="IANA timezone (e.g., America/New_York)")

    @property
    def tz(self) -> ZoneInfo:
        """Return the ZoneInfo object for this contract's exchange."""
        return ZoneInfo(self.tz_name)

    class Config:
        json_encoders = {
            Decimal: lambda v: float(v),
            time: lambda v: v.isoformat(),
        }


class StockSpec(BaseModel):
    """Specification for a stock or ETF contract on IB.

    Used by the universal scanner and LLM equity strategy
    for qualifying and trading Stock contracts via SMART routing.
    """

    symbol: str = Field(..., description="Ticker symbol (e.g., AAPL, SPY)")
    exchange: str = Field(default="SMART", description="IB exchange (SMART for best routing)")
    currency: str = Field(default="USD", description="Contract currency")
    tick_size: Decimal = Field(default=Decimal("0.01"), gt=0, description="Minimum price increment")

    class Config:
        json_encoders = {
            Decimal: lambda v: float(v),
        }


# US session times (Eastern Time)
_US_SESSION_START = time(9, 30)
_US_SESSION_END = time(16, 0)
_US_OR_START = time(9, 30)
_US_OR_END = time(9, 45)

# EU session times (Central European Time)
_EU_SESSION_START = time(9, 0)
_EU_SESSION_END = time(17, 30)
_EU_OR_START = time(9, 0)
_EU_OR_END = time(9, 30)


CONTRACTS: dict[str, FuturesSpec] = {
    "ES": FuturesSpec(
        symbol="ES",
        exchange="CME",
        currency="USD",
        tick_size=Decimal("0.25"),
        tick_value=Decimal("12.50"),
        multiplier=50,
        session_start=_US_SESSION_START,
        session_end=_US_SESSION_END,
        or_start=_US_OR_START,
        or_end=_US_OR_END,
        tz_name="America/New_York",
    ),
    "NQ": FuturesSpec(
        symbol="NQ",
        exchange="CME",
        currency="USD",
        tick_size=Decimal("0.25"),
        tick_value=Decimal("5.00"),
        multiplier=20,
        session_start=_US_SESSION_START,
        session_end=_US_SESSION_END,
        or_start=_US_OR_START,
        or_end=_US_OR_END,
        tz_name="America/New_York",
    ),
    "MES": FuturesSpec(
        symbol="MES",
        exchange="CME",
        currency="USD",
        tick_size=Decimal("0.25"),
        tick_value=Decimal("1.25"),
        multiplier=5,
        session_start=_US_SESSION_START,
        session_end=_US_SESSION_END,
        or_start=_US_OR_START,
        or_end=_US_OR_END,
        tz_name="America/New_York",
    ),
    "MNQ": FuturesSpec(
        symbol="MNQ",
        exchange="CME",
        currency="USD",
        tick_size=Decimal("0.25"),
        tick_value=Decimal("0.50"),
        multiplier=2,
        session_start=_US_SESSION_START,
        session_end=_US_SESSION_END,
        or_start=_US_OR_START,
        or_end=_US_OR_END,
        tz_name="America/New_York",
    ),
    "YM": FuturesSpec(
        symbol="YM",
        exchange="CBOT",
        currency="USD",
        tick_size=Decimal("1.00"),
        tick_value=Decimal("5.00"),
        multiplier=5,
        session_start=_US_SESSION_START,
        session_end=_US_SESSION_END,
        or_start=_US_OR_START,
        or_end=_US_OR_END,
        tz_name="America/New_York",
    ),
    "GC": FuturesSpec(
        symbol="GC",
        exchange="COMEX",
        currency="USD",
        tick_size=Decimal("0.10"),
        tick_value=Decimal("10.00"),
        multiplier=100,
        session_start=_US_SESSION_START,
        session_end=_US_SESSION_END,
        or_start=_US_OR_START,
        or_end=_US_OR_END,
        tz_name="America/New_York",
    ),
    "SI": FuturesSpec(
        symbol="SI",
        exchange="COMEX",
        currency="USD",
        tick_size=Decimal("0.005"),
        tick_value=Decimal("25.00"),
        multiplier=5000,
        session_start=_US_SESSION_START,
        session_end=_US_SESSION_END,
        or_start=_US_OR_START,
        or_end=_US_OR_END,
        tz_name="America/New_York",
    ),
    "CL": FuturesSpec(
        symbol="CL",
        exchange="NYMEX",
        currency="USD",
        tick_size=Decimal("0.01"),
        tick_value=Decimal("10.00"),
        multiplier=1000,
        session_start=_US_SESSION_START,
        session_end=_US_SESSION_END,
        or_start=_US_OR_START,
        or_end=_US_OR_END,
        tz_name="America/New_York",
    ),
    "NG": FuturesSpec(
        symbol="NG",
        exchange="NYMEX",
        currency="USD",
        tick_size=Decimal("0.001"),
        tick_value=Decimal("10.00"),
        multiplier=10000,
        session_start=_US_SESSION_START,
        session_end=_US_SESSION_END,
        or_start=_US_OR_START,
        or_end=_US_OR_END,
        tz_name="America/New_York",
    ),
    "ZB": FuturesSpec(
        symbol="ZB",
        exchange="CBOT",
        currency="USD",
        tick_size=Decimal("0.03125"),
        tick_value=Decimal("31.25"),
        multiplier=1000,
        session_start=_US_SESSION_START,
        session_end=_US_SESSION_END,
        or_start=_US_OR_START,
        or_end=_US_OR_END,
        tz_name="America/New_York",
    ),
    "ZN": FuturesSpec(
        symbol="ZN",
        exchange="CBOT",
        currency="USD",
        tick_size=Decimal("0.015625"),
        tick_value=Decimal("15.625"),
        multiplier=1000,
        session_start=_US_SESSION_START,
        session_end=_US_SESSION_END,
        or_start=_US_OR_START,
        or_end=_US_OR_END,
        tz_name="America/New_York",
    ),
    "DAX": FuturesSpec(
        symbol="DAX",
        exchange="EUREX",
        currency="EUR",
        tick_size=Decimal("0.50"),
        tick_value=Decimal("12.50"),
        multiplier=25,
        session_start=_EU_SESSION_START,
        session_end=_EU_SESSION_END,
        or_start=_EU_OR_START,
        or_end=_EU_OR_END,
        tz_name="Europe/Berlin",
    ),
}
