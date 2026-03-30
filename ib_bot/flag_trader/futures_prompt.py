"""
Futures Prompt Builder — Market State to Structured Text for US Futures
========================================================================

Adapts EquityPromptBuilder for US futures intraday trading.
Key differences:
- Preamble: "US futures trading agent"
- Adds session phase (pre-market, regular, after-hours)
- Adds volume profile context
- Tighter risk parameters
"""

from __future__ import annotations

import json
from typing import Any

from .equity_prompt import EquityPromptBuilder


class FuturesPromptBuilder(EquityPromptBuilder):
    """Builds structured prompts for the futures LLM trading agent.

    Extends EquityPromptBuilder with futures-specific context:
    - Session phase awareness (pre-market, regular, after-hours)
    - Volume profile (POC, value area)
    """

    def build_prompt(
        self,
        candles: list[dict[str, float]],
        portfolio: dict[str, float],
        history: dict[str, list[Any]],
        similar_trades_text: str = "",
        position_info: dict[str, Any] | None = None,
        market_context: dict | list[dict] | None = None,
        sector: str | None = None,
        session_phase: str | None = None,
        volume_profile: dict[str, float] | None = None,
    ) -> str:
        """Build a structured prompt for futures trading.

        Args:
            candles: List of OHLCV dicts. Most recent last.
            portfolio: Account state dict.
            history: Recent trading history.
            similar_trades_text: RAG-injected similar past trades.
            position_info: Current position info for exit evaluation.
            market_context: Longer timeframe context.
            sector: Not used for futures, kept for interface compatibility.
            session_phase: Current trading session phase
                (e.g., "pre_market", "regular", "after_hours").
            volume_profile: Dict with volume profile data
                (e.g., {"poc": 5420.5, "val": 5410.0, "vah": 5435.0}).

        Returns:
            Formatted prompt string.
        """
        trimmed = candles[-self.candle_window:]
        normalized = self._normalize_candles(trimmed)

        prompt = (
            "Task: You are a US futures trading agent. Your goal is to maximize "
            "intraday risk-adjusted returns trading index futures (ES, NQ, MES, MNQ). "
            "Futures are leveraged instruments — small percentage moves have large dollar impact. "
            "Focus on session context, volume levels, and clean risk/reward setups.\n\n"
            "Trading Costs:\n"
            "- MES: $1.24 per contract per side ($2.48 round-trip)\n"
            "- ES: $2.24 per contract per side ($4.48 round-trip)\n"
            "- Point values: MES=$5/pt, ES=$50/pt, MNQ=$2/pt, NQ=$20/pt\n"
            "- Costs are minimal relative to contract value; focus on direction and timing.\n\n"
            "Legible Actions: Choose from {Buy, Sell, Hold}\n\n"
        )

        # Futures-specific context
        if session_phase:
            phase_descriptions = {
                "pre_market": "Pre-Market (lower liquidity, wider spreads)",
                "opening_range": "Opening Range (first 15-30 min, high volatility)",
                "regular": "Regular Session (9:30-16:00 ET, full liquidity)",
                "active_trading": "Active Trading (post-opening, normal flow)",
                "afternoon": "Afternoon Session (lower volume, mean-reversion bias)",
                "eod_flatten": "End of Day (flattening, avoid new positions)",
                "after_hours": "After Hours (thin liquidity, event-driven)",
                "closed": "Market Closed",
            }
            phase_desc = phase_descriptions.get(session_phase, session_phase)
            prompt += f"Session Phase: {phase_desc}\n"

        if volume_profile:
            prompt += "Volume Profile:\n"
            if "poc" in volume_profile:
                prompt += f"  POC (Point of Control): {volume_profile['poc']:.1f}\n"
            if "val" in volume_profile:
                prompt += f"  Value Area Low: {volume_profile['val']:.1f}\n"
            if "vah" in volume_profile:
                prompt += f"  Value Area High: {volume_profile['vah']:.1f}\n"

        if session_phase or volume_profile:
            prompt += "\n"

        prompt += "Current State:\n"
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
                    round(r, self.decimal_places)
                    for r in (history.get("recent_rewards") or [])[-10:]
                ],
                "net_values": [
                    round(v, self.decimal_places)
                    for v in (history.get("net_values") or [])[-10:]
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
                f"You are currently {direction} {symbol} from {entry_price:.2f}, "
                f"unrealized PnL: {pnl_pct:+.1f}%\n"
                f"Consider whether to maintain or reverse your position."
            )

        if market_context:
            prompt += "\n\nMarket Context (longer timeframe):\n"
            items = market_context if isinstance(market_context, list) else [market_context]
            for symbol_ctx in items:
                s = symbol_ctx.get("symbol", "?")
                prompt += f"  {s}: "
                parts: list[str] = []
                if "pct_1d" in symbol_ctx:
                    parts.append(f"1d: {symbol_ctx['pct_1d']:+.1f}%")
                if "pct_5d" in symbol_ctx:
                    parts.append(f"5d: {symbol_ctx['pct_5d']:+.1f}%")
                prompt += " | ".join(parts) + "\n"

        prompt += (
            '\n\nOutput Action: Format your answer as JSON: '
            '{"Action": "Buy"}, {"Action": "Sell"}, or {"Action": "Hold"}'
        )
        return prompt
