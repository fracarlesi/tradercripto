"""
Vigilant Asset Allocation (VAA-G4) ETF Rotation Strategy
==========================================================

Monthly rebalance strategy based on Wouter Keller's VAA-G4 paper.

Rules:
- Offensive universe: SPY, EFA, EEM, AGG
- Defensive universe: BIL, IEF, LQD
- Momentum score: 12*(p0/p1) + 4*(p0/p3) + 2*(p0/p6) + (p0/p12) - 19
  where pN = closing price N months ago
- If ALL offensive ETFs have positive momentum → buy best offensive ETF
- If ANY offensive ETF has negative momentum → buy best defensive ETF
- Rebalance on the last trading day of each month
- Always 100% in ONE ETF
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# State file for tracking current holding
_STATE_FILE = Path(__file__).parent.parent / "data" / "etf_rotation_state.json"


class RotationAction(str, Enum):
    """Action to take on rebalance."""
    HOLD = "hold"           # Already in the recommended ETF
    BUY = "buy"             # No current position, buy recommended
    SWITCH = "switch"       # Sell current, buy recommended


@dataclass
class MomentumScore:
    """Momentum score for a single ETF."""
    symbol: str
    score: float
    p0: float   # current price
    p1: float   # 1 month ago
    p3: float   # 3 months ago
    p6: float   # 6 months ago
    p12: float  # 12 months ago


@dataclass
class RotationResult:
    """Result of the monthly evaluation."""
    recommended_etf: str
    action: RotationAction
    current_holding: Optional[str]
    is_offensive: bool
    offensive_scores: list[MomentumScore]
    defensive_scores: list[MomentumScore]
    all_offensive_positive: bool
    rebalance_date: date


def compute_momentum(
    symbol: str,
    prices: dict[int, float],
) -> MomentumScore:
    """Compute VAA momentum score for a single ETF.

    Formula: 12*(p0/p1) + 4*(p0/p3) + 2*(p0/p6) + (p0/p12) - 19

    Args:
        symbol: ETF ticker symbol.
        prices: Mapping of months_ago -> closing price.
            Keys must include 0, 1, 3, 6, 12.

    Returns:
        MomentumScore with the computed score.

    Raises:
        ValueError: If any required price is missing or zero.
    """
    required_months = [0, 1, 3, 6, 12]
    for m in required_months:
        if m not in prices:
            raise ValueError(f"{symbol}: missing price for {m} months ago")
        if prices[m] <= 0:
            raise ValueError(f"{symbol}: price for {m} months ago is <= 0")

    p0 = prices[0]
    p1 = prices[1]
    p3 = prices[3]
    p6 = prices[6]
    p12 = prices[12]

    score = 12 * (p0 / p1) + 4 * (p0 / p3) + 2 * (p0 / p6) + (p0 / p12) - 19

    return MomentumScore(
        symbol=symbol,
        score=score,
        p0=p0,
        p1=p1,
        p3=p3,
        p6=p6,
        p12=p12,
    )


def pick_etf(
    offensive_scores: list[MomentumScore],
    defensive_scores: list[MomentumScore],
) -> tuple[str, bool, bool]:
    """Apply VAA-G4 decision rule.

    Args:
        offensive_scores: Momentum scores for offensive ETFs.
        defensive_scores: Momentum scores for defensive ETFs.

    Returns:
        Tuple of (recommended_symbol, is_offensive, all_offensive_positive).
    """
    all_positive = all(s.score > 0 for s in offensive_scores)

    if all_positive:
        # Buy best offensive ETF
        best = max(offensive_scores, key=lambda s: s.score)
        return best.symbol, True, True
    else:
        # Buy best defensive ETF
        best = max(defensive_scores, key=lambda s: s.score)
        return best.symbol, False, False


def is_last_trading_day_of_month(dt: date) -> bool:
    """Check if the given date is the last trading day (Mon-Fri) of its month.

    Approximation: last business day of the month. Does not account for
    market holidays (NYSE closures) — for those edge cases, the check
    on the next trading day will catch up.

    Args:
        dt: The date to check.

    Returns:
        True if dt is the last weekday of its month.
    """
    # Find the last day of the month
    if dt.month == 12:
        next_month_first = date(dt.year + 1, 1, 1)
    else:
        next_month_first = date(dt.year, dt.month + 1, 1)

    last_day = next_month_first - timedelta(days=1)

    # Walk backwards to find the last weekday
    while last_day.weekday() >= 5:  # 5=Saturday, 6=Sunday
        last_day -= timedelta(days=1)

    return dt == last_day


def _month_offset_date(reference: date, months_ago: int) -> date:
    """Get a date approximately N months before the reference date.

    Handles year boundaries and short months.

    Args:
        reference: The reference date.
        months_ago: Number of months to go back.

    Returns:
        A date approximately N months before reference.
    """
    year = reference.year
    month = reference.month - months_ago

    while month <= 0:
        month += 12
        year -= 1

    # Clamp day to the valid range for the target month
    max_day = calendar.monthrange(year, month)[1]
    day = min(reference.day, max_day)

    return date(year, month, day)


def extract_monthly_prices(
    daily_bars: list[dict[str, Any]],
    reference_date: date,
) -> dict[int, float]:
    """Extract closing prices at 0, 1, 3, 6, 12 months ago from daily bars.

    For each target date, finds the closest trading day at or before
    that date in the bar data.

    Args:
        daily_bars: List of dicts with 'date' (date) and 'close' (float) keys,
            sorted ascending by date.
        reference_date: The current/reference date (p0).

    Returns:
        Dict mapping months_ago -> closing price.

    Raises:
        ValueError: If not enough historical data.
    """
    if not daily_bars:
        raise ValueError("No daily bars provided")

    # Build a date -> close lookup
    date_to_close: dict[date, float] = {}
    for bar in daily_bars:
        bar_date = bar["date"]
        if isinstance(bar_date, datetime):
            bar_date = bar_date.date()
        date_to_close[bar_date] = float(bar["close"])

    sorted_dates = sorted(date_to_close.keys())

    def _find_closest_price(target: date) -> float:
        """Find the closest price at or before the target date."""
        # Binary search for the closest date at or before target
        best_date = None
        for d in reversed(sorted_dates):
            if d <= target:
                best_date = d
                break

        if best_date is None:
            raise ValueError(
                f"No trading day found at or before {target}. "
                f"Earliest bar: {sorted_dates[0] if sorted_dates else 'none'}"
            )

        return date_to_close[best_date]

    months_needed = [0, 1, 3, 6, 12]
    prices: dict[int, float] = {}

    for m in months_needed:
        target = _month_offset_date(reference_date, m)
        prices[m] = _find_closest_price(target)

    return prices


# =============================================================================
# State Persistence
# =============================================================================

def load_state() -> dict[str, Any]:
    """Load ETF rotation state from disk.

    Returns:
        State dict with keys: current_holding, last_rebalance_date, history.
    """
    if _STATE_FILE.exists():
        try:
            with open(_STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load ETF rotation state: %s", e)

    return {
        "current_holding": None,
        "last_rebalance_date": None,
        "history": [],
    }


def save_state(state: dict[str, Any]) -> None:
    """Save ETF rotation state to disk.

    Args:
        state: State dict to persist.
    """
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)
    logger.info("ETF rotation state saved: holding=%s", state.get("current_holding"))


# =============================================================================
# Main Evaluation (called from main.py)
# =============================================================================

class ETFRotationStrategy:
    """Vigilant Asset Allocation (VAA-G4) ETF rotation strategy.

    Monthly rebalance: always 100% in one ETF.
    """

    def __init__(
        self,
        offensive: list[str],
        defensive: list[str],
    ) -> None:
        self._offensive = offensive
        self._defensive = defensive
        self._all_symbols = offensive + defensive
        self._logger = logging.getLogger("ib_bot.strategy.etf_rotation")

    @property
    def name(self) -> str:
        return "vaa_g4"

    @property
    def all_symbols(self) -> list[str]:
        """All ETF symbols that need historical data."""
        return list(self._all_symbols)

    def should_rebalance(self, dt: date) -> bool:
        """Check if today is a rebalance day.

        Args:
            dt: The date to check.

        Returns:
            True if dt is the last trading day of the month.
        """
        return is_last_trading_day_of_month(dt)

    def evaluate(
        self,
        bars_by_symbol: dict[str, list[dict[str, Any]]],
        reference_date: date,
    ) -> RotationResult:
        """Evaluate all ETFs and decide which one to hold.

        Args:
            bars_by_symbol: Dict of symbol -> list of daily bar dicts,
                each with 'date' and 'close' keys.
            reference_date: The current date for evaluation.

        Returns:
            RotationResult with the recommendation.

        Raises:
            ValueError: If bar data is insufficient.
        """
        # Calculate momentum scores for all ETFs
        offensive_scores: list[MomentumScore] = []
        defensive_scores: list[MomentumScore] = []

        for symbol in self._offensive:
            if symbol not in bars_by_symbol:
                raise ValueError(f"Missing bar data for offensive ETF: {symbol}")
            prices = extract_monthly_prices(bars_by_symbol[symbol], reference_date)
            score = compute_momentum(symbol, prices)
            offensive_scores.append(score)
            self._logger.info(
                "Momentum [%s]: score=%.4f (p0=%.2f p1=%.2f p3=%.2f p6=%.2f p12=%.2f)",
                symbol, score.score, score.p0, score.p1, score.p3, score.p6, score.p12,
            )

        for symbol in self._defensive:
            if symbol not in bars_by_symbol:
                raise ValueError(f"Missing bar data for defensive ETF: {symbol}")
            prices = extract_monthly_prices(bars_by_symbol[symbol], reference_date)
            score = compute_momentum(symbol, prices)
            defensive_scores.append(score)
            self._logger.info(
                "Momentum [%s]: score=%.4f (p0=%.2f p1=%.2f p3=%.2f p6=%.2f p12=%.2f)",
                symbol, score.score, score.p0, score.p1, score.p3, score.p6, score.p12,
            )

        # Apply VAA-G4 decision rule
        recommended, is_offensive, all_positive = pick_etf(
            offensive_scores, defensive_scores,
        )

        # Determine action
        state = load_state()
        current_holding = state.get("current_holding")

        if current_holding is None:
            action = RotationAction.BUY
        elif current_holding == recommended:
            action = RotationAction.HOLD
        else:
            action = RotationAction.SWITCH

        self._logger.info(
            "VAA-G4 decision: all_offensive_positive=%s recommended=%s "
            "is_offensive=%s current=%s action=%s",
            all_positive, recommended, is_offensive, current_holding, action.value,
        )

        return RotationResult(
            recommended_etf=recommended,
            action=action,
            current_holding=current_holding,
            is_offensive=is_offensive,
            offensive_scores=offensive_scores,
            defensive_scores=defensive_scores,
            all_offensive_positive=all_positive,
            rebalance_date=reference_date,
        )

    async def execute_rebalance(
        self,
        result: RotationResult,
        ib_client: Any,
        notifications: Any,
    ) -> None:
        """Execute the rebalance: sell current holding if needed, buy recommended.

        Args:
            result: The RotationResult from evaluate().
            ib_client: IBClient instance for order placement.
            notifications: NotificationService instance.
        """
        from ib_insync import Stock

        if result.action == RotationAction.HOLD:
            self._logger.info(
                "ETF Rotation HOLD: staying in %s", result.recommended_etf,
            )
            msg = (
                f"ETF Rotation: HOLD {result.recommended_etf}\n"
                f"All offensive positive: {result.all_offensive_positive}\n"
                f"No rebalance needed."
            )
            await notifications.send(msg, title="ETF Rotation - HOLD", tags="white_check_mark")
            # Update state with latest rebalance date
            state = load_state()
            state["last_rebalance_date"] = result.rebalance_date.isoformat()
            state["history"].append({
                "date": result.rebalance_date.isoformat(),
                "action": "hold",
                "etf": result.recommended_etf,
            })
            save_state(state)
            return

        # --- SELL current holding if switching ---
        if result.action == RotationAction.SWITCH and result.current_holding:
            self._logger.info(
                "ETF Rotation SELL: liquidating %s", result.current_holding,
            )
            try:
                # Flatten position using IB
                for pos in ib_client.ib.positions():
                    if pos.contract.symbol == result.current_holding and pos.position != 0:
                        contract = Stock(
                            result.current_holding, "SMART", "USD",
                        )
                        await ib_client.ib.qualifyContractsAsync(contract)
                        from ib_insync import MarketOrder
                        action = "SELL" if pos.position > 0 else "BUY"
                        qty = abs(int(pos.position))
                        order = MarketOrder(action=action, totalQuantity=qty)
                        trade = ib_client.ib.placeOrder(contract, order)
                        self._logger.info(
                            "Sold %s: %s x%d", result.current_holding, action, qty,
                        )
                        # Wait for fill
                        await asyncio.sleep(2)
            except Exception as e:
                self._logger.error(
                    "Failed to sell %s: %s", result.current_holding, e, exc_info=True,
                )
                await notifications.send(
                    f"ETF Rotation ERROR: failed to sell {result.current_holding}: {e}",
                    title="ETF Rotation - ERROR",
                    tags="warning",
                )
                return

        # --- BUY recommended ETF ---
        self._logger.info(
            "ETF Rotation BUY: purchasing %s", result.recommended_etf,
        )
        try:
            # Get account value to determine how many shares to buy
            account_values = ib_client.ib.accountSummary()
            net_liq = Decimal("0")
            for av in account_values:
                if av.tag == "NetLiquidation" and av.currency == "USD":
                    net_liq = Decimal(str(av.value))
                    break

            if net_liq <= 0:
                self._logger.error("Cannot determine account value for sizing")
                await notifications.send(
                    "ETF Rotation ERROR: cannot determine account value",
                    title="ETF Rotation - ERROR",
                    tags="warning",
                )
                return

            # Get current price of the recommended ETF
            contract = Stock(result.recommended_etf, "SMART", "USD")
            await ib_client.ib.qualifyContractsAsync(contract)

            # Request a snapshot to get current price
            ticker = ib_client.ib.reqMktData(contract, snapshot=True)
            await asyncio.sleep(3)  # Wait for snapshot data

            price = ticker.marketPrice()
            if price != price or price <= 0:  # NaN check
                # Fall back to last close
                bars = await ib_client.ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime="",
                    durationStr="2 D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    keepUpToDate=False,
                )
                if bars:
                    price = bars[-1].close
                else:
                    raise ValueError(f"Cannot get price for {result.recommended_etf}")

            ib_client.ib.cancelMktData(contract)

            # Calculate shares: use ~99% of net liquidation to leave room for commissions
            usable = float(net_liq) * 0.99
            shares = int(usable / price)

            if shares <= 0:
                self._logger.error(
                    "Computed 0 shares for %s at $%.2f with $%.2f account",
                    result.recommended_etf, price, float(net_liq),
                )
                return

            # Place market order
            from ib_insync import MarketOrder
            order = MarketOrder(action="BUY", totalQuantity=shares)
            trade = ib_client.ib.placeOrder(contract, order)

            self._logger.info(
                "ETF Rotation BUY: %s x%d shares @ ~$%.2f (account $%.2f)",
                result.recommended_etf, shares, price, float(net_liq),
            )

            # Notification
            offensive_str = " | ".join(
                f"{s.symbol}: {s.score:+.4f}" for s in result.offensive_scores
            )
            defensive_str = " | ".join(
                f"{s.symbol}: {s.score:+.4f}" for s in result.defensive_scores
            )

            msg = (
                f"ETF Rotation {result.action.value.upper()}: "
                f"{'SELL ' + (result.current_holding or '') + ' -> ' if result.action == RotationAction.SWITCH else ''}"
                f"BUY {result.recommended_etf} x{shares} @ ${price:.2f}\n"
                f"\n"
                f"Universe: {'OFFENSIVE' if result.is_offensive else 'DEFENSIVE'}\n"
                f"All offensive positive: {result.all_offensive_positive}\n"
                f"\n"
                f"Offensive: {offensive_str}\n"
                f"Defensive: {defensive_str}\n"
                f"\n"
                f"Account: ${float(net_liq):,.2f}"
            )
            await notifications.send(
                msg,
                title=f"ETF Rotation - {result.action.value.upper()}",
                tags="chart_with_upwards_trend",
            )

            # Update state
            state = load_state()
            state["current_holding"] = result.recommended_etf
            state["last_rebalance_date"] = result.rebalance_date.isoformat()
            state["history"].append({
                "date": result.rebalance_date.isoformat(),
                "action": result.action.value,
                "from_etf": result.current_holding,
                "to_etf": result.recommended_etf,
                "shares": shares,
                "price": float(price),
            })
            save_state(state)

        except Exception as e:
            self._logger.error(
                "Failed to buy %s: %s", result.recommended_etf, e, exc_info=True,
            )
            await notifications.send(
                f"ETF Rotation ERROR: failed to buy {result.recommended_etf}: {e}",
                title="ETF Rotation - ERROR",
                tags="warning",
            )


async def fetch_etf_bars(
    ib_client: Any,
    symbols: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Fetch 13 months of daily bars for all ETFs from IB.

    Uses Stock contracts on SMART exchange.

    Args:
        ib_client: IBClient instance (connected).
        symbols: List of ETF ticker symbols.

    Returns:
        Dict of symbol -> list of bar dicts with 'date' and 'close' keys.
    """
    from ib_insync import Stock

    bars_by_symbol: dict[str, list[dict[str, Any]]] = {}

    for symbol in symbols:
        logger.info("Fetching daily bars for %s...", symbol)

        contract = Stock(symbol, "SMART", "USD")
        await ib_client.ib.qualifyContractsAsync(contract)

        ib_bars = await ib_client.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr="13 M",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            keepUpToDate=False,
        )

        if not ib_bars:
            raise ValueError(f"No daily bars returned for {symbol}")

        bar_list: list[dict[str, Any]] = []
        for b in ib_bars:
            bar_date = b.date if hasattr(b.date, "year") else b.date.date()
            bar_list.append({
                "date": bar_date,
                "close": float(b.close),
            })

        bars_by_symbol[symbol] = bar_list
        logger.info(
            "Fetched %d daily bars for %s (%s to %s)",
            len(bar_list), symbol,
            bar_list[0]["date"] if bar_list else "?",
            bar_list[-1]["date"] if bar_list else "?",
        )

        # Small delay between requests to avoid rate limiting
        await asyncio.sleep(1)

    return bars_by_symbol
