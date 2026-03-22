"""
FLAG-Trader Model — SmolLM2 + Policy/Value Heads
==================================================

Loads SmolLM2-135M-Instruct, freezes bottom 80% of layers,
and adds policy head (3 actions) + value head (state value)
for PPO training. Based on FLAG-Trader paper Section 4.2.
"""

from __future__ import annotations

from pathlib import Path

import torch
import torch.nn as nn
from torch.distributions import Categorical
from transformers import AutoModelForCausalLM, AutoTokenizer


class FlagTraderModel(nn.Module):
    """SmolLM2-135M with policy and value heads for RL trading.

    Args:
        model_name: HuggingFace model identifier.
        freeze_pct: Fraction of transformer layers to freeze (bottom).
        device: Device to run on ('cpu' or 'cuda').
    """

    NUM_ACTIONS = 3  # Sell=0, Hold=1, Buy=2

    def __init__(
        self,
        model_name: str = "HuggingFaceTB/SmolLM2-135M-Instruct",
        freeze_pct: float = 0.8,
        device: str = "cpu",
    ) -> None:
        super().__init__()
        self.device = torch.device(device)
        self.model_name = model_name

        # Load pretrained LLM
        self.llm = AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype=torch.float32,
            trust_remote_code=False,
        )
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        hidden_size: int = self.llm.config.hidden_size  # 576 for SmolLM2-135M

        # Freeze bottom freeze_pct% of transformer layers
        self._freeze_layers(freeze_pct)

        # Policy head: logits over actions [Sell, Hold, Buy]
        self.policy_head = nn.Sequential(
            nn.Linear(hidden_size, 64),
            nn.ReLU(),
            nn.Linear(64, self.NUM_ACTIONS),
        )

        # Value head: scalar state value estimate
        self.value_head = nn.Sequential(
            nn.Linear(hidden_size, 64),
            nn.ReLU(),
            nn.Linear(64, 1),
        )

        self.to(self.device)

    def _freeze_layers(self, freeze_pct: float) -> None:
        """Freeze the bottom freeze_pct of transformer layers."""
        # Freeze embeddings always
        for param in self.llm.model.embed_tokens.parameters():
            param.requires_grad = False

        layers = self.llm.model.layers
        num_layers = len(layers)
        num_frozen = int(num_layers * freeze_pct)

        for i, layer in enumerate(layers):
            if i < num_frozen:
                for param in layer.parameters():
                    param.requires_grad = False
            # Top layers remain trainable (requires_grad=True by default)

        # Freeze the LM head (we use our own heads)
        for param in self.llm.lm_head.parameters():
            param.requires_grad = False

    def _extract_last_hidden(
        self, input_ids: torch.Tensor, attention_mask: torch.Tensor
    ) -> torch.Tensor:
        """Forward through LLM and extract last token's hidden state.

        Returns:
            Hidden state tensor of shape (batch, hidden_size).
        """
        outputs = self.llm.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            output_hidden_states=True,
        )
        # Last layer hidden states: (batch, seq_len, hidden_size)
        last_hidden = outputs.last_hidden_state

        # Extract hidden state at last non-padding token
        # Use attention_mask to find the last valid position
        seq_lengths = attention_mask.sum(dim=1) - 1  # (batch,)
        batch_idx = torch.arange(last_hidden.size(0), device=last_hidden.device)
        hidden = last_hidden[batch_idx, seq_lengths]  # (batch, hidden_size)
        return hidden

    def forward(
        self, input_ids: torch.Tensor, attention_mask: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Forward pass through LLM + heads.

        Args:
            input_ids: Token IDs, shape (batch, seq_len).
            attention_mask: Attention mask, shape (batch, seq_len).

        Returns:
            (logits, value) where logits is (batch, 3) and value is (batch, 1).
        """
        hidden = self._extract_last_hidden(input_ids, attention_mask)
        logits = self.policy_head(hidden)
        value = self.value_head(hidden)
        return logits, value

    @torch.no_grad()
    def get_action(self, prompt: str) -> tuple[int, float, torch.Tensor]:
        """Get a trading action from a text prompt.

        Used during rollout collection. Frozen layers run without grad,
        but we need grad for trainable params during training — however
        get_action is typically called during data collection (no grad needed).

        Args:
            prompt: Structured text prompt from PromptBuilder.

        Returns:
            (action_id, state_value, log_prob) where:
                action_id: 0=Sell, 1=Hold, 2=Buy
                state_value: estimated V(s)
                log_prob: log probability of sampled action (detached)
        """
        tokens = self.tokenizer(
            prompt,
            return_tensors="pt",
            max_length=512,
            truncation=True,
            padding=True,
        )
        input_ids = tokens["input_ids"].to(self.device)
        attention_mask = tokens["attention_mask"].to(self.device)

        logits, value = self.forward(input_ids, attention_mask)

        dist = Categorical(logits=logits.squeeze(0))
        action = dist.sample()
        log_prob = dist.log_prob(action)

        return int(action.item()), float(value.item()), log_prob

    def evaluate_actions(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        actions: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Evaluate log_probs, values, and entropy for taken actions.

        Used during PPO update step. Runs with gradients enabled.

        Args:
            input_ids: (batch, seq_len)
            attention_mask: (batch, seq_len)
            actions: (batch,) — action indices taken during rollout

        Returns:
            (log_probs, values, entropy) all with grad.
        """
        logits, values = self.forward(input_ids, attention_mask)
        dist = Categorical(logits=logits)

        log_probs = dist.log_prob(actions)
        entropy = dist.entropy()

        return log_probs, values.squeeze(-1), entropy

    def get_trainable_params(self) -> list[nn.Parameter]:
        """Return only parameters that require gradients."""
        return [p for p in self.parameters() if p.requires_grad]

    def save_trainable(self, path: Path) -> None:
        """Save only trainable weights (top LLM layers + heads).

        This avoids saving the full 270MB model — only the fine-tuned
        delta (~10-20MB) is persisted.
        """
        trainable_llm = {
            k: v for k, v in self.llm.named_parameters() if v.requires_grad
        }
        state = {
            "trainable_layers": {k: v.data for k, v in trainable_llm.items()},
            "policy_head": self.policy_head.state_dict(),
            "value_head": self.value_head.state_dict(),
            "model_name": self.model_name,
        }
        torch.save(state, path)

    def load_trainable(self, path: Path) -> None:
        """Load trainable weights from a checkpoint."""
        state = torch.load(path, map_location=self.device, weights_only=True)

        # Restore trainable LLM layers
        llm_params = dict(self.llm.named_parameters())
        for name, tensor in state["trainable_layers"].items():
            if name in llm_params:
                llm_params[name].data.copy_(tensor)

        # Restore heads
        self.policy_head.load_state_dict(state["policy_head"])
        self.value_head.load_state_dict(state["value_head"])
