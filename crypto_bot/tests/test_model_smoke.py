"""
Smoke tests for FlagTraderModel.

Tests the model structure (heads, freeze logic, save/load) WITHOUT
downloading the real model — uses mock LLM internals instead.
Tests both small (576) and large (2560) hidden sizes.
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
        seq_len = 10
        return {
            "input_ids": torch.randint(0, 999, (1, seq_len)),
            "attention_mask": torch.ones(1, seq_len, dtype=torch.long),
        }


def _make_model(hidden: int = 576, num_layers: int = 30) -> "FlagTraderModel":
    """Create FlagTraderModel with mocked LLM (no download)."""
    from flag_trader.model import FlagTraderModel

    with (
        patch(
            "flag_trader.model.AutoModelForCausalLM.from_pretrained",
            return_value=FakeLLM(hidden=hidden, num_layers=num_layers),
        ),
        patch(
            "flag_trader.model.AutoTokenizer.from_pretrained",
            return_value=FakeTokenizer(),
        ),
    ):
        m = FlagTraderModel(model_name="fake/model", freeze_pct=0.8, device="cpu")
    return m


@pytest.fixture
def model() -> "FlagTraderModel":
    """Create FlagTraderModel with small hidden size (SmolLM2-like)."""
    return _make_model(hidden=576, num_layers=30)


@pytest.fixture
def large_model() -> "FlagTraderModel":
    """Create FlagTraderModel with large hidden size (Qwen-like)."""
    return _make_model(hidden=2560, num_layers=32)


class TestModelStructure:
    """Test that heads have correct shapes and freeze logic works."""

    def test_policy_head_output_shape(self, model: "FlagTraderModel") -> None:
        hidden_size = model.llm.config.hidden_size
        hidden = torch.randn(2, hidden_size)
        logits = model.policy_head(hidden)
        assert logits.shape == (2, 3)

    def test_value_head_output_shape(self, model: "FlagTraderModel") -> None:
        hidden_size = model.llm.config.hidden_size
        hidden = torch.randn(2, hidden_size)
        value = model.value_head(hidden)
        assert value.shape == (2, 1)

    def test_forward_shapes(self, model: "FlagTraderModel") -> None:
        input_ids = torch.randint(0, 999, (2, 10))
        attention_mask = torch.ones(2, 10, dtype=torch.long)
        logits, value = model.forward(input_ids, attention_mask)
        assert logits.shape == (2, 3)
        assert value.shape == (2, 1)

    def test_freeze_layers(self, model: "FlagTraderModel") -> None:
        layers = model.llm.model.layers
        num_frozen = int(len(layers) * 0.8)
        for i, layer in enumerate(layers):
            for param in layer.parameters():
                if i < num_frozen:
                    assert not param.requires_grad, f"Layer {i} should be frozen"
                else:
                    assert param.requires_grad, f"Layer {i} should be trainable"

    def test_embeddings_frozen(self, model: "FlagTraderModel") -> None:
        for param in model.llm.model.embed_tokens.parameters():
            assert not param.requires_grad

    def test_lm_head_frozen(self, model: "FlagTraderModel") -> None:
        for param in model.llm.lm_head.parameters():
            assert not param.requires_grad

    def test_heads_trainable(self, model: "FlagTraderModel") -> None:
        for param in model.policy_head.parameters():
            assert param.requires_grad
        for param in model.value_head.parameters():
            assert param.requires_grad


class TestLargeModel:
    """Test with large hidden size (Qwen 3.5 4B-like: hidden=2560, 32 layers)."""

    def test_policy_head_output_shape(self, large_model: "FlagTraderModel") -> None:
        hidden_size = large_model.llm.config.hidden_size
        assert hidden_size == 2560
        hidden = torch.randn(2, hidden_size)
        logits = large_model.policy_head(hidden)
        assert logits.shape == (2, 3)

    def test_intermediate_size_256(self, large_model: "FlagTraderModel") -> None:
        """Large models should use 256 intermediate size in heads."""
        first_linear = large_model.policy_head[0]
        assert first_linear.in_features == 2560
        assert first_linear.out_features == 256

    def test_forward_shapes(self, large_model: "FlagTraderModel") -> None:
        input_ids = torch.randint(0, 999, (2, 10))
        attention_mask = torch.ones(2, 10, dtype=torch.long)
        logits, value = large_model.forward(input_ids, attention_mask)
        assert logits.shape == (2, 3)
        assert value.shape == (2, 1)

    def test_freeze_32_layers(self, large_model: "FlagTraderModel") -> None:
        layers = large_model.llm.model.layers
        assert len(layers) == 32
        num_frozen = int(32 * 0.8)  # 25
        for i, layer in enumerate(layers):
            for param in layer.parameters():
                if i < num_frozen:
                    assert not param.requires_grad
                else:
                    assert param.requires_grad


class TestGetAction:
    """Test action sampling from prompt."""

    def test_get_action_returns_valid(self, model: "FlagTraderModel") -> None:
        action_id, value, log_prob = model.get_action("fake prompt")
        assert action_id in (0, 1, 2)
        assert isinstance(value, float)
        assert isinstance(log_prob, torch.Tensor)
        assert log_prob.shape == ()


class TestEvaluateActions:
    """Test PPO evaluation pass."""

    def test_evaluate_actions_shapes(self, model: "FlagTraderModel") -> None:
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
        trainable = model.get_trainable_params()
        assert len(trainable) > 0
        for p in trainable:
            assert p.requires_grad

    def test_trainable_count_less_than_total(self, model: "FlagTraderModel") -> None:
        total = sum(1 for _ in model.parameters())
        trainable = len(model.get_trainable_params())
        assert trainable < total


class TestSaveLoad:
    """Test checkpoint save/load for trainable weights."""

    def test_save_and_load(self, model: "FlagTraderModel", tmp_path: Path) -> None:
        ckpt = tmp_path / "test_checkpoint.pt"
        model.save_trainable(ckpt)
        assert ckpt.exists()

        original_weight = model.policy_head[0].weight.data.clone()
        model.policy_head[0].weight.data.fill_(999.0)
        assert not torch.equal(model.policy_head[0].weight.data, original_weight)

        model.load_trainable(ckpt)
        assert torch.equal(model.policy_head[0].weight.data, original_weight)


class TestParseActionThinking:
    """Test that parse_action strips <think> blocks."""

    def test_strip_think_block(self) -> None:
        from flag_trader.prompt import PromptBuilder

        pb = PromptBuilder()
        output = '<think>Let me analyze the market...</think> {"Action": "Buy"}'
        assert pb.parse_action(output) == 2  # Buy

    def test_no_think_block_unchanged(self) -> None:
        from flag_trader.prompt import PromptBuilder

        pb = PromptBuilder()
        assert pb.parse_action('{"Action": "Sell"}') == 0

    def test_think_block_bare_keyword(self) -> None:
        from flag_trader.prompt import PromptBuilder

        pb = PromptBuilder()
        output = "<think>reasoning here</think>\nHold"
        assert pb.parse_action(output) == 1  # Hold
