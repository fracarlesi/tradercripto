"""
Equity FLAG-Trader Model — Wrapper with Equity TP/SL Ranges
=============================================================

Wraps the base FlagTraderModel from crypto_bot, overriding TP/SL ranges
for US equity daily trading:
  - TP: 1.0% to 8.0%  (vs crypto 0.5%-5.0%)
  - SL: 0.5% to 4.0%  (vs crypto 0.3%-2.0%)

Equities on daily timeframes have larger moves but lower volatility
than crypto on 15m, hence wider ranges.
"""

from __future__ import annotations

import logging
from pathlib import Path

import torch

# Import from crypto_bot — shared model
from crypto_bot.flag_trader.model import FlagTraderModel

logger = logging.getLogger(__name__)


class EquityFlagTraderModel(FlagTraderModel):
    """FlagTraderModel with equity-specific TP/SL ranges.

    Inherits all LLM + policy/value/TP/SL head logic from FlagTraderModel,
    only overrides the TP/SL scaling ranges.

    Args:
        model_name: HuggingFace model identifier.
        freeze_pct: Fraction of transformer layers to freeze (bottom).
        device: Device to run on ('auto', 'cpu', 'cuda', 'mps').
    """

    # Equity TP/SL ranges (wider than crypto)
    EQUITY_TP_MIN = 1.0
    EQUITY_TP_MAX = 8.0
    EQUITY_SL_MIN = 0.5
    EQUITY_SL_MAX = 4.0

    def __init__(
        self,
        model_name: str = "Qwen/Qwen2.5-0.5B-Instruct",
        freeze_pct: float = 0.8,
        device: str = "auto",
    ) -> None:
        super().__init__(model_name=model_name, freeze_pct=freeze_pct, device=device)

        # Override TP/SL ranges for equity
        self.TP_MIN = self.EQUITY_TP_MIN
        self.TP_MAX = self.EQUITY_TP_MAX
        self.SL_MIN = self.EQUITY_SL_MIN
        self.SL_MAX = self.EQUITY_SL_MAX

        logger.info(
            "EquityFlagTraderModel initialized | TP=[%.1f%%, %.1f%%] | SL=[%.1f%%, %.1f%%] | device=%s",
            self.TP_MIN, self.TP_MAX, self.SL_MIN, self.SL_MAX, self.device,
        )

    def get_action(
        self, prompt: str, return_tokens: bool = False
    ) -> (
        tuple[int, float, torch.Tensor, float, float]
        | tuple[int, float, torch.Tensor, float, float, torch.Tensor, torch.Tensor]
    ):
        """Get a trading action from a text prompt.

        Returns:
            (action_id, state_value, log_prob, tp_pct, sl_pct) where:
            - action_id: 0=Sell, 1=Hold, 2=Buy
            - state_value: value head output (confidence proxy)
            - log_prob: log probability of the sampled action
            - tp_pct: take-profit % in [1.0, 8.0]
            - sl_pct: stop-loss % in [0.5, 4.0]
        """
        return super().get_action(prompt, return_tokens=return_tokens)

    def save_trainable(self, path: Path) -> None:
        """Save trainable weights with equity range metadata."""
        super().save_trainable(path)
        logger.info("Equity model checkpoint saved to %s", path)

    def load_trainable(self, path: Path) -> None:
        """Load trainable weights and re-apply equity ranges."""
        super().load_trainable(path)
        # Re-assert equity ranges after loading (checkpoint may have crypto ranges)
        self.TP_MIN = self.EQUITY_TP_MIN
        self.TP_MAX = self.EQUITY_TP_MAX
        self.SL_MIN = self.EQUITY_SL_MIN
        self.SL_MAX = self.EQUITY_SL_MAX
        logger.info("Equity model checkpoint loaded from %s", path)
