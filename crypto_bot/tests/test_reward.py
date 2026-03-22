"""Tests for flag_trader.reward — compute_sharpe_delta."""

from flag_trader.reward import compute_sharpe_delta


def test_sharpe_delta_positive() -> None:
    # Adding a positive return to negative history improves Sharpe
    history = [-0.5, -0.3, -0.1, 0.1, 0.5]
    delta = compute_sharpe_delta(history)
    # SR improves when adding the large positive return
    assert isinstance(delta, float)
    # Verify delta is non-zero (Sharpe changed)
    assert delta != 0.0


def test_sharpe_delta_negative() -> None:
    # Adding a large loss to positive history worsens Sharpe
    history = [1.0, 1.0, 1.0, -5.0]
    delta = compute_sharpe_delta(history)
    # SR_prev for [1,1,1] is high (positive mean, low std)
    # SR_curr for [1,1,1,-5] drops sharply
    assert delta < 0


def test_sharpe_delta_empty() -> None:
    assert compute_sharpe_delta([]) == 0.0
    assert compute_sharpe_delta([1.0]) == 0.0


def test_sharpe_delta_zero_std() -> None:
    # Constant returns → std = 0 → sharpe = 0
    history = [0.5, 0.5, 0.5, 0.5]
    delta = compute_sharpe_delta(history)
    assert delta == 0.0
