"""
ATR Percentile Filter
======================

Maintains a rolling window of daily OR-period ATR values and filters out
days where volatility is in the extreme percentiles (too quiet or too volatile).

Persistence: stores ATR history in data/atr_history.json for cross-restart continuity.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path


from ..config.loader import ATRFilterConfig

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_ATR_FILE = _DATA_DIR / "atr_history.json"


class ATRFilter:
    """Rolling ATR percentile filter for daily trade gating.

    After the Opening Range is detected, call ``record_and_check()``
    with today's OR-period ATR value.  The method returns True if
    trading is allowed, False if the day should be skipped.
    """

    def __init__(self, config: ATRFilterConfig) -> None:
        self._config = config
        self._history: list[float] = []
        self._today_checked = False
        self._today_allowed = True

        if config.enabled:
            self._load_history()

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    @property
    def is_enabled(self) -> bool:
        return self._config.enabled

    @property
    def today_allowed(self) -> bool:
        """Whether trading is allowed today (only valid after record_and_check)."""
        return self._today_allowed

    @property
    def today_checked(self) -> bool:
        """Whether the ATR filter has been evaluated today."""
        return self._today_checked

    def record_and_check(self, atr_value: Decimal) -> bool:
        """Record today's ATR and check if it passes the percentile filter.

        Args:
            atr_value: ATR computed from OR-window bars.

        Returns:
            True if trading is allowed, False if the day should be skipped.
        """
        if not self._config.enabled:
            self._today_checked = True
            self._today_allowed = True
            return True

        atr_f = float(atr_value)
        if atr_f <= 0:
            logger.warning("ATR filter: invalid ATR value %.6f, allowing trade", atr_f)
            self._today_checked = True
            self._today_allowed = True
            return True

        # Add to history
        self._history.append(atr_f)

        # Trim to lookback window
        max_len = self._config.lookback_days
        if len(self._history) > max_len:
            self._history = self._history[-max_len:]

        # Persist
        self._save_history()

        # Need minimum history for meaningful percentile
        if len(self._history) < 5:
            logger.info(
                "ATR filter: only %d days of history (need 5), allowing trade",
                len(self._history),
            )
            self._today_checked = True
            self._today_allowed = True
            return True

        # Compute percentile thresholds
        sorted_atrs = sorted(self._history)
        n = len(sorted_atrs)
        low_idx = int(n * self._config.low_percentile / 100.0)
        high_idx = int(n * self._config.high_percentile / 100.0)
        high_idx = min(high_idx, n - 1)

        low_threshold = sorted_atrs[low_idx]
        high_threshold = sorted_atrs[high_idx]

        allowed = low_threshold <= atr_f <= high_threshold

        self._today_checked = True
        self._today_allowed = allowed

        if allowed:
            logger.info(
                "ATR filter PASS: ATR=%.4f in range [%.4f, %.4f] "
                "(p%d-p%d of %d days)",
                atr_f, low_threshold, high_threshold,
                int(self._config.low_percentile),
                int(self._config.high_percentile),
                n,
            )
        else:
            reason = "too low" if atr_f < low_threshold else "too high"
            logger.warning(
                "ATR filter SKIP: ATR=%.4f %s — range [%.4f, %.4f] "
                "(p%d-p%d of %d days). No trades today.",
                atr_f, reason, low_threshold, high_threshold,
                int(self._config.low_percentile),
                int(self._config.high_percentile),
                n,
            )

        return allowed

    def reset_daily(self) -> None:
        """Reset the daily check flag (call at start of each new session)."""
        self._today_checked = False
        self._today_allowed = True

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------

    def _load_history(self) -> None:
        """Load ATR history from JSON file."""
        if not _ATR_FILE.exists():
            logger.info("ATR filter: no history file, starting fresh")
            return

        try:
            with open(_ATR_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                self._history = [float(v) for v in data]
                # Trim to lookback
                max_len = self._config.lookback_days
                if len(self._history) > max_len:
                    self._history = self._history[-max_len:]
                logger.info(
                    "ATR filter: loaded %d days of history", len(self._history)
                )
            else:
                logger.warning("ATR filter: invalid history format, starting fresh")
        except (json.JSONDecodeError, ValueError, OSError) as e:
            logger.warning("ATR filter: failed to load history: %s", e)

    def _save_history(self) -> None:
        """Persist ATR history to JSON file."""
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(_ATR_FILE, "w") as f:
                json.dump(self._history, f, indent=2)
        except OSError as e:
            logger.warning("ATR filter: failed to save history: %s", e)
