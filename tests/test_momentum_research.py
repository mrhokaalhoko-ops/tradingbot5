from datetime import datetime, timedelta, timezone

from trading_v2.models import Bar
from trading_v2.momentum_research import (
    MomentumResearchConfig,
    apply_research_confirmation_policy,
    apply_research_impulse_policy,
    closed_bars_to_live_frame,
    historical_hot_candidate,
)


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _minute_bars(count, *, last_jump_pct=0.0):
    rows = []
    price = 100.0
    for index in range(count):
        opened = T0 + timedelta(minutes=index)
        close = price
        if index == count - 1:
            close = price * (1.0 + last_jump_pct / 100.0)
        rows.append(Bar(
            symbol="TEST/USDT",
            timeframe="1m",
            open_time=opened,
            close_time=opened + timedelta(seconds=59, milliseconds=999),
            open=price,
            high=max(price, close) * 1.001,
            low=min(price, close) * 0.999,
            close=close,
            volume=10_000,
        ))
        price = close
    return tuple(rows)


def test_closed_frame_preserves_latest_closed_bar_and_appends_live_placeholder():
    bars = _minute_bars(3)
    frame = closed_bars_to_live_frame(bars)
    assert len(frame) == 4
    assert frame.iloc[-2]["timestamp"] == bars[-1].open_time
    assert frame.iloc[-2]["close"] == bars[-1].close
    assert frame.iloc[-1]["volume"] == 0.0


def test_historical_hot_candidate_uses_only_current_and_past_bars():
    bars = _minute_bars(24 * 60 + 1, last_jump_pct=1.0)
    candidate = historical_hot_candidate(
        bars,
        len(bars) - 1,
        config=MomentumResearchConfig(),
    )
    assert candidate is not None
    assert candidate["tags"] == ["MOMENTUM_UP"]
    assert candidate["change_1m_pct"] == 1.0


def test_historical_hot_candidate_requires_full_24h_history():
    bars = _minute_bars(100, last_jump_pct=2.0)
    assert historical_hot_candidate(
        bars, len(bars) - 1, config=MomentumResearchConfig()
    ) is None


def test_scored_impulse_keeps_risk_gates_hard_but_scores_supporting_evidence():
    impulse = {
        "qualified": False,
        "side": "buy",
        "failed_gates": ["local_15m_structure", "volume_expansion"],
        "evidence": {"gates": {
            "not_new_contract": True,
            "liquidity": True,
            "not_extreme_24h": True,
            "regime_known": True,
            "regime_policy": True,
            "directional_body": True,
            "breakout": True,
            "local_15m_structure": False,
            "ema_5m_alignment": True,
            "close_location": True,
            "volume_expansion": False,
            "displacement_range": False,
        }},
    }
    accepted = apply_research_impulse_policy(
        impulse,
        context_15m={"up_structure": False, "down_structure": False},
        policy="scored_v2",
    )
    assert accepted["qualified"] is True
    opposed = apply_research_impulse_policy(
        impulse,
        context_15m={"up_structure": False, "down_structure": True},
        policy="scored_v2",
    )
    assert opposed["qualified"] is False
    assert "15m_not_opposed" in opposed["failed_gates"]


def test_scored_confirmation_never_softens_invalidation_or_breakout_hold():
    confirmation = {
        "status": "WAIT",
        "passed": False,
        "evidence": {"gates": {
            "not_invalidated": True,
            "holds_breakout": True,
            "not_extended": True,
            "directional_close": True,
            "close_location": True,
            "participation": False,
        }},
    }
    assert apply_research_confirmation_policy(
        confirmation, policy="scored_v2"
    )["passed"] is True
    confirmation["evidence"]["gates"]["not_invalidated"] = False
    rejected = apply_research_confirmation_policy(
        confirmation, policy="scored_v2"
    )
    assert rejected["passed"] is False
    assert "not_invalidated" in rejected["failed_gates"]
