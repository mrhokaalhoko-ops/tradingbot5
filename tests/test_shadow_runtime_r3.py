from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.prove_shadow_no_execution import scan
from shadow_runtime.engine import ShadowRuntime
from shadow_runtime.features import build_level_map, shared_repriced_geometry
from shadow_runtime.fixtures import synthetic_snapshot
from shadow_runtime.market import PublicBinanceDemoMarket
from shadow_runtime.models import Candidate, ExecutionQuote, PolicyDecision
from shadow_runtime.policies import (
    MomentumRepricedPolicy,
    POLICY_IDS,
    TopDownPolicy,
    thesis_id,
)
from shadow_runtime.portfolio import PortfolioSimulator
from shadow_runtime.store import ShadowStore


ROOT = Path(__file__).resolve().parents[1]


def _execution_quote(snapshot, *, bid=None, ask=None, at=None, update_id=1):
    return ExecutionQuote(
        symbol=snapshot.symbol, event_time=at or snapshot.as_of,
        received_at=at or snapshot.as_of,
        best_bid=float(bid if bid is not None else snapshot.quote.best_bid),
        best_ask=float(ask if ask is not None else snapshot.quote.best_ask),
        update_id=update_id, source="TEST_ORDERED_PUBLIC_QUOTE",
    )


class FakeExecutionProvider:
    def __init__(self, quote):
        self.quote = quote

    def wait_fresh_quote(self, symbol, ready_at, timeout_seconds=5.0):
        return self.quote if self.quote.event_time >= ready_at else None

    def first_quote_near(self, symbol, ready_at, window_seconds=2.0):
        return self.quote if ready_at <= self.quote.event_time <= ready_at + timedelta(seconds=window_seconds) else None

    def exact_candle_open(self, symbol, open_time):
        return None


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class GetOnlySession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params, timeout))
        return _Response({"serverTime": 1_797_000_000_000})

    def __getattr__(self, name):
        if name.lower() in {"post", "put", "patch", "delete", "request"}:
            raise AssertionError(f"non-GET method requested: {name}")
        raise AttributeError(name)


def test_static_no_execution_scan_passes():
    proof = scan(ROOT)
    assert proof["passed"] is True
    assert proof["findings"] == []
    assert proof["conclusion"] == "NO_EXCHANGE_ORDER_ROUTE_IN_RUNTIME_SOURCE"


def test_public_market_gateway_can_only_get_allowlisted_public_route():
    session = GetOnlySession()
    market = PublicBinanceDemoMarket(session=session)
    assert market.server_now().tzinfo is not None
    assert session.calls[0][0] == "GET"
    with pytest.raises(PermissionError):
        market._get("/fapi/v1/order")
    assert len(session.calls) == 1


def test_engine_restart_is_idempotent(tmp_path):
    database = tmp_path / "shadow.sqlite3"
    snapshot = synthetic_snapshot()
    first_store = ShadowStore(database)
    first = ShadowRuntime(root=ROOT, store=first_store)
    first.process_snapshot(snapshot)
    counts_before = first_store.counts()
    first_store.close()

    second_store = ShadowStore(database)
    second = ShadowRuntime(root=ROOT, store=second_store)
    second.process_snapshot(snapshot)
    assert second_store.counts() == counts_before
    events = second_store.events()
    assert len({row["event_id"] for row in events}) == len(events)
    assert len({(row["identity"]["policy_id"], row["candidate_id"]) for row in events if row["stage"] == "CANDIDATE_EMITTED"}) == 4
    second_store.close()


class SpyPolicy:
    def __init__(self, policy_id, seen):
        self.policy_id = policy_id
        self.seen = seen

    def evaluate(self, snapshot, state=None):
        self.seen.append((self.policy_id, id(snapshot), snapshot.snapshot_id))
        return PolicyDecision(
            policy_id=self.policy_id,
            candidate_id=snapshot.candidate.candidate_id,
            thesis_id=thesis_id(self.policy_id, snapshot),
            stage="THESIS_STATE",
            status="OBSERVED",
            state_before=None,
            state_after="OBSERVED",
            reason_codes=["TEST_OBSERVED_SAME_SNAPSHOT"],
            metadata={"status": "OBSERVED"},
        )


def test_all_four_policies_receive_same_snapshot_object(tmp_path):
    seen = []
    policies = [SpyPolicy(policy_id, seen) for policy_id in POLICY_IDS]
    store = ShadowStore(tmp_path / "shared.sqlite3")
    runtime = ShadowRuntime(root=ROOT, store=store, policies=policies)
    snapshot = synthetic_snapshot()
    runtime.process_snapshot(snapshot)
    assert len(seen) == 4
    assert {object_id for _, object_id, _ in seen} == {id(snapshot)}
    assert {snapshot_id for _, _, snapshot_id in seen} == {snapshot.snapshot_id}
    market_ids = {event["market_snapshot_id"] for event in store.events()}
    assert market_ids == {snapshot.snapshot_id}
    store.close()


