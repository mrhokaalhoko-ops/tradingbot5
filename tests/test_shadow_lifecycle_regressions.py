"""Regression coverage for causal shadow lifecycle and portfolio reservations."""
import copy

import pandas as pd
import pytest

from strategy.active_altcoin_shadow import (
    advance_setup, build_shadow_plan, evaluate_shadow_plan,
)
from utils.active_altcoin_shadow_monitor import ActiveAltcoinShadowMonitor


def minute_frame(start="2026-09-07T12:00:00Z", count=60):
    opens = pd.date_range(start, periods=count, freq="min")
    return pd.DataFrame({
        "open_time": opens,
        "close_time": opens + pd.Timedelta(minutes=1) - pd.Timedelta(milliseconds=1),
        "open": 100.0, "high": 100.2, "low": 99.8, "close": 100.1,
        "volume": 10.0, "quote_volume": 1000.0, "taker_buy_quote_volume": 500.0,
    })


def confirmed_setup(symbol="ALTUSDT", side="LONG"):
    return {
        "setup_id": "setup-" + symbol, "symbol": symbol, "side": side,
        "status": "RECLAIM_CONFIRMED", "touched": True,
        "extreme": 99.0 if side == "LONG" else 101.0,
        "last_checked_close_time": "2026-09-07T11:59:59.999Z",
        "confirmation_close_time": "2026-09-07T11:59:59.999Z",
        "event": {
            "event_id": "event-" + symbol, "symbol": symbol, "side": side,
            "close_time": "2026-09-07T11:54:59.999Z", "close": 100.0, "atr": 1.0,
        },
    }


def shadow_plan(symbol="ALTUSDT", **updates):
    result = {
        "plan_id": "plan-" + symbol, "setup_id": "setup-" + symbol,
        "symbol": symbol, "side": "LONG", "filled_at": "2026-09-07T12:00:00Z",
        "entry": 100.0, "stop": 99.0, "target": 101.5, "risk": 1.0,
        "status": "OPEN", "execution_enabled": False,
    }
    result.update(updates)
    return result


@pytest.fixture(autouse=True)
def no_external_audit(monkeypatch):
    monkeypatch.setattr(
        "utils.active_altcoin_shadow_monitor.audit_decision_event", lambda **_: True
    )


def test_confirmed_setup_keeps_original_trigger_while_waiting_for_entry():
    setup = confirmed_setup()
    before = copy.deepcopy(setup)
    bars = minute_frame(count=1)
    bars.loc[0, "low"] = 90.0
    result = advance_setup(setup, bars, bars.iloc[-1]["close_time"])
    assert result == before
    assert setup == before


def test_missing_exact_entry_minute_is_not_replaced_with_later_open():
    result = build_shadow_plan(confirmed_setup(), minute_frame().iloc[1:])
    assert result["valid"] is False
    assert result["reason"] == "MISSING_NEXT_1M_OPEN"


@pytest.mark.parametrize("side,entry", [("LONG", 98.0), ("SHORT", 102.0)])
def test_entry_gapped_beyond_stop_is_rejected(side, entry):
    bars = minute_frame(count=1)
    bars.loc[0, "open"] = entry
    result = build_shadow_plan(confirmed_setup(side=side), bars)
    assert result["valid"] is False
    assert result["reason"] == "INVALID_RISK"


@pytest.mark.parametrize("missing_index", [0, 10, 59])
def test_missing_outcome_minute_cannot_be_reported_as_time_exit(missing_index):
    bars = minute_frame().drop(index=missing_index)
    result = evaluate_shadow_plan(shadow_plan(), bars, "2026-09-07T13:00:00Z")
    assert result["terminal"] is False
    assert result["status"] == "DATA_GAP"
    assert "net_r" not in result


