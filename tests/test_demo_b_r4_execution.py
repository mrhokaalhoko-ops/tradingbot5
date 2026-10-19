from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import requests

from demo_execution.client import DemoBinanceClient, DemoBinanceError
from demo_execution.config import DemoEndpointGuard, DemoExecutionConfig
from demo_execution.cross_environment import (
    DEMO_EXECUTION_QUOTE_SOURCE,
    MAINNET_SNAPSHOT_SOURCE,
    MAINNET_USDM_PUBLIC_URL,
    audit_cross_environment_candidate,
    compare_contracts,
    validate_mainnet_signal_endpoint,
)
from demo_execution.executor import DemoBExecutor
from demo_execution.store import DemoExecutionStore
from shadow_runtime.fixtures import synthetic_snapshot
from shadow_runtime.models import Candidate, ExecutionQuote, Geometry, PolicyDecision


class FakePolicy:
    policy_id = "MOMENTUM_REPRICED_V1"

    def evaluate(self, snapshot, state=None):
        return PolicyDecision(
            policy_id=self.policy_id,
            candidate_id=snapshot.candidate.candidate_id,
            thesis_id="thesis-test",
            stage="ENTRY_READY",
            status="ENTRY_READY",
            state_before=None,
            state_after="ENTRY_READY",
            reason_codes=["B_INITIAL_CANDIDATE_READY"],
            entry_ready_at=snapshot.candidate.emitted_at,
        )

    def resolve_geometry(self, snapshot, decision, **_kwargs):
        geometry = Geometry(
            executable_quote_time=snapshot.quote.observed_at,
            executable_entry=100.0,
            entry_quote_side="BEST_ASK",
            entry_price_source="TEST_EXECUTABLE_ASK",
            old_reference_entry=99.5,
            stop=99.0,
            stop_source="TEST_CAUSAL_STOP",
            stop_level_id="stop-test",
            target=102.0,
            target_source="TEST_CAUSAL_TARGET",
            target_level_id="target-test",
            planned_rr=2.0,
            actual_rr_at_entry=2.0,
            geometry_change_reason="TEST_B_REPRICED",
        )
        return geometry, "ENTRY_GEOMETRY_VALID"


class FakeClient:
    def __init__(self, entry_response=None):
        self.entry_response = entry_response or {
            "orderId": "entry-1", "clientOrderId": "entry-client",
            "status": "FILLED", "origQty": "0.2", "executedQty": "0.2",
            "avgPrice": "100.0",
        }
        self.positions = []
        self.orders = []
        self.algo_orders = []
        self.query_results = {}
        self.algo_query_results = {}
        self.entry_calls = []
        self.protective_calls = []
        self.leverage_calls = []
        self.cancel_calls = []
        self.exit_calls = []
        self.query_calls = []
        self.algo_query_calls = []
        self.algo_cancel_calls = []
        self.position_risk_calls = 0
        self.account_calls = 0

    def prove_environment(self):
        return {
            "base_url": "https://demo-fapi.binance.com",
            "host": "demo-fapi.binance.com",
            "is_demo_or_testnet": True,
            "is_mainnet": False,
        }

    def position_mode(self):
        return {"dualSidePosition": False}

    def position_risk(self):
        self.position_risk_calls += 1
        return list(self.positions)

    def open_orders(self, symbol=None):
        return list(self.orders)

    def open_algo_orders(self, symbol=None):
        return list(self.algo_orders)

    def account(self):
        self.account_calls += 1
        return {"totalWalletBalance": "1000", "availableBalance": "1000"}

    def set_leverage(self, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))
        return {"symbol": symbol, "leverage": leverage}

    def create_market_entry(self, symbol, quantity, client_id):
        self.entry_calls.append((symbol, quantity, client_id))
        response = dict(self.entry_response)
        response.setdefault("clientOrderId", client_id)
        return response

    def create_protective(self, symbol, quantity, trigger_price, *, kind, client_id):
        self.protective_calls.append(
            (symbol, quantity, trigger_price, kind, client_id, True)
        )
        return {
            "algoId": f"{kind}-id", "clientAlgoId": client_id,
            "algoStatus": "NEW", "orderType": kind, "reduceOnly": True,
        }

    def create_reduce_only_market_exit(self, symbol, quantity, client_id):
        self.exit_calls.append((symbol, quantity, client_id))
        return {"orderId": "exit-1", "status": "FILLED", "avgPrice": "100"}

    def query_order(self, symbol, *, order_id=None, client_id=None):
        key = str(order_id or client_id)
        self.query_calls.append((symbol, str(order_id or ""), str(client_id or "")))
        if key in self.query_results:
            result = self.query_results[key]
            if isinstance(result, list):
                result = result.pop(0)
            if isinstance(result, Exception):
                raise result
            return dict(result)
        return dict(self.entry_response)

    def cancel_order(self, symbol, *, order_id=None, client_id=None):
        self.cancel_calls.append((symbol, str(order_id or client_id)))
        return {"status": "CANCELED", "orderId": order_id}

    def query_algo_order(self, *, algo_id=None, client_id=None):
        key = str(algo_id or client_id)
        self.algo_query_calls.append((str(algo_id or ""), str(client_id or "")))
        if key in self.algo_query_results:
            result = self.algo_query_results[key]
            if isinstance(result, list):
                result = result.pop(0)
            if isinstance(result, Exception):
                raise result
            return dict(result)
        for row in self.algo_orders:
            if str(row.get("algoId") or "") == key or str(row.get("clientAlgoId") or "") == key:
                return dict(row)
        raise DemoBinanceError(
            "BINANCE_DEMO_HTTP_400:CODE=-2013:MSG=Order does not exist.",
            status_code=400, binance_code=-2013,
        )

    def cancel_algo_order(self, *, algo_id=None, client_id=None):
        self.algo_cancel_calls.append((str(algo_id or ""), str(client_id or "")))
        return {
            "algoId": algo_id, "clientAlgoId": client_id,
            "algoStatus": "CANCELED",
        }

    def user_trades(self, symbol, start_time_ms=None):
        return [{"commission": "0.01", "realizedPnl": "0.5"}]

    def funding_income(self, symbol, start_time_ms=None):
        return [{"income": "-0.001"}]


