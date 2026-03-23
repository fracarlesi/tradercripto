"""
Prompt Builder — Market State to Structured Text
=================================================

Converts raw candle data and portfolio state into the structured prompt
format described in FLAG-Trader paper (Figure 3).
"""

from __future__ import annotations

import json
import re
from typing import Any


class PromptBuilder:
    """Builds structured prompts for the LLM trading agent."""

    # Canonical action mapping
    ACTION_MAP: dict[str, int] = {"sell": 0, "hold": 1, "buy": 2}

    def __init__(self, candle_window: int = 20, decimal_places: int = 4) -> None:
        self.candle_window = candle_window
        self.decimal_places = decimal_places

    def build_prompt(
        self,
        candles: list[dict[str, float]],
        portfolio: dict[str, float],
        history: dict[str, list[Any]],
        similar_trades_text: str = "",
        position_info: dict[str, Any] | None = None,
    ) -> str:
        """Build a structured prompt from market state.

        Args:
            candles: List of dicts with keys: open, high, low, close, volume.
                     Most recent candle last. Will be trimmed to candle_window.
            portfolio: Dict with cash_balance, asset_position, total_account_value.
            history: Dict with recent_rewards, net_values, actions (lists).

        Returns:
            Formatted prompt string following FLAG-Trader Figure 3.
        """
        trimmed = candles[-self.candle_window :]
        normalized = self._normalize_candles(trimmed)

        prompt = (
            "Task: You are a cryptocurrency trading agent. Your goal is to maximize "
            "long-term risk-adjusted returns. Choose optimal buy, sell, or hold "
            "decisions based on market conditions and risk assessment.\n\n"
            "Legible Actions: Choose from {Buy, Sell, Hold}\n\n"
            "Current State:\n"
        )

        state = {
            "historical_prices": normalized,
            "account_status": {
                "cash_balance": round(portfolio.get("cash_balance", 0.0), self.decimal_places),
                "asset_position": round(portfolio.get("asset_position", 0.0), self.decimal_places),
                "total_account_value": round(
                    portfolio.get("total_account_value", 0.0), self.decimal_places
                ),
            },
            "previous_decision_metrics": {
                "recent_rewards": [
                    round(r, self.decimal_places) for r in (history.get("recent_rewards") or [])[-10:]
                ],
                "net_values": [
                    round(v, self.decimal_places) for v in (history.get("net_values") or [])[-10:]
                ],
                "actions": (history.get("actions") or [])[-10:],
            },
        }

        prompt += json.dumps(state, indent=2)
        if similar_trades_text:
            prompt += "\n\n" + similar_trades_text
        if position_info:
            direction = position_info.get("direction", "long").upper()
            symbol = position_info.get("symbol", "?")
            entry_price = position_info.get("entry_price", 0.0)
            pnl_pct = position_info.get("pnl_pct", 0.0)
            prompt += (
                f"\n\nCurrent Position:\n"
                f"You are currently {direction} {symbol} from ${entry_price:.2f}, "
                f"unrealized PnL: {pnl_pct:+.1f}%\n"
                f"Consider whether to maintain or reverse your position."
            )
        prompt += (
            '\n\nOutput Action: Format your answer as JSON: '
            '{"Action": "Buy"}, {"Action": "Sell"}, or {"Action": "Hold"}'
        )
        return prompt

    def _normalize_candles(
        self, candles: list[dict[str, float]]
    ) -> list[dict[str, float]]:
        """Normalize prices as pct change from the first candle's close."""
        if not candles:
            return []

        base_close = candles[0].get("close", 1.0)
        if base_close == 0:
            base_close = 1.0

        dp = self.decimal_places
        normalized: list[dict[str, float]] = []
        for c in candles:
            normalized.append(
                {
                    "open": round((c["open"] / base_close - 1.0), dp),
                    "high": round((c["high"] / base_close - 1.0), dp),
                    "low": round((c["low"] / base_close - 1.0), dp),
                    "close": round((c["close"] / base_close - 1.0), dp),
                    "volume": round(c["volume"], dp),
                }
            )
        return normalized

    def parse_action(self, llm_output: str) -> int:
        """Extract action from LLM output.

        Handles various formats:
            - {"Action": "Buy"}
            - "Buy"
            - Action: Buy
            - buy / SELL / Hold (case-insensitive)
            - <think>...</think> {"Action": "Buy"} (Qwen thinking mode)

        Returns:
            0 = Sell, 1 = Hold, 2 = Buy.
            Defaults to 1 (Hold) if parsing fails.
        """
        text = llm_output.strip()

        # Strip Qwen-style thinking blocks
        think_match = re.search(r"</think>\s*", text)
        if think_match:
            text = text[think_match.end():].strip()

        # Try JSON parse first
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                action_str = str(parsed.get("Action", parsed.get("action", "")))
                action_lower = action_str.lower().strip()
                if action_lower in self.ACTION_MAP:
                    return self.ACTION_MAP[action_lower]
        except (json.JSONDecodeError, ValueError):
            pass

        # Try regex for JSON-like pattern inside text
        json_match = re.search(r'["\']?[Aa]ction["\']?\s*:\s*["\']?(\w+)["\']?', text)
        if json_match:
            action_lower = json_match.group(1).lower()
            if action_lower in self.ACTION_MAP:
                return self.ACTION_MAP[action_lower]

        # Try bare keyword
        text_lower = text.lower()
        for keyword in ("sell", "buy", "hold"):
            if keyword in text_lower:
                return self.ACTION_MAP[keyword]

        # Default to Hold
        return 1
