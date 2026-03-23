"""
FLAG-Trader Model — LLM + Policy/Value Heads
=============================================

Loads any HuggingFace causal LM (SmolLM2, Qwen, Llama, etc.),
freezes bottom 80% of layers, and adds policy head (3 actions)
+ value head (state value) for PPO training.
Based on FLAG-Trader paper Section 4.2.
"""

from __future__ import annotations

from pathlib import Path

import torch
import torch.nn as nn
from torch.distributions import Categorical
from transformers import AutoModelForCausalLM, AutoTokenizer


def _resolve_device(device: str) -> torch.device:
    """Resolve 'auto' to the best available device."""
    if device == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return torch.device("mps")
        else:
            return torch.device("cpu")
    return torch.device(device)


class FlagTraderModel(nn.Module):
    """HuggingFace causal LM with policy and value heads for RL trading.

    Supports any HuggingFace model (SmolLM2, Qwen, Llama, Mistral, etc.)
    by auto-detecting hidden_size and transformer layers.

    Args:
        model_name: HuggingFace model identifier.
        freeze_pct: Fraction of transformer layers to freeze (bottom).
        device: Device to run on ('auto', 'cpu', 'cuda', 'mps').
    """

    NUM_ACTIONS = 3  # Sell=0, Hold=1, Buy=2

    def __init__(
        self,
        model_name: str = "HuggingFaceTB/SmolLM2-135M-Instruct",
        freeze_pct: float = 0.8,
        device: str = "auto",
    ) -> None:
        super().__init__()
        self.device = _resolve_device(device)
        self.model_name = model_name

        # Load pretrained LLM - always load in float32 for compatibility
        # Some models (e.g. Qwen 3.5) use bfloat16 internally which MPS doesn't support
        self.llm = AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype=torch.float32,
            trust_remote_code=True,
        )
        # Force ALL parameters to float32 (some models have mixed dtypes internally)
        self.llm = self.llm.float()
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name, trust_remote_code=True
        )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        # Auto-detect hidden_size from model config
        hidden_size: int = self.llm.config.hidden_size

        # Freeze bottom layers
        self._freeze_layers(freeze_pct)

        # Policy head: logits over actions [Sell, Hold, Buy]
        intermediate = 256 if hidden_size >= 1024 else 64
        self.policy_head = nn.Sequential(
            nn.Linear(hidden_size, intermediate),
            nn.ReLU(),
            nn.Linear(intermediate, self.NUM_ACTIONS),
        )

        # Value head: scalar state value estimate
        self.value_head = nn.Sequential(
            nn.Linear(hidden_size, intermediate),
            nn.ReLU(),
            nn.Linear(intermediate, 1),
        )

        self.to(self.device)

    def _get_transformer_layers(self) -> nn.ModuleList | list[nn.Module]:
        """Auto-detect transformer layers from various architectures.

        Supports:
        - model.model.layers (Llama, Qwen, Mistral, SmolLM2)
        - model.transformer.h (GPT-2, GPT-Neo)
        - model.model.decoder.layers (OPT)
        """
        if hasattr(self.llm, "model") and hasattr(self.llm.model, "layers"):
            return self.llm.model.layers
        elif hasattr(self.llm, "transformer") and hasattr(self.llm.transformer, "h"):
            return self.llm.transformer.h
        elif (
            hasattr(self.llm, "model")
            and hasattr(self.llm.model, "decoder")
            and hasattr(self.llm.model.decoder, "layers")
        ):
            return self.llm.model.decoder.layers
        else:
            return []

    def _get_embeddings(self) -> nn.Module | None:
        """Auto-detect embedding layer from various architectures."""
        if hasattr(self.llm, "model") and hasattr(self.llm.model, "embed_tokens"):
            return self.llm.model.embed_tokens
        elif hasattr(self.llm, "transformer") and hasattr(
            self.llm.transformer, "wte"
        ):
            return self.llm.transformer.wte
        return None

    def _get_inner_model(self) -> nn.Module:
        """Get the inner transformer model (without lm_head).

        Used for forward pass to get hidden states.
        """
        if hasattr(self.llm, "model"):
            return self.llm.model
        elif hasattr(self.llm, "transformer"):
            return self.llm.transformer
        else:
            return self.llm

    def _freeze_layers(self, freeze_pct: float) -> None:
        """Freeze the bottom freeze_pct of transformer layers."""
        # Freeze embeddings always
        embeddings = self._get_embeddings()
        if embeddings is not None:
            for param in embeddings.parameters():
                param.requires_grad = False

        layers = self._get_transformer_layers()
        num_layers = len(layers)

        if num_layers > 0:
            num_frozen = int(num_layers * freeze_pct)
            for i, layer in enumerate(layers):
                if i < num_frozen:
                    for param in layer.parameters():
                        param.requires_grad = False
        else:
            # Fallback: freeze all params, then unfreeze last 20%
            all_params = list(self.llm.parameters())
            for param in all_params:
                param.requires_grad = False
            num_unfreeze = max(1, int(len(all_params) * (1 - freeze_pct)))
            for param in all_params[-num_unfreeze:]:
                param.requires_grad = True

        # Freeze the LM head (we use our own heads)
        if hasattr(self.llm, "lm_head"):
            for param in self.llm.lm_head.parameters():
                param.requires_grad = False

    def _extract_last_hidden(
        self, input_ids: torch.Tensor, attention_mask: torch.Tensor
    ) -> torch.Tensor:
        """Forward through LLM and extract last token's hidden state.

        Returns:
            Hidden state tensor of shape (batch, hidden_size).
        """
        inner_model = self._get_inner_model()
        outputs = inner_model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            output_hidden_states=True,
        )
        # Last layer hidden states: (batch, seq_len, hidden_size)
        last_hidden = outputs.last_hidden_state

        # Extract hidden state at last non-padding token
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
        # Ensure float32 for heads (some models use bfloat16 internally)
        hidden = hidden.float()
        logits = self.policy_head(hidden)
        value = self.value_head(hidden)
        return logits, value

    @torch.no_grad()
    def get_action(self, prompt: str) -> tuple[int, float, torch.Tensor]:
        """Get a trading action from a text prompt.

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
            actions: (batch,) -- action indices taken during rollout

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
        """Save only trainable weights (top LLM layers + heads)."""
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
