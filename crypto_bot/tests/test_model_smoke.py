"""
Smoke tests for FlagTraderModel.

Tests the model structure (heads, freeze logic, save/load) WITHOUT
downloading the real SmolLM2 model — uses mock LLM internals instead.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import torch
import torch.nn as nn


class FakeLlamaLayer(nn.Module):
    """Minimal fake transformer layer."""

    def __init__(self, hidden: int) -> None:
        super().__init__()
        self.linear = nn.Linear(hidden, hidden)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.linear(x)


class FakeModelOutput:
    def __init__(self, last_hidden_state: torch.Tensor) -> None:
        self.last_hidden_state = last_hidden_state


class FakeLLMInner(nn.Module):
    """Mimics llm.model (the inner transformer without lm_head)."""

    def __init__(self, hidden: int = 576, num_layers: int = 30) -> None:
        super().__init__()
        self.embed_tokens = nn.Embedding(1000, hidden)
        self.layers = nn.ModuleList([FakeLlamaLayer(hidden) for _ in range(num_layers)])

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor | None = None,
        output_hidden_states: bool = False,
    ) -> FakeModelOutput:
        batch, seq_len = input_ids.shape
        hidden = self.embed_tokens(input_ids)
        for layer in self.layers:
            hidden = layer(hidden)
        return FakeModelOutput(last_hidden_state=hidden)


class FakeLLM(nn.Module):
    """Mimics AutoModelForCausalLM structure."""

    def __init__(self, hidden: int = 576, num_layers: int = 30) -> None:
        super().__init__()
        self.config = MagicMock()
        self.config.hidden_size = hidden
        self.model = FakeLLMInner(hidden, num_layers)
        self.lm_head = nn.Linear(hidden, 1000)


class FakeTokenizer:
    """Minimal tokenizer mock."""

    pad_token: str | None = "<pad>"
    eos_token: str = "<eos>"

    def __call__(
        self,
        text: str,
        return_tensors: str = "pt",
        max_length: int = 512,
        truncation: bool = True,
        padding: bool = True,
    ) -> dict[str, torch.Tensor]:
        # Return fake token IDs (10 tokens)
        seq_len = 10
        return {
            "input_ids": torch.randint(0, 999, (1, seq_len)),
            "attention_mask": torch.ones(1, seq_len, dtype=torch.long),
        }


@pytest.fixture
def model():
    """Create FlagTraderModel with mocked LLM (no download)."""
    from flag_trader.model import FlagTraderModel

    with (
        patch(
            "flag_trader.model.AutoModelForCausalLM.from_pretrained",
            return_value=FakeLLM(),
        ),
        patch(
            "flag_trader.model.AutoTokenizer.from_pretrained",
            return_value=FakeTokenizer(),
        ),
    ):
        m = FlagTraderModel(model_name="fake/model", freeze_pct=0.8, device="cpu")
    return m


class TestModelStructure:
    """Test that heads have correct shapes and freeze logic works."""

    def test_policy_head_output_shape(self, model: "FlagTraderModel") -> None:
        """Policy head outputs (batch, 3) logits."""
        hidden = torch.randn(2, 576)
        logits = model.policy_head(hidden)
        assert logits.shape == (2, 3)

    def test_value_head_output_shape(self, model: "FlagTraderModel") -> None:
        """Value head outputs (batch, 1) scalar."""
        hidden = torch.randn(2, 576)
        value = model.value_head(hidden)
        assert value.shape == (2, 1)

    def test_forward_shapes(self, model: "FlagTraderModel") -> None:
        """Full forward pass returns correct shapes."""
        input_ids = torch.randint(0, 999, (2, 10))
        attention_mask = torch.ones(2, 10, dtype=torch.long)

        logits, value = model.forward(input_ids, attention_mask)
        assert logits.shape == (2, 3)
        assert value.shape == (2, 1)

    def test_freeze_layers(self, model: "FlagTraderModel") -> None:
        """Bottom 80% of layers should be frozen."""
        layers = model.llm.model.layers
        num_frozen = int(len(layers) * 0.8)  # 24

        for i, layer in enumerate(layers):
            for param in layer.parameters():
                if i < num_frozen:
                    assert not param.requires_grad, f"Layer {i} should be frozen"
                else:
                    assert param.requires_grad, f"Layer {i} should be trainable"

    def test_embeddings_frozen(self, model: "FlagTraderModel") -> None:
        """Embeddings should always be frozen."""
        for param in model.llm.model.embed_tokens.parameters():
            assert not param.requires_grad

    def test_lm_head_frozen(self, model: "FlagTraderModel") -> None:
        """LM head should be frozen (we use custom heads)."""
        for param in model.llm.lm_head.parameters():
            assert not param.requires_grad

    def test_heads_trainable(self, model: "FlagTraderModel") -> None:
        """Policy and value heads must be trainable."""
        for param in model.policy_head.parameters():
            assert param.requires_grad
        for param in model.value_head.parameters():
            assert param.requires_grad


class TestGetAction:
    """Test action sampling from prompt."""

    def test_get_action_returns_valid(self, model: "FlagTraderModel") -> None:
        """get_action returns (action_id, value, log_prob)."""
        action_id, value, log_prob = model.get_action("fake prompt")

        assert action_id in (0, 1, 2)
        assert isinstance(value, float)
        assert isinstance(log_prob, torch.Tensor)
        assert log_prob.shape == ()


class TestEvaluateActions:
    """Test PPO evaluation pass."""

    def test_evaluate_actions_shapes(self, model: "FlagTraderModel") -> None:
        """evaluate_actions returns correct shapes for PPO update."""
        batch = 4
        input_ids = torch.randint(0, 999, (batch, 10))
        attention_mask = torch.ones(batch, 10, dtype=torch.long)
        actions = torch.tensor([0, 1, 2, 1])

        log_probs, values, entropy = model.evaluate_actions(
            input_ids, attention_mask, actions
        )

        assert log_probs.shape == (batch,)
        assert values.shape == (batch,)
        assert entropy.shape == (batch,)

    def test_evaluate_actions_has_grad(self, model: "FlagTraderModel") -> None:
        """Output tensors must have grad for PPO backprop."""
        input_ids = torch.randint(0, 999, (2, 10))
        attention_mask = torch.ones(2, 10, dtype=torch.long)
        actions = torch.tensor([0, 2])

        log_probs, values, entropy = model.evaluate_actions(
            input_ids, attention_mask, actions
        )

        assert log_probs.requires_grad
        assert values.requires_grad
        assert entropy.requires_grad


class TestTrainableParams:
    """Test trainable parameter selection."""

    def test_get_trainable_params(self, model: "FlagTraderModel") -> None:
        """get_trainable_params returns only unfrozen parameters."""
        trainable = model.get_trainable_params()
        assert len(trainable) > 0
        for p in trainable:
            assert p.requires_grad

    def test_trainable_count_less_than_total(self, model: "FlagTraderModel") -> None:
        """Trainable params should be a subset of total params."""
        total = sum(1 for _ in model.parameters())
        trainable = len(model.get_trainable_params())
        assert trainable < total


class TestSaveLoad:
    """Test checkpoint save/load for trainable weights."""

    def test_save_and_load(self, model: "FlagTraderModel", tmp_path: Path) -> None:
        """Save trainable weights, modify them, reload, verify restored."""
        ckpt = tmp_path / "test_checkpoint.pt"

        # Save original weights
        model.save_trainable(ckpt)
        assert ckpt.exists()

        # Get original policy head weight
        original_weight = model.policy_head[0].weight.data.clone()

        # Corrupt the weight
        model.policy_head[0].weight.data.fill_(999.0)
        assert not torch.equal(model.policy_head[0].weight.data, original_weight)

        # Reload
        model.load_trainable(ckpt)

        # Verify restored
        assert torch.equal(model.policy_head[0].weight.data, original_weight)
