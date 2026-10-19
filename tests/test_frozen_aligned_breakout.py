from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.replay_frozen_aligned_breakout import build_plan, select_candidates
from trading_v2.models import Side


def _frame(**overrides):
    values = {
        "global_regime": "MILD_RISK_ON", "observation_valid": True,
        "breakout_up_24": 1, "breakdown_down_24": 0,
        "ema_alignment": 1, "15m_ema_alignment": 1, "1h_ema_alignment": 1,
        "volume_ratio": 1.8, "body_pct": 0.6, "atr_pct": 1.0,
        "close_location": 0.8, "ema20_distance_atr": 1.0,
    }
    values.update(overrides)
    return pd.DataFrame([values])


def test_selector_requires_alignment_volume_and_regime_permission():
    assert bool(select_candidates(_frame()).iloc[0]) is True
    assert bool(select_candidates(_frame(volume_ratio=1.49)).iloc[0]) is False
    assert bool(select_candidates(_frame(**{"1h_ema_alignment": -1})).iloc[0]) is False
    assert bool(select_candidates(_frame(global_regime="STRONG_RISK_OFF")).iloc[0]) is False


def test_build_plan_uses_structural_stop_and_fixed_two_r_target():
    row = SimpleNamespace(
        breakout_up_24=1,
        close=101.0,
        atr=2.0,
        low=99.5,
        prior_high_24=100.0,
        close_time=pd.Timestamp("2026-01-01T00:04:59.999Z"),
        symbol="BTCUSDT",
        volume_ratio=2.0,
    )
    plan = build_plan(row)
    assert plan.side is Side.BUY
    assert plan.stop == pytest.approx(99.3)
    assert plan.target == pytest.approx(104.4)
    assert (plan.target - plan.entry) / (plan.entry - plan.stop) == pytest.approx(2.0)


def test_build_plan_rejects_excessive_structural_risk():
    row = SimpleNamespace(
        breakout_up_24=1,
        close=101.0,
        atr=1.0,
        low=96.0,
        prior_high_24=100.0,
        close_time=pd.Timestamp("2026-01-01T00:04:59.999Z"),
        symbol="BTCUSDT",
        volume_ratio=2.0,
    )
    with pytest.raises(ValueError, match="risk outside"):
        build_plan(row)
