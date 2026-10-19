import ast
import math
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import Config
from core.scanner_universe import ACTIVITY_SOURCE, select_demo_targets, subscription_symbols
from core.universe_filter import UniverseFilter
from utils.hot_list_monitor import HotListShadowMonitor, get_latest_hot_list_snapshot


NOW = 10000.0


def snapshot(rows, age=0, **overrides):
    result = {
        "status": "OK", "scanner_source": ACTIVITY_SOURCE,
        "timestamp": datetime.fromtimestamp(NOW - age, timezone.utc).isoformat(),
        "scanner_activity_candidates": [{"symbol": s, "activity_score": score} for s, score in rows],
    }
    result.update(overrides)
    return result


def eligible(n=60):
    return [(f"S{i:02d}/USDT:USDT", {}, 100_000_000 - i) for i in range(n)]


def choose(data=None, **kwargs):
    options = dict(forced=[], limit=50, snapshot=data, now_ts=NOW)
    options.update(kwargs)
    return select_demo_targets(eligible(), **options)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr("utils.hot_list_monitor.audit_decision_event", lambda **_: True)
    monkeypatch.setattr("utils.hot_list_monitor._latest_snapshot", {})
    monkeypatch.setattr(Config, "USE_TESTNET", True)
    monkeypatch.setattr(Config, "TESTNET_FORCE_SYMBOLS", [])
    monkeypatch.setattr(Config, "DEMO_ACTIVITY_UNIVERSE_ENABLED", True)
    monkeypatch.setattr("core.universe_filter.time.time", lambda: NOW)


def test_activity_promotes_eligible_symbols_outside_liquidity_top_fifty():
    targets, details = choose(snapshot([("S59USDT", 20), ("S58USDT", 18)]))
    assert len(targets) == 50 == len(set(targets))
    assert {"S59/USDT:USDT", "S58/USDT:USDT"} <= set(targets)
    assert details["added_vs_liquidity"] == ["S59/USDT:USDT", "S58/USDT:USDT"]
    assert details["displaced_vs_liquidity"] == ["S48/USDT:USDT", "S49/USDT:USDT"]


@pytest.mark.parametrize("age", [181, -1])
def test_stale_and_future_snapshots_fall_back_to_liquidity(age):
    targets, details = choose(snapshot([("S59USDT", 100)], age=age))
    assert targets == [row[0] for row in eligible()[:50]]
    assert details["activity_status"] == "STALE_OR_FUTURE"


@pytest.mark.parametrize("override", [
    {"status": "STALE"}, {"status": "DISABLED"}, {"scanner_source": "UNKNOWN"},
    {"timestamp": "invalid"}, {"timestamp": "2026-09-08T00:00:00"},
    {"scanner_activity_candidates": None},
])
def test_unusable_observations_cannot_change_targets(override):
    targets, _ = choose(snapshot([("S59USDT", 100)], **override))
    assert targets == [row[0] for row in eligible()[:50]]


def test_venue_intersection_precedes_activity_budget_and_deduplicates_aliases():
    data = snapshot([(f"MISSING{i}USDT", 1000) for i in range(80)] + [
        ("S59USDT", 30), ("S59/USDT:USDT", 29), ("S58USDT", 28),
        ("S57USDT", math.inf), ("S56USDT", math.nan), ("S55USDT", -1),
    ])
    targets, details = choose(data, activity_slots=2)
    assert len(targets) == 50
    assert details["added_vs_liquidity"] == ["S59/USDT:USDT", "S58/USDT:USDT"]


def test_forced_symbols_obey_eligibility_aliases_and_total_limit():
    targets, details = choose(snapshot([("S59USDT", 100)]), limit=2,
                              forced=["MISSING/USDT", "S02/USDT", "S02/USDT:USDT", "S03USDT"])
    assert targets == ["S02/USDT:USDT", "S03/USDT:USDT"]
    assert details["forced_unavailable"] == ["MISSING/USDT"]
    assert choose(limit=0)[0] == []
    assert choose(limit=-4)[0] == []


