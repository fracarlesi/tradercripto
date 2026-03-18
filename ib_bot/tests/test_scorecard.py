"""Tests for paper-trading scorecard."""

import pytest
from decimal import Decimal

from ib_bot.services.scorecard import (
    PromotionState,
    Scorecard,
    ScorecardMetrics,
)
from ib_bot.services.trade_journal import SessionRecord, TradeRecord


def _make_session(
    date: str,
    pnl: str,
    trades: int = 2,
    trade_pnl: str | None = None,
) -> SessionRecord:
    """Create a test session with given P&L."""
    t_pnl = trade_pnl or str(Decimal(pnl) / trades) if trades > 0 else "0"
    records = [
        TradeRecord(
            trade_id=f"t_{date}_{i}",
            symbol="MES",
            direction="long",
            setup_type="orb_long",
            entry_price="5000",
            entry_time=f"{date}T10:00:00",
            contracts=1,
            stop_price="4990",
            target_price="5015",
            exit_price="5010",
            exit_time=f"{date}T11:00:00",
            exit_reason="TP",
            pnl=t_pnl,
        )
        for i in range(trades)
    ]
    return SessionRecord(
        date=date,
        trades=records,
        total_pnl=pnl,
        trade_count=trades,
    )


@pytest.fixture
def scorecard() -> Scorecard:
    return Scorecard()


def test_empty_sessions(scorecard: Scorecard) -> None:
    """Empty sessions returns HOLD_PAPER."""
    metrics = scorecard.evaluate([])
    assert metrics.promotion_state == PromotionState.HOLD_PAPER
    assert metrics.total_trades == 0


def test_hold_paper_insufficient_trades(scorecard: Scorecard) -> None:
    """Less than 30 trades in 20 sessions -> HOLD_PAPER."""
    sessions = [
        _make_session(f"2026-03-{i+1:02d}", "10.00", trades=1)
        for i in range(20)
    ]
    metrics = scorecard.evaluate(sessions)
    assert metrics.promotion_state == PromotionState.HOLD_PAPER
    assert metrics.total_trades == 20  # < 30


def test_halt_on_drawdown(scorecard: Scorecard) -> None:
    """Drawdown > $400 triggers HALT."""
    # 10 sessions with escalating losses
    sessions = [
        _make_session(f"2026-03-{i+1:02d}", "-50.00", trades=2, trade_pnl="-25.00")
        for i in range(10)
    ]
    metrics = scorecard.evaluate(sessions)
    assert metrics.promotion_state == PromotionState.HALT


def test_halt_on_5s_loss(scorecard: Scorecard) -> None:
    """5-session loss > $150 triggers HALT."""
    # Good sessions followed by bad recent 5
    good = [
        _make_session(f"2026-03-{i+1:02d}", "20.00", trades=2, trade_pnl="10.00")
        for i in range(15)
    ]
    bad = [
        _make_session(f"2026-03-{i+16:02d}", "-35.00", trades=2, trade_pnl="-17.50")
        for i in range(5)
    ]
    metrics = scorecard.evaluate(good + bad)
    assert metrics.promotion_state == PromotionState.HALT


def test_candidate_live_micro() -> None:
    """Meeting all criteria -> CANDIDATE_LIVE_MICRO."""
    scorecard = Scorecard(
        candidate_min_trades=10,  # Lower for test
        candidate_pf_20s=Decimal("1.1"),
        candidate_pf_10s=Decimal("1.0"),
        candidate_max_dd=Decimal("300"),
        candidate_min_wr=30.0,
    )
    # 20 sessions, 2 trades each = 40 trades, mostly winning
    sessions = [
        _make_session(f"2026-03-{i+1:02d}", "15.00", trades=2, trade_pnl="7.50")
        for i in range(20)
    ]
    metrics = scorecard.evaluate(sessions)
    assert metrics.promotion_state == PromotionState.CANDIDATE_LIVE_MICRO


def test_fail_state() -> None:
    """20 sessions with negative PnL and low PF -> FAIL."""
    scorecard = Scorecard()
    # 20 sessions: mostly losing with PF < 0.8
    sessions = []
    for i in range(20):
        if i % 5 == 0:
            sessions.append(
                _make_session(f"2026-03-{i+1:02d}", "5.00", trades=2, trade_pnl="2.50")
            )
        else:
            sessions.append(
                _make_session(f"2026-03-{i+1:02d}", "-10.00", trades=2, trade_pnl="-5.00")
            )
    metrics = scorecard.evaluate(sessions)
    assert metrics.total_pnl < 0
    assert metrics.promotion_state == PromotionState.FAIL


def test_format_report() -> None:
    """Format report produces string output."""
    metrics = ScorecardMetrics(
        window_sessions=20,
        total_trades=42,
        total_pnl=Decimal("150"),
        profit_factor=Decimal("1.35"),
        win_rate=48.0,
        max_drawdown=Decimal("85"),
        recent_5s_pnl=Decimal("30"),
        promotion_state=PromotionState.CANDIDATE_LIVE_MICRO,
    )
    report = Scorecard.format_report(metrics)
    assert "CANDIDATE_LIVE_MICRO" in report
    assert "$150" in report
