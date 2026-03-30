"""
Futures FLAG-Trader Model — Wrapper with Futures TP/SL Ranges
==============================================================

Wraps the base FlagTraderModel from crypto_bot, overriding TP/SL ranges
for US futures intraday trading:
  - TP: 0.3% to 3.0%  (futures intraday, smaller % moves)
  - SL: 0.2% to 1.5%

Futures are leveraged instruments with tight daily ranges in percentage terms.
"""

from __future__ import annotations

import logging
from pathlib import Path

import torch

from crypto_bot.flag_trader.model import FlagTraderModel

logger = logging.getLogger(__name__)


class FuturesFlagTraderModel(FlagTraderModel):
    """FlagTraderModel with futures-specific TP/SL ranges.

    Inherits all LLM + policy/value/TP/SL head logic from FlagTraderModel,
    only overrides the TP/SL scaling ranges for futures instruments.

    Args:
        model_name: HuggingFace model identifier.
        freeze_pct: Fraction of transformer layers to freeze (bottom).
        device: Device to run on ('auto', 'cpu', 'cuda', 'mps').
    """

    FUTURES_TP_MIN = 0.3
    FUTURES_TP_MAX = 3.0
    FUTURES_SL_MIN = 0.2
    FUTURES_SL_MAX = 1.5

    def __init__(
        self,
        model_name: str = "Qwen/Qwen2.5-0.5B-Instruct",
        freeze_pct: float = 0.8,
        device: str = "auto",
    ) -> None:
        super().__init__(model_name=model_name, freeze_pct=freeze_pct, device=device)

        self.TP_MIN = self.FUTURES_TP_MIN
        self.TP_MAX = self.FUTURES_TP_MAX
        self.SL_MIN = self.FUTURES_SL_MIN
        self.SL_MAX = self.FUTURES_SL_MAX

        logger.info(
            "FuturesFlagTraderModel initialized | TP=[%.1f%%, %.1f%%] | SL=[%.1f%%, %.1f%%] | device=%s",
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
            - tp_pct: take-profit % in [0.3, 3.0]
            - sl_pct: stop-loss % in [0.2, 1.5]
        """
        return super().get_action(prompt, return_tokens=return_tokens)

    def save_trainable(self, path: Path) -> None:
        """Save trainable weights with futures range metadata."""
        super().save_trainable(path)
        logger.info("Futures model checkpoint saved to %s", path)

    def load_trainable(self, path: Path) -> None:
        """Load trainable weights and re-apply futures ranges."""
        super().load_trainable(path)
        self.TP_MIN = self.FUTURES_TP_MIN
        self.TP_MAX = self.FUTURES_TP_MAX
        self.SL_MIN = self.FUTURES_SL_MIN
        self.SL_MAX = self.FUTURES_SL_MAX
        logger.info("Futures model checkpoint loaded from %s", path)