def test_b_and_c_have_distinct_entry_ready_time_but_identical_mechanics(monkeypatch):
    original = synthetic_snapshot()
    candidate = Candidate(
        candidate_id=original.candidate.candidate_id,
        symbol=original.symbol,
        side="LONG",
        emitted_at=original.as_of - timedelta(minutes=7),
        event=original.candidate.event,
    )
    snapshot = synthetic_snapshot(as_of=original.as_of, candidate=candidate, snapshot_suffix="timing")

    def ready_observation(snapshot, levels):
        return {
            "missing_timeframes": [], "location_available": True,
            "closed_4h_below_invalidation": False,
            "weekly_long": True, "daily_long": True,
            "too_extended": False, "recovered_from_extension": True,
            "bias_structure_location_valid": True,
            "breakout_1h": False, "pullback_at_zone": True,
            "trigger_15m": True, "zone_tolerance": 0.1,
            "level_map": levels,
        }

    monkeypatch.setattr("shadow_runtime.policies.top_down_observation", ready_observation)
    b = MomentumRepricedPolicy().evaluate(snapshot)
    c = TopDownPolicy().evaluate(snapshot, {"status": "WAIT_FOR_TRIGGER", "level_map": build_level_map(snapshot)})
    assert b.stage == c.stage == "ENTRY_READY"
    assert b.entry_ready_at == candidate.emitted_at
    assert c.entry_ready_at == snapshot.as_of
    assert b.entry_ready_at != c.entry_ready_at
    quote = _execution_quote(snapshot)
    b_geometry, b_reason = MomentumRepricedPolicy().resolve_geometry(snapshot, b, execution_quote=quote)
    c_geometry, c_reason = TopDownPolicy().resolve_geometry(snapshot, c, execution_quote=quote)
    assert b_reason == c_reason == "ENTRY_GEOMETRY_VALID"
    assert b_geometry.as_dict() == c_geometry.as_dict()


def test_common_portfolio_model_enforces_10_positions_5x_and_one_percent_margin(tmp_path):
    store = ShadowStore(tmp_path / "portfolio.sqlite3")
    portfolio = PortfolioSimulator(store, initial_equity=1_000.0)
    snapshot = synthetic_snapshot()
    quote = _execution_quote(snapshot)
    geometry, reason = shared_repriced_geometry(snapshot, build_level_map(snapshot), quote)
    assert reason == "ENTRY_GEOMETRY_VALID"
    exchange_filter = {"step_size": 0.000001, "min_qty": 0.0, "min_notional": 0.0}
    for index in range(10):
        simulation, costs, position, status = portfolio.simulate_fill(
            policy_id="MOMENTUM_REPRICED_V1",
            thesis_id=f"thesis_{index}", snapshot=snapshot,
            geometry=geometry, execution_quote=quote, exchange_filter=exchange_filter,
        )
        assert status == "SIMULATED_FILL"
        assert simulation["leverage"] == 5.0
        assert simulation["max_open_positions"] == 10
        assert simulation["base_stop_risk_pct_of_equity"] == 0.25
        assert simulation["hard_max_stop_risk_pct_of_equity"] == 1.0
        assert simulation["initial_margin_pct_of_equity"] == pytest.approx(1.0, rel=2e-4)
        assert simulation["planned_stop_risk_pct_of_equity"] <= 1.0
        store.upsert_position(position)
    simulation, _, position, status = portfolio.simulate_fill(
        policy_id="MOMENTUM_REPRICED_V1",
        thesis_id="thesis_11", snapshot=snapshot,
        geometry=geometry, execution_quote=quote, exchange_filter=exchange_filter,
    )
    assert status == "REJECT_CAPACITY_10"
    assert simulation is None and position is None
    store.close()


def test_simulated_fill_and_terminal_outcome_are_machine_readable(tmp_path):
    store = ShadowStore(tmp_path / "outcome.sqlite3")
    runtime = ShadowRuntime(root=ROOT, store=store)
    # Entry causality is measured against wall-clock resolution start. Keep
    # this execution fixture prospective instead of tying it to a fixed noon
    # timestamp that becomes stale later on the same day.
    first = synthetic_snapshot(as_of=datetime.now(timezone.utc) + timedelta(seconds=5))
    first_quote = _execution_quote(first)
    runtime.process_snapshot(first, execution_provider=FakeExecutionProvider(first_quote))
    position = store.open_positions("MOMENTUM_REPRICED_V1")[0]
    target = float(position["target"])
    later = _execution_quote(
        first, at=first.as_of + timedelta(minutes=1),
        bid=target + 0.05, ask=target + 0.07, update_id=2,
    )
    runtime.process_execution_quote(later)
    terminal = [
        row for row in store.events("MOMENTUM_REPRICED_V1")
        if row["stage"] == "TERMINAL_OUTCOME"
    ]
    assert len(terminal) == 1
    event = terminal[0]
    assert event["outcome"]["terminal_status"] == "TP"
    assert isinstance(event["outcome"]["net_R"], float)
    assert event["execution_costs"]["actual_exit_fee_bps"] == 5.0
    assert store.open_positions("MOMENTUM_REPRICED_V1") == []
    store.close()
