"""
SPY Credit Put Spread Strategy for IB Bot.

Sells bull put credit spreads on SPY every 2 weeks:
- Short leg: ~20 delta put
- Long leg: $5 below short leg
- DTE at entry: ~45 days
- Exits: 50% profit, 2x loss, 21 DTE time exit, 0.30 delta breach
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("ib_bot.strategy.options_spreads")

# ============================================================================
# Data Models
# ============================================================================


class SpreadDefinition(BaseModel):
    """Defines a credit put spread before entry."""

    underlying: str
    expiry: str  # YYYYMMDD
    short_strike: float
    long_strike: float
    short_delta: float
    estimated_credit: float  # per share (multiply by 100 for per-contract)
    dte: int


class OpenSpread(BaseModel):
    """An open credit put spread position being tracked."""

    spread_id: str
    underlying: str
    expiry: str  # YYYYMMDD
    short_strike: float
    long_strike: float
    entry_date: str  # YYYY-MM-DD
    credit_received: float  # total credit per contract (already x100)
    status: str = "open"  # open, closed
    close_reason: str = ""
    close_pnl: float = 0.0
    ib_order_id: Optional[int] = None


class SpreadStateFile(BaseModel):
    """Persistent state for the options spread strategy."""

    open_positions: list[OpenSpread] = Field(default_factory=list)
    closed_positions: list[OpenSpread] = Field(default_factory=list)
    last_entry_date: Optional[str] = None  # YYYY-MM-DD


# ============================================================================
# Strategy
# ============================================================================


class CreditSpreadStrategy:
    """
    Automated SPY Bull Put Credit Spread strategy.

    Sells put spreads every 2 weeks on Tuesdays, manages exits based on
    P&L targets, time decay, and delta thresholds.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._underlying: str = config.get("underlying", "SPY")
        self._spread_width: float = config.get("spread_width", 5.0)
        self._target_delta: float = config.get("target_delta", 0.20)
        self._target_dte: int = config.get("target_dte", 45)
        self._profit_target_pct: float = config.get("profit_target_pct", 50.0)
        self._stop_loss_mult: float = config.get("stop_loss_mult", 2.0)
        self._dte_exit: int = config.get("dte_exit", 21)
        self._delta_exit: float = config.get("delta_exit", 0.30)
        self._max_positions: int = config.get("max_positions", 3)
        self._entry_frequency_days: int = config.get("entry_frequency_days", 14)
        self._enabled: bool = config.get("enabled", False)

        # State file path
        self._state_path = Path(__file__).parent.parent / "data" / "spread_state.json"
        self._state = self._load_state()

        logger.info(
            "CreditSpreadStrategy initialized: %s width=$%s delta=%.2f dte=%d",
            self._underlying,
            self._spread_width,
            self._target_delta,
            self._target_dte,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def open_positions(self) -> list[OpenSpread]:
        return self._state.open_positions

    # ========================================================================
    # State Persistence
    # ========================================================================

    def _load_state(self) -> SpreadStateFile:
        """Load state from JSON file."""
        if self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text())
                return SpreadStateFile(**data)
            except Exception as e:
                logger.error("Failed to load spread state: %s", e)
        return SpreadStateFile()

    def _save_state(self) -> None:
        """Persist state to JSON file."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            self._state.model_dump_json(indent=2)
        )
        logger.debug("Spread state saved to %s", self._state_path)

    # ========================================================================
    # Entry Logic
    # ========================================================================

    def should_enter(self, today: date) -> bool:
        """
        Check if today is a valid entry day.

        Rules:
        - Must be a Tuesday (weekday == 1)
        - At least entry_frequency_days since last entry
        - Less than max_positions open
        """
        if not self._enabled:
            return False

        # Must be Tuesday
        if today.weekday() != 1:
            logger.debug("Not Tuesday (weekday=%d), skipping entry check", today.weekday())
            return False

        # Check max positions
        if len(self._state.open_positions) >= self._max_positions:
            logger.info(
                "Max positions reached (%d/%d), skipping entry",
                len(self._state.open_positions),
                self._max_positions,
            )
            return False

        # Check frequency
        if self._state.last_entry_date:
            last_entry = date.fromisoformat(self._state.last_entry_date)
            days_since = (today - last_entry).days
            if days_since < self._entry_frequency_days:
                logger.info(
                    "Only %d days since last entry (need %d), skipping",
                    days_since,
                    self._entry_frequency_days,
                )
                return False

        return True

    async def find_spread(self, ib: Any) -> Optional[SpreadDefinition]:
        """
        Find a suitable credit put spread using IB market data.

        Steps:
        1. Get SPY option chain parameters (expirations + strikes)
        2. Find expiry closest to target DTE
        3. Request greeks for OTM puts near target delta
        4. Select the put closest to target delta as short leg
        5. Long leg = short_strike - spread_width

        Args:
            ib: ib_insync IB instance (already connected)

        Returns:
            SpreadDefinition if a suitable spread is found, None otherwise.
        """
        from ib_insync import Stock, Option

        underlying_contract = Stock(self._underlying, "SMART", "USD")
        await ib.qualifyContractsAsync(underlying_contract)

        # Get underlying price for reference
        [ticker] = await ib.reqTickersAsync(underlying_contract)
        und_price = ticker.marketPrice()
        if not und_price or und_price != und_price:  # NaN check
            logger.warning("Cannot get %s price, aborting spread search", self._underlying)
            return None
        logger.info("%s price: %.2f", self._underlying, und_price)

        # Get option chain parameters
        chains = await ib.reqSecDefOptParamsAsync(
            underlying_contract.symbol,
            "",  # futFopExchange
            underlying_contract.secType,
            underlying_contract.conId,
        )
        if not chains:
            logger.error("No option chains returned for %s", self._underlying)
            return None

        # Use SMART exchange chain
        smart_chain = None
        for chain in chains:
            if chain.exchange == "SMART":
                smart_chain = chain
                break
        if not smart_chain:
            # Fallback to first chain
            smart_chain = chains[0]
            logger.warning(
                "No SMART chain found, using %s", smart_chain.exchange
            )

        # Find expiry closest to target DTE
        today = date.today()
        target_date = today + timedelta(days=self._target_dte)
        best_expiry: Optional[str] = None
        best_dte_diff = 999

        for exp_str in smart_chain.expirations:
            try:
                exp_date = datetime.strptime(exp_str, "%Y%m%d").date()
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte < 30 or dte > 60:
                # Only consider 30-60 DTE range
                continue
            diff = abs(dte - self._target_dte)
            if diff < best_dte_diff:
                best_dte_diff = diff
                best_expiry = exp_str

        if not best_expiry:
            logger.error("No suitable expiry found near %d DTE", self._target_dte)
            return None

        exp_date = datetime.strptime(best_expiry, "%Y%m%d").date()
        actual_dte = (exp_date - today).days
        logger.info("Selected expiry: %s (%d DTE)", best_expiry, actual_dte)

        # Filter strikes: OTM puts (below current price)
        candidate_strikes = sorted(
            [s for s in smart_chain.strikes if und_price * 0.85 <= s <= und_price * 0.98]
        )
        if not candidate_strikes:
            logger.error("No suitable put strikes found below %.2f", und_price)
            return None

        logger.info(
            "Evaluating %d put strikes from %.1f to %.1f",
            len(candidate_strikes),
            candidate_strikes[0],
            candidate_strikes[-1],
        )

        # Request greeks for candidate puts (batch)
        option_contracts = []
        for strike in candidate_strikes:
            opt = Option(
                self._underlying,
                best_expiry,
                strike,
                "P",
                "SMART",
            )
            option_contracts.append(opt)

        qualified = await ib.qualifyContractsAsync(*option_contracts)
        qualified_opts = [c for c in qualified if c]
        if not qualified_opts:
            logger.error("Could not qualify any put options")
            return None

        # Request market data for greeks
        tickers = await ib.reqTickersAsync(*qualified_opts)

        # Wait briefly for greeks to populate
        await asyncio.sleep(2.0)

        # Find put closest to target delta
        best_opt = None
        best_delta_diff = 999.0
        best_delta = 0.0
        best_strike = 0.0

        for t in tickers:
            if not t.modelGreeks:
                continue
            delta = abs(t.modelGreeks.delta or 0.0)
            if delta < 0.05 or delta > 0.40:
                # Skip very far OTM or near ATM
                continue
            diff = abs(delta - self._target_delta)
            if diff < best_delta_diff:
                best_delta_diff = diff
                best_delta = delta
                best_strike = t.contract.strike
                best_opt = t

        # Cancel market data subscriptions
        for t in tickers:
            ib.cancelMktData(t.contract)

        if not best_opt:
            logger.error("No put found near %.2f delta", self._target_delta)
            return None

        short_strike = best_strike
        long_strike = short_strike - self._spread_width

        # Verify long strike exists in chain
        if long_strike not in smart_chain.strikes:
            # Find nearest available strike
            available_longs = [
                s for s in smart_chain.strikes
                if s < short_strike and s >= short_strike - self._spread_width - 2
            ]
            if not available_longs:
                logger.error(
                    "No long strike available near $%.0f (short=%.0f)",
                    long_strike,
                    short_strike,
                )
                return None
            long_strike = max(available_longs)

        # Estimate credit (short put premium - long put premium)
        # Use the short put's price as approximation; the spread credit
        # will be determined at order fill time
        estimated_credit = 0.0
        if best_opt.bid and best_opt.ask:
            estimated_credit = (best_opt.bid + best_opt.ask) / 2.0
            # Rough estimate: subtract long put value (~40-60% of short put for $5 wide)
            estimated_credit *= 0.45  # Conservative estimate of net credit

        spread = SpreadDefinition(
            underlying=self._underlying,
            expiry=best_expiry,
            short_strike=short_strike,
            long_strike=long_strike,
            short_delta=best_delta,
            estimated_credit=estimated_credit,
            dte=actual_dte,
        )

        logger.info(
            "SPREAD FOUND: %s %s P%.0f/P%.0f delta=%.3f "
            "est_credit=$%.2f/share dte=%d",
            spread.underlying,
            spread.expiry,
            spread.short_strike,
            spread.long_strike,
            spread.short_delta,
            spread.estimated_credit,
            spread.dte,
        )

        return spread

    async def place_spread(
        self,
        ib: Any,
        spread: SpreadDefinition,
    ) -> Optional[OpenSpread]:
        """
        Place a credit put spread as a BAG combo order on IB.

        Uses a limit order at the natural credit (midpoint of bid/ask).

        Args:
            ib: ib_insync IB instance
            spread: Spread definition from find_spread()

        Returns:
            OpenSpread if order placed successfully, None otherwise.
        """
        from ib_insync import Option, Contract, ComboLeg, Order, LimitOrder

        # Qualify both legs
        short_opt = Option(
            self._underlying,
            spread.expiry,
            spread.short_strike,
            "P",
            "SMART",
        )
        long_opt = Option(
            self._underlying,
            spread.expiry,
            spread.long_strike,
            "P",
            "SMART",
        )

        qualified = await ib.qualifyContractsAsync(short_opt, long_opt)
        if len([c for c in qualified if c]) < 2:
            logger.error("Could not qualify spread legs")
            return None

        short_opt = qualified[0]
        long_opt = qualified[1]

        # Build combo (BAG) contract
        combo = Contract()
        combo.symbol = self._underlying
        combo.secType = "BAG"
        combo.currency = "USD"
        combo.exchange = "SMART"

        # Sell short put (higher strike), Buy long put (lower strike)
        combo.comboLegs = [
            ComboLeg(
                conId=short_opt.conId,
                ratio=1,
                action="SELL",
                exchange="SMART",
            ),
            ComboLeg(
                conId=long_opt.conId,
                ratio=1,
                action="BUY",
                exchange="SMART",
            ),
        ]

        # Get combo price (request market data for the combo)
        [combo_ticker] = await ib.reqTickersAsync(combo)
        await asyncio.sleep(1.0)

        # Determine limit price (credit we receive)
        # For credit spreads sold as BAG: positive price = credit received
        credit = 0.0
        if combo_ticker.bid and combo_ticker.ask:
            # Use midpoint as limit price
            credit = round((combo_ticker.bid + combo_ticker.ask) / 2.0, 2)
        elif spread.estimated_credit > 0:
            credit = round(spread.estimated_credit, 2)
        else:
            logger.error("Cannot determine credit for spread")
            ib.cancelMktData(combo)
            return None

        ib.cancelMktData(combo)

        if credit <= 0:
            logger.error("Credit is <= 0 ($%.2f), aborting", credit)
            return None

        # Place limit order to SELL the spread (receive credit)
        order = LimitOrder(
            action="SELL",
            totalQuantity=1,
            lmtPrice=credit,
            tif="DAY",
            outsideRth=False,
        )

        trade = ib.placeOrder(combo, order)

        logger.info(
            "SPREAD ORDER PLACED: SELL P%.0f / BUY P%.0f exp=%s "
            "credit=$%.2f/share ($%.2f total)",
            spread.short_strike,
            spread.long_strike,
            spread.expiry,
            credit,
            credit * 100,
        )

        # Track the position
        today_str = date.today().isoformat()
        spread_id = (
            f"{self._underlying}_"
            f"P{spread.short_strike:.0f}_P{spread.long_strike:.0f}_"
            f"{spread.expiry}"
        )

        open_spread = OpenSpread(
            spread_id=spread_id,
            underlying=self._underlying,
            expiry=spread.expiry,
            short_strike=spread.short_strike,
            long_strike=spread.long_strike,
            entry_date=today_str,
            credit_received=credit * 100,  # Per-contract total
            ib_order_id=trade.order.orderId if trade else None,
        )

        self._state.open_positions.append(open_spread)
        self._state.last_entry_date = today_str
        self._save_state()

        return open_spread

    # ========================================================================
    # Exit Logic
    # ========================================================================

    async def check_exits(
        self, ib: Any
    ) -> list[tuple[OpenSpread, str]]:
        """
        Check all open spreads for exit conditions.

        Exit conditions (checked in order):
        1. Profit target: spread can be bought back at <= 50% of credit
        2. Stop loss: spread cost to close >= 2x credit received
        3. Time exit: DTE <= 21 days
        4. Delta exit: short leg delta >= 0.30

        Args:
            ib: ib_insync IB instance

        Returns:
            List of (spread, reason) tuples for positions that should be closed.
        """
        from ib_insync import Option

        exits: list[tuple[OpenSpread, str]] = []
        today = date.today()

        for pos in self._state.open_positions:
            # Check time exit first (no market data needed)
            try:
                exp_date = datetime.strptime(pos.expiry, "%Y%m%d").date()
            except ValueError:
                logger.error("Invalid expiry format: %s", pos.expiry)
                continue

            remaining_dte = (exp_date - today).days

            if remaining_dte <= self._dte_exit:
                exits.append((pos, f"DTE exit: {remaining_dte} DTE <= {self._dte_exit}"))
                continue

            # Need market data for P&L and delta checks
            try:
                short_opt = Option(
                    pos.underlying,
                    pos.expiry,
                    pos.short_strike,
                    "P",
                    "SMART",
                )
                long_opt = Option(
                    pos.underlying,
                    pos.expiry,
                    pos.long_strike,
                    "P",
                    "SMART",
                )

                qualified = await ib.qualifyContractsAsync(short_opt, long_opt)
                if len([c for c in qualified if c]) < 2:
                    logger.warning(
                        "Cannot qualify legs for %s, skipping exit check",
                        pos.spread_id,
                    )
                    continue

                tickers = await ib.reqTickersAsync(qualified[0], qualified[1])
                await asyncio.sleep(1.5)

                short_ticker, long_ticker = tickers[0], tickers[1]

                # Calculate current spread value (cost to buy back)
                short_mid = 0.0
                long_mid = 0.0

                if short_ticker.bid and short_ticker.ask:
                    short_mid = (short_ticker.bid + short_ticker.ask) / 2.0
                if long_ticker.bid and long_ticker.ask:
                    long_mid = (long_ticker.bid + long_ticker.ask) / 2.0

                # Cost to close = buy back short - sell long (per share)
                close_cost_per_share = short_mid - long_mid
                close_cost = close_cost_per_share * 100  # per contract

                credit = pos.credit_received  # already per contract

                # Cancel market data
                for t in tickers:
                    ib.cancelMktData(t.contract)

                # --- Check profit target ---
                if close_cost <= credit * (self._profit_target_pct / 100.0):
                    pnl = credit - close_cost
                    exits.append((
                        pos,
                        f"Profit target: close_cost=${close_cost:.2f} <= "
                        f"{self._profit_target_pct:.0f}% of credit ${credit:.2f} "
                        f"(P&L: +${pnl:.2f})",
                    ))
                    continue

                # --- Check stop loss ---
                if close_cost >= credit * self._stop_loss_mult:
                    pnl = credit - close_cost
                    exits.append((
                        pos,
                        f"Stop loss: close_cost=${close_cost:.2f} >= "
                        f"{self._stop_loss_mult}x credit ${credit:.2f} "
                        f"(P&L: -${abs(pnl):.2f})",
                    ))
                    continue

                # --- Check delta exit ---
                if short_ticker.modelGreeks:
                    current_delta = abs(short_ticker.modelGreeks.delta or 0.0)
                    if current_delta >= self._delta_exit:
                        exits.append((
                            pos,
                            f"Delta exit: short delta={current_delta:.3f} >= "
                            f"{self._delta_exit}",
                        ))
                        continue

                logger.debug(
                    "SPREAD OK [%s]: cost_to_close=$%.2f credit=$%.2f "
                    "DTE=%d delta=%.3f",
                    pos.spread_id,
                    close_cost,
                    credit,
                    remaining_dte,
                    abs(short_ticker.modelGreeks.delta or 0)
                    if short_ticker.modelGreeks
                    else 0,
                )

            except Exception as e:
                logger.error(
                    "Error checking exits for %s: %s",
                    pos.spread_id,
                    e,
                    exc_info=True,
                )

        return exits

    async def close_spread(
        self,
        ib: Any,
        pos: OpenSpread,
        reason: str,
    ) -> bool:
        """
        Close an open spread by buying it back.

        Places a market order on the combo to close.

        Args:
            ib: ib_insync IB instance
            pos: The open spread to close
            reason: Why we are closing

        Returns:
            True if order placed successfully.
        """
        from ib_insync import Option, Contract, ComboLeg, MarketOrder

        short_opt = Option(
            pos.underlying,
            pos.expiry,
            pos.short_strike,
            "P",
            "SMART",
        )
        long_opt = Option(
            pos.underlying,
            pos.expiry,
            pos.long_strike,
            "P",
            "SMART",
        )

        try:
            qualified = await ib.qualifyContractsAsync(short_opt, long_opt)
            if len([c for c in qualified if c]) < 2:
                logger.error("Cannot qualify legs for closing %s", pos.spread_id)
                return False

            short_opt = qualified[0]
            long_opt = qualified[1]

            # Build closing combo: BUY back the spread
            combo = Contract()
            combo.symbol = pos.underlying
            combo.secType = "BAG"
            combo.currency = "USD"
            combo.exchange = "SMART"
            combo.comboLegs = [
                ComboLeg(
                    conId=short_opt.conId,
                    ratio=1,
                    action="BUY",   # Buy back short put
                    exchange="SMART",
                ),
                ComboLeg(
                    conId=long_opt.conId,
                    ratio=1,
                    action="SELL",  # Sell long put
                    exchange="SMART",
                ),
            ]

            # Use market order for exits (ensure fill)
            order = MarketOrder(action="BUY", totalQuantity=1)
            trade = ib.placeOrder(combo, order)

            logger.info(
                "SPREAD CLOSED [%s]: %s | reason=%s",
                pos.spread_id,
                "BUY to close",
                reason,
            )

            # Update state
            pos.status = "closed"
            pos.close_reason = reason
            self._state.open_positions = [
                p for p in self._state.open_positions
                if p.spread_id != pos.spread_id
            ]
            self._state.closed_positions.append(pos)
            self._save_state()

            return True

        except Exception as e:
            logger.error(
                "Failed to close spread %s: %s",
                pos.spread_id,
                e,
                exc_info=True,
            )
            return False

    # ========================================================================
    # Status / Reporting
    # ========================================================================

    def status_report(self) -> str:
        """Generate a human-readable status report."""
        lines = [
            f"=== Credit Spread Strategy ({self._underlying}) ===",
            f"Open positions: {len(self._state.open_positions)}/{self._max_positions}",
        ]

        if self._state.last_entry_date:
            lines.append(f"Last entry: {self._state.last_entry_date}")

        for pos in self._state.open_positions:
            try:
                exp_date = datetime.strptime(pos.expiry, "%Y%m%d").date()
                dte = (exp_date - date.today()).days
            except ValueError:
                dte = -1
            lines.append(
                f"  {pos.spread_id}: credit=${pos.credit_received:.2f} "
                f"DTE={dte} entered={pos.entry_date}"
            )

        closed_count = len(self._state.closed_positions)
        if closed_count > 0:
            total_pnl = sum(p.close_pnl for p in self._state.closed_positions)
            lines.append(f"Closed trades: {closed_count} (total P&L: ${total_pnl:.2f})")

        return "\n".join(lines)

    def reset_daily(self) -> None:
        """No daily reset needed for this strategy (positions span weeks)."""
        pass
