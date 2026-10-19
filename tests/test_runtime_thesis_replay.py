from datetime import datetime, timedelta, timezone

from scripts.audit_runtime_theses_1m import (
    aggregate_bars,
    extract_theses,
    verify_recorded_candles,
)
from trading_v2.models import Bar


UTC = timezone.utc
T0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _bar(minute: int, *, close: float, volume: float = 10.0) -> Bar:
    opened = T0 + timedelta(minutes=minute)
    return Bar(
        symbol="TEST/USDT",
        timeframe="1m",
        open_time=opened,
        close_time=opened + timedelta(minutes=1),
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.2,
        close=close,
        volume=volume,
    )


def test_extract_theses_uses_setup_thesis_identity_not_repeated_signals():
    thesis_id = "setup_same_thesis"
    events = [
        {
            "timestamp": "2026-08-30T12:00:00Z",
            "stage": "MARKET_CONTEXT",
            "decision_id": "decision-1",
            "symbol": "TEST/USDT",
            "details": {
                "indicators": {"atr": 1.25},
                "setup_context": {
                    "setup_anchor": {"low": 99.0, "high": 101.0},
                    "optimal_entry": 100.0,
                    "invalidation_level": 98.5,
                },
            },
        },
        {
            "timestamp": "2026-08-30T12:00:01Z",
            "stage": "HYBRID_DECISION",
            "status": "BUY",
            "decision_id": "decision-1",
            "trade_id": "trade-1",
            "symbol": "TEST/USDT",
            "details": {"setup_thesis_id": thesis_id},
        },
        {
            "timestamp": "2026-08-30T12:15:01Z",
            "stage": "HYBRID_DECISION",
            "status": "BUY",
            "decision_id": "decision-2",
            "trade_id": "trade-2",
            "symbol": "TEST/USDT",
            "details": {"setup_thesis_id": thesis_id},
        },
        {
            "timestamp": "2026-08-30T12:15:02Z",
            "stage": "RR_FILTER",
            "status": "PASS",
            "decision_id": "decision-2",
            "trade_id": "trade-2",
            "symbol": "TEST/USDT",
            "details": {"rr": 1.8},
        },
    ]

    theses = extract_theses(events)

    assert len(theses) == 1
    assert theses[0]["thesis_id"] == thesis_id
    assert theses[0]["signal_event_count"] == 2
    assert theses[0]["rr_passes"] == 1
    assert theses[0]["proximal_edge"] == 101.0
    assert theses[0]["rr_snapshots"][0]["market_context"] == {}


def test_aggregate_bars_requires_a_complete_contiguous_window():
    bars = tuple(_bar(index, close=100.0 + index, volume=index + 1) for index in range(5))

    result = aggregate_bars(bars, opened_at=T0, minutes=5)

    assert result == {
        "open": 99.9,
        "high": 104.2,
        "low": 99.8,
        "close": 104.0,
        "volume": 15.0,
        "rows": 5,
    }
    assert aggregate_bars(bars[:-1], opened_at=T0, minutes=5) is None


def test_verify_recorded_candles_reports_exact_match_and_field_mismatch():
    bars = tuple(_bar(index, close=100.0 + index) for index in range(15))
    aggregated = aggregate_bars(bars, opened_at=T0, minutes=15)
    exact = {
        "timestamp": "2026-08-30T12:15:01Z",
        "stage": "MARKET_BAR",
        "symbol": "TEST/USDT",
        "details": {
            "timeframe": "15m",
            "open_time": T0.isoformat().replace("+00:00", "Z"),
            **{key: aggregated[key] for key in ("open", "high", "low", "close", "volume")},
        },
    }
    changed = {
        **exact,
        "details": {**exact["details"], "close": exact["details"]["close"] + 1.0},
    }

    result = verify_recorded_candles(
        [exact, changed], {"TEST/USDT": bars}
    )

    assert result["comparison_count"] == 2
    assert result["matched"] == 1
    assert result["mismatched"] == 1
    assert result["comparisons"][1]["mismatched_fields"] == ["close"]


def test_verify_recorded_candles_includes_market_context_closed_bar():
    bars = tuple(_bar(index, close=100.0 + index) for index in range(15))
    aggregated = aggregate_bars(bars, opened_at=T0, minutes=15)
    event = {
        "timestamp": "2026-08-30T12:15:01Z",
        "stage": "MARKET_CONTEXT",
        "symbol": "TEST/USDT",
        "details": {
            "timeframe": "15m",
            "closed_bar": {
                "open_time": T0.isoformat().replace("+00:00", "Z"),
                **{
                    key: aggregated[key]
                    for key in ("open", "high", "low", "close", "volume")
                },
            },
        },
    }

    result = verify_recorded_candles([event], {"TEST/USDT": bars})

    assert result["comparison_count"] == 1
    assert result["matched"] == 1
    assert result["comparisons"][0]["stage"] == "MARKET_CONTEXT"
