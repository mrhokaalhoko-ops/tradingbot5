from datetime import datetime, timezone

from trading_v2.binance_archive import KlineRow
from trading_v2.launch_events import (
    BAR_MS,
    _candidate_side,
    find_anchor_index,
    measure_launch_event,
)


def _rows(closes, *, start=0, interval_ms=BAR_MS):
    rows = []
    for index, close in enumerate(closes):
        open_price = 100.0 if index == 0 else float(closes[index - 1])
        high = max(open_price, float(close)) + 0.5
        low = min(open_price, float(close)) - 0.5
        rows.append(KlineRow(
            open_time_ms=start + index * interval_ms,
            open=open_price,
            high=high,
            low=low,
            close=float(close),
            base_volume=1.0,
            close_time_ms=start + index * interval_ms + interval_ms - 1,
            quote_volume=100.0,
            trades=1,
            taker_buy_base_volume=0.6,
            taker_buy_quote_volume=60.0,
        ))
    return rows


def test_anchor_rejects_large_future_gap():
    assert find_anchor_index(_rows([100], start=13 * BAR_MS), 0) is None


def test_candidate_gate_is_symmetric_and_does_not_guess_missing_data():
    assert _candidate_side(6.0, 0.8, 0.60) == "LONG"
    assert _candidate_side(-6.0, 0.2, 0.40) == "SHORT"
    assert _candidate_side(6.0, 0.8, None) == "NONE"


def test_forward_returns_start_after_observation_window():
    # First 30m rises 6%; the next bar is the entry.  A later fall must not
    # contaminate the first-30m classification itself.
    closes = [101, 102, 103, 104, 105, 106, 105, 104, 103, 102, 101, 100, 99, 98, 97, 96, 95, 94, 93, 92]
    launch = datetime.fromtimestamp(0, tz=timezone.utc)
    event = measure_launch_event("TESTUSDT", launch, _rows(closes))

    assert event.status == "OK"
    assert abs(event.first_30m_return_pct - 6.0) < 1e-9
    assert event.candidate_side == "LONG"
    assert event.returns_pct["1h"] is not None
    assert event.candidate_returns_pct["1h"] == event.returns_pct["1h"]


def test_one_minute_study_observes_exactly_first_thirty_closed_bars():
    closes = [100.0 + 0.1 * index for index in range(100)]
    launch = datetime.fromtimestamp(0, tz=timezone.utc)
    event = measure_launch_event(
        "TESTUSDT",
        launch,
        _rows(closes, interval_ms=60_000),
        bar_minutes=1,
    )

    assert event.status == "OK"
    assert event.bars_observed == 30
    assert event.returns_pct["1h"] is not None
