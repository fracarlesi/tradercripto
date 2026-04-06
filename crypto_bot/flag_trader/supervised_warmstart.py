"""Supervised warm-start for FLAG-Trader model.

Pre-trains the policy, TP, and SL heads on auto-labeled candle data
before PPO fine-tuning. This gives the model a meaningful starting point
instead of random head outputs.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.cuda.amp import autocast

from crypto_bot.flag_trader.model import FlagTraderModel
from crypto_bot.flag_trader.prompt import PromptBuilder

logger = logging.getLogger(__name__)

# Label constants matching model convention: Sell=0, Hold=1, Buy=2
SELL, HOLD, BUY = 0, 1, 2


def auto_label_candles(
    candles: list[dict[str, float]],
    lookahead: int = 24,
    tp_thresh: float = 2.0,
    sl_thresh: float = 1.0,
) -> list[tuple[int, float, float]]:
    """Auto-label candles by looking ahead for TP/SL hits.

    For each candle index *i*, examines the next *lookahead* bars to decide
    whether a long or short entry would have been profitable.

    Args:
        candles: List of candle dicts with keys open, high, low, close, volume.
        lookahead: Number of bars to look ahead.
        tp_thresh: Take-profit threshold in percent for labeling BUY/SELL.
        sl_thresh: Stop-loss threshold in percent that invalidates the signal.

    Returns:
        List of (label, tp_pct, sl_pct) tuples, one per candle.
        label is BUY(2), SELL(0), or HOLD(1).
        tp_pct is the max favorable excursion capped at 5.0%.
        sl_pct is the max adverse excursion capped at 2.0%.
    """
    n = len(candles)
    results: list[tuple[int, float, float]] = []

    for i in range(n):
        close_i = candles[i]["close"]
        if close_i <= 0:
            results.append((HOLD, 0.0, 0.0))
            continue

        end = min(i + 1 + lookahead, n)
        future = candles[i + 1 : end]

        if not future:
            results.append((HOLD, 0.0, 0.0))
            continue

        # Track max up/down excursions and which threshold is hit first
        max_up = 0.0
        max_down = 0.0
        buy_triggered = False
        sell_triggered = False
        buy_stopped = False
        sell_stopped = False

        for candle in future:
            high_pct = (candle["high"] - close_i) / close_i * 100.0
            low_pct = (close_i - candle["low"]) / close_i * 100.0

            max_up = max(max_up, high_pct)
            max_down = max(max_down, low_pct)

            # Check BUY scenario: price goes up to TP before going down to SL
            if not buy_triggered and not buy_stopped:
                if low_pct >= sl_thresh:
                    buy_stopped = True  # SL hit first, no buy
                if high_pct >= tp_thresh and not buy_stopped:
                    buy_triggered = True

            # Check SELL scenario: price goes down to TP before going up to SL
            if not sell_triggered and not sell_stopped:
                if high_pct >= sl_thresh:
                    sell_stopped = True  # SL hit first, no sell
                if low_pct >= tp_thresh and not sell_stopped:
                    sell_triggered = True

        # Determine label
        if buy_triggered and not sell_triggered:
            label = BUY
        elif sell_triggered and not buy_triggered:
            label = SELL
        elif buy_triggered and sell_triggered:
            # Both triggered -- pick the one with larger excursion
            label = BUY if max_up >= max_down else SELL
        else:
            label = HOLD

        tp_pct = min(max_up, 5.0)
        sl_pct = min(max_down, 2.0)

        results.append((label, tp_pct, sl_pct))

    return results


class SupervisedWarmStart:
    """Supervised pre-training for FLAG-Trader heads.

    Trains policy, TP, and SL heads on auto-labeled candle data using
    cross-entropy (policy) and MSE (TP/SL) losses, with inverse-frequency
    class weighting to handle the BUY/SELL vs HOLD imbalance.

    Args:
        model: The FlagTraderModel instance to warm-start.
        prompt_builder: PromptBuilder for converting candle windows to prompts.
        lr: Learning rate for the optimizer.
    """

    def __init__(
        self,
        model: FlagTraderModel,
        prompt_builder: PromptBuilder,
        lr: float = 1e-4,
    ) -> None:
        self.model = model
        self.prompt_builder = prompt_builder
        self.lr = lr
        self.device = model.device

    def _build_dataset(
        self,
        candles_by_symbol: dict[str, list[dict[str, float]]],
        window_size: int,
        lookahead: int = 24,
        tp_thresh: float = 2.0,
        sl_thresh: float = 1.0,
    ) -> list[dict[str, Any]]:
        """Build labeled dataset from raw candles.

        For each symbol, auto-labels candles and creates (prompt, label, tp, sl)
        samples by sliding a window over the candle series.

        Returns:
            List of sample dicts with keys: prompt, label, tp_pct, sl_pct.
        """
        dataset: list[dict[str, Any]] = []

        for symbol, candles in candles_by_symbol.items():
            if len(candles) < window_size + 1:
                logger.warning(
                    "Skipping %s: only %d candles (need %d+1)",
                    symbol, len(candles), window_size,
                )
                continue

            labels = auto_label_candles(
                candles,
                lookahead=lookahead,
                tp_thresh=tp_thresh,
                sl_thresh=sl_thresh,
            )

            # Slide window: for index i, the window is [i-window_size : i]
            # and the label is for candle i (the last candle in the window)
            for i in range(window_size, len(candles)):
                label, tp_pct, sl_pct = labels[i]
                window = candles[i - window_size : i]

                # Build prompt using the same interface as the trainer
                prompt = self.prompt_builder.build_prompt(
                    candles=window,
                    portfolio={
                        "cash_balance": 1000.0,
                        "asset_position": 0.0,
                        "total_account_value": 1000.0,
                    },
                    history={
                        "recent_rewards": [],
                        "net_values": [],
                        "actions": [],
                    },
                )

                dataset.append({
                    "prompt": prompt,
                    "label": label,
                    "tp_pct": tp_pct,
                    "sl_pct": sl_pct,
                })

        logger.info(
            "Built dataset: %d samples from %d symbols",
            len(dataset), len(candles_by_symbol),
        )
        return dataset

    @staticmethod
    def _compute_class_weights(
        dataset: list[dict[str, Any]],
    ) -> torch.Tensor:
        """Compute inverse-frequency class weights for the 3 action classes.

        Returns:
            Tensor of shape (3,) with weights inversely proportional to
            class frequency, normalized so the mean weight is 1.0.
        """
        counts = [0, 0, 0]
        for sample in dataset:
            counts[sample["label"]] += 1

        total = len(dataset)
        weights = torch.zeros(3)
        for c in range(3):
            if counts[c] > 0:
                weights[c] = total / (3.0 * counts[c])
            else:
                weights[c] = 1.0

        logger.info(
            "Class distribution: SELL=%d (%.1f%%), HOLD=%d (%.1f%%), BUY=%d (%.1f%%)",
            counts[SELL], 100.0 * counts[SELL] / max(total, 1),
            counts[HOLD], 100.0 * counts[HOLD] / max(total, 1),
            counts[BUY], 100.0 * counts[BUY] / max(total, 1),
        )
        return weights

    def train(
        self,
        candles_by_symbol: dict[str, list[dict[str, float]]],
        num_steps: int = 500,
        batch_size: int = 16,
        window_size: int = 20,
        max_seq_len: int = 512,
        lookahead: int = 24,
        tp_thresh: float = 2.0,
        sl_thresh: float = 1.0,
    ) -> dict[str, float]:
        """Run supervised warm-start training.

        Auto-labels candles, builds prompts, and trains the model heads
        using cross-entropy for policy and MSE for TP/SL predictions.

        Args:
            candles_by_symbol: Mapping of symbol name to list of candle dicts.
            num_steps: Number of gradient steps.
            batch_size: Samples per gradient step.
            window_size: Number of candles per prompt window.
            max_seq_len: Maximum token sequence length for truncation.
            lookahead: Bars to look ahead for labeling.
            tp_thresh: TP threshold in percent for BUY/SELL labeling.
            sl_thresh: SL threshold in percent for labeling.

        Returns:
            Dict with final training stats: loss, accuracy, tp_mse, sl_mse,
            and class distribution info.
        """
        # 1. Build dataset
        dataset = self._build_dataset(
            candles_by_symbol,
            window_size=window_size,
            lookahead=lookahead,
            tp_thresh=tp_thresh,
            sl_thresh=sl_thresh,
        )

        if len(dataset) < batch_size:
            logger.error(
                "Dataset too small: %d samples < batch_size %d",
                len(dataset), batch_size,
            )
            return {"error": "dataset_too_small", "num_samples": float(len(dataset))}  # pyright: ignore[reportReturnType]  # torch/SDK typing

        # 2. Compute class weights
        class_weights = self._compute_class_weights(dataset).to(self.device)

        # 3. Set up optimizer (only trainable params -- heads + unfrozen layers)
        trainable_params = self.model.get_trainable_params()
        optimizer = torch.optim.AdamW(trainable_params, lr=self.lr)

        # 4. Determine AMP settings
        use_amp = self.device.type == "cuda"
        amp_dtype = torch.bfloat16 if use_amp else torch.float32

        # 5. Training loop
        self.model.train()
        tokenizer = self.model.tokenizer

        running_loss = 0.0
        running_correct = 0
        running_total = 0
        running_tp_mse = 0.0
        running_sl_mse = 0.0
        last_log_loss = 0.0
        last_log_acc = 0.0

        logger.info(
            "Starting supervised warm-start: %d steps, batch_size=%d, "
            "dataset=%d samples, device=%s, AMP=%s",
            num_steps, batch_size, len(dataset), self.device, use_amp,
        )

        for step in range(1, num_steps + 1):
            # Sample random batch
            batch_samples = random.choices(dataset, k=batch_size)

            prompts = [s["prompt"] for s in batch_samples]
            labels = torch.tensor(
                [s["label"] for s in batch_samples],
                dtype=torch.long,
                device=self.device,
            )
            tp_targets = torch.tensor(
                [s["tp_pct"] for s in batch_samples],
                dtype=torch.float32,
                device=self.device,
            ).unsqueeze(1)  # (batch, 1)
            sl_targets = torch.tensor(
                [s["sl_pct"] for s in batch_samples],
                dtype=torch.float32,
                device=self.device,
            ).unsqueeze(1)  # (batch, 1)

            # Tokenize
            tokens = tokenizer(
                prompts,
                return_tensors="pt",
                max_length=max_seq_len,
                truncation=True,
                padding=True,
            )
            input_ids = tokens["input_ids"].to(self.device)
            attention_mask = tokens["attention_mask"].to(self.device)

            # Forward pass with AMP
            with autocast(dtype=amp_dtype, enabled=use_amp):
                logits, _value, tp_pred, sl_pred = self.model.forward(
                    input_ids, attention_mask,
                )

                # Policy loss: weighted cross-entropy
                policy_loss = F.cross_entropy(logits, labels, weight=class_weights)

                # TP/SL losses: MSE
                tp_mse = F.mse_loss(tp_pred, tp_targets)
                sl_mse = F.mse_loss(sl_pred, sl_targets)

                # Combined loss
                loss = policy_loss + 0.5 * tp_mse + 0.5 * sl_mse

            # Backward + step
            optimizer.zero_grad()
            loss.backward()

            # Gradient clipping for stability
            torch.nn.utils.clip_grad_norm_(
                [p for p in self.model.parameters() if p.requires_grad],
                max_norm=1.0,
            )
            optimizer.step()

            # Track metrics
            with torch.no_grad():
                preds = logits.argmax(dim=-1)
                correct = (preds == labels).sum().item()

            running_loss += loss.item()
            running_correct += correct
            running_total += batch_size
            running_tp_mse += tp_mse.item()
            running_sl_mse += sl_mse.item()

            # Log every 50 steps
            if step % 50 == 0:
                avg_loss = running_loss / 50
                avg_acc = running_correct / running_total
                avg_tp = running_tp_mse / 50
                avg_sl = running_sl_mse / 50

                logger.info(
                    "Step %d/%d | loss=%.4f | acc=%.3f | tp_mse=%.4f | sl_mse=%.4f",
                    step, num_steps, avg_loss, avg_acc, avg_tp, avg_sl,
                )

                last_log_loss = avg_loss
                last_log_acc = avg_acc
                running_loss = 0.0
                running_correct = 0
                running_total = 0
                running_tp_mse = 0.0
                running_sl_mse = 0.0

        # Final stats (handle case where num_steps is not a multiple of 50)
        if running_total > 0:
            last_log_loss = running_loss / max(1, num_steps % 50)
            last_log_acc = running_correct / running_total

        stats = {
            "final_loss": last_log_loss,
            "final_accuracy": last_log_acc,
            "num_samples": float(len(dataset)),
            "num_steps": float(num_steps),
        }

        logger.info("Supervised warm-start complete: %s", stats)
        return stats
