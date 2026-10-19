from datetime import datetime, timedelta, timezone
import inspect

import pandas as pd

from strategy.active_altcoin_shadow import (
    advance_setup,
    build_shadow_plan,
    detect_active_event,
    evaluate_shadow_plan,
    new_setup,
)


def _minute_bars(*, crowded=False):
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for index in range(128):
        open_price = price
        close = price
        quote = 20_000.0
        buy_share = 0.50
        if 120 <= index <= 124:
            close = 100.0 + (index - 119) * 1.0
            quote = 100_000.0
            buy_share = 0.95 if crowded else 0.51
        high = max(open_price, close) + 0.03
        low = min(open_price, close) - 0.03
        price = close
        rows.append({
            "open_time": start + timedelta(minutes=index),
            "close_time": start + timedelta(minutes=index + 1) - timedelta(milliseconds=1),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": quote / max(close, 1.0),
            "quote_volume": quote,
            "taker_buy_quote_volume": quote * buy_share,
        })
    return pd.DataFrame(rows)


def test_detects_only_completed_event_and_applies_flow_gate():
    bars = _minute_bars()
    now = bars.iloc[124]["close_time"]

    event = detect_active_event("ALTUSDT", bars, now)

    assert event["base_qualified"] is True
    assert event["score_passed"] is True
    assert event["micro"]["gate_passed"] is True
    assert event["qualified"] is True
    assert event["close_time"] == pd.Timestamp(now).isoformat()


def test_crowded_aggressive_flow_is_shadow_rejected():
    bars = _minute_bars(crowded=True)
    event = detect_active_event("ALTUSDT", bars, bars.iloc[124]["close_time"])

    assert event["base_qualified"] is True
    assert event["micro"]["gate_passed"] is False
    assert event["qualified"] is False
    assert event["reason"] == "CROWDED_FLOW_PRICE_AGREEMENT"


def test_pullback_reclaim_enters_next_minute_and_tracks_target():
    bars = _minute_bars()
    event = detect_active_event("ALTUSDT", bars, bars.iloc[124]["close_time"])
    atr = event["atr"]
    event_close = event["close"]
    bars.loc[125, ["open", "high", "low", "close"]] = [
        event_close,
        event_close + 0.02,
        event_close - 0.40 * atr,
        event_close - 0.20 * atr,
    ]
    bars.loc[126, ["open", "high", "low", "close"]] = [
        event_close - 0.20 * atr,
        event_close + 0.08 * atr,
        event_close - 0.35 * atr,
        event_close + 0.05 * atr,
    ]
    bars.loc[127, ["open", "high", "low", "close"]] = [
        event_close + 0.05 * atr,
        event_close + 0.06 * atr,
        event_close + 0.04 * atr,
        event_close + 0.05 * atr,
    ]
    setup = advance_setup(new_setup(event), bars, bars.iloc[126]["close_time"])
    assert setup["status"] == "RECLAIM_CONFIRMED"

    plan = build_shadow_plan(setup, bars)
    assert plan["valid"] is True
    assert plan["filled_at"] == pd.Timestamp(bars.iloc[127]["open_time"]).isoformat()

    bars.loc[127, "high"] = plan["target"] + 0.01
    outcome = evaluate_shadow_plan(plan, bars, bars.iloc[127]["close_time"])
    assert outcome["terminal"] is True
    assert outcome["status"] == "TP_FIRST"
    assert 0.5 < outcome["net_r"] < 1.5


def test_shadow_module_has_no_order_execution_dependency():
    import strategy.active_altcoin_shadow as module

    source = inspect.getsource(module).lower()
    forbidden = ("create_order", "execute_and_protect", "save_order_intent", "order_intent")
    assert not any(term in source for term in forbidden)
