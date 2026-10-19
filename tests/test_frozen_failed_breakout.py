from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts.replay_frozen_failed_breakout import (
    FailureDecision,
    build_plan,
    find_failed_breakout,
)
from trading_v2.models import Bar, Side


def _bar(index, open_price, high, low, close):
    opened = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * index)
    return Bar(
        symbol="TEST/USDT", timeframe="5m", open_time=opened,
        close_time=opened + timedelta(minutes=5) - timedelta(milliseconds=1),
        open=open_price, high=high, low=low, close=close, volume=1.0,
    )


def _row():
    return SimpleNamespace(
        symbol="TESTUSDT", breakout_up_24=1, prior_high_24=100.0,
        prior_low_24=96.0, atr=1.0, high=100.8, low=99.4,
        volume_ratio=1.5,
    )


def test_reentry_close_confirms_fade_but_extension_close_invalidates():
    confirmed = find_failed_breakout(
        _row(), (_bar(0, 100.5, 100.7, 99.5, 99.7),)
    )
    assert confirmed.status == "CONFIRMED"
    invalid = find_failed_breakout(
        _row(), (_bar(0, 100.5, 101.4, 100.4, 101.2),)
    )
    assert invalid.status == "HARD_INVALIDATED"


def test_plan_fades_original_breakout_toward_range_midpoint():
    confirmation = _bar(0, 100.5, 100.7, 99.5, 99.7)
    plan = build_plan(
        _row(), FailureDecision("CONFIRMED", confirmation, extreme=100.8)
    )
    assert plan.side is Side.SELL
    assert plan.stop == pytest.approx(100.9)
    assert plan.target == pytest.approx(98.0)
