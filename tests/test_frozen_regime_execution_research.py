from datetime import datetime, timedelta, timezone

from scripts.run_frozen_regime_execution_research import build_levels
from trading_v2.models import Bar


def _bar(low: float) -> Bar:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Bar(
        symbol="TEST/USDT",
        timeframe="5m",
        open_time=start,
        close_time=start + timedelta(minutes=5),
        open=100,
        high=101,
        low=low,
        close=100,
        volume=1,
    )


def test_execution_variants_use_predeclared_stop_and_reward_geometry():
    bars = [_bar(99.0), _bar(98.0)]

    atr_stop, atr_target = build_levels(100.0, 2.0, bars, "ATR15_TARGET2R")
    structural_stop, structural_target = build_levels(
        100.0, 2.0, bars, "STRUCTURE12_TARGET2R"
    )

    assert atr_stop == 97.0
    assert atr_target == 106.0
    assert structural_stop == 97.8
    assert structural_target == 104.4