class FakeResponse:
    text = ""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse({"orderId": 1, "status": "NEW"})


class SequenceSession:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.actions:
            raise AssertionError(f"unexpected request: {method} {url}")
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


def config(**changes):
    base = DemoExecutionConfig(
        enabled=True,
        api_key="new-demo-key",
        api_secret="new-demo-secret",
        fill_timeout_seconds=0.001,
    )
    return replace(base, **changes)


def filters():
    return {
        "tick_size": 0.01, "step_size": 0.001,
        "min_qty": 0.001, "max_qty": 1000.0, "min_notional": 5.0,
    }


def contract(**changes):
    value = {
        "symbol": "FIXTUREUSDT",
        "status": "TRADING",
        "contract_type": "PERPETUAL",
        "base_asset": "FIXTURE",
        "quote_asset": "USDT",
        "margin_asset": "USDT",
        "tick_size": 0.01,
        "step_size": 0.001,
        "min_qty": 0.001,
        "max_qty": 1000.0,
        "min_notional": 5.0,
    }
    value.update(changes)
    return value


def mainnet_snapshot():
    return replace(
        synthetic_snapshot(),
        source=MAINNET_SNAPSHOT_SOURCE,
    )


def demo_quote(snapshot, *, bid=99.99, ask=100.0, seconds=1):
    observed = snapshot.candidate.emitted_at + timedelta(seconds=seconds)
    return ExecutionQuote(
        symbol=snapshot.symbol,
        event_time=observed,
        received_at=observed,
        best_bid=bid,
        best_ask=ask,
        update_id=int(observed.timestamp() * 1000),
        source=DEMO_EXECUTION_QUOTE_SOURCE,
    )


def executor(tmp_path, client=None):
    store = DemoExecutionStore(tmp_path / "demo.sqlite3")
    item = DemoBExecutor(
        config(database_path=tmp_path / "demo.sqlite3"),
        client or FakeClient(), store, policy=FakePolicy(), sleep=lambda _value: None,
    )
    return item, store


def test_demo_endpoint_guard_accepts_only_demo_or_testnet():
    proof = DemoEndpointGuard.validate("https://demo-fapi.binance.com")
    assert proof["is_demo_or_testnet"] is True
    assert proof["is_mainnet"] is False
    assert DemoEndpointGuard.validate(
        "https://testnet.binancefuture.com"
    )["is_demo_or_testnet"] is True


@pytest.mark.parametrize(
    "url",
    [
        "https://fapi.binance.com",
        "https://api.binance.com",
        "http://demo-fapi.binance.com",
        "https://demo-fapi.binance.com.evil.example",
    ],
)
def test_mainnet_and_lookalike_endpoints_are_rejected(url):
    with pytest.raises(RuntimeError, match="DEMO_ENDPOINT_REJECTED"):
        DemoEndpointGuard.validate(url)


def test_signed_protection_is_reduce_only_and_demo_host_only():
    session = FakeSession()
    client = DemoBinanceClient(config(), session=session)
    client.create_protective(
        "FIXTUREUSDT", "0.2", "99", kind="STOP_MARKET", client_id="dbr4-s-test"
    )
    _method, url, kwargs = session.calls[0]
    assert url == "https://demo-fapi.binance.com/fapi/v1/algoOrder"
    assert kwargs["params"]["algoType"] == "CONDITIONAL"
    assert kwargs["params"]["triggerPrice"] == "99"
    assert "stopPrice" not in kwargs["params"]
    assert kwargs["params"]["clientAlgoId"] == "dbr4-s-test"
    assert "newClientOrderId" not in kwargs["params"]
    assert kwargs["params"]["reduceOnly"] == "true"
    assert kwargs["params"]["workingType"] == "MARK_PRICE"


def test_algo_query_and_cancel_use_signed_demo_routes_only():
    session = SequenceSession([
        FakeResponse({"algoId": 7, "algoStatus": "NEW"}),
        FakeResponse({"algoId": 7, "algoStatus": "CANCELED"}),
        FakeResponse([]),
    ])
    client = DemoBinanceClient(config(), session=session)
    assert client.query_algo_order(algo_id=7)["algoStatus"] == "NEW"
    assert client.cancel_algo_order(algo_id=7)["algoStatus"] == "CANCELED"
    assert client.open_algo_orders() == []
    assert [call[0] for call in session.calls] == ["GET", "DELETE", "GET"]
    assert [call[1] for call in session.calls] == [
        "https://demo-fapi.binance.com/fapi/v1/algoOrder",
        "https://demo-fapi.binance.com/fapi/v1/algoOrder",
        "https://demo-fapi.binance.com/fapi/v1/openAlgoOrders",
    ]


def test_one_way_mode_switch_is_demo_only_and_explicit():
    session = FakeSession()
    client = DemoBinanceClient(config(), session=session)
    client.set_one_way_position_mode()
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url == "https://demo-fapi.binance.com/fapi/v1/positionSide/dual"
    assert kwargs["params"]["dualSidePosition"] == "false"


def test_reconciliation_get_timeout_then_success_uses_bounded_backoff():
    sleeps = []
    session = SequenceSession([
        requests.Timeout("read timed out"),
        FakeResponse([]),
    ])
    client = DemoBinanceClient(
        config(),
        session=session,
        timeout_seconds=0.1,
        reconciliation_retry_delays=(1.0, 2.0),
        reconciliation_retry_total_seconds=10.0,
        sleep=sleeps.append,
        random_uniform=lambda _low, _high: 0.0,
    )
    assert client.position_risk() == []
    assert len(session.calls) == 2
    assert sleeps == [1.0]
    assert client.reconciliation_retry_state["status"] == "RECOVERED"
    assert client.reconciliation_retry_state["attempts"] == 2
    assert client.reconciliation_retry_history[-1]["status"] == "RECOVERED"


