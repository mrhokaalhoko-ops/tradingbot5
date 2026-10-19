from types import SimpleNamespace

from scripts.replay_frozen_breakout_retest import (
    build_retest_plan,
    find_retest_confirmation,
)
from trading_v2.models import Bar, Side


def _row(**overrides):
    values = {
        "breakout_up_24": 1,
        "atr": 2.0,
        "prior_high_24": 100.0,
        "prior_low_24": 90.0,
        "symbol": "BTCUSDT",
        "close_time": __import__("pandas").Timestamp("2026-01-01T00:04:59.999Z"),
        "volume_ratio": 2.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _bar(minute, *, open_, high, low, close):
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc)
    return Bar(
        symbol="BTC/USDT", timeframe="5m",
        open_time=start, close_time=start + timedelta(minutes=5) - timedelta(milliseconds=1),
        open=open_, high=high, low=low, close=close, volume=1.0,
    )


def test_touch_and_directional_reclaim_can_confirm_on_same_closed_bar():
    bar = _bar(5, open_=100.1, high=101.0, low=99.8, close=100.9)
    result = find_retest_confirmation(_row(), (bar,))
    assert result.status == "CONFIRMED"
    assert result.confirmation_bar is bar


def test_wick_is_soft_but_close_beyond_hard_level_invalidates():
    wick = _bar(5, open_=100.0, high=100.3, low=98.7, close=99.3)
    reclaim = _bar(10, open_=99.8, high=101.0, low=99.6, close=100.8)
    assert find_retest_confirmation(_row(), (wick, reclaim)).status == "CONFIRMED"

    hard_close = _bar(5, open_=100.0, high=100.1, low=98.5, close=98.9)
    assert find_retest_confirmation(_row(), (hard_close,)).status == "HARD_INVALIDATED"


def test_no_touch_and_touch_without_reclaim_are_distinguished():
    no_touch = _bar(5, open_=102.0, high=103.0, low=101.0, close=102.5)
    assert find_retest_confirmation(_row(), (no_touch,)).status == "NO_TOUCH"

    touch = _bar(5, open_=100.4, high=100.5, low=99.8, close=100.1)
    assert find_retest_confirmation(_row(), (touch,)).status == "TOUCHED_NO_RECLAIM"


def test_retest_plan_enters_after_confirmation_and_uses_two_r():
    confirmation = _bar(5, open_=100.1, high=101.0, low=99.8, close=100.9)
    plan = build_retest_plan(_row(), confirmation)
    assert plan.side is Side.BUY
    assert plan.created_at == confirmation.close_time
    assert plan.target - plan.entry == 2.0 * (plan.entry - plan.stop)
