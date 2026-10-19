from datetime import datetime, timedelta, timezone

from scripts.replay_runtime_theses_1m import (
    BALANCED_CONFIRMATION_PROFILE,
    aggregate_1m,
    invalidation_buffer_stop,
    planned_rr,
    snapshot_plan_inputs,
)
from trading_v2.models import Bar


UTC = timezone.utc
T0 = datetime(2026, 8, 30, tzinfo=UTC)


def test_snapshot_plan_inputs_uses_nearest_blocking_target_on_rr_reject():
    thesis = {
        "side": "BUY", "midpoint": 100, "invalidation": 99,
        "zone_low": 99.5, "zone_high": 100.5, "atr_at_creation": 2,
    }
    snapshot = {
        "details": {
            "target_plan": {
                "valid": False,
                "entry": 100,
                "sl": 96,
                "rr": 0.5,
                "blocking_target": {"type": "liquidity", "tp": 102},
                "stop_plan": {"invalidation_level": 99, "atr": 2},
            }
        },
        "market_context": {"setup_context": {}},
    }

    result = snapshot_plan_inputs(thesis, snapshot)

    assert result["target"] == 102
    assert result["target_type"] == "liquidity"
    assert result["recorded_plan_valid"] is False
    assert BALANCED_CONFIRMATION_PROFILE["min_rr"] == 1.5


def test_invalidation_buffer_stop_and_rr_are_directional():
    assert invalidation_buffer_stop(
        side="BUY", invalidation=99, atr=2, multiplier=0.2
    ) == 98.6
    assert invalidation_buffer_stop(
        side="SELL", invalidation=101, atr=2, multiplier=0.2
    ) == 101.4
    assert planned_rr(side="BUY", entry=100, stop=99, target=102) == 2
    assert planned_rr(side="SELL", entry=100, stop=101, target=98) == 2


def test_aggregate_1m_drops_incomplete_five_minute_bucket():
    bars = []
    for minute in (0, 1, 2, 3, 4, 5, 6, 8, 9):
        opened = T0 + timedelta(minutes=minute)
        bars.append(Bar(
            symbol="TEST/USDT", timeframe="1m",
            open_time=opened, close_time=opened + timedelta(minutes=1),
            open=100, high=101, low=99, close=100.5, volume=1,
        ))

    result = aggregate_1m(bars, minutes=5)

    assert len(result) == 1
    assert result[0].open_time == T0
    assert result[0].volume == 5