def test_binance_408_minus_1007_then_success_is_retried():
    session = SequenceSession([
        FakeResponse(
            {"code": -1007, "msg": "Timeout waiting for response from backend server."},
            status_code=408,
        ),
        FakeResponse([]),
    ])
    client = DemoBinanceClient(
        config(), session=session, timeout_seconds=0.1,
        reconciliation_retry_delays=(0.0,),
        reconciliation_retry_total_seconds=5.0,
        sleep=lambda _value: None,
        random_uniform=lambda _low, _high: 0.0,
    )
    assert client.position_risk() == []
    assert len(session.calls) == 2
    failure = client.reconciliation_retry_state["failures"][0]
    assert "BINANCE_DEMO_HTTP_408:CODE=-1007" in failure["error"]


def test_post_order_timeout_is_never_retried():
    session = SequenceSession([requests.Timeout("write outcome unknown")])
    client = DemoBinanceClient(
        config(), session=session,
        reconciliation_retry_delays=(0.0, 0.0, 0.0),
        sleep=lambda _value: None,
    )
    with pytest.raises(requests.Timeout, match="write outcome unknown"):
        client.create_market_entry("FIXTUREUSDT", "0.2", "dbr4-e-no-retry")
    assert len(session.calls) == 1
    assert session.calls[0][0] == "POST"


def test_execution_is_impossible_when_global_switch_is_false(tmp_path):
    fake = FakeClient()
    store = DemoExecutionStore(tmp_path / "disabled.sqlite3")
    item = DemoBExecutor(
        DemoExecutionConfig(database_path=tmp_path / "disabled.sqlite3"),
        fake, store, policy=FakePolicy(),
    )
    assert item.startup()["status"] == "DISABLED"
    with pytest.raises(Exception, match="DEMO_EXECUTION_DISABLED"):
        item.execute_candidate(synthetic_snapshot(), filters())
    assert fake.entry_calls == []
    store.close()


def test_duplicate_candidate_never_duplicates_orders(tmp_path):
    fake = FakeClient()
    item, store = executor(tmp_path, fake)
    assert item.startup()["status"] == "READY"
    snapshot = synthetic_snapshot()
    first = item.execute_candidate(snapshot, filters())
    second = item.execute_candidate(snapshot, filters())
    assert first["state"] == "PROTECTED"
    assert second["status"] == "DUPLICATE_CANDIDATE"
    assert len(fake.entry_calls) == 1
    assert len(fake.protective_calls) == 2
    store.close()


def test_entry_fill_then_stop_and_take_profit_are_created(tmp_path):
    fake = FakeClient()
    item, store = executor(tmp_path, fake)
    item.startup()
    result = item.execute_candidate(synthetic_snapshot(), filters())
    assert result["state"] == "PROTECTED"
    assert result["payload"]["filled_qty"] == pytest.approx(0.2)
    assert [row[3] for row in fake.protective_calls] == [
        "STOP_MARKET", "TAKE_PROFIT_MARKET",
    ]
    assert all(row[-1] is True for row in fake.protective_calls)
    assert result["payload"]["protection_api"] == "BINANCE_USDM_ALGO_ORDER"
    assert result["payload"]["stop_order_id"] == "STOP_MARKET-id"
    assert result["payload"]["tp_order_id"] == "TAKE_PROFIT_MARKET-id"
    store.close()


def test_ambiguous_algo_submission_queries_same_client_id_without_repost(tmp_path):
    class AmbiguousProtectionClient(FakeClient):
        def create_protective(
            self, symbol, quantity, trigger_price, *, kind, client_id,
        ):
            self.protective_calls.append(
                (symbol, quantity, trigger_price, kind, client_id, True)
            )
            if kind == "STOP_MARKET":
                self.algo_query_results[client_id] = {
                    "algoId": "stop-recovered", "clientAlgoId": client_id,
                    "algoStatus": "NEW", "orderType": kind,
                    "reduceOnly": True,
                }
                raise requests.Timeout("algo write outcome unknown")
            return {
                "algoId": "tp-created", "clientAlgoId": client_id,
                "algoStatus": "NEW", "orderType": kind,
                "reduceOnly": True,
            }

    fake = AmbiguousProtectionClient()
    item, store = executor(tmp_path, fake)
    item.startup()
    result = item.execute_candidate(synthetic_snapshot(), filters())
    assert result["state"] == "PROTECTED"
    assert len(fake.entry_calls) == 1
    assert [row[3] for row in fake.protective_calls] == [
        "STOP_MARKET", "TAKE_PROFIT_MARKET",
    ]
    assert result["payload"]["stop_order_id"] == "stop-recovered"
    assert any(call[1] == result["payload"]["stop_client_id"] for call in fake.algo_query_calls)
    assert fake.exit_calls == []
    store.close()


def test_unresolved_algo_submission_fails_closed_without_duplicate_post(tmp_path):
    class UnresolvedProtectionClient(FakeClient):
        def create_protective(
            self, symbol, quantity, trigger_price, *, kind, client_id,
        ):
            self.protective_calls.append(
                (symbol, quantity, trigger_price, kind, client_id, True)
            )
            raise requests.Timeout("algo write outcome unknown")

        def query_algo_order(self, *, algo_id=None, client_id=None):
            self.algo_query_calls.append((str(algo_id or ""), str(client_id or "")))
            raise DemoBinanceError(
                "RECONCILIATION_READ_RETRY_EXHAUSTED:/fapi/v1/algoOrder",
                retryable=True,
            )

    fake = UnresolvedProtectionClient()
    item, store = executor(tmp_path, fake)
    item.startup()
    snapshot = synthetic_snapshot()
    with pytest.raises(DemoBinanceError, match="RETRY_EXHAUSTED"):
        item.execute_candidate(snapshot, filters())
    failed = store.get(snapshot.candidate.candidate_id)
    assert failed["state"] == "FAILED_CLOSED"
    assert failed["payload"]["emergency_exit"]["status"] == "FILLED"
    assert "RETRY_EXHAUSTED" in failed["payload"]["protection_failure"]
    assert len(fake.protective_calls) == 1
    assert len(fake.entry_calls) == 1
    assert len(fake.exit_calls) == 1
    duplicate = item.execute_candidate(snapshot, filters())
    assert duplicate["status"] == "BLOCKED_RECONCILIATION"
    assert len(fake.protective_calls) == 1
    assert len(fake.entry_calls) == 1
    store.close()


