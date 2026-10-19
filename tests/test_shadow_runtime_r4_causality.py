from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shadow_runtime.engine import ShadowRuntime
from shadow_runtime.fixtures import synthetic_snapshot
from shadow_runtime.market import PublicBinanceDemoMarket
from shadow_runtime.models import Candle, CandleOpenEvent, ExecutionQuote
from shadow_runtime.policies import ActiveAltcoinLongPolicy, MomentumRepricedPolicy
from shadow_runtime.store import ShadowStore
from shadow_runtime.stream import StreamExecutionProvider, parse_public_stream_message


ROOT = Path(__file__).resolve().parents[1]


class ExplodingFakeExchange:
    def __init__(self):
        self.order_calls = 0

    def create_order(self, *args, **kwargs):
        self.order_calls += 1
        raise AssertionError("Shadow runtime attempted an exchange order")


def quote(snapshot, *, bid=None, ask=None, seconds=0, update_id=1, receipt_delay=0):
    event_time = snapshot.as_of + timedelta(seconds=seconds)
    return ExecutionQuote(
        symbol=snapshot.symbol, event_time=event_time,
        received_at=event_time + timedelta(seconds=receipt_delay),
        best_bid=float(bid if bid is not None else snapshot.quote.best_bid),
        best_ask=float(ask if ask is not None else snapshot.quote.best_ask),
        update_id=update_id, source="TEST_ORDERED_PUBLIC_QUOTE",
    )


class Provider:
    def __init__(self, execution_quote=None, candle_open=None):
        self.execution_quote = execution_quote
        self.candle_open = candle_open

    def wait_fresh_quote(self, symbol, ready_at, timeout_seconds=5.0):
        return self.execution_quote

    def first_quote_near(self, symbol, ready_at, window_seconds=2.0):
        return self.execution_quote

    def exact_candle_open(self, symbol, open_time):
        return self.candle_open

    def wait_candle_open(self, symbol, open_time, timeout_seconds=5.0):
        return self.candle_open


def _filled_runtime(tmp_path):
    store = ShadowStore(tmp_path / "causal.sqlite3")
    runtime = ShadowRuntime(root=ROOT, store=store, policies=[MomentumRepricedPolicy()])
    # The engine deliberately rejects execution quotes older than the actual
    # wall-clock resolution start. Use a prospective fake quote so this test
    # remains deterministic regardless of the hour at which pytest is run.
    snapshot = synthetic_snapshot(as_of=datetime.now(timezone.utc) + timedelta(seconds=5))
    runtime.process_snapshot(snapshot, execution_provider=Provider(quote(snapshot)))
    position = store.open_positions("MOMENTUM_REPRICED_V1")[0]
    return store, runtime, snapshot, position


def test_runtime_cannot_accept_or_call_an_exchange_client(tmp_path):
    fake = ExplodingFakeExchange()
    store = ShadowStore(tmp_path / "no-exchange.sqlite3")
    with pytest.raises(TypeError):
        ShadowRuntime(root=ROOT, store=store, exchange_client=fake)
    assert fake.order_calls == 0
    store.close()


def test_a_never_looks_up_historical_next_open_after_reclaim(monkeypatch):
    snapshot = synthetic_snapshot()
    confirmation = snapshot.as_of - timedelta(minutes=2)

    def confirmed(setup, frame, now):
        return {
            **setup, "status": "RECLAIM_CONFIRMED",
            "confirmation_close_time": confirmation.isoformat(), "extreme": 99.6,
            "setup_id": "prospective-a",
        }

    monkeypatch.setattr("shadow_runtime.policies.advance_setup", confirmed)
    decision = ActiveAltcoinLongPolicy().evaluate(snapshot)
    assert decision.stage == "THESIS_STATE"
    assert decision.status == "WAITING_NEXT_1M_OPEN"
    assert decision.geometry is None
    assert "next_open" not in decision.metadata["setup"]
    assert decision.metadata["scheduled_fill_at"] == (
        confirmation.replace(second=0, microsecond=0) + timedelta(minutes=1)
    ).isoformat()