def test_gap_before_target_cannot_be_reported_as_a_win():
    bars = minute_frame(count=3).drop(index=1)
    bars.loc[2, "high"] = 102.0
    result = evaluate_shadow_plan(shadow_plan(), bars, "2026-09-07T12:03:00Z")
    assert result["terminal"] is False
    assert result["status"] == "DATA_GAP"


def test_gap_after_observed_exit_does_not_erase_valid_result():
    bars = minute_frame(count=3).drop(index=2)
    bars.loc[0, "high"] = 102.0
    result = evaluate_shadow_plan(shadow_plan(), bars, "2026-09-07T12:03:00Z")
    assert result["terminal"] is True
    assert result["status"] == "TP_FIRST"


def test_outcome_rejects_duplicate_minutes_instead_of_counting_them_as_coverage():
    bars = minute_frame(count=3)
    bars = pd.concat([bars.iloc[:1], bars], ignore_index=True)
    result = evaluate_shadow_plan(shadow_plan(), bars, "2026-09-07T12:03:00Z")
    assert result["terminal"] is False
    assert result["status"] == "DATA_GAP"


def test_complete_horizon_and_same_bar_ambiguity_remain_conservative():
    bars = minute_frame()
    result = evaluate_shadow_plan(shadow_plan(), bars, "2026-09-07T13:00:00Z")
    assert result["status"] == "TIME_EXIT"
    assert result["net_r"] == pytest.approx(-0.02)
    bars.loc[0, ["low", "high"]] = [98.0, 102.0]
    result = evaluate_shadow_plan(shadow_plan(), bars, "2026-09-07T13:00:00Z")
    assert result["status"] == "AMBIGUOUS_AS_LOSS"
    assert result["net_r"] == pytest.approx(-1.12)


def test_restart_after_missing_entry_open_uses_original_confirmation(monkeypatch):
    now = pd.Timestamp("2026-09-07T12:00:00Z").timestamp()
    saved = {}
    class State:
        def get(self, key, default=None):
            return copy.deepcopy(saved.get(key, default))
        def set(self, key, value):
            saved[key] = copy.deepcopy(value)
            return True
    first = ActiveAltcoinShadowMonitor(State(), now_fn=lambda: now)
    first._state["setups"]["ALTUSDT"] = confirmed_setup()
    first._persist()
    restarted = ActiveAltcoinShadowMonitor(State(), now_fn=lambda: now + 61)
    bars = minute_frame(count=2)
    # The first post-confirmation bar would otherwise hard-invalidate the setup.
    bars.loc[0, "low"] = 98.0
    restarted._process_existing({"ALTUSDT": bars}, "2026-09-07T12:01:01Z")
    records = list(restarted._state["plans"].values()) + [
        row["plan"] for row in restarted._state["completed"] if row["kind"] == "PLAN"
    ]
    assert len(records) == 1
    assert pd.Timestamp(records[0]["filled_at"]) == pd.Timestamp("2026-09-07T12:00:00Z")


def test_six_simultaneous_confirmations_cannot_exceed_five_positions():
    monitor = ActiveAltcoinShadowMonitor()
    symbols = ["ALT" + str(i) + "USDT" for i in range(6)]
    monitor._state["setups"] = {s: confirmed_setup(s) for s in symbols}
    bars = minute_frame(count=1)
    # Only the open is observable; the candle has not closed.
    monitor._process_existing({s: bars for s in symbols}, "2026-09-07T12:00:30Z")
    assert len(monitor._state["plans"]) == 5
    rejected = [r for r in monitor._state["completed"] if r["kind"] == "PLAN_REJECTED"]
    assert len(rejected) == 1
    assert rejected[0]["plan"]["reason"] == "PORTFOLIO_CAP"


