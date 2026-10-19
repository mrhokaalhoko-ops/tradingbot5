import inspect
from collections import deque

import pytest

from utils.hot_list_monitor import HotListShadowMonitor, classify_hot_candidate


@pytest.fixture(autouse=True)
def _disable_audit_writes(monkeypatch):
    monkeypatch.setattr("utils.hot_list_monitor.audit_decision_event", lambda **_: True)


def _ticker(symbol="ABCUSDT", last=101.0, low=90.0, high=110.0, volume=5_000_000):
    return {
        "symbol": symbol,
        "lastPrice": str(last),
        "lowPrice": str(low),
        "highPrice": str(high),
        "quoteVolume": str(volume),
        "priceChangePercent": "3.0",
    }


def test_classifies_momentum_without_creating_trade_instruction():
    result = classify_hot_candidate(_ticker(), change_1m=1.2, change_5m=2.5)

    assert "MOMENTUM_UP" in result["tags"]
    assert "side" not in result
    assert "entry" not in result
    assert "stop_loss" not in result


def test_classifies_broad_market_twenty_minute_active_event_watch():
    result = classify_hot_candidate(
        _ticker(), change_1m=0.2, change_5m=1.1, change_20m=2.4
    )

    assert "ACTIVE_EVENT_WATCH" in result["tags"]
    assert result["change_20m_pct"] == 2.4


def test_twenty_minute_prefilter_survives_short_term_direction_reversal():
    result = classify_hot_candidate(
        _ticker(), change_1m=-0.2, change_5m=-0.4, change_20m=2.4
    )

    assert "ACTIVE_EVENT_PREFILTER" in result["tags"]
    assert "ACTIVE_EVENT_WATCH" not in result["tags"]


def test_baseline_does_not_call_existing_symbols_new(monkeypatch):
    timeline = iter([1_000.0, 1_060.0])
    monitor = HotListShadowMonitor(now_fn=lambda: next(timeline))
    monkeypatch.setattr(monitor, "min_quote_volume", 0.0)

    first = monitor.collect_once([_ticker("AAAUSDT", last=100)])
    second = monitor.collect_once([
        _ticker("AAAUSDT", last=101),
        _ticker("NEWUSDT", last=1, low=0.8, high=1.2),
    ])

    assert first["new_contract_count"] == 0
    assert second["new_contract_count"] == 1
    new_item = next(item for item in second["candidates"] if item["symbol"] == "NEWUSDT")
    assert "NEW_CONTRACT" in new_item["tags"]


def test_monitor_source_has_no_trading_execution_dependency():
    source = inspect.getsource(HotListShadowMonitor).lower()

    forbidden = ("create_order", "execute_and_protect", "save_order_intent", "virtual_order")
    assert not any(term in source for term in forbidden)


def test_low_volume_symbol_is_not_promoted():
    result = classify_hot_candidate(
        _ticker(volume=1_000), change_1m=9.0, min_quote_volume=2_000_000
    )
    assert result is None


def test_active_event_discovery_uses_top_fifty_liquidity_proxy(monkeypatch):
    now = 10_000.0
    monitor = HotListShadowMonitor(now_fn=lambda: now)
    monkeypatch.setattr(monitor, "min_quote_volume", 0.0)
    monitor.top_limit = 5
    tickers = []
    for index in range(51):
        symbol = f"S{index:02d}USDT"
        monitor._history[symbol] = deque([(now - 1_200.0, 100.0)], maxlen=20)
        tickers.append(_ticker(symbol, last=103.0, volume=10_000_000 - index))

    snapshot = monitor.collect_once(tickers)

    discovered = snapshot["active_event_candidates"]
    assert len(snapshot["candidates"]) == 5
    assert len(discovered) == 50
    assert max(item["liquidity_rank_24h"] for item in discovered) == 50
    assert all(item["research_universe_proxy"] for item in discovered)