def test_filled_response_without_price_is_refreshed_by_get_then_protected(tmp_path):
    incomplete = {
        "orderId": "entry-missing-price", "status": "FILLED",
        "origQty": "0.2", "executedQty": "0.2",
    }
    fake = FakeClient(incomplete)
    fake.query_results["entry-missing-price"] = {
        **incomplete, "avgPrice": "100.0", "cumQuote": "20.0",
    }
    item, store = executor(tmp_path, fake)
    item.startup()
    result = item.execute_candidate(synthetic_snapshot(), filters())
    assert result["state"] == "PROTECTED"
    assert result["payload"]["entry_fill"] == pytest.approx(100.0)
    assert len(fake.entry_calls) == 1
    assert len(fake.query_calls) == 1
    assert len(fake.protective_calls) == 2
    store.close()


def test_filled_price_get_408_minus_1007_then_success_is_bounded_and_safe():
    complete = {
        "orderId": 42, "status": "FILLED", "executedQty": "0.2",
        "avgPrice": "100.0", "cumQuote": "20.0",
    }
    session = SequenceSession([
        FakeResponse(
            {"code": -1007, "msg": "Timeout waiting for backend."},
            status_code=408,
        ),
        FakeResponse(complete),
    ])
    client = DemoBinanceClient(
        config(), session=session, timeout_seconds=0.1,
        reconciliation_retry_delays=(0.0,),
        reconciliation_retry_total_seconds=5.0,
        sleep=lambda _value: None,
        random_uniform=lambda _low, _high: 0.0,
    )
    assert client.query_order("FIXTUREUSDT", order_id=42) == complete
    assert len(session.calls) == 2
    assert all(call[0] == "GET" for call in session.calls)
    assert client.reconciliation_retry_state["status"] == "RECOVERED"


def test_filled_price_get_exhaustion_keeps_active_and_never_reposts(tmp_path):
    incomplete = {
        "orderId": "entry-unpriced", "status": "FILLED",
        "origQty": "0.2", "executedQty": "0.2",
    }
    fake = FakeClient(incomplete)
    fake.query_results["entry-unpriced"] = DemoBinanceError(
        "RECONCILIATION_READ_RETRY_EXHAUSTED:/fapi/v1/order",
        retryable=True,
    )
    item, store = executor(tmp_path, fake)
    item.startup()
    result = item.execute_candidate(synthetic_snapshot(), filters())
    assert result["state"] == "ENTRY_FILLED_UNPRICED"
    assert result["payload"]["filled_qty"] == pytest.approx(0.2)
    assert item.reconciliation_ok is False
    assert len(fake.entry_calls) == 1
    assert fake.protective_calls == []
    assert store.active()[0]["state"] == "ENTRY_FILLED_UNPRICED"
    store.close()


def test_entry_terminal_get_failure_stays_active_and_never_reposts(tmp_path):
    pending_response = {
        "orderId": "entry-pending", "status": "NEW",
        "origQty": "0.2", "executedQty": "0",
    }
    fake = FakeClient(pending_response)
    fake.query_results["entry-pending"] = DemoBinanceError(
        "RECONCILIATION_READ_RETRY_EXHAUSTED:/fapi/v1/order",
        retryable=True,
    )
    item, store = executor(tmp_path, fake)
    item.startup()
    result = item.execute_candidate(synthetic_snapshot(), filters())
    assert result["state"] == "ENTRY_SUBMITTED"
    assert result["payload"]["reconciliation_required"] is True
    assert result["payload"]["entry_post_retried"] is False
    assert item.reconciliation_ok is False
    assert len(fake.entry_calls) == 1
    assert fake.protective_calls == []
    store.close()


def test_ambiguous_entry_post_is_reconciled_without_duplicate_post(tmp_path):
    class AmbiguousEntryClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.recovered = False

        def create_market_entry(self, symbol, quantity, client_id):
            self.entry_calls.append((symbol, quantity, client_id))
            raise requests.Timeout("entry write outcome unknown")

        def query_order(self, symbol, *, order_id=None, client_id=None):
            self.query_calls.append(
                (symbol, str(order_id or ""), str(client_id or ""))
            )
            if not self.recovered:
                raise DemoBinanceError(
                    "RECONCILIATION_READ_RETRY_EXHAUSTED:/fapi/v1/order",
                    retryable=True,
                )
            return {
                "orderId": "entry-recovered", "status": "FILLED",
                "origQty": "0.2", "executedQty": "0.2",
                "avgPrice": "100.0", "cumQuote": "20.0",
            }

    fake = AmbiguousEntryClient()
    item, store = executor(tmp_path, fake)
    item.startup()
    pending = item.execute_candidate(synthetic_snapshot(), filters())
    assert pending["state"] == "ENTRY_SUBMITTED"
    assert pending["payload"]["entry_submission_ambiguous"] is True
    assert pending["payload"]["reconciliation_required"] is True
    assert len(fake.entry_calls) == 1
    assert fake.protective_calls == []
    assert item.reconciliation_ok is False

    fake.recovered = True
    fake.positions = [{"symbol": "FIXTUREUSDT", "positionAmt": "0.2"}]
    reconciled = item.reconcile()
    assert reconciled["ok"] is True
    assert store.get(synthetic_snapshot().candidate.candidate_id)["state"] == (
        "PROTECTED"
    )
    assert len(fake.entry_calls) == 1
    assert len(fake.protective_calls) == 2

    # A subsequent restart/reconciliation observes protection; it must never
    # submit another entry or another SL/TP pair.
    fake.orders = [
        {"orderId": "STOP_MARKET-id", "clientOrderId": "dbr4-s-test", "symbol": "FIXTUREUSDT"},
        {"orderId": "TAKE_PROFIT_MARKET-id", "clientOrderId": "dbr4-t-test", "symbol": "FIXTUREUSDT"},
    ]
    item.reconcile()
    assert len(fake.entry_calls) == 1
    assert len(fake.protective_calls) == 2
    store.close()


