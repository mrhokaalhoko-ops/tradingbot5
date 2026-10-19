import inspect

import pytest

from utils.active_altcoin_shadow_monitor import ActiveAltcoinShadowMonitor


@pytest.fixture(autouse=True)
def _disable_real_audit(monkeypatch):
    monkeypatch.setattr(
        "utils.active_altcoin_shadow_monitor.audit_decision_event", lambda **_: True
    )


def test_monitor_is_shadow_only_and_has_no_execution_dependency():
    source = inspect.getsource(ActiveAltcoinShadowMonitor).lower()

    forbidden = (
        "create_order", "execute_and_protect", "save_order_intent",
        "exchange_order", "risk_manager",
    )
    assert not any(term in source for term in forbidden)


def test_disabled_monitor_publishes_no_trade_capability(monkeypatch):
    monkeypatch.setenv("ACTIVE_ALTCOIN_SHADOW_ENABLED", "false")
    monitor = ActiveAltcoinShadowMonitor(now_fn=lambda: 1_800_000_000.0)

    snapshot = monitor.collect_once({"candidates": []})

    assert snapshot["status"] == "DISABLED"
    assert snapshot["execution_enabled"] is False
    assert snapshot["active_plans"] == 0


def test_cross_sectional_cap_admits_only_two_strongest_events():
    monitor = ActiveAltcoinShadowMonitor(now_fn=lambda: 1_800_000_000.0)
    events = []
    for score, symbol in ((9.0, "AAAUSDT"), (11.0, "BBBUSDT"), (10.0, "CCCUSDT")):
        events.append({
            "event_id": f"event-{symbol}",
            "symbol": symbol,
            "side": "LONG",
            "open_time": "2026-08-01T01:55:00+00:00",
            "close_time": "2026-08-01T01:59:59.999000+00:00",
            "event_score": score,
            "base_qualified": True,
            "qualified": True,
        })

    monitor._admit_events(events, 1_800_000_000.0)

    assert set(monitor._state["setups"]) == {"BBBUSDT", "CCCUSDT"}
    assert set(monitor._state["cooldown_until"]) == {"BBBUSDT", "CCCUSDT"}


def test_discovery_prefilter_is_rechecked_with_exact_closed_candles(monkeypatch):
    monitor = ActiveAltcoinShadowMonitor(now_fn=lambda: 1_800_000_000.0)
    monkeypatch.setattr(monitor, "_fetch_1m", lambda _symbol: object())
    monkeypatch.setattr(
        "utils.active_altcoin_shadow_monitor.detect_active_event",
        lambda symbol, _bars, _now: {
            "event_id": f"event-{symbol}", "symbol": symbol,
            "close_time": "2026-08-01T01:59:59.999000+00:00",
            "base_qualified": False, "qualified": False,
        },
    )

    events = monitor._candidate_events(
        [{"symbol": "AAAUSDT", "tags": ["ACTIVE_EVENT_PREFILTER"]}],
        {}, "2026-08-01T02:01:00+00:00",
    )

    assert [event["symbol"] for event in events] == ["AAAUSDT"]


def test_cross_sectional_cap_is_preserved_across_monitor_cycles():
    monitor = ActiveAltcoinShadowMonitor(now_fn=lambda: 1_800_000_000.0)
    close_time = "2026-08-01T01:59:59.999000+00:00"
    first = [
        {
            "event_id": f"event-{symbol}", "symbol": symbol, "side": "LONG",
            "open_time": "2026-08-01T01:55:00+00:00", "close_time": close_time,
            "event_score": score, "base_qualified": True, "qualified": True,
        }
        for score, symbol in ((11.0, "AAAUSDT"), (10.0, "BBBUSDT"))
    ]
    late = [{
        "event_id": "event-CCCUSDT", "symbol": "CCCUSDT", "side": "LONG",
        "open_time": "2026-08-01T01:55:00+00:00", "close_time": close_time,
        "event_score": 9.0, "base_qualified": True, "qualified": True,
    }]

    monitor._admit_events(first, 1_800_000_000.0)
    monitor._admit_events(late, 1_800_000_060.0)

    assert set(monitor._state["setups"]) == {"AAAUSDT", "BBBUSDT"}
    assert monitor._state["bar_capacity"][close_time] == [
        "event-AAAUSDT", "event-BBBUSDT"
    ]


def test_persist_prunes_bar_capacity_using_utc_timestamps():
    now = 1_800_000_000.0
    monitor = ActiveAltcoinShadowMonitor(now_fn=lambda: now)
    recent = "2027-01-15T07:00:00+00:00"
    stale = "2027-01-14T23:00:00+00:00"
    monitor._state["bar_capacity"] = {
        recent: ["event-recent"],
        stale: ["event-stale"],
    }

    assert monitor._persist() is True
    assert monitor._state["bar_capacity"] == {recent: ["event-recent"]}
