"""
Gymnasium Trading Environment
==============================

Simulates trading on Hyperliquid historical candles.

State: Last N candles (OHLCV) + portfolio (cash, position, unrealized PnL, total value)
Action: Discrete(3) — 0=Sell, 1=Hold, 2=Buy
Reward: Delta of Sharpe ratio (SR_t - SR_{t-1})
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from .reward import compute_sharpe_delta


class HyperliquidTradingEnv(gym.Env):
    """Gymnasium environment for simulated crypto trading.

    Args:
        candles: np.ndarray of shape (num_candles, 5) — columns: O, H, L, C, V.
        initial_cash: Starting cash balance.
        transaction_cost_bps: Transaction cost in basis points (default 5 = 0.05%).
        window_size: Number of historical candles in the observation.
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        candles: np.ndarray,
        initial_cash: float = 1000.0,
        transaction_cost_bps: float = 5.0,
        window_size: int = 20,
        reward_fn: Callable[..., float] | None = None,
    ) -> None:
        super().__init__()
        assert candles.ndim == 2 and candles.shape[1] == 5, (
            "candles must be shape (N, 5) with columns [O, H, L, C, V]"
        )
        assert candles.shape[0] > window_size, (
            f"Need more candles ({candles.shape[0]}) than window_size ({window_size})"
        )

        self.candles = candles.astype(np.float32)
        self.initial_cash = initial_cash
        self.tx_cost_pct = transaction_cost_bps / 10_000.0
        self.window_size = window_size
        self.reward_history_len = 10
        self._reward_fn: Callable[..., float] = reward_fn or compute_sharpe_delta

        # Action: 0=Sell, 1=Hold, 2=Buy
        self.action_space = spaces.Discrete(3)

        # Observation: dict of candles + portfolio + history
        self.observation_space = spaces.Dict(
            {
                "candles": spaces.Box(
                    low=-np.inf,
                    high=np.inf,
                    shape=(window_size, 8),
                    dtype=np.float32,
                ),
                "portfolio": spaces.Box(
                    low=-np.inf,
                    high=np.inf,
                    shape=(4,),
                    dtype=np.float32,
                ),
                "history": spaces.Box(
                    low=-np.inf,
                    high=np.inf,
                    shape=(self.reward_history_len,),
                    dtype=np.float32,
                ),
            }
        )

        # Internal state (set in reset)
        self._step_idx: int = 0
        self._cash: float = 0.0
        self._position: float = 0.0  # units of asset held
        self._entry_price: float = 0.0
        self._pnl_history: list[float] = []
        self._reward_history: list[float] = []
        self._total_value_prev: float = 0.0

        # Dynamic TP/SL from model
        self._tp_pct: float = 2.5  # default TP %
        self._sl_pct: float = 1.0  # default SL %
        self._tp_price: float = 0.0
        self._sl_price: float = 0.0

    def reset(
        self,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
        """Reset environment to initial state."""
        super().reset(seed=seed)
        self._step_idx = self.window_size
        self._cash = self.initial_cash
        self._position = 0.0
        self._entry_price = 0.0
        self._pnl_history = []
        self._reward_history = []
        self._total_value_prev = self.initial_cash
        self._tp_pct = 2.5
        self._sl_pct = 1.0
        self._tp_price = 0.0
        self._sl_price = 0.0

        obs = self._get_obs()
        info = {"total_value": self.initial_cash, "step": 0}
        return obs, info

    def set_tp_sl(self, tp_pct: float, sl_pct: float) -> None:
        """Set TP/SL for next trade. Called before step()."""
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct

    def step(
        self, action: int
    ) -> tuple[dict[str, np.ndarray], float, bool, bool, dict[str, Any]]:
        """Execute one trading step.

        Args:
            action: 0=Sell, 1=Hold, 2=Buy.

        Returns:
            (observation, reward, terminated, truncated, info)
        """
        current_close = float(self.candles[self._step_idx, 3])  # Close price
        current_high = float(self.candles[self._step_idx, 1])  # High
        current_low = float(self.candles[self._step_idx, 2])   # Low
        executed_action = action

        # Check TP/SL hit on current candle (for open positions)
        step_pnl = 0.0
        if self._position > 0:
            if self._tp_price > 0 and current_high >= self._tp_price:
                # TP hit — force sell at TP price
                gross = self._position * self._tp_price
                cost = gross * self.tx_cost_pct
                proceeds = gross - cost
                step_pnl = proceeds - (self._position * self._entry_price)
                self._cash = proceeds
                self._position = 0.0
                self._entry_price = 0.0
                self._tp_price = 0.0
                self._sl_price = 0.0
                executed_action = 1  # Treat as hold for the rest of step
                # Skip normal trade execution
                return self._finalize_step(step_pnl, executed_action, current_close)
            elif self._sl_price > 0 and current_low <= self._sl_price:
                # SL hit — force sell at SL price
                gross = self._position * self._sl_price
                cost = gross * self.tx_cost_pct
                proceeds = gross - cost
                step_pnl = proceeds - (self._position * self._entry_price)
                self._cash = proceeds
                self._position = 0.0
                self._entry_price = 0.0
                self._tp_price = 0.0
                self._sl_price = 0.0
                executed_action = 1
                return self._finalize_step(step_pnl, executed_action, current_close)

        # Action masking: enforce valid actions
        if action == 2 and self._position > 0:
            # Already in position, can't buy again — treat as Hold
            executed_action = 1
        elif action == 0 and self._position <= 0:
            # No position to sell — treat as Hold
            executed_action = 1

        # Execute trade
        step_pnl = 0.0
        if executed_action == 2:  # Buy
            cost = self._cash * self.tx_cost_pct
            investable = self._cash - cost
            self._position = investable / current_close
            self._entry_price = current_close
            self._cash = 0.0
            # Set TP/SL prices
            self._tp_price = current_close * (1 + self._tp_pct / 100)
            self._sl_price = current_close * (1 - self._sl_pct / 100)
        elif executed_action == 0:  # Sell
            gross = self._position * current_close
            cost = gross * self.tx_cost_pct
            proceeds = gross - cost
            step_pnl = proceeds - (self._position * self._entry_price)
            self._cash = proceeds
            self._position = 0.0
            self._entry_price = 0.0
            self._tp_price = 0.0
            self._sl_price = 0.0

        return self._finalize_step(step_pnl, executed_action, current_close)

    def _finalize_step(
        self,
        step_pnl: float,
        executed_action: int,
        current_close: float,
    ) -> tuple[dict[str, np.ndarray], float, bool, bool, dict[str, Any]]:
        """Calculate portfolio value, reward, advance step, and return transition."""
        # Calculate portfolio value
        unrealized_pnl = 0.0
        position_value = 0.0
        if self._position > 0:
            position_value = self._position * current_close
            unrealized_pnl = position_value - (self._position * self._entry_price)

        total_value = self._cash + position_value

        # PnL for this step (mark-to-market change)
        mtm_pnl = total_value - self._total_value_prev
        self._pnl_history.append(mtm_pnl)
        self._total_value_prev = total_value

        # Reward via configurable reward function
        trade_opened = executed_action == 2
        trade_closed_profit: bool | None = None
        if step_pnl != 0.0:
            trade_closed_profit = step_pnl > 0

        try:
            reward = self._reward_fn(
                self._pnl_history,
                trade_opened=trade_opened,
                trade_closed_profit=trade_closed_profit,
            )
        except TypeError:
            # Fallback for reward functions that don't accept extra kwargs
            reward = self._reward_fn(self._pnl_history)
        self._reward_history.append(reward)

        # Advance
        self._step_idx += 1
        terminated = self._step_idx >= len(self.candles)
        truncated = False

        obs = self._get_obs() if not terminated else self._get_terminal_obs()

        info = {
            "total_value": total_value,
            "cash": self._cash,
            "position": self._position,
            "unrealized_pnl": unrealized_pnl,
            "step_pnl": step_pnl,
            "mtm_pnl": mtm_pnl,
            "executed_action": executed_action,
            "step": self._step_idx - self.window_size,
        }

        return obs, float(reward), terminated, truncated, info

    def _get_obs(self) -> dict[str, np.ndarray]:
        """Build observation dict."""
        # Candle window normalized by first candle's close
        raw = self.candles[self._step_idx - self.window_size : self._step_idx].copy()
        base_close = raw[0, 3]
        if base_close > 0:
            raw[:, :4] = raw[:, :4] / base_close - 1.0  # Normalize OHLC
            raw[:, 4] = raw[:, 4] / (raw[:, 4].mean() + 1e-12)  # Normalize volume

        # Compute proxy features and concatenate: (N, 5) -> (N, 8)
        candles_enriched = self._compute_proxy_features(raw)

        # Portfolio state
        current_close = float(self.candles[min(self._step_idx, len(self.candles) - 1), 3])
        position_value = self._position * current_close
        unrealized_pnl = position_value - (self._position * self._entry_price) if self._position > 0 else 0.0
        total_value = self._cash + position_value
        normalizer = max(self.initial_cash, 1e-12)

        portfolio = np.array(
            [
                self._cash / normalizer,
                position_value / normalizer,
                unrealized_pnl / normalizer,
                total_value / normalizer,
            ],
            dtype=np.float32,
        )

        # Reward history (padded)
        rh = self._reward_history[-self.reward_history_len :]
        padded = [0.0] * (self.reward_history_len - len(rh)) + rh
        history = np.array(padded, dtype=np.float32)

        return {"candles": candles_enriched, "portfolio": portfolio, "history": history}

    def _get_terminal_obs(self) -> dict[str, np.ndarray]:
        """Return a valid observation for terminal state."""
        return {
            "candles": np.zeros((self.window_size, 8), dtype=np.float32),
            "portfolio": np.zeros(4, dtype=np.float32),
            "history": np.zeros(self.reward_history_len, dtype=np.float32),
        }

    def _compute_proxy_features(self, raw: np.ndarray) -> np.ndarray:
        """Compute 3 proxy features from OHLCV candles and concatenate.

        Args:
            raw: Already-normalized candle array of shape (N, 5) with columns
                 [open, high, low, close, volume].

        Returns:
            Array of shape (N, 8) with columns
            [O, H, L, C, V, funding_proxy, oi_proxy, vol_delta_proxy].
        """
        open_ = raw[:, 0]
        high = raw[:, 1]
        low = raw[:, 2]
        close = raw[:, 3]
        volume = raw[:, 4]

        # funding_proxy: (close - vwap) / close, where vwap ≈ (H+L+C)/3
        # Already normalized OHLC (pct-change from base), so close can be ~0.
        # Use close + 1 to avoid division by zero (since normalized close = price/base - 1).
        vwap = (high + low + close) / 3.0
        denom_funding = np.where(np.abs(close) > 1e-8, close, 1e-8)
        funding_proxy = (close - vwap) / denom_funding
        # Clip to (-1, 1) for stability
        funding_proxy = np.clip(funding_proxy, -1.0, 1.0)

        # oi_proxy: volume * (close + 1) as notional proxy, normalized like volume
        # close is normalized (pct change), so close+1 ≈ price_ratio
        notional = volume * (close + 1.0)
        notional_mean = notional.mean() + 1e-12
        oi_proxy = notional / notional_mean

        # vol_delta_proxy: (close - open) / max(high - low, eps) * volume
        candle_range = np.maximum(high - low, 1e-8)
        vol_delta = (close - open_) / candle_range * volume
        # Normalize like volume (divide by mean absolute value)
        vol_delta_mean = np.abs(vol_delta).mean() + 1e-12
        vol_delta_proxy = vol_delta / vol_delta_mean

        # Stack: (N, 3)
        proxies = np.stack([funding_proxy, oi_proxy, vol_delta_proxy], axis=1).astype(np.float32)
        return np.concatenate([raw, proxies], axis=1)
