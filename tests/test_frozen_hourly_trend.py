from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.replay_frozen_hourly_trend import build_plan, select_candidates
from trading_v2.models import Side


def _frame():
    rows = []
    start = pd.Timestamp("2026-01-01T00:04:59.999Z")
    for index in range(13):
        rows.append({
            "close_time": start + pd.Timedelta(minutes=5 * index),
            "observation_valid": True,
            "global_regime": "MILD_RISK_ON",
            "1h_return_1_pct": 1.0,
            "1h_atr_pct": 1.0,
            "1h_volume_ratio": 1.1,
            "1h_ema_alignment": 0 if index == 0 else 1,
        })
    return pd.DataFrame(rows)


def test_selection_requires_fresh_completed_hour_transition():
    frame = _frame()
    chosen = select_candidates(frame)
    assert chosen.sum() == 1
    assert chosen.iloc[11]
    frame.loc[11, "global_regime"] = "MILD_RISK_OFF"
    assert not select_candidates(frame).any()


def test_plan_uses_slow_hourly_atr_risk_and_two_r_target():
    row = SimpleNamespace(**{
        "symbol": "TESTUSDT",
        "close_time": pd.Timestamp("2026-01-01T00:59:59.999Z"),
        "close": 100.0,
        "h1_atr": 2.0,
        "h1_ema_alignment": 1,
        "h1_volume_ratio": 1.1,
    })
    plan = build_plan(row)
    assert plan.side is Side.BUY
    assert plan.stop == pytest.approx(97.0)
    assert plan.target == pytest.approx(106.0)
    assert plan.exit_by - plan.created_at == pd.Timedelta(hours=48)