def test_closed_plans_still_reserve_capacity_at_backdated_fill():
    monitor = ActiveAltcoinShadowMonitor()
    monitor._state["completed"] = [{
        "kind": "PLAN", "plan": shadow_plan("OLD" + str(i)),
        "outcome": {"terminal": True, "exited_at": "2026-09-07T12:30:00Z"},
    } for i in range(5)]
    monitor._state["setups"]["ALTUSDT"] = confirmed_setup()
    monitor._process_existing(
        {"ALTUSDT": minute_frame()}, "2026-09-07T12:31:00Z"
    )
    assert "ALTUSDT" not in monitor._state["plans"]
    assert monitor._state["completed"][-1]["kind"] == "PLAN_REJECTED"
    assert monitor._state["completed"][-1]["plan"]["reason"] == "PORTFOLIO_CAP"


def test_plan_recovery_requests_history_from_fill_instead_of_recent_window(monkeypatch):
    monitor = ActiveAltcoinShadowMonitor()
    monitor._state["plans"]["ALTUSDT"] = shadow_plan()
    requests = []
    def fetch(symbol, **kwargs):
        requests.append((symbol, kwargs))
        return minute_frame()
    monkeypatch.setattr(monitor, "_fetch_1m", fetch)
    monitor._process_existing({}, "2026-09-07T18:00:00Z")
    assert pd.Timestamp(requests[0][1]["start_time"]) == pd.Timestamp("2026-09-07T12:00:00Z")
    assert monitor._state["completed"][-1]["outcome"]["status"] == "TIME_EXIT"

@pytest.mark.parametrize("side,entry", [("LONG", 98.0), ("SHORT", 102.0)])
def test_historical_reclaim_rejects_entry_beyond_invalidation(side, entry):
    from scripts.replay_active_altcoin_1m import replay_one
    from trading_v2.active_altcoin_scalper import ActiveAltcoinConfig, replay_event
    bars = minute_frame(start="2026-09-07T11:50:00Z", count=75)
    if side == "LONG":
        bars.loc[5, ["open", "high", "low", "close"]] = [99.8, 100.2, 99.0, 100.1]
    else:
        bars.loc[5, ["open", "high", "low", "close"]] = [100.2, 101.0, 99.8, 99.9]
    bars.loc[6, ["open", "high", "low", "close"]] = [entry, entry + 0.1, entry - 0.1, entry]
    event = pd.Series({
        "symbol": "ALTUSDT", "side": side, "close_time": bars.iloc[4]["close_time"],
        "close": 100.0, "atr": 1.0, "event_score": 9.0,
        "return_1_pct": 2.0, "return_4_pct": 3.0, "volume_ratio": 2.0,
        "quote_volume": 500000.0, "body_fraction": 0.8, "close_location": 0.9,
    })
    result = replay_one(event, bars, "PULLBACK_RECLAIM_1M", ActiveAltcoinConfig())
    assert result["status"] == "INVALID_RISK"
    # The original 5m kernel must enforce the same directional geometry.
    five_bars = bars.copy()
    five_bars["open_time"] = pd.date_range("2026-09-07T11:30:00Z", periods=len(bars), freq="5min")
    five_bars["close_time"] = five_bars["open_time"] + pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=1)
    five_bars["close_location"] = (five_bars["close"] - five_bars["low"]) / (five_bars["high"] - five_bars["low"])
    event["close_time"] = five_bars.iloc[4]["close_time"]
    result = replay_event(event, five_bars, "PULLBACK_RECLAIM", ActiveAltcoinConfig())
    assert result["status"] == "INVALID_RISK"


def test_plan_open_does_not_read_future_rows_before_entry_time():
    result = build_shadow_plan(
        confirmed_setup(), minute_frame(), now="2026-09-07T11:59:59.999Z"
    )
    assert result == {"valid": False, "reason": "WAITING_NEXT_1M_OPEN"}


