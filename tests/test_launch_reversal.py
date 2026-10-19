from datetime import datetime, timezone

from trading_v2.binance_archive import KlineRow
from trading_v2.launch_reversal import detect_launch_reversal
from trading_v2.models import Side


def _bar(index, open_price, close_price, *, low=None, high=None):
    low = min(open_price, close_price) - 0.2 if low is None else low
    high = max(open_price, close_price) + 0.2 if high is None else high
    return KlineRow(
        open_time_ms=index * 60_000,
        open=float(open_price), high=float(high), low=float(low), close=float(close_price),
        base_volume=1.0, close_time_ms=(index + 1) * 60_000 - 1,
        quote_volume=100.0, trades=1,
        taker_buy_base_volume=0.5, taker_buy_quote_volume=50.0,
    )


def _buy_reversal_rows():
    rows = []
    previous = 100.0
    for index in range(30):
        close = 100.0 - (20.0 * (index + 1) / 30.0)
        rows.append(_bar(index, previous, close))
        previous = close
    # Keep the full 60m confirmation window contiguous.  The first bar reclaims
    # the observed midpoint with a directional close.
    rows.append(_bar(30, 89.0, 91.0, low=88.5, high=91.2))
    for index in range(31, 90):
        rows.append(_bar(index, 91.0, 91.0))
    return rows


def test_launch_flush_requires_midpoint_reclaim_before_plan():
    decision = detect_launch_reversal(
        "TESTUSDT",
        datetime.fromtimestamp(0, tz=timezone.utc),
        _buy_reversal_rows(),
    )

    assert decision.status == "PLAN_CREATED"
    assert decision.side is Side.BUY
    assert decision.plan is not None
    assert decision.plan.stop < decision.plan.entry < decision.plan.target
    assert decision.plan.planned_rr == 2.0


def test_launch_wick_is_soft_but_close_beyond_buffer_is_hard_invalidation():
    rows = _buy_reversal_rows()
    # The low alone can sweep the range, but a close below the buffered level
    # must terminate the thesis before any later reclaim.
    rows[30] = _bar(30, 80.0, 73.0, low=70.0, high=80.2)
    decision = detect_launch_reversal(
        "TESTUSDT",
        datetime.fromtimestamp(0, tz=timezone.utc),
        rows,
    )

    assert decision.status == "HARD_INVALIDATED"
    assert decision.plan is None


def test_launch_without_extreme_is_not_forced_into_a_trade():
    rows = [_bar(index, 100.0, 100.0) for index in range(90)]
    decision = detect_launch_reversal(
        "TESTUSDT",
        datetime.fromtimestamp(0, tz=timezone.utc),
        rows,
    )

    assert decision.status == "NO_EXTREME_PATTERN"
    assert decision.plan is None

