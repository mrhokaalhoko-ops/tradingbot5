from __future__ import annotations

import pandas as pd

from strategy.short_high_confidence import evaluate_short_high_confidence


def _bars(last: dict | None = None, history_low: float = 95.0) -> pd.DataFrame:
    rows = []
    start = pd.Timestamp("2026-10-11T00:00:00Z")
    for index in range(20):
        rows.append({
            "timestamp": start + pd.Timedelta(minutes=5 * index),
            "open": 100.5,
            "high": 101.0,
            "low": history_low,
            "close": 100.0,
            "volume": 100.0,
        })
    rows.append({
        "timestamp": start + pd.Timedelta(minutes=100),
        "open": 101.0,
        "high": 102.5,
        "low": 99.5,
        "close": 100.0,
        "volume": 80.0,
        **(last or {}),
    })
    # Forming candle: the evaluator must never consume it.
    rows.append({
        "timestamp": start + pd.Timedelta(minutes=105),
        "open": 100.0,
        "high": 110.0,
        "low": 90.0,
        "close": 109.0,
        "volume": 9999.0,
    })
    return pd.DataFrame(rows)


def _evaluate(**overrides):
    kwargs = {
        "decision_price": 100.0,
        "atr": 2.0,
        "trend_1h": "bearish",
        "context_4h": {"known": True, "trend": "BEARISH"},
        "context_1h": {"known": True, "support": 90.0, "resistance": 101.0},
        "market_levels": {
            "swing_highs": [{"price": 101.0}],
            "swing_lows": [{"price": 90.0}],
            "bearish_obs": [],
            "bullish_obs": [],
        },
        "trigger_bars": _bars(),
        "invalidation_level": 104.0,
    }
    kwargs.update(overrides)
    return evaluate_short_high_confidence(**kwargs)


def test_rejects_clearly_bullish_4h_context():
    result = _evaluate(context_4h={"known": True, "trend": "BULLISH"})
    assert result["status"] == "REJECTED"
    assert result["reason"] == "SHORT_REJECT_HTF_BULLISH"


def test_rejects_when_support_is_too_close_below_entry():
    result = _evaluate(
        context_1h={"known": True, "support": 99.5, "resistance": 101.0},
        market_levels={
            "swing_highs": [{"price": 101.0}],
            "swing_lows": [{"price": 99.5}],
        },
    )
    assert result["status"] == "REJECTED"
    assert result["reason"] == "SHORT_REJECT_SUPPORT_TOO_CLOSE"
    assert result["nearest_support_demand"]["distance_atr"] == 0.25


def test_resistance_rejection_passes_without_volume_or_sweep_requirement():
    result = _evaluate()
    assert result["status"] == "PASS"
    assert result["reason"] == "SHORT_PASS_RESISTANCE_REJECTION"
    assert result["trigger"]["volume_required"] is False
    assert result["trigger"]["sweep_required"] is False


def test_breakdown_and_failed_retest_is_a_second_valid_pattern():
    result = _evaluate(
        decision_price=99.0,
        trigger_bars=_bars(
            last={"open": 100.8, "high": 101.0, "low": 98.5, "close": 99.0},
            history_low=100.0,
        ),
        context_1h={"known": True, "support": 90.0, "resistance": 110.0},
        market_levels={
            "swing_highs": [{"price": 110.0}],
            "swing_lows": [{"price": 90.0}],
        },
    )
    assert result["status"] == "PASS"
    assert result["reason"] == "SHORT_PASS_BREAKDOWN_RETEST"
    assert result["trigger"]["breakdown_retest"] is True


def test_valid_location_without_closed_5m_trigger_waits():
    result = _evaluate(
        trigger_bars=_bars(
            last={"open": 99.8, "high": 100.5, "low": 99.5, "close": 100.2}
        )
    )
    assert result["status"] == "WAIT_SHORT_CONFIRMATION"
    assert result["reason"] == "SHORT_REJECT_5M_WAITING"


def test_mid_air_short_without_resistance_or_breakdown_is_rejected():
    result = _evaluate(
        context_1h={"known": True, "support": 90.0, "resistance": None},
        market_levels={"swing_lows": [{"price": 90.0}]},
        trigger_bars=_bars(
            last={"open": 100.2, "high": 100.4, "low": 99.8, "close": 100.0},
            history_low=95.0,
        ),
    )
    assert result["status"] == "REJECTED"
    assert result["reason"] == "SHORT_REJECT_LOCATION"


def test_forming_5m_candle_is_ignored():
    bars = _bars()
    bars.loc[bars.index[-1], ["open", "high", "low", "close"]] = [
        99.0, 105.0, 98.0, 104.0
    ]
    result = _evaluate(trigger_bars=bars)
    assert result["status"] == "PASS"
    assert result["trigger"]["close"] == 100.0