def ticker(symbol, last=100, volume=20_000_000):
    return {"symbol": symbol, "lastPrice": last, "lowPrice": 90, "highPrice": 200,
            "quoteVolume": volume, "priceChangePercent": 500}


def test_history_gap_cannot_masquerade_as_short_term_activity():
    monitor = HotListShadowMonitor(now_fn=lambda: NOW)
    monitor._history["GAPUSDT"] = deque([(NOW - 1200, 100)])
    result = monitor.collect_once([ticker("GAPUSDT", 105)])
    assert result["scanner_activity_candidates"] == []
    candidate = result["candidates"][0]
    assert candidate["change_1m_pct"] == candidate["change_5m_pct"] == 0
    assert "ACTIVE_EVENT_PREFILTER" in candidate["tags"]


def test_missing_minute_does_not_relabel_two_minute_change():
    monitor = HotListShadowMonitor(now_fn=lambda: NOW)
    monitor._history["GAPUSDT"] = deque([(NOW - 120, 100)])
    assert monitor.collect_once([ticker("GAPUSDT", 105)])["scanner_activity_candidates"] == []


def test_warmup_and_new_listing_do_not_promote_without_short_history():
    monitor = HotListShadowMonitor(now_fn=lambda: NOW)
    monitor.collect_once([ticker("OLDUSDT")])
    result = monitor.collect_once([ticker("OLDUSDT"), ticker("NEWUSDT", 120)])
    assert result["scanner_activity_candidates"] == []


def test_thirty_second_polling_retains_twenty_minute_history():
    clock = [0.0]
    monitor = HotListShadowMonitor(now_fn=lambda: clock[0])
    for i in range(42):
        clock[0] = i * 30.0
        result = monitor.collect_once([ticker("AAAUSDT", 100 + i)])
    candidate = result["candidates"][0]
    assert candidate["change_20m_pct"] == pytest.approx((141 / 101 - 1) * 100, abs=.0001)


def test_snapshot_copies_nested_tags_and_activity_rows():
    monitor = HotListShadowMonitor(now_fn=lambda: NOW)
    monitor._history["AAAUSDT"] = deque([(NOW - 60, 100)])
    monitor.collect_once([ticker("AAAUSDT", 102)])
    copy = get_latest_hot_list_snapshot()
    copy["candidates"][0]["tags"].append("CHANGED")
    copy["scanner_activity_candidates"][0]["activity_score"] = 9999
    fresh = get_latest_hot_list_snapshot()
    assert "CHANGED" not in fresh["candidates"][0]["tags"]
    assert fresh["scanner_activity_candidates"][0]["activity_score"] != 9999


class Exchange:
    def __init__(self, markets, volumes):
        self.markets, self.volumes = markets, volumes

    def fetch_tickers(self):
        return {s: {"quoteVolume": v, "bid": 100, "ask": 100.01} for s, v in self.volumes.items()}

    def load_markets(self):
        return self.markets


def universe(symbols):
    return UniverseFilter(SimpleNamespace(exchange=Exchange(
        {s: {"type": "swap", "active": True, "quote": "USDT", "settle": "USDT", "linear": True}
         for s in symbols}, {s: 50_000_000 - i for i, s in enumerate(symbols)})))


def test_actual_filter_rejects_forced_low_volume_inactive_and_non_perpetual(monkeypatch):
    symbols = ["OK/USDT:USDT", "LOW/USDT:USDT", "OFF/USDT:USDT", "DATED/USDT:USDT",
               "USDC/USDT:USDT", "INV/USDT:USDT", "BAD/USDT:USDT"]
    filt = universe(symbols)
    exchange = filt.client.exchange
    exchange.volumes["LOW/USDT:USDT"] = 1_000
    exchange.volumes["BAD/USDT:USDT"] = math.nan
    exchange.markets["OFF/USDT:USDT"]["active"] = False
    exchange.markets["DATED/USDT:USDT"]["expiry"] = 20000
    exchange.markets["INV/USDT:USDT"]["inverse"] = True
    monkeypatch.setattr(Config, "TESTNET_FORCE_SYMBOLS", symbols + ["GONE/USDT"])
    assert filt.get_scanning_targets() == ["OK/USDT:USDT"]