def test_partial_fill_protects_only_filled_quantity(tmp_path):
    partial = {
        "orderId": "entry-partial", "status": "PARTIALLY_FILLED",
        "origQty": "0.2", "executedQty": "0.1", "avgPrice": "100.0",
    }
    fake = FakeClient(partial)
    fake.query_results["entry-partial"] = {
        **partial, "status": "CANCELED",
    }
    item, store = executor(tmp_path, fake)
    item.startup()
    result = item.execute_candidate(synthetic_snapshot(), filters())
    assert result["state"] == "PROTECTED"
    assert result["payload"]["partial_fill"] is True
    assert result["payload"]["filled_qty"] == pytest.approx(0.1)
    assert {row[1] for row in fake.protective_calls} == {"0.1"}
    assert fake.cancel_calls
    store.close()


def test_rejected_entry_creates_no_protection(tmp_path):
    fake = FakeClient({
        "orderId": "rejected", "status": "REJECTED",
        "origQty": "0.2", "executedQty": "0", "avgPrice": "0",
    })
    item, store = executor(tmp_path, fake)
    item.startup()
    result = item.execute_candidate(synthetic_snapshot(), filters())
    assert result["state"] == "ENTRY_REJECTED"
    assert fake.protective_calls == []
    store.close()


def test_restart_reconciliation_recovers_fill_before_new_entry(tmp_path):
    fake = FakeClient()
    item, store = executor(tmp_path, fake)
    snapshot = synthetic_snapshot()
    candidate_id = snapshot.candidate.candidate_id
    store.claim_candidate(candidate_id, snapshot.symbol, {"policy_id": item.policy.policy_id})
    store.update(
        candidate_id, "ENTRY_SUBMITTED",
        requested_qty=0.2, stop=99.0, target=102.0,
        entry_client_id="dbr4-e-restart", entry_order_id="entry-restart",
    )
    fake.query_results["entry-restart"] = {
        "orderId": "entry-restart", "status": "FILLED",
        "origQty": "0.2", "executedQty": "0.2", "avgPrice": "100",
    }
    fake.positions = [{"symbol": snapshot.symbol, "positionAmt": "0.2"}]
    result = item.startup()
    assert result["status"] == "READY"
    assert store.get(candidate_id)["state"] == "PROTECTED"
    assert len(fake.entry_calls) == 0
    assert len(fake.protective_calls) == 2
    store.close()


def test_restart_after_one_algo_leg_creates_only_missing_leg_once(tmp_path):
    fake = FakeClient()
    item, store = executor(tmp_path, fake)
    snapshot = synthetic_snapshot()
    candidate_id = snapshot.candidate.candidate_id
    stop_client_id = "dbr4-s-existing"
    tp_client_id = "dbr4-t-missing"
    store.claim_candidate(candidate_id, snapshot.symbol, {"policy_id": item.policy.policy_id})
    store.update(
        candidate_id, "PROTECTION_PENDING",
        requested_qty=0.2, filled_qty=0.2, stop=99.0, target=102.0,
        entry_client_id="dbr4-e-existing", entry_order_id="entry-existing",
        stop_client_id=stop_client_id, stop_order_id="stop-existing",
        stop_post_attempted=True, tp_client_id=tp_client_id,
        protection_api="BINANCE_USDM_ALGO_ORDER",
    )
    stop_order = {
        "algoId": "stop-existing", "clientAlgoId": stop_client_id,
        "algoStatus": "NEW", "orderType": "STOP_MARKET",
        "symbol": snapshot.symbol,
    }
    fake.positions = [{"symbol": snapshot.symbol, "positionAmt": "0.2"}]
    fake.algo_orders = [stop_order]

    started = item.startup()
    assert started["status"] == "READY"
    protected = store.get(candidate_id)
    assert protected["state"] == "PROTECTED"
    assert [row[3] for row in fake.protective_calls] == ["TAKE_PROFIT_MARKET"]
    assert fake.entry_calls == []

    fake.algo_orders = [
        stop_order,
        {
            "algoId": protected["payload"]["tp_order_id"],
            "clientAlgoId": protected["payload"]["tp_client_id"],
            "algoStatus": "NEW", "orderType": "TAKE_PROFIT_MARKET",
            "symbol": snapshot.symbol,
        },
    ]
    repeated = item.reconcile()
    assert repeated["ok"] is True
    assert [row[3] for row in fake.protective_calls] == ["TAKE_PROFIT_MARKET"]
    assert fake.entry_calls == []
    store.close()


def test_reconciliation_recognizes_algo_protection_and_does_not_duplicate(tmp_path):
    fake = FakeClient()
    item, store = executor(tmp_path, fake)
    item.startup()
    snapshot = synthetic_snapshot()
    protected = item.execute_candidate(snapshot, filters())
    fake.positions = [{"symbol": snapshot.symbol, "positionAmt": "0.2"}]
    fake.algo_orders = [
        {
            "algoId": protected["payload"]["stop_order_id"],
            "clientAlgoId": protected["payload"]["stop_client_id"],
            "algoStatus": "NEW", "symbol": snapshot.symbol,
        },
        {
            "algoId": protected["payload"]["tp_order_id"],
            "clientAlgoId": protected["payload"]["tp_client_id"],
            "algoStatus": "NEW", "symbol": snapshot.symbol,
        },
    ]
    result = item.reconcile()
    assert result["ok"] is True
    assert result["exchange_open_regular_orders"] == 0
    assert result["exchange_open_algo_orders"] == 2
    assert result["mismatches"] == []
    assert len(fake.protective_calls) == 2
    assert fake.algo_cancel_calls == []
    store.close()