def test_a_missing_prospective_boundary_event_is_no_fill(monkeypatch, tmp_path):
    snapshot = synthetic_snapshot()
    scheduled = snapshot.as_of
    policy = ActiveAltcoinLongPolicy()
    store = ShadowStore(tmp_path / "a-missing.sqlite3")
    runtime = ShadowRuntime(root=ROOT, store=store, policies=[policy])
    store.upsert_thesis(
        policy.policy_id, "ignored", snapshot.candidate.candidate_id, snapshot.symbol,
        "WAITING_NEXT_1M_OPEN",
        {
            "status": "WAITING_NEXT_1M_OPEN", "scheduled_fill_at": scheduled.isoformat(),
            "setup": {"confirmation_close_time": (scheduled - timedelta(minutes=1)).isoformat(),
                      "extreme": 99.6, "setup_id": "a", "status": "RECLAIM_CONFIRMED"},
        }, scheduled.isoformat(),
    )
    # Use the deterministic thesis id actually expected by the runtime.
    from shadow_runtime.policies import thesis_id
    expected = thesis_id(policy.policy_id, snapshot)
    store._connection.execute("UPDATE theses SET thesis_id=?", (expected,))
    runtime.process_snapshot(snapshot, execution_provider=Provider(quote(snapshot), None))
    events = store.events(policy.policy_id)
    assert any(row["status"] == "DATA_UNAVAILABLE_NO_PROSPECTIVE_1M_OPEN" for row in events)
    assert store.open_positions() == []
    store.close()


def test_a_accepts_buffered_boundary_event_captured_after_wait_registration(tmp_path):
    scheduled = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=1)
    snapshot = synthetic_snapshot(as_of=scheduled)
    policy = ActiveAltcoinLongPolicy()
    store = ShadowStore(tmp_path / "a-buffered.sqlite3")
    runtime = ShadowRuntime(root=ROOT, store=store, policies=[policy])
    from shadow_runtime.policies import thesis_id
    tid = thesis_id(policy.policy_id, snapshot)
    setup = {
        "confirmation_close_time": (snapshot.as_of - timedelta(minutes=1)).isoformat(),
        "extreme": 99.6, "setup_id": "setup-a", "status": "RECLAIM_CONFIRMED",
    }
    store.upsert_thesis(
        policy.policy_id, tid, snapshot.candidate.candidate_id, snapshot.symbol,
        "WAITING_NEXT_1M_OPEN",
        {
            "status": "WAITING_NEXT_1M_OPEN",
            "scheduled_fill_at": scheduled.isoformat(),
            "wait_registered_at": (scheduled - timedelta(seconds=10)).isoformat(),
            "setup": setup,
        },
        snapshot.as_of.isoformat(),
    )
    buffered_open = CandleOpenEvent(
        symbol=snapshot.symbol, open_time=scheduled,
        received_at=scheduled + timedelta(milliseconds=100), open_price=100.0,
    )
    buffered_quote = ExecutionQuote(
        symbol=snapshot.symbol, event_time=scheduled + timedelta(milliseconds=150),
        received_at=scheduled + timedelta(milliseconds=200), best_bid=99.99,
        best_ask=100.0, update_id=11, source="TEST_ORDERED_PUBLIC_QUOTE",
    )
    runtime.process_snapshot(
        snapshot, execution_provider=Provider(buffered_quote, buffered_open)
    )
    assert len(store.open_positions(policy.policy_id)) == 1
    assert sum(row["stage"] == "SIMULATED_FILL" for row in store.events(policy.policy_id)) == 1
    store.close()


def test_a_rejects_boundary_event_captured_before_wait_registration(tmp_path):
    scheduled = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=1)
    snapshot = synthetic_snapshot(as_of=scheduled)
    policy = ActiveAltcoinLongPolicy()
    store = ShadowStore(tmp_path / "a-pre-registration.sqlite3")
    runtime = ShadowRuntime(root=ROOT, store=store, policies=[policy])
    from shadow_runtime.policies import thesis_id
    tid = thesis_id(policy.policy_id, snapshot)
    setup = {
        "confirmation_close_time": (scheduled - timedelta(minutes=1)).isoformat(),
        "extreme": 99.6, "setup_id": "setup-a", "status": "RECLAIM_CONFIRMED",
    }
    store.upsert_thesis(
        policy.policy_id, tid, snapshot.candidate.candidate_id, snapshot.symbol,
        "WAITING_NEXT_1M_OPEN",
        {
            "status": "WAITING_NEXT_1M_OPEN",
            "scheduled_fill_at": scheduled.isoformat(),
            "wait_registered_at": (scheduled + timedelta(milliseconds=500)).isoformat(),
            "setup": setup,
        },
        scheduled.isoformat(),
    )
    pre_registration_open = CandleOpenEvent(
        symbol=snapshot.symbol, open_time=scheduled,
        received_at=scheduled + timedelta(milliseconds=100), open_price=100.0,
    )
    pre_registration_quote = ExecutionQuote(
        symbol=snapshot.symbol, event_time=scheduled + timedelta(milliseconds=150),
        received_at=scheduled + timedelta(milliseconds=200), best_bid=99.99,
        best_ask=100.0, update_id=12, source="TEST_ORDERED_PUBLIC_QUOTE",
    )
    runtime.process_snapshot(
        snapshot, execution_provider=Provider(pre_registration_quote, pre_registration_open)
    )
    assert store.open_positions() == []
    assert any(
        row["status"] == "DATA_UNAVAILABLE_NO_PROSPECTIVE_1M_OPEN"
        for row in store.events(policy.policy_id)
    )
    store.close()


