"""
ETF Prompt Builder — Market State to Structured Text for ETFs
==============================================================

Adapts EquityPromptBuilder for ETF trading.
Key differences:
- Preamble: "ETF trading agent" (not equity)
- Adds SPY correlation and sector beta context
- Same candle normalization logic
"""

from __future__ import annotations

from typing import Any

from .equity_prompt import EquityPromptBuilder


class ETFPromptBuilder(EquityPromptBuilder):
    """Builds structured prompts for the ETF LLM trading agent.

    Extends EquityPromptBuilder with ETF-specific context:
    - SPY correlation
    - Sector beta
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
        spy_correlation: float | None = None,
        sector_beta: float | None = None,
    ) -> str:
        """Build a structured prompt for ETF trading.

        Args:
            candles: List of OHLCV dicts. Most recent last.
            portfolio: Account state dict.
            history: Recent trading history.
            similar_trades_text: RAG-injected similar past trades.
            position_info: Current position info for exit evaluation.
            market_context: Longer timeframe context.
            sector: ETF category (e.g., "Technology", "Bonds", "Commodities").
            spy_correlation: Correlation with SPY over trailing period.
            sector_beta: Beta relative to the sector benchmark.

        Returns:
            Formatted prompt string.
        """
        # Build the base prompt but override the preamble
        trimmed = candles[-self.candle_window:]
        normalized = self._normalize_candles(trimmed)

        prompt = (
            "Task: You are an ETF trading agent. Your goal is to maximize "
            "long-term risk-adjusted returns by trading exchange-traded funds. "
            "ETFs are diversified instruments with lower volatility than individual stocks. "
            "Choose optimal buy, sell, or hold decisions based on market conditions, "
            "macro trends, and inter-market correlations.\n\n"
            "Trading Costs:\n"
            "- IBKR commission: $0.005 per share\n"
            "- ETF spreads are typically tight (1-3 cents for liquid ETFs)\n"
            "- Focus on trend alignment and sector rotation rather than short-term noise.\n\n"
            "Legible Actions: Choose from {Buy, Sell, Hold}\n\n"
        )

        # ETF-specific context
        if sector:
            prompt += f"ETF Category: {sector}\n"
        if spy_correlation is not None:
            prompt += f"SPY Correlation (20d): {spy_correlation:.2f}\n"
        if sector_beta is not None:
            prompt += f"Sector Beta: {sector_beta:.2f}\n"
        if sector or spy_correlation is not None or sector_beta is not None:
            prompt += "\n"

        # Reuse the state building from parent (inline to avoid double preamble)
        import json

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
                f"You are currently {direction} {symbol} from ${entry_price:.2f}, "
                f"unrealized PnL: {pnl_pct:+.1f}%\n"
                f"Consider whether to maintain or reverse your position."
            )

        # Insert market context if provided
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
                if "pct_20d" in symbol_ctx:
                    parts.append(f"20d: {symbol_ctx['pct_20d']:+.1f}%")
                prompt += " | ".join(parts) + "\n"

        prompt += (
            '\n\nOutput Action: Format your answer as JSON: '
            '{"Action": "Buy"}, {"Action": "Sell"}, or {"Action": "Hold"}'
        )
        return prompt