def test_orphan_algo_protection_is_cleaned_without_new_entry(tmp_path):
    fake = FakeClient()
    fake.algo_orders = [{
        "algoId": "orphan-algo", "clientAlgoId": "dbr4-s-orphan-algo",
        "symbol": "ORPHANUSDT", "algoStatus": "NEW",
    }]
    item, store = executor(tmp_path, fake)
    result = item.startup()
    assert result["status"] == "READY"
    assert result["reconciliation"]["cleaned_orphan_orders"] == [
        "algo:orphan-algo",
    ]
    assert fake.algo_cancel_calls == [
        ("orphan-algo", "dbr4-s-orphan-algo"),
    ]
    assert fake.entry_calls == []
    store.close()


def test_orphan_protective_order_is_cleaned_without_new_entry(tmp_path):
    fake = FakeClient()
    fake.orders = [{
        "orderId": "orphan-stop", "clientOrderId": "dbr4-s-orphan",
        "symbol": "ORPHANUSDT", "status": "NEW",
    }]
    item, store = executor(tmp_path, fake)
    result = item.startup()
    assert result["status"] == "READY"
    assert result["reconciliation"]["cleaned_orphan_orders"] == ["orphan-stop"]
    assert fake.cancel_calls == [("ORPHANUSDT", "orphan-stop")]
    assert fake.entry_calls == []
    store.close()


def test_exchange_local_position_mismatch_fails_closed(tmp_path):
    fake = FakeClient()
    fake.positions = [{"symbol": "UNKNOWNUSDT", "positionAmt": "1"}]
    item, store = executor(tmp_path, fake)
    result = item.startup()
    assert result["status"] == "BLOCKED"
    assert result["reconciliation"]["mismatches"] == [
        "ORPHAN_EXCHANGE_POSITION:UNKNOWNUSDT"
    ]
    assert item.execute_candidate(synthetic_snapshot(), filters())["status"] == (
        "BLOCKED_RECONCILIATION"
    )
    assert fake.entry_calls == []
    store.close()


def test_repeated_reconciliation_timeout_stays_observable_and_fail_closed(tmp_path):
    responses = [
        FakeResponse({"serverTime": 1_800_000_000_000}),
        FakeResponse({"timezone": "UTC", "symbols": []}),
        FakeResponse({"dualSidePosition": False}),
        requests.Timeout("positionRisk timeout 1"),
        requests.Timeout("positionRisk timeout 2"),
        requests.Timeout("positionRisk timeout 3"),
        # A later service-loop/startup attempt gets a complete clean snapshot.
        FakeResponse({"serverTime": 1_800_000_001_000}),
        FakeResponse({"timezone": "UTC", "symbols": []}),
        FakeResponse({"dualSidePosition": False}),
        FakeResponse([]),
        FakeResponse([]),
        FakeResponse([]),
    ]
    session = SequenceSession(responses)
    cfg = config(database_path=tmp_path / "retry.sqlite3")
    client = DemoBinanceClient(
        cfg,
        session=session,
        timeout_seconds=0.1,
        reconciliation_retry_delays=(0.0, 0.0),
        reconciliation_retry_total_seconds=5.0,
        sleep=lambda _value: None,
        random_uniform=lambda _low, _high: 0.0,
    )
    store = DemoExecutionStore(cfg.database_path)
    item = DemoBExecutor(cfg, client, store, policy=FakePolicy())

    blocked = item.startup()
    assert blocked["status"] == "RECONCILIATION_WAITING"
    assert blocked["reconciliation"]["complete"] is False
    assert blocked["reconciliation"]["exchange_open_positions"] is None
    assert blocked["reconciliation"]["exchange_open_orders"] is None
    assert blocked["reconciliation"]["retry"]["status"] == "EXHAUSTED"
    assert item.execute_candidate(synthetic_snapshot(), filters())["status"] == (
        "BLOCKED_RECONCILIATION"
    )
    assert all(call[0] == "GET" for call in session.calls)
    assert not any(event["event_type"] == "RECONCILIATION" for event in store.events())

    recovered = item.startup()
    assert recovered["status"] == "READY"
    assert recovered["reconciliation"]["complete"] is True
    assert recovered["reconciliation"]["exchange_open_positions"] == 0
    assert recovered["reconciliation"]["exchange_open_orders"] == 0
    assert sum(
        event["event_type"] == "RECONCILIATION" for event in store.events()
    ) == 1
    assert all(call[0] == "GET" for call in session.calls)
    store.close()


def test_retry_inside_reconciliation_does_not_duplicate_side_effects(tmp_path):
    session = SequenceSession([
        FakeResponse({"serverTime": 1_800_000_000_000}),
        FakeResponse({"timezone": "UTC", "symbols": []}),
        FakeResponse({"dualSidePosition": False}),
        FakeResponse([]),
        requests.ConnectionError("connection reset"),
        FakeResponse([]),
        FakeResponse([]),
        # Complete snapshot is read again after recovery.
        FakeResponse([]),
        FakeResponse([]),
        FakeResponse([]),
    ])
    cfg = config(database_path=tmp_path / "single-reconcile.sqlite3")
    client = DemoBinanceClient(
        cfg,
        session=session,
        timeout_seconds=0.1,
        reconciliation_retry_delays=(0.0,),
        reconciliation_retry_total_seconds=5.0,
        sleep=lambda _value: None,
        random_uniform=lambda _low, _high: 0.0,
    )
    store = DemoExecutionStore(cfg.database_path)
    item = DemoBExecutor(cfg, client, store, policy=FakePolicy())
    result = item.startup()
    assert result["status"] == "READY"
    assert result["reconciliation"]["complete_snapshot_repeated_after_retry"] is True
    assert sum(
        event["event_type"] == "RECONCILIATION" for event in store.events()
    ) == 1
    assert not any(call[0] in {"POST", "DELETE"} for call in session.calls)
    open_order_reads = [
        call for call in session.calls if call[1].endswith("/fapi/v1/openOrders")
    ]
    open_algo_order_reads = [
        call for call in session.calls if call[1].endswith("/fapi/v1/openAlgoOrders")
    ]
    position_reads = [
        call for call in session.calls if call[1].endswith("/fapi/v2/positionRisk")
    ]
    assert len(position_reads) == 2
    assert len(open_order_reads) == 3
    assert len(open_algo_order_reads) == 2
    store.close()


