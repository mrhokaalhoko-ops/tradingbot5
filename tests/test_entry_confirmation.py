from datetime import datetime, timezone

import pandas as pd

from strategy.entry_confirmation import (
    build_setup_thesis_id,
    classify_virtual_invalidation,
    evaluate_entry_confirmation,
)


NOW = datetime(2026, 8, 20, 12, 7, tzinfo=timezone.utc)


def _bars(*, trigger="confirmed", trigger_time="2026-08-20T12:00:00Z"):
    timestamps = pd.date_range(end="2026-08-20T12:05:00Z", periods=24, freq="5min")
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 100.0,
    })
    frame.loc[frame.index[-2], "timestamp"] = pd.Timestamp(trigger_time)
    if trigger == "confirmed":
        frame.loc[frame.index[-2], ["open", "high", "low", "close", "volume"]] = [
            100.0, 100.7, 98.8, 100.5, 150.0,
        ]
    elif trigger == "direction_only":
        frame.loc[frame.index[-2], ["open", "high", "low", "close", "volume"]] = [
            99.9, 100.5, 99.8, 100.3, 100.0,
        ]
    else:
        frame.loc[frame.index[-2], ["open", "high", "low", "close", "volume"]] = [
            100.4, 100.5, 99.8, 100.0, 100.0,
        ]
    return frame


def _context(htf="bullish"):
    return {
        "score": 3,
        "htf_trend": htf,
        "ema_50": 105.0,
        "ema_200": 100.0,
        "adx": 24.0,
        "optimal_entry": 100.0,
        "invalidation_level": 98.5,
        "setup_anchor": {
            "type": "bullish_ob",
            "created_at": "2026-08-20T10:00:00Z",
            "low": 98.5,
            "high": 100.0,
        },
    }


def _profile(**updates):
    profile = {
        "key": "smc_balanced",
        "require_ob": True,
        "min_rr": 1.5,
        "trigger_evidence_min": 1,
        "allow_countertrend_reversal": True,
    }
    profile.update(updates)
    return profile


def _evaluate(bars, context=None, profile=None):
    return evaluate_entry_confirmation(
        side="buy",
        trigger_bars=bars,
        current_price=100.1,
        optimal_entry=100.0,
        invalidation_level=98.5,
        atr_15m=2.0,
        ta_context=context or _context(),
        filter_profile=profile or _profile(),
        planned_rr=1.8,
        now=NOW,
    )


def test_closed_5m_reclaim_with_real_evidence_passes():
    result = _evaluate(_bars())

    assert result["passed"] is True
    assert result["reason"] == "CONFIRMED"
    assert result["evidence"]["swept_liquidity"] is True
    assert result["evidence"]["volume_confirmed"] is True


def test_nearby_but_not_latest_closed_bar_is_rejected():
    bars = _bars(trigger_time="2026-08-20T11:55:00Z")

    result = _evaluate(bars)

    assert result["passed"] is False
    assert result["closed_bar"]["exact_closed_bar"] is False
    assert "data_fresh" in result["failed_gates"]


def test_recorded_zone_touch_allows_reclaim_after_live_price_leaves_zone():
    result = evaluate_entry_confirmation(
        side="buy",
        trigger_bars=_bars(),
        current_price=101.2,
        optimal_entry=100.0,
        invalidation_level=98.5,
        atr_15m=2.0,
        ta_context=_context(),
        filter_profile=_profile(),
        planned_rr=1.8,
        zone_touch_recorded=True,
        now=NOW,
    )

    assert result["passed"] is True
    assert result["evidence"]["current_in_zone"] is False
    assert result["evidence"]["zone_touch_recorded"] is True
    assert result["evidence"]["entry_location_valid"] is True


def test_reclaim_outside_zone_without_recorded_touch_remains_blocked():
    result = evaluate_entry_confirmation(
        side="buy",
        trigger_bars=_bars(),
        current_price=101.2,
        optimal_entry=100.0,
        invalidation_level=98.5,
        atr_15m=2.0,
        ta_context=_context(),
        filter_profile=_profile(),
        planned_rr=1.8,
        now=NOW,
    )

    assert result["passed"] is False
    assert "entry_location" in result["failed_gates"]


def test_recorded_sell_touch_allows_bearish_reclaim_outside_zone():
    bars = _bars()
    bars.loc[bars.index[-2], ["open", "high", "low", "close", "volume"]] = [
        100.1, 101.2, 99.3, 99.5, 150.0,
    ]
    context = {
        **_context(htf="bearish"),
        "ema_50": 95.0,
        "ema_200": 100.0,
        "setup_anchor": {
            "type": "bearish_ob",
            "created_at": "2026-08-20T10:00:00Z",
            "low": 100.0,
            "high": 102.0,
        },
    }

    result = evaluate_entry_confirmation(
        side="sell",
        trigger_bars=bars,
        current_price=98.8,
        optimal_entry=100.0,
        invalidation_level=102.0,
        atr_15m=2.0,
        ta_context=context,
        filter_profile=_profile(),
        planned_rr=1.8,
        zone_touch_recorded=True,
        now=NOW,
    )

    assert result["passed"] is True
    assert result["evidence"]["current_in_zone"] is False
    assert result["evidence"]["entry_location_valid"] is True


