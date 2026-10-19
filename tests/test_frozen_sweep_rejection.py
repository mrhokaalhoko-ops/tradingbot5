from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.replay_frozen_sweep_rejection import (
    build_plan,
    planned_net_rr,
    select_candidates,
)
from trading_v2.models import Side
from trading_v2.replay import ReplayCosts


def _frame(**updates):
    values = {
        "observation_valid": True,
        "global_regime": "RANGE",
        "volume_ratio": 1.3,
        "atr": 1.0,
        "prior_low_24": 99.0,
        "prior_high_24": 103.0,
        "low": 98.8,
        "high": 100.5,
        "open": 99.2,
        "close": 100.2,
        "close_location": 0.82,
        "wick_sweep_low_24": 1,
        "wick_sweep_high_24": 0,
        "15m_ema_alignment": 0,
        "1h_ema_alignment": 1,
    }
    values.update(updates)
    return pd.DataFrame([values])


def test_candidate_requires_real_sweep_depth_rejection_and_participation():
    assert bool(select_candidates(_frame()).iloc[0])
    assert not bool(select_candidates(_frame(volume_ratio=1.19)).iloc[0])
    assert not bool(select_candidates(_frame(low=98.99)).iloc[0])
    assert not bool(select_candidates(_frame(close=99.0, open=99.2)).iloc[0])


def test_build_plan_uses_sweep_wick_stop_and_prior_range_midpoint():
    row = SimpleNamespace(
        symbol="TESTUSDT",
        close_time=pd.Timestamp("2026-01-01T00:04:59.999Z"),
        open=99.2,
        high=100.5,
        low=98.8,
        close=100.2,
        atr=1.0,
        prior_low_24=99.0,
        prior_high_24=103.0,
        wick_sweep_low_24=1,
        wick_sweep_high_24=0,
        volume_ratio=1.3,
    )
    plan = build_plan(row)
    assert plan.side is Side.BUY
    assert plan.stop == pytest.approx(98.7)
    assert plan.target == pytest.approx(101.0)


def test_net_rr_charges_entry_target_slippage_and_both_fees():
    costs = ReplayCosts()
    net = planned_net_rr(
        entry=100.0,
        stop=99.0,
        target=102.0,
        side=Side.BUY,
        costs=costs,
    )
    assert net < 2.0
    assert net > 1.8
    assert planned_net_rr(
        entry=103.0,
        stop=99.0,
        target=102.0,
        side=Side.BUY,
        costs=costs,
    ) == float("-inf")