def test_max_ten_positions_blocks_eleventh(tmp_path):
    fake = FakeClient()
    fake.positions = [
        {"symbol": f"ALT{index}USDT", "positionAmt": "1"}
        for index in range(10)
    ]
    item, store = executor(tmp_path, fake)
    store.put_meta("first_lifecycle_verified", True)
    item.reconciliation_ok = True
    result = item.execute_candidate(synthetic_snapshot(), filters())
    assert result["state"] == "SKIPPED_CAPACITY"
    assert result["payload"]["effective_position_cap"] == 10
    assert fake.entry_calls == []
    store.close()


def test_leverage_is_exactly_five_and_risk_semantics_hold(tmp_path):
    fake = FakeClient()
    item, store = executor(tmp_path, fake)
    item.startup()
    item.execute_candidate(synthetic_snapshot(), filters())
    assert fake.leverage_calls == [("FIXTUREUSDT", 5)]
    sizing = DemoBExecutor.position_size(
        equity=1000, available_balance=1000, entry=100, stop=99,
    )
    assert sizing["initial_margin_fraction"] <= 0.01 + 1e-12
    assert sizing["stop_risk_fraction"] <= 0.0025 + 1e-12
    assert sizing["hard_risk_fraction"] == 0.01
    store.close()


def test_first_exact_entry_exit_lifecycle_restores_capacity_to_ten(tmp_path):
    fake = FakeClient()
    item, store = executor(tmp_path, fake)
    item.startup()
    snapshot = synthetic_snapshot()
    protected = item.execute_candidate(snapshot, filters())
    assert item.effective_position_cap == 1
    fake.positions = []
    fake.orders = []
    fake.algo_query_results[protected["payload"]["stop_order_id"]] = {
        "algoId": protected["payload"]["stop_order_id"],
        "clientAlgoId": protected["payload"]["stop_client_id"],
        "algoStatus": "FINISHED", "actualOrderId": "actual-stop",
        "actualPrice": "99", "actualQty": "0.2",
    }
    fake.algo_query_results[protected["payload"]["tp_order_id"]] = {
        "algoId": protected["payload"]["tp_order_id"],
        "clientAlgoId": protected["payload"]["tp_client_id"],
        "algoStatus": "CANCELED", "actualOrderId": "",
        "actualPrice": "0", "actualQty": "0",
    }
    fake.query_results["actual-stop"] = {
        "orderId": "actual-stop", "status": "FILLED",
        "avgPrice": "99", "executedQty": "0.2",
    }
    reconciled = item.reconcile()
    assert reconciled["ok"] is True
    assert item.effective_position_cap == 10
    closed = store.get(snapshot.candidate.candidate_id)
    assert closed["state"] == "CLOSED_RECONCILED"
    assert closed["payload"]["fees"] == pytest.approx(0.01)
    assert closed["payload"]["funding"] == pytest.approx(-0.001)
    store.close()


def test_mainnet_signal_endpoint_is_exact_and_credential_free():
    proof = validate_mainnet_signal_endpoint(MAINNET_USDM_PUBLIC_URL)
    assert proof == {
        "signal_market_environment": "MAINNET",
        "signal_base_url": MAINNET_USDM_PUBLIC_URL,
        "public_get_only": True,
        "credentials_present": False,
        "order_route": False,
    }
    for rejected in (
        "https://demo-fapi.binance.com",
        "https://fapi.binance.com.evil.example",
        "http://fapi.binance.com",
    ):
        with pytest.raises(RuntimeError, match="MAINNET_SIGNAL_ENDPOINT_REJECTED"):
            validate_mainnet_signal_endpoint(rejected)


def test_cross_environment_contract_checks_identity_and_all_filters():
    signal = contract()
    execution = contract(step_size=0.01, min_notional=10.0)
    accepted = compare_contracts("FIXTUREUSDT", signal, execution)
    assert accepted["compatible"] is True
    assert accepted["filter_comparison"]["step_size"] == {
        "mainnet": "0.001", "demo": "0.01", "equal": False,
    }
    assert accepted["filter_comparison"]["min_notional"]["equal"] is False
    assert accepted["execution_filters_authoritative"] is True
    assert accepted["post_rounding_rr_recheck_required"] is True

    rejected = compare_contracts(
        "FIXTUREUSDT", signal, contract(quote_asset="USDC")
    )
    assert rejected["compatible"] is False
    assert any(
        reason.startswith("CONTRACT_IDENTITY_MISMATCH:quote_asset")
        for reason in rejected["reasons"]
    )

    tokenized = compare_contracts(
        "FIXTUREUSDT",
        contract(contract_type="TRADIFI_PERPETUAL"),
        contract(contract_type="TRADIFI_PERPETUAL"),
    )
    assert tokenized["compatible"] is False
    assert "UNSUPPORTED_CONTRACT_TYPE:MAINNET:TRADIFI_PERPETUAL" in tokenized[
        "reasons"
    ]
    assert "UNSUPPORTED_CONTRACT_TYPE:DEMO:TRADIFI_PERPETUAL" in tokenized[
        "reasons"
    ]


def test_missing_demo_symbol_fails_before_account_or_order_calls(tmp_path):
    fake = FakeClient()
    store = DemoExecutionStore(tmp_path / "cross-missing.sqlite3")
    item = DemoBExecutor(
        config(database_path=store.path),
        fake,
        store,
        policy=FakePolicy(),
        signal_market_environment="MAINNET",
        execution_environment="DEMO",
    )
    assert item.startup()["status"] == "READY"
    reads_before = fake.position_risk_calls
    snapshot = mainnet_snapshot()
    audit = audit_cross_environment_candidate(
        snapshot, contract(), {}, None
    )
    result = item.execute_candidate(
        snapshot,
        {},
        cross_environment_audit=audit,
    )
    assert result["state"] == "SKIPPED_CROSS_ENVIRONMENT"
    assert "SYMBOL_NOT_AVAILABLE_ON_DEMO" in result["payload"][
        "cross_environment_reasons"
    ]
    assert fake.position_risk_calls == reads_before
    assert fake.account_calls == 0
    assert fake.entry_calls == []
    assert result["payload"]["signal_market_environment"] == "MAINNET"
    assert result["payload"]["execution_environment"] == "DEMO"
    store.close()


