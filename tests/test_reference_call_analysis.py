from datetime import datetime, timedelta, timezone

from scripts.analyze_reference_calls import simulate_reference_outcome
from trading_v2.models import Bar


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(minutes, *, high, low, open_price=100, close_price=100):
    opened = T0 + timedelta(minutes=minutes)
    return Bar(
        symbol="TEST/USDT",
        timeframe="1m",
        open_time=opened,
        close_time=opened + timedelta(minutes=1),
        open=open_price,
        high=high,
        low=low,
        close=close_price,
        volume=1,
    )


def test_reference_outcome_reports_target_before_later_stop():
    call = {
        "timestamp": T0.isoformat(),
        "side": "BUY",
        "entry": 100,
        "stop": 98,
        "targets": [104],
    }
    result = simulate_reference_outcome(
        call,
        (
            _bar(0, high=101, low=99.5),
            _bar(1, high=104.5, low=99.5),
            _bar(2, high=101, low=97.5),
        ),
    )

    assert result["target_hits"][0] is not None
    assert result["filled_at"] is not None
    assert result["stop_at"] is None
    assert result["mfe_r"] == 2.25


def test_reference_outcome_does_not_credit_an_unfilled_limit():
    call = {
        "timestamp": T0.isoformat(),
        "side": "BUY",
        "entry": 100,
        "stop": 98,
        "targets": [104],
    }
    result = simulate_reference_outcome(
        call,
        (
            _bar(0, high=110, low=105, open_price=107, close_price=108),
            _bar(1, high=112, low=106, open_price=108, close_price=110),
        ),
    )

    assert result["filled_at"] is None
    assert result["target_hits"] == [None]
    assert result["mfe_r"] is None


def test_reference_outcome_marks_entry_bar_terminal_order_ambiguous():
    call = {
        "timestamp": T0.isoformat(),
        "side": "BUY",
        "entry": 100,
        "stop": 98,
        "targets": [104],
    }
    result = simulate_reference_outcome(
        call,
        (_bar(0, high=105, low=97),),
    )

    assert result["filled_at"] is not None
    assert result["ambiguous_entry_bar"] is True
    assert result["target_hits"] == [None]
    assert result["stop_at"] is None
