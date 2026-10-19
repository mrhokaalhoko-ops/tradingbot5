from datetime import datetime, timedelta, timezone

import pytest

from scripts.analyze_runtime_thesis_funnel_1m import directional_path
from trading_v2.models import Bar


UTC = timezone.utc
T0 = datetime(2026, 8, 30, tzinfo=UTC)


def _bar(minute: int, close: float) -> Bar:
    opened = T0 + timedelta(minutes=minute)
    return Bar(
        symbol="TEST/USDT", timeframe="1m",
        open_time=opened, close_time=opened + timedelta(minutes=1),
        open=100 if minute == 1 else close,
        high=max(100, close) + 1, low=min(100, close) - 1,
        close=close, volume=1,
    )


def test_directional_path_starts_at_first_full_minute_after_decision():
    bars = tuple(_bar(minute, 100 + minute) for minute in range(5))

    result = directional_path(
        bars, at=T0 + timedelta(seconds=30), side="BUY", minutes=3
    )

    assert result["entry_at"] == "2026-08-30T00:01:00Z"
    assert result["entry"] == 100
    assert result["exit"] == 102
    assert result["signed_return_pct"] == pytest.approx(2.0)


def test_directional_path_flips_return_for_sell():
    bars = tuple(_bar(minute, 100 + minute) for minute in range(5))

    result = directional_path(bars, at=T0, side="SELL", minutes=2)

    assert result["signed_return_pct"] < 0
