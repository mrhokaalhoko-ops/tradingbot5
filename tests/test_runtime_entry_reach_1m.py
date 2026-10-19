from datetime import datetime, timedelta, timezone

from scripts.analyze_runtime_entry_reach_1m import distance_to_zone, reach_metrics
from trading_v2.models import Bar


UTC = timezone.utc
T0 = datetime(2026, 8, 30, tzinfo=UTC)


def _bar(minute: int, low: float, high: float) -> Bar:
    opened = T0 + timedelta(minutes=minute)
    return Bar(
        symbol="TEST/USDT", timeframe="1m", open_time=opened,
        close_time=opened + timedelta(minutes=1), open=(low + high) / 2,
        high=high, low=low, close=(low + high) / 2, volume=1,
    )


def test_distance_to_zone_is_zero_on_intersection_and_exact_outside():
    assert distance_to_zone(_bar(0, 101, 102), entry=100, tolerance=1) == 0
    assert distance_to_zone(_bar(0, 101.5, 102), entry=100, tolerance=1) == 0.5
    assert distance_to_zone(_bar(0, 98, 98.5), entry=100, tolerance=1) == 0.5


def test_reach_metrics_reports_first_touch_without_future_window_leakage():
    bars = tuple(_bar(minute, 102 if minute < 10 else 100, 103 if minute < 10 else 101)
                 for minute in range(180))

    result = reach_metrics(bars, at=T0, entry=100, atr=1)

    assert result["ttl"]["20"]["status"] == "TOUCHED"
    assert result["ttl"]["20"]["minutes_to_touch"] == 11