def test_capacity_releases_after_known_exit_without_alphabetical_bias():
    monitor = ActiveAltcoinShadowMonitor()
    # Existing Z symbols sort after the arriving A symbol.
    monitor._state["plans"] = {
        "Z" + str(i): shadow_plan("Z" + str(i), filled_at="2026-09-07T11:00:00Z")
        for i in range(5)
    }
    monitor._state["setups"]["AAAUSDT"] = confirmed_setup("AAAUSDT")
    frames = {s: minute_frame("2026-09-07T11:00:00Z") for s in monitor._state["plans"]}
    frames["AAAUSDT"] = minute_frame(count=1)
    monitor._process_existing(frames, "2026-09-07T12:00:30Z")
    assert set(monitor._state["plans"]) == {"AAAUSDT"}


def test_gap_before_reclaim_keeps_setup_pending_for_backfill():
    setup = confirmed_setup()
    setup["status"] = "WAITING_PULLBACK"
    setup["touched"] = False
    setup["extreme"] = None
    setup.pop("confirmation_close_time")
    setup["last_checked_close_time"] = setup["event"]["close_time"]
    bars = minute_frame(start="2026-09-07T11:56:00Z", count=1)
    bars.loc[0, ["open", "high", "low", "close"]] = [99.8, 100.2, 99.0, 100.1]
    result = advance_setup(setup, bars, bars.iloc[0]["close_time"])
    assert result["status"] == "DATA_GAP"
    assert result["last_checked_close_time"] == setup["last_checked_close_time"]


@pytest.mark.parametrize("side,price", [("buy", 105.0), ("sell", 95.0)])
def test_deferred_quality_guard_cannot_reach_order_submission(side, price, monkeypatch):
    from unittest.mock import Mock
    import main as runtime
    from engines.ta_engine import TAEngine
    from strategy.risk_manager import RiskManager

    state = Mock()
    state.get.side_effect = lambda key, default=None: default
    state.is_state_recovery_blocked.return_value = False
    state.get_order_intent.return_value = {}
    state.get_max_open_trades.return_value = 5
    state.get_active_symbols.return_value = set()
    client = Mock()
    client.has_open_position.return_value = False
    client.exchange.fetch_ticker.return_value = {
        "bid": price - 0.01 if side == "buy" else price,
        "ask": price if side == "buy" else price + 0.01,
    }
    bars = pd.DataFrame({
        "open": [99.8] * 100, "high": [100.3] * 100, "low": [99.7] * 100,
        "close": [100.0] * 100, "atr": [1.0] * 100, "volume": [1000.0] * 100,
    })
    engine = TAEngine()
    quality = Mock(wraps=engine.validate_entry_quality)
    monkeypatch.setattr(engine, "validate_entry_quality", quality)
    audit = Mock()
    monkeypatch.setattr(runtime, "audit_decision_event", audit)
    coin = {
        "optimal_entry": 100.0, "entry_confirmation": {"passed": True},
        "target_plan": {"sl": 90.0, "tp": 130.0} if side == "buy" else {"sl": 110.0, "tp": 60.0},
    }
    result = runtime.execute_and_protect_trade(
        "ALT/USDT", side, 0.1, price, 5, 0.0025, 1.0,
        coin, None, bars, {}, side, client, RiskManager(), Mock(),
        state, Mock(), set(), [], ta_engine=engine, trade_id="review-deferred",
    )
    assert result is False
    quality.assert_called_once()
    assert quality.call_args.kwargs["audit_details"]["passed"] is True
    assert any(
        c.kwargs.get("stage") == "EXECUTABLE_PRICE_GUARD"
        and c.kwargs["details"]["reason"] == "EXECUTABLE_OUTSIDE_ENTRY_ZONE"
        for c in audit.call_args_list
    )
    client.execute_trade.assert_not_called()
    client.exchange.create_order.assert_not_called()
    state.save_order_intent.assert_not_called()


def test_empty_recovery_payload_is_unknown_instead_of_an_outcome():
    result = evaluate_shadow_plan(shadow_plan(), pd.DataFrame(), "2026-09-07T14:00:00Z")
    assert result["status"] == "DATA_GAP"
    assert result["terminal"] is False
    assert build_shadow_plan(confirmed_setup(), pd.DataFrame())["valid"] is False