def test_a_delayed_fill_replays_quotes_and_is_restart_idempotent(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    scheduled = (now + timedelta(minutes=2)).replace(second=0, microsecond=0)
    confirmation = scheduled - timedelta(minutes=1)

    def confirmed(setup, frame, as_of):
        return {
            **setup, "status": "RECLAIM_CONFIRMED",
            "confirmation_close_time": confirmation.isoformat(),
            "extreme": 99.6, "setup_id": "prospective-a-replay",
        }

    monkeypatch.setattr("shadow_runtime.policies.advance_setup", confirmed)
    database = tmp_path / "a-delayed-replay.sqlite3"
    policy = ActiveAltcoinLongPolicy()
    store = ShadowStore(database)
    runtime = ShadowRuntime(root=ROOT, store=store, policies=[policy])

    arm_snapshot = synthetic_snapshot(
        as_of=scheduled - timedelta(seconds=30), snapshot_suffix="arm"
    )
    runtime.process_snapshot(arm_snapshot)
    armed = store.thesis_for_candidate(policy.policy_id, arm_snapshot.candidate.candidate_id)
    assert armed["status"] == "WAITING_NEXT_1M_OPEN"
    assert armed.get("wait_registered_at")

    open_event = CandleOpenEvent(
        symbol=arm_snapshot.symbol, open_time=scheduled,
        received_at=scheduled + timedelta(milliseconds=100), open_price=100.0,
    )
    entry_quote = ExecutionQuote(
        symbol=arm_snapshot.symbol, event_time=scheduled + timedelta(milliseconds=150),
        received_at=scheduled + timedelta(milliseconds=200), best_bid=99.99,
        best_ask=100.0, update_id=101, source="TEST_ORDERED_PUBLIC_QUOTE",
    )
    terminal_quote = ExecutionQuote(
        symbol=arm_snapshot.symbol, event_time=scheduled + timedelta(seconds=10),
        received_at=scheduled + timedelta(seconds=10, milliseconds=50),
        best_bid=101.0, best_ask=101.01, update_id=102,
        source="TEST_ORDERED_PUBLIC_QUOTE",
    )
    store.insert_candle_open(open_event)
    store.insert_execution_quote(entry_quote)
    store.insert_execution_quote(terminal_quote)
    provider = StreamExecutionProvider(store)

    due_snapshot = synthetic_snapshot(as_of=scheduled, snapshot_suffix="due")
    runtime.process_snapshot(due_snapshot, execution_provider=provider)
    events = store.events(policy.policy_id)
    assert sum(row["stage"] == "SIMULATED_FILL" for row in events) == 1
    terminal = [row for row in events if row["stage"] == "TERMINAL_OUTCOME"]
    assert len(terminal) == 1
    assert terminal[0]["outcome"]["terminal_status"] == "TP"
    assert store.open_positions(policy.policy_id) == []
    store.close()

    store = ShadowStore(database)
    runtime = ShadowRuntime(root=ROOT, store=store, policies=[policy])
    provider = StreamExecutionProvider(store)
    runtime.process_snapshot(due_snapshot, execution_provider=provider)
    runtime.process_execution_quote(terminal_quote)
    events = store.events(policy.policy_id)
    assert sum(row["stage"] == "SIMULATED_FILL" for row in events) == 1
    assert sum(row["stage"] == "TERMINAL_OUTCOME" for row in events) == 1
    store.close()


@pytest.mark.parametrize("seconds,receipt_delay", [(-1, 0), (0, 3)])
def test_b_rejects_pre_ready_or_late_received_quote(tmp_path, seconds, receipt_delay):
    store = ShadowStore(tmp_path / f"stale-{seconds}-{receipt_delay}.sqlite3")
    runtime = ShadowRuntime(root=ROOT, store=store, policies=[MomentumRepricedPolicy()])
    snapshot = synthetic_snapshot()
    runtime.process_snapshot(
        snapshot,
        execution_provider=Provider(quote(snapshot, seconds=seconds, receipt_delay=receipt_delay)),
    )
    assert store.open_positions() == []
    assert any(
        row["status"] == "DATA_UNAVAILABLE_NO_FRESH_QUOTE_WITHIN_5S"
        for row in store.events("MOMENTUM_REPRICED_V1")
    )
    store.close()


def test_tp_between_strategy_cycles_is_captured_from_ordered_quote(tmp_path):
    store, runtime, snapshot, position = _filled_runtime(tmp_path)
    runtime.process_execution_quote(quote(
        snapshot, bid=float(position["target"]) + 0.01,
        ask=float(position["target"]) + 0.02, seconds=7, update_id=2,
    ))
    terminal = [row for row in store.events() if row["stage"] == "TERMINAL_OUTCOME"]
    assert len(terminal) == 1
    assert terminal[0]["outcome"]["terminal_status"] == "TP"
    assert terminal[0]["outcome"]["same_bar_conflict"] is False
    store.close()


def test_sl_between_strategy_cycles_is_captured_from_ordered_quote(tmp_path):
    store, runtime, snapshot, position = _filled_runtime(tmp_path)
    runtime.process_execution_quote(quote(
        snapshot, bid=float(position["stop"]) - 0.01,
        ask=float(position["stop"]), seconds=7, update_id=2,
    ))
    terminal = [row for row in store.events() if row["stage"] == "TERMINAL_OUTCOME"]
    assert len(terminal) == 1
    assert terminal[0]["outcome"]["terminal_status"] == "SL"
    store.close()


def test_tp_and_sl_same_1m_bar_without_quote_order_is_ambiguous(tmp_path):
    store, runtime, snapshot, position = _filled_runtime(tmp_path)
    candle = Candle(
        timeframe="1m", open_time=snapshot.as_of + timedelta(seconds=1),
        close_time=snapshot.as_of + timedelta(minutes=1), open=float(position["entry"]),
        high=float(position["target"]) + 0.1, low=float(position["stop"]) - 0.1,
        close=float(position["entry"]), volume=1.0,
    )
    runtime.reconcile_closed_bar(snapshot.symbol, candle, ordered_quote_coverage=False)
    terminal = [row for row in store.events() if row["stage"] == "TERMINAL_OUTCOME"]
    assert len(terminal) == 1
    assert terminal[0]["outcome"]["terminal_status"] == "CAUSAL_AMBIGUOUS"
    assert terminal[0]["outcome"]["same_bar_conflict"] is True
    store.close()


def test_terminal_quote_is_restart_idempotent(tmp_path):
    database = tmp_path / "restart.sqlite3"
    store = ShadowStore(database)
    runtime = ShadowRuntime(root=ROOT, store=store, policies=[MomentumRepricedPolicy()])
    snapshot = synthetic_snapshot(as_of=datetime.now(timezone.utc) + timedelta(seconds=5))
    runtime.process_snapshot(snapshot, execution_provider=Provider(quote(snapshot)))
    target = float(store.open_positions()[0]["target"])
    store.close()

    store = ShadowStore(database)
    runtime = ShadowRuntime(root=ROOT, store=store, policies=[MomentumRepricedPolicy()])
    terminal_quote = quote(snapshot, bid=target + 0.1, ask=target + 0.2, seconds=4, update_id=22)
    runtime.process_execution_quote(terminal_quote)
    runtime.process_execution_quote(terminal_quote)
    assert sum(row["stage"] == "TERMINAL_OUTCOME" for row in store.events()) == 1
    store.close()


def test_snapshot_fetches_analysis_quote_after_all_candles(monkeypatch):
    snapshot = synthetic_snapshot()
    calls = []
    market = PublicBinanceDemoMarket()

    def raw(symbol, timeframe, limit):
        calls.append(f"candle:{timeframe}")
        rows = snapshot.closed(timeframe)
        return [[
            int(row.open_time.timestamp() * 1000), str(row.open), str(row.high), str(row.low),
            str(row.close), str(row.volume), int(row.close_time.timestamp() * 1000),
            str(row.quote_volume), 1, "0", str(row.taker_buy_quote_volume), "0",
        ] for row in rows]

    def analysis_quote(symbol):
        calls.append("quote")
        return snapshot.quote

    monkeypatch.setattr(market, "raw_klines", raw)
    monkeypatch.setattr(market, "quote", analysis_quote)
    market.snapshot(snapshot.candidate)
    assert calls[-1] == "quote"
    assert all(call.startswith("candle:") for call in calls[:-1])


def test_public_stream_parser_accepts_only_bookticker_and_1m_open():
    now = synthetic_snapshot().as_of
    parsed = parse_public_stream_message(
        {"e": "bookTicker", "E": int(now.timestamp() * 1000), "s": "XUSDT", "u": 7, "b": "1", "a": "1.01"},
        received_at=now,
    )
    assert isinstance(parsed, ExecutionQuote)
    opened = parse_public_stream_message(
        {"e": "kline", "s": "XUSDT", "k": {"i": "1m", "t": int(now.timestamp() * 1000), "o": "1.0"}},
        received_at=now,
    )
    assert isinstance(opened, CandleOpenEvent)
    assert parse_public_stream_message({"e": "ACCOUNT_UPDATE"}, received_at=now) is None
