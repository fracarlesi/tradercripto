"""Smoke test: verify FLAG-Trader training pipeline works end-to-end.

Uses synthetic candles — does NOT download real data.

Usage:
    python -m scripts.test_training_smoke
"""

import numpy as np
import torch

from flag_trader.environment import HyperliquidTradingEnv
from flag_trader.model import FlagTraderModel
from flag_trader.prompt import PromptBuilder


def main() -> None:
    # 1. Synthetic candles: 100 bars of random walk
    np.random.seed(42)
    prices = 100.0 + np.cumsum(np.random.randn(100) * 0.5)
    candles = np.column_stack([
        prices - 0.3,  # open
        prices + 0.5,  # high
        prices - 0.5,  # low
        prices,         # close
        np.random.rand(100) * 1000,  # volume
    ]).astype(np.float32)

    # 2. Environment works
    env = HyperliquidTradingEnv(candles=candles, window_size=20)
    obs, info = env.reset()
    assert "candles" in obs and "portfolio" in obs, "Obs missing keys"
    obs2, reward, done, trunc, info2 = env.step(1)  # Hold
    print(f"Env step OK: reward={reward:.6f}, value={info2['total_value']:.2f}")

    # 3. Model loads and forward pass works
    print("Loading SmolLM2-135M (this may take a moment)...")
    model = FlagTraderModel(model_name="HuggingFaceTB/SmolLM2-135M-Instruct", device="cpu")
    prompt_builder = PromptBuilder()

    prompt = prompt_builder.build_prompt(
        candles=[{"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 500.0}],
        portfolio={"cash_balance": 1000.0, "asset_position": 0.0, "total_account_value": 1000.0},
        history={"recent_rewards": [], "net_values": [1000.0], "actions": []},
    )

    action_id, value, log_prob = model.get_action(prompt)
    assert action_id in (0, 1, 2), f"Invalid action: {action_id}"
    print(f"Forward pass OK: action={action_id}, value={value:.4f}, log_prob={log_prob.item():.4f}")

    # 4. Single training step (backward pass)
    tokens = model.tokenizer(prompt, return_tensors="pt", max_length=512, truncation=True, padding=True)
    logits, val = model.forward(tokens["input_ids"], tokens["attention_mask"])
    loss = -torch.mean(logits) + torch.mean(val)  # Dummy loss
    loss.backward()
    print(f"Backward pass OK: loss={loss.item():.4f}")

    print("\nAll smoke tests passed!")


if __name__ == "__main__":
    main()