def test_live_and_disabled_demo_ignore_activity(monkeypatch):
    symbols = [row[0] for row in eligible()]
    filt = universe(symbols)
    data = snapshot([("S59USDT", 999)])
    monkeypatch.setattr(Config, "USE_TESTNET", False)
    assert filt.get_scanning_targets(activity_snapshot=data) == symbols[:50]
    monkeypatch.setattr(Config, "USE_TESTNET", True)
    monkeypatch.setattr(Config, "DEMO_ACTIVITY_UNIVERSE_ENABLED", False)
    assert filt.get_scanning_targets(activity_snapshot=data) == symbols[:50]


def test_market_wide_activity_reaches_actual_scanner_without_display_truncation(monkeypatch):
    monitor = HotListShadowMonitor(now_fn=lambda: NOW)
    monitor.top_limit = 5
    public = [ticker(f"UNAVAILABLE{i}USDT", 150) for i in range(60)] + [ticker("S59USDT", 101)]
    for item in public:
        monitor._history[item["symbol"]] = deque([(NOW - 60, 100)])
    data = monitor.collect_once(public)
    assert len(data["candidates"]) == 5
    assert len(data["scanner_activity_candidates"]) == 61
    targets = universe([row[0] for row in eligible()]).get_scanning_targets(activity_snapshot=data)
    assert "S59/USDT:USDT" in targets
    assert len(targets) == 50


def test_main_wires_activity_and_preserves_managed_subscriptions():
    # Execute the real scanner integration statements without booting the daemon.
    tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and
                    any(isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "target_symbols"
                                                         for t in x.targets) for x in n.body))
    start = next(i for i, n in enumerate(function.body) if isinstance(n, ast.Assign) and
                 any(isinstance(t, ast.Name) and t.id == "target_symbols" for t in n.targets))
    audit, subscribed = [], []
    context = {
        "universe_filter": universe([row[0] for row in eligible()]), "Config": Config,
        "get_latest_hot_list_snapshot": lambda: snapshot([("S59USDT", 100)]),
        "audit_decision_event": lambda **kw: audit.append(kw), "scan_id": "test-scan",
        "logger": SimpleNamespace(info=lambda *args: None),
        "_candle_stream": SimpleNamespace(set_symbols=lambda symbols: subscribed.extend(symbols)),
        "active_symbols": {"MANAGED/USDT"},
        "state_manager": SimpleNamespace(get_virtual_orders=lambda: {"PENDING/USDT": {}}),
        "subscription_symbols": subscription_symbols,
    }
    exec(compile(ast.Module(body=function.body[start:start + 4], type_ignores=[]), "main.py", "exec"), context)
    assert "S59/USDT:USDT" in context["target_symbols"]
    assert {"MANAGED/USDT", "PENDING/USDT", "S59/USDT"} <= set(subscribed)
    assert audit[0]["stage"] == "SCANNER_UNIVERSE"
    assert audit[0]["scan_id"] == "test-scan"


@pytest.mark.parametrize("interval", [59.9, 60.1])
def test_normal_polling_jitter_preserves_one_minute_activity(interval):
    monitor = HotListShadowMonitor(now_fn=lambda: NOW)
    monitor._history["AAAUSDT"] = deque([(NOW - interval, 100)])
    result = monitor.collect_once([ticker("AAAUSDT", 102)])
    assert result["scanner_activity_candidates"][0]["change_1m_pct"] == 2.0


def test_bearish_activity_is_ranked_equally_without_authorizing_a_side():
    monitor = HotListShadowMonitor(now_fn=lambda: NOW)
    for symbol in ("UPUSDT", "DOWNUSDT"):
        monitor._history[symbol] = deque([(NOW - 60, 100)])
    result = monitor.collect_once([ticker("UPUSDT", 102), ticker("DOWNUSDT", 98)])
    rows = result["scanner_activity_candidates"]
    assert {row["activity_score"] for row in rows} == {10.0}
    assert all("side" not in row and "entry" not in row for row in rows)
