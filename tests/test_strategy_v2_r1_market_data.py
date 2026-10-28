"""Adversarial point-in-time closed-bar contract tests."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from strategy_v2.market_data import ClosedBar, validate_bar_series


BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
D = Decimal


def bar(index: int, *, high="12", low="8", open_="10", close="10",
        volume="5", final=True, available_delay=0) -> ClosedBar:
    start = BASE + timedelta(minutes=index)
    return ClosedBar(f"bar-{index}", "ABCUSDT", "1m", start,
                     start + timedelta(minutes=1),
                     start + timedelta(minutes=1 + available_delay),
                     D(open_), D(high), D(low), D(close), D(volume), final)


def test_closed_bar_is_immutable_and_timestamps_are_explicit():
    item = bar(0)
    assert validate_bar_series((item,), as_of=item.available_at) == (item,)
    with pytest.raises(FrozenInstanceError):
        item.high = D("20")
    with pytest.raises(TypeError):
        ClosedBar(bar_id="missing")


@pytest.mark.parametrize("change", [
    {"final": False}, {"high": D("9")}, {"low": D("11")},
    {"open": D("0")}, {"high": D("NaN")}, {"volume": D("-1")},
    {"close": 10.0}, {"available_at": BASE},
    {"open_time": datetime(2026, 10, 1)},
])
def test_invalid_or_unclosed_bars_fail_closed(change):
    with pytest.raises((ValueError, TypeError)):
        replace(bar(0), **change)


def test_one_bar_and_empty_input_are_safe():
    assert validate_bar_series((), as_of=BASE) == ()
    assert validate_bar_series((bar(0),), as_of=bar(0).available_at) == (bar(0),)


def test_duplicate_bar_id_timestamp_and_out_of_order_rejected():
    first, second = bar(0), bar(1)
    with pytest.raises(ValueError, match="duplicate bar_id"):
        validate_bar_series((first, replace(second, bar_id=first.bar_id)), as_of=second.available_at)
    with pytest.raises(ValueError, match="chronological"):
        validate_bar_series((second, first), as_of=second.available_at)
    with pytest.raises(ValueError, match="chronological"):
        validate_bar_series((first, replace(second, open_time=first.open_time)), as_of=second.available_at)
    with pytest.raises(ValueError, match="availability"):
        validate_bar_series((replace(first, available_at=second.available_at), second),
                            as_of=second.available_at)


def test_future_unavailable_bar_is_rejected_not_silently_filtered():
    with pytest.raises(ValueError, match="future unavailable"):
        validate_bar_series((bar(0), bar(1)), as_of=bar(0).available_at)


def test_mixed_symbol_or_timeframe_rejected():
    with pytest.raises(ValueError, match="mixed"):
        validate_bar_series((bar(0), replace(bar(1), symbol="OTHER")), as_of=bar(1).available_at)
    with pytest.raises(ValueError, match="mixed"):
        validate_bar_series((bar(0), replace(bar(1), timeframe="5m")), as_of=bar(1).available_at)