def test_demo_quote_before_mainnet_candidate_fails_closed(tmp_path):
    fake = FakeClient()
    store = DemoExecutionStore(tmp_path / "cross-stale.sqlite3")
    item = DemoBExecutor(
        config(database_path=store.path), fake, store,
        policy=FakePolicy(), signal_market_environment="MAINNET",
    )
    item.startup()
    snapshot = mainnet_snapshot()
    quote = demo_quote(snapshot, seconds=-1)
    audit = audit_cross_environment_candidate(
        snapshot, contract(), contract(), quote
    )
    result = item.execute_candidate(
        snapshot, contract(), execution_quote=quote,
        cross_environment_audit=audit,
    )
    assert result["state"] == "SKIPPED_CROSS_ENVIRONMENT"
    assert "DEMO_QUOTE_BEFORE_CANDIDATE" in result["payload"][
        "cross_environment_reasons"
    ]
    assert fake.entry_calls == []
    store.close()


def test_mainnet_candidate_close_after_emission_fails_causality_audit():
    snapshot = mainnet_snapshot()
    event = dict(snapshot.candidate.event)
    event["close_time"] = (
        snapshot.candidate.emitted_at + timedelta(seconds=1)
    ).isoformat()
    candidate = Candidate(
        candidate_id="future-close-candidate",
        symbol=snapshot.symbol,
        side="LONG",
        emitted_at=snapshot.candidate.emitted_at,
        event=event,
    )
    snapshot = replace(snapshot, candidate=candidate)
    quote = demo_quote(snapshot)
    audit = audit_cross_environment_candidate(
        snapshot, contract(), contract(), quote
    )
    assert audit["ok"] is False
    assert "CANDIDATE_CLOSE_AFTER_EMISSION" in audit["reasons"]


def test_valid_mainnet_signal_demo_execution_stamps_every_lifecycle_event(tmp_path):
    fake = FakeClient()
    store = DemoExecutionStore(tmp_path / "cross-valid.sqlite3")
    item = DemoBExecutor(
        config(database_path=store.path), fake, store,
        policy=FakePolicy(), signal_market_environment="MAINNET",
    )
    assert item.startup()["status"] == "READY"
    snapshot = mainnet_snapshot()
    quote = demo_quote(snapshot)
    audit = audit_cross_environment_candidate(
        snapshot, contract(), contract(), quote
    )
    assert audit["ok"] is True
    result = item.execute_candidate(
        snapshot, contract(), execution_quote=quote,
        cross_environment_audit=audit,
    )
    assert result["state"] == "PROTECTED"
    assert result["payload"]["signal_market_environment"] == "MAINNET"
    assert result["payload"]["execution_environment"] == "DEMO"
    assert result["payload"]["cross_environment_audit"]["ok"] is True
    assert result["payload"]["signal_best_ask"] == pytest.approx(100.02)
    assert result["payload"]["execution_best_ask"] == pytest.approx(100.0)
    lifecycle = [
        event for event in store.events()
        if event["candidate_id"] == snapshot.candidate.candidate_id
    ]
    assert {event["event_type"] for event in lifecycle} == {
        "ENTRY_SUBMITTED", "ENTRY_FILL", "PROTECTION_SUBMITTED",
    }
    assert all(
        event["payload"]["signal_market_environment"] == "MAINNET"
        and event["payload"]["execution_environment"] == "DEMO"
        for event in lifecycle
    )
    store.close()


def test_mainnet_geometry_invalid_at_demo_quote_places_no_order(tmp_path):
    class RejectingAtDemoQuotePolicy(FakePolicy):
        def resolve_geometry(self, snapshot, decision, **kwargs):
            quote = kwargs["execution_quote"]
            assert quote.source == DEMO_EXECUTION_QUOTE_SOURCE
            return None, "SKIP_REPRICED_RR"

    fake = FakeClient()
    store = DemoExecutionStore(tmp_path / "cross-geometry.sqlite3")
    item = DemoBExecutor(
        config(database_path=store.path), fake, store,
        policy=RejectingAtDemoQuotePolicy(),
        signal_market_environment="MAINNET",
    )
    item.startup()
    snapshot = mainnet_snapshot()
    quote = demo_quote(snapshot, bid=104.99, ask=105.0)
    audit = audit_cross_environment_candidate(
        snapshot, contract(), contract(), quote
    )
    result = item.execute_candidate(
        snapshot, contract(), execution_quote=quote,
        cross_environment_audit=audit,
    )
    assert result["state"] == "SKIPPED_GEOMETRY"
    assert result["payload"]["geometry_reason"] == "SKIP_REPRICED_RR"
    assert result["payload"]["signal_best_ask"] == pytest.approx(100.02)
    assert result["payload"]["execution_best_ask"] == pytest.approx(105.0)
    assert fake.account_calls == 0
    assert fake.entry_calls == []
    store.close()


def test_tampered_cross_environment_audit_is_rejected_before_order(tmp_path):
    fake = FakeClient()
    store = DemoExecutionStore(tmp_path / "cross-tampered.sqlite3")
    item = DemoBExecutor(
        config(database_path=store.path), fake, store,
        policy=FakePolicy(), signal_market_environment="MAINNET",
    )
    item.startup()
    snapshot = mainnet_snapshot()
    quote = demo_quote(snapshot)
    audit = audit_cross_environment_candidate(
        snapshot, contract(), contract(), quote
    )
    audit["execution_best_ask"] = 1.0
    result = item.execute_candidate(
        snapshot, contract(), execution_quote=quote,
        cross_environment_audit=audit,
    )
    assert result["state"] == "SKIPPED_CROSS_ENVIRONMENT"
    assert "AUDIT_EXECUTION_PRICE_MISMATCH" in result["payload"][
        "cross_environment_reasons"
    ]
    assert "AUDIT_HASH_MISMATCH" in result["payload"][
        "cross_environment_reasons"
    ]
    assert fake.entry_calls == []
    store.close()