def test_profile_volume_flag_is_an_independent_gate():
    bars = _bars()
    bars.loc[bars.index[-2], "volume"] = 100.0

    result = _evaluate(
        bars,
        profile=_profile(
            require_volume=True,
            require_entry_sweep=False,
            trigger_evidence_min=1,
        ),
    )

    assert result["evidence"]["swept_liquidity"] is True
    assert result["evidence"]["volume_confirmed"] is False
    assert "participation" in result["failed_gates"]


def test_profile_entry_sweep_flag_is_an_independent_gate():
    bars = _bars()
    bars.loc[bars.index[-2], ["open", "high", "low", "close", "volume"]] = [
        100.0, 100.8, 99.2, 100.6, 150.0,
    ]

    result = _evaluate(
        bars,
        profile=_profile(
            require_volume=False,
            require_entry_sweep=True,
            trigger_evidence_min=1,
        ),
    )

    assert result["evidence"]["volume_confirmed"] is True
    assert result["evidence"]["swept_liquidity"] is False
    assert "participation" in result["failed_gates"]


def test_price_proximity_cannot_bypass_missing_confirmation():
    result = _evaluate(_bars(trigger="unconfirmed"))

    assert result["passed"] is False
    assert "directional_reclaim" in result["failed_gates"]


def test_stale_closed_candle_is_fail_closed():
    result = _evaluate(_bars(trigger_time="2026-08-20T11:40:00Z"))

    assert result["passed"] is False
    assert "data_fresh" in result["failed_gates"]


def test_unknown_htf_data_is_not_treated_as_neutral():
    result = _evaluate(_bars(), context=_context(htf="unknown"))

    assert result["passed"] is False
    assert "htf_known" in result["failed_gates"]


def test_countertrend_requires_rejection_sweep_and_volume_together():
    weak = _evaluate(_bars(trigger="direction_only"), context=_context(htf="bearish"))
    strong = _evaluate(_bars(), context=_context(htf="bearish"))

    assert weak["passed"] is False
    assert "participation" in weak["failed_gates"]
    assert strong["passed"] is True
    assert strong["evidence"]["countertrend"] is True


def test_setup_identity_changes_only_when_structure_changes():
    context = _context()
    same_with_new_indicator_value = {**context, "adx": 31.0, "rsi": 62.0}
    same_timestamp_format = {
        **context,
        "setup_anchor": {
            **context["setup_anchor"],
            "created_at": "2026-08-20 10:00:00+00:00",
        },
    }
    changed = {
        **context,
        "setup_anchor": {**context["setup_anchor"], "created_at": "2026-08-20T11:00:00Z"},
    }

    first = build_setup_thesis_id("BTC/USDT", "buy", context, "smc_balanced")
    same = build_setup_thesis_id(
        "BTC/USDT", "buy", same_with_new_indicator_value, "smc_balanced"
    )
    same_time = build_setup_thesis_id(
        "BTC/USDT", "buy", same_timestamp_format, "smc_balanced"
    )
    new_structure = build_setup_thesis_id(
        "BTC/USDT", "buy", changed, "smc_balanced"
    )

    assert first == same
    assert first == same_time
    assert first != new_structure


def test_intrabar_invalidation_waits_for_closed_15m_classification():
    assert classify_virtual_invalidation(
        side="buy", current_price=98.0, invalidation_level=98.0
    ) == "SWEEP_PENDING"
    assert classify_virtual_invalidation(
        side="buy",
        current_price=99.0,
        invalidation_level=98.0,
        prior_state="SWEEP_PENDING",
        closed_price=98.5,
        closed_after_breach=True,
    ) == "RECLAIM_CONFIRMED"
    assert classify_virtual_invalidation(
        side="buy",
        current_price=97.5,
        invalidation_level=98.0,
        prior_state="SWEEP_PENDING",
        closed_price=97.8,
        closed_after_breach=True,
    ) == "HARD_INVALIDATED"


def test_sell_against_strong_risk_on_requires_full_reversal_evidence():
    bars = _bars(trigger="direction_only")
    context = {
        **_context(htf="bearish"),
        "ema_50": 95.0,
        "ema_200": 100.0,
        "setup_anchor": {
            "type": "bearish_ob",
            "created_at": "2026-08-20T10:00:00Z",
            "low": 100.0,
            "high": 102.0,
        },
        "market_regime": {"label": "STRONG_RISK_ON", "known": True},
    }
    result = evaluate_entry_confirmation(
        side="sell",
        trigger_bars=bars,
        current_price=99.9,
        optimal_entry=100.0,
        invalidation_level=102.0,
        atr_15m=2.0,
        ta_context=context,
        filter_profile=_profile(),
        planned_rr=1.8,
        now=NOW,
    )

    assert result["passed"] is False
    assert "regime_policy" in result["failed_gates"]
