"""Idempotent, fail-closed Demo executor for frozen policy B only."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import hashlib
import threading
import time
from typing import Any, Mapping

from shadow_runtime.models import ExecutionQuote, Geometry, MarketSnapshot
from shadow_runtime.policies import MomentumRepricedPolicy

from .client import DemoBinanceError
from .config import DemoExecutionConfig
from .cross_environment import (
    EXECUTION_ENVIRONMENT,
    SIGNAL_MARKET_ENVIRONMENT,
    verify_cross_environment_audit,
)
from .store import DemoExecutionStore


POLICY_ID = "MOMENTUM_REPRICED_V1"
CLIENT_PREFIX = "dbr4-"
TERMINAL_ORDER_STATES = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED"})
TERMINAL_ALGO_STATES = frozenset({
    "CANCELED", "EXPIRED", "REJECTED", "TRIGGERED", "FINISHED",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _client_id(kind: str, candidate_id: str) -> str:
    digest = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:22]
    return f"{CLIENT_PREFIX}{kind}-{digest}"[:36]


def _floor_to_step(value: float, step: float) -> float:
    if value <= 0 or step <= 0:
        return 0.0
    raw = Decimal(str(value))
    quantum = Decimal(str(step))
    return float((raw / quantum).to_integral_value(rounding=ROUND_DOWN) * quantum)


def _format_number(value: float) -> str:
    rendered = format(Decimal(str(value)).normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _position_amount(row: dict) -> float:
    try:
        return float(row.get("positionAmt") or 0)
    except (TypeError, ValueError):
        return 0.0


def _algo_order_id(order: Mapping[str, Any]) -> str:
    return str(order.get("algoId") or "")


def _algo_status(order: Mapping[str, Any]) -> str:
    return str(order.get("algoStatus") or "").upper()


def _algo_triggered(order: Mapping[str, Any]) -> bool:
    try:
        actual_qty = float(order.get("actualQty") or 0)
    except (TypeError, ValueError):
        actual_qty = 0.0
    return bool(order.get("actualOrderId")) and (
        actual_qty > 0 or _algo_status(order) in {"TRIGGERED", "FINISHED"}
    )


class DemoBExecutor:
    """Execution authority exists only here and only for Demo/Testnet policy B."""

    def __init__(
        self,
        config: DemoExecutionConfig,
        client,
        store: DemoExecutionStore,
        *,
        policy=None,
        sleep=time.sleep,
        signal_market_environment: str = "DEMO",
        execution_environment: str = EXECUTION_ENVIRONMENT,
    ):
        self.config = config
        self.client = client
        self.store = store
        self.policy = policy or MomentumRepricedPolicy()
        self.sleep = sleep
        self.signal_market_environment = str(signal_market_environment).upper()
        self.execution_environment = str(execution_environment).upper()
        if self.execution_environment != EXECUTION_ENVIRONMENT:
            raise RuntimeError("DEMO_EXECUTOR_EXECUTION_ENVIRONMENT_MUST_BE_DEMO")
        if self.signal_market_environment not in {"DEMO", SIGNAL_MARKET_ENVIRONMENT}:
            raise RuntimeError("UNSUPPORTED_SIGNAL_MARKET_ENVIRONMENT")
        if getattr(self.policy, "policy_id", None) != POLICY_ID:
            raise RuntimeError("DEMO_EXECUTOR_POLICY_MUST_BE_MOMENTUM_REPRICED_V1")
        self.reconciliation_ok = False
        self.environment_proof: dict[str, Any] | None = None
        self._execution_lock = threading.RLock()

    @property
    def environment_fields(self) -> dict[str, str]:
        return {
            "signal_market_environment": self.signal_market_environment,
            "execution_environment": self.execution_environment,
        }

    def _event_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {**self.environment_fields, **dict(payload)}

    @property
    def effective_position_cap(self) -> int:
        if self.store.get_meta("first_lifecycle_verified", False):
            return self.config.frozen_max_open_positions
        return self.config.initial_validation_cap

    def startup(self) -> dict:
        if not self.config.enabled:
            self.reconciliation_ok = False
            result = {
                "status": "DISABLED", "execution_enabled": False,
                "policy_id": POLICY_ID, "effective_position_cap": 0,
            }
            self.store.put_meta("startup", result)
            return result
        self.environment_proof = self.client.prove_environment()
        if not self.environment_proof.get("is_demo_or_testnet"):
            raise RuntimeError("DEMO_ENVIRONMENT_PROOF_FAILED")
        try:
            mode = self.client.position_mode()
        except DemoBinanceError as exc:
            reconciliation = self._reconciliation_waiting(
                exc, phase="POSITION_MODE_READ"
            )
            result = self._startup_result("RECONCILIATION_WAITING", reconciliation)
            self.store.put_meta("startup", result)
            return result
        if bool(mode.get("dualSidePosition")):
            raise RuntimeError("DEMO_HEDGE_MODE_UNSUPPORTED_FAIL_CLOSED")
        reconciliation = self.reconcile()
        if not reconciliation.get("complete", True):
            status = "RECONCILIATION_WAITING"
        else:
            status = "READY" if reconciliation["ok"] else "BLOCKED"
        result = self._startup_result(status, reconciliation)
        self.store.put_meta("startup", result)
        return result

    def _startup_result(self, status: str, reconciliation: dict) -> dict:
        return {
            "status": status,
            "execution_enabled": True,
            "policy_id": POLICY_ID,
            "environment": self.environment_proof,
            "reconciliation": reconciliation,
            "effective_position_cap": self.effective_position_cap,
        }

    def _reconciliation_waiting(self, exc: Exception, *, phase: str) -> dict:
        self.reconciliation_ok = False
        retry_state = getattr(exc, "retry_state", None)
        if retry_state is None:
            retry_state = getattr(self.client, "reconciliation_retry_state", None)
        result = {
            "ok": False,
            "complete": False,
            "state": "RECONCILIATION_WAITING",
            "phase": phase,
            "checked_at": _utc_now(),
            "exchange_open_positions": None,
            "exchange_open_orders": None,
            "local_active": len(self.store.active()),
            "mismatches": [],
            "cleaned_orphan_orders": [],
            "error": f"{type(exc).__name__}:{exc}",
            "retry": retry_state,
            "retry_history": list(
                getattr(self.client, "reconciliation_retry_history", [])[-10:]
            ),
        }
        self.store.put_meta("last_reconciliation", result)
        self.store.event("RECONCILIATION_WAITING", result)
        return result

    @staticmethod
    def position_size(
        *, equity: float, available_balance: float, entry: float, stop: float,
        leverage: int = 5, target_margin_fraction: float = 0.01,
        base_risk_fraction: float = 0.0025,
        hard_risk_fraction: float = 0.01,
    ) -> dict[str, float]:
        risk_per_unit = entry - stop
        if min(equity, available_balance, entry, stop, risk_per_unit) <= 0:
            raise ValueError("INVALID_SIZING_INPUT")
        margin_notional = equity * target_margin_fraction * leverage
        available_notional = available_balance * 0.95 * leverage
        base_risk_qty = equity * base_risk_fraction / risk_per_unit
        hard_risk_qty = equity * hard_risk_fraction / risk_per_unit
        margin_qty = margin_notional / entry
        available_qty = available_notional / entry
        quantity = min(margin_qty, available_qty, base_risk_qty, hard_risk_qty)
        notional = quantity * entry
        stop_risk = quantity * risk_per_unit
        return {
            "quantity": quantity,
            "notional": notional,
            "initial_margin": notional / leverage,
            "initial_margin_fraction": (notional / leverage) / equity,
            "stop_risk": stop_risk,
            "stop_risk_fraction": stop_risk / equity,
            "hard_risk_fraction": hard_risk_fraction,
        }

    def execute_candidate(
        self,
        snapshot: MarketSnapshot,
        exchange_filter: dict[str, Any],
        *,
        execution_quote: ExecutionQuote | None = None,
        cross_environment_audit: Mapping[str, Any] | None = None,
    ) -> dict:
        # The capacity and same-symbol checks must be atomic inside this
        # process. Exchange reconciliation remains the cross-restart source
        # of truth.
        with self._execution_lock:
            return self._execute_candidate_locked(
                snapshot,
                exchange_filter,
                execution_quote=execution_quote,
                cross_environment_audit=cross_environment_audit,
            )

    def _execute_candidate_locked(
        self,
        snapshot: MarketSnapshot,
        exchange_filter: dict[str, Any],
        *,
        execution_quote: ExecutionQuote | None = None,
        cross_environment_audit: Mapping[str, Any] | None = None,
    ) -> dict:
        if not self.config.enabled:
            raise DemoBinanceError("DEMO_EXECUTION_DISABLED")
        if not self.reconciliation_ok:
            return {"status": "BLOCKED_RECONCILIATION"}
        candidate = snapshot.candidate
        claimed = self.store.claim_candidate(
            candidate.candidate_id,
            snapshot.symbol,
            {
                "policy_id": POLICY_ID,
                "snapshot_id": snapshot.snapshot_id,
                "candidate_emitted_at": candidate.emitted_at.isoformat(),
                "candidate_event": dict(candidate.event),
                **self.environment_fields,
                "signal_snapshot_source": snapshot.source,
                "signal_snapshot_as_of": snapshot.as_of.isoformat(),
                "cross_environment_audit": dict(cross_environment_audit or {}),
            },
        )
        if not claimed:
            return {"status": "DUPLICATE_CANDIDATE", "candidate_id": candidate.candidate_id}

        try:
            same_symbol_local = [
                row for row in self.store.active()
                if row["candidate_id"] != candidate.candidate_id
                and row["symbol"] == snapshot.symbol
            ]
            if same_symbol_local:
                return self.store.update(
                    candidate.candidate_id, "SKIPPED_SYMBOL_ACTIVE",
                    symbol_guard_source="LOCAL_ACTIVE_LIFECYCLE",
                    conflicting_candidate_ids=[
                        row["candidate_id"] for row in same_symbol_local
                    ],
                )

            if self.signal_market_environment == SIGNAL_MARKET_ENVIRONMENT:
                audit = dict(cross_environment_audit or {})
                audit_reasons = verify_cross_environment_audit(
                    snapshot, execution_quote, audit
                )
                if audit_reasons:
                    return self.store.update(
                        candidate.candidate_id,
                        "SKIPPED_CROSS_ENVIRONMENT",
                        cross_environment_reasons=audit_reasons,
                    )

            positions = [
                row for row in self.client.position_risk()
                if abs(_position_amount(row)) > 0
            ]
            same_symbol_exchange = [
                row for row in positions
                if str(row.get("symbol") or "") == snapshot.symbol
            ]
            if same_symbol_exchange:
                return self.store.update(
                    candidate.candidate_id, "SKIPPED_SYMBOL_ACTIVE",
                    symbol_guard_source="EXCHANGE_OPEN_POSITION",
                    exchange_position_count=len(same_symbol_exchange),
                )
            if len(positions) >= self.effective_position_cap:
                return self.store.update(
                    candidate.candidate_id, "SKIPPED_CAPACITY",
                    effective_position_cap=self.effective_position_cap,
                    exchange_open_positions=len(positions),
                )

            decision = self.policy.evaluate(snapshot)
            if decision.status != "ENTRY_READY" or decision.entry_ready_at is None:
                return self.store.update(
                    candidate.candidate_id, "SKIPPED_NOT_READY",
                    decision_status=decision.status,
                )
            if execution_quote is None:
                return self.store.update(
                    candidate.candidate_id,
                    "SKIPPED_EXECUTION_QUOTE_UNAVAILABLE",
                    quote_required=True,
                    snapshot_quote_reused=False,
                )
            if execution_quote.event_time < decision.entry_ready_at:
                return self.store.update(
                    candidate.candidate_id, "SKIPPED_STALE_QUOTE",
                    entry_ready_at=decision.entry_ready_at.isoformat(),
                    quote_time=execution_quote.event_time.isoformat(),
                )
            geometry, reason = self.policy.resolve_geometry(
                snapshot, decision, execution_quote=execution_quote
            )
            if geometry is None or geometry.actual_rr_at_entry < self.config.min_actual_rr:
                return self.store.update(
                    candidate.candidate_id, "SKIPPED_GEOMETRY",
                    geometry_reason=reason,
                    signal_best_ask=float(snapshot.quote.best_ask),
                    execution_best_ask=float(execution_quote.best_ask),
                )
            self.store.update(
                candidate.candidate_id,
                "CLAIMED",
                signal_best_ask=float(snapshot.quote.best_ask),
                execution_best_ask=float(execution_quote.best_ask),
                execution_quote_source=execution_quote.source,
                execution_quote_time=execution_quote.event_time.isoformat(),
                geometry_reason=reason,
                geometry={
                    "entry": geometry.executable_entry,
                    "stop": geometry.stop,
                    "target": geometry.target,
                    "actual_rr_at_entry": geometry.actual_rr_at_entry,
                    "stop_source": geometry.stop_source,
                    "target_source": geometry.target_source,
                },
            )
            return self._submit_entry(snapshot, geometry, exchange_filter)
        except Exception as exc:
            current = self.store.get(candidate.candidate_id)
            current_state = str((current or {}).get("state") or "")
            if current_state.startswith("EMERGENCY_EXIT_"):
                self.store.update(
                    candidate.candidate_id, current_state,
                    failure=f"{type(exc).__name__}:{exc}",
                    reconciliation_required=True,
                )
            else:
                self.store.update(
                    candidate.candidate_id, "FAILED_CLOSED",
                    failure=f"{type(exc).__name__}:{exc}",
                )
            self.store.event(
                (
                    "EMERGENCY_EXIT_EXCEPTION"
                    if current_state.startswith("EMERGENCY_EXIT_")
                    else "FAILED_CLOSED"
                ),
                self._event_payload({"error": f"{type(exc).__name__}:{exc}"}),
                candidate.candidate_id,
            )
            raise

    def _submit_entry(
        self, snapshot: MarketSnapshot, geometry: Geometry,
        exchange_filter: dict[str, Any],
    ) -> dict:
        candidate_id = snapshot.candidate.candidate_id
        symbol = snapshot.symbol
        tick = float(exchange_filter.get("tick_size") or 0)
        step = float(exchange_filter.get("step_size") or 0)
        min_qty = float(exchange_filter.get("min_qty") or 0)
        min_notional = float(exchange_filter.get("min_notional") or 0)
        if min(tick, step, min_qty, min_notional) <= 0:
            return self.store.update(candidate_id, "SKIPPED_FILTERS", filter=exchange_filter)

        stop = _floor_to_step(geometry.stop, tick)
        target = _floor_to_step(geometry.target, tick)
        expected_entry = float(geometry.executable_entry)
        expected_rr = (target - expected_entry) / (expected_entry - stop)
        if not 0 < stop < expected_entry < target or expected_rr < self.config.min_actual_rr:
            return self.store.update(
                candidate_id, "SKIPPED_ROUNDED_RR", expected_rr=expected_rr,
                stop=stop, target=target,
            )

        account = self.client.account()
        equity = float(account.get("totalWalletBalance") or account.get("totalMarginBalance") or 0)
        available = float(account.get("availableBalance") or 0)
        sizing = self.position_size(
            equity=equity, available_balance=available,
            entry=expected_entry, stop=stop,
            leverage=self.config.leverage,
            target_margin_fraction=self.config.target_initial_margin_fraction,
            base_risk_fraction=self.config.base_stop_risk_fraction,
            hard_risk_fraction=self.config.hard_stop_risk_fraction,
        )
        quantity = _floor_to_step(sizing["quantity"], step)
        max_qty = float(exchange_filter.get("max_qty") or 0)
        if max_qty > 0:
            quantity = min(quantity, _floor_to_step(max_qty, step))
        notional = quantity * expected_entry
        actual_risk_fraction = quantity * (expected_entry - stop) / equity
        if (
            quantity < min_qty
            or notional < min_notional
            or actual_risk_fraction > self.config.hard_stop_risk_fraction + 1e-12
        ):
            return self.store.update(
                candidate_id, "SKIPPED_SIZE_FILTER",
                quantity=quantity, notional=notional,
                actual_risk_fraction=actual_risk_fraction, sizing=sizing,
            )

        leverage_response = self.client.set_leverage(symbol, self.config.leverage)
        entry_client_id = _client_id("e", candidate_id)
        self.store.update(
            candidate_id, "ENTRY_SUBMITTED",
            requested_qty=quantity, requested_qty_text=_format_number(quantity),
            expected_entry=expected_entry, stop=stop, target=target,
            expected_rr=expected_rr, sizing=sizing,
            leverage=self.config.leverage,
            leverage_response=leverage_response,
            entry_client_id=entry_client_id,
            entry_submitted_at=_utc_now(),
        )
        self.store.event(
            "ENTRY_SUBMITTED",
            self._event_payload({
                "symbol": symbol, "requested_qty": quantity,
                "client_id": entry_client_id,
            }),
            candidate_id,
        )
        try:
            order = self.client.create_market_entry(
                symbol, _format_number(quantity), entry_client_id
            )
        except Exception as exc:
            # A write timeout/connection failure has an unknown outcome.  The
            # deterministic client id is the idempotency key: never repeat the
            # POST.  Resolve the original intent with an authenticated GET.
            self.reconciliation_ok = False
            self.store.update(
                candidate_id, "ENTRY_SUBMITTED",
                entry_submission_ambiguous=True,
                entry_submit_failure=f"{type(exc).__name__}:{exc}",
                reconciliation_required=True,
            )
            self.store.event(
                "ENTRY_SUBMISSION_AMBIGUOUS",
                self._event_payload({
                    "symbol": symbol,
                    "client_id": entry_client_id,
                    "error": f"{type(exc).__name__}:{exc}",
                    "post_retried": False,
                }),
                candidate_id,
            )
            try:
                order = self.client.query_order(
                    symbol, client_id=entry_client_id
                )
            except Exception as read_exc:
                return self.store.update(
                    candidate_id, "ENTRY_SUBMITTED",
                    entry_lookup_failure=f"{type(read_exc).__name__}:{read_exc}",
                    reconciliation_required=True,
                )
            self.store.update(
                candidate_id, "ENTRY_SUBMITTED",
                entry_order_id=str(order.get("orderId") or ""),
                entry_response=order,
                entry_recovered_after_ambiguous_post=True,
                reconciliation_required=False,
            )
            return self._await_or_defer_entry(
                candidate_id, symbol, order, entry_client_id
            )
        self.store.update(
            candidate_id, "ENTRY_SUBMITTED",
            entry_order_id=str(order.get("orderId") or ""),
            entry_response=order,
        )
        return self._await_or_defer_entry(
            candidate_id, symbol, order, entry_client_id
        )

    def _await_or_defer_entry(
        self, candidate_id: str, symbol: str, order: dict, client_id: str,
    ) -> dict:
        """Poll only with GET; unresolved reads stay active for reconciliation."""
        try:
            final_order = self._await_entry_terminal(symbol, order, client_id)
        except Exception as exc:
            self.reconciliation_ok = False
            return self.store.update(
                candidate_id, "ENTRY_SUBMITTED",
                entry_terminal_lookup_failure=f"{type(exc).__name__}:{exc}",
                reconciliation_required=True,
                entry_post_retried=False,
            )
        return self._finalize_entry(candidate_id, symbol, final_order)

    @staticmethod
    def _filled_order_price(order: Mapping[str, Any]) -> float:
        filled_qty = float(order.get("executedQty") or 0)
        if filled_qty <= 0:
            return 0.0
        avg_price = float(order.get("avgPrice") or 0)
        if avg_price > 0:
            return avg_price
        cumulative = float(
            order.get("cumQuote") or order.get("cumQuoteQty") or 0
        )
        return cumulative / filled_qty if cumulative > 0 else 0.0

    def _refresh_filled_order(
        self, symbol: str, order: Mapping[str, Any], client_id: str | None,
    ) -> dict:
        """Resolve a filled write response using the safe bounded GET path."""
        if self._filled_order_price(order) > 0:
            return dict(order)
        order_id = order.get("orderId")
        return self.client.query_order(
            symbol,
            order_id=order_id if order_id not in (None, "") else None,
            client_id=client_id,
        )

    def _await_entry_terminal(self, symbol: str, order: dict, client_id: str) -> dict:
        current = dict(order)
        deadline = time.monotonic() + self.config.fill_timeout_seconds
        while str(current.get("status") or "").upper() not in TERMINAL_ORDER_STATES:
            if time.monotonic() >= deadline:
                break
            self.sleep(0.2)
            current = self.client.query_order(
                symbol, order_id=current.get("orderId"), client_id=client_id
            )
        status = str(current.get("status") or "").upper()
        executed = float(current.get("executedQty") or 0)
        if status not in TERMINAL_ORDER_STATES:
            try:
                self.client.cancel_order(
                    symbol, order_id=current.get("orderId"), client_id=client_id
                )
            finally:
                current = self.client.query_order(
                    symbol, order_id=current.get("orderId"), client_id=client_id
                )
        elif status == "PARTIALLY_FILLED" or (0 < executed < float(current.get("origQty") or executed)):
            try:
                self.client.cancel_order(
                    symbol, order_id=current.get("orderId"), client_id=client_id
                )
            except Exception:
                pass
            current = self.client.query_order(
                symbol, order_id=current.get("orderId"), client_id=client_id
            )
        return current

    def _finalize_entry(self, candidate_id: str, symbol: str, order: dict) -> dict:
        status = str(order.get("status") or "").upper()
        filled_qty = float(order.get("executedQty") or 0)
        if filled_qty <= 0:
            state = "ENTRY_REJECTED" if status == "REJECTED" else "ENTRY_UNFILLED"
            return self.store.update(
                candidate_id, state, entry_final_status=status, entry_final_response=order,
            )
        avg_price = self._filled_order_price(order)
        if avg_price <= 0:
            payload = self.store.get(candidate_id)["payload"]
            try:
                order = self._refresh_filled_order(
                    symbol, order, payload.get("entry_client_id")
                )
            except Exception as exc:
                self.reconciliation_ok = False
                pending = self.store.update(
                    candidate_id, "ENTRY_FILLED_UNPRICED",
                    filled_qty=filled_qty,
                    entry_final_status=status,
                    entry_final_response=order,
                    fill_price_lookup_failure=f"{type(exc).__name__}:{exc}",
                    reconciliation_required=True,
                )
                self.store.event(
                    "ENTRY_FILL_PRICE_PENDING",
                    self._event_payload({
                        "symbol": symbol,
                        "filled_qty": filled_qty,
                        "error": f"{type(exc).__name__}:{exc}",
                        "entry_post_retried": False,
                    }),
                    candidate_id,
                )
                return pending
            status = str(order.get("status") or status).upper()
            filled_qty = float(order.get("executedQty") or filled_qty)
            avg_price = self._filled_order_price(order)
        if avg_price <= 0:
            self.reconciliation_ok = False
            return self.store.update(
                candidate_id, "ENTRY_FILLED_UNPRICED",
                filled_qty=filled_qty,
                entry_final_status=status,
                entry_final_response=order,
                fill_price_lookup_failure=(
                    "FILLED_ENTRY_WITHOUT_AVERAGE_PRICE_AFTER_GET"
                ),
                reconciliation_required=True,
            )

        row = self.store.get(candidate_id)
        payload = row["payload"]
        stop = float(payload["stop"])
        target = float(payload["target"])
        actual_rr = (target - avg_price) / (avg_price - stop) if avg_price > stop else 0.0
        partial = filled_qty + 1e-12 < float(payload["requested_qty"])
        self.store.update(
            candidate_id, "PARTIALLY_FILLED" if partial else "ENTRY_FILLED",
            filled_qty=filled_qty, entry_fill=avg_price,
            entry_final_status=status, entry_final_response=order,
            actual_rr_at_fill=actual_rr, partial_fill=partial,
            entry_filled_at=_utc_now(),
        )
        self.store.event(
            "ENTRY_FILL",
            self._event_payload({
                "symbol": symbol, "requested_qty": payload["requested_qty"],
                "filled_qty": filled_qty, "entry_fill": avg_price,
                "partial": partial, "actual_rr": actual_rr,
            }),
            candidate_id,
        )
        if actual_rr < self.config.min_actual_rr:
            return self._emergency_close_and_confirm(
                candidate_id,
                symbol,
                filled_qty,
                reason="POST_FILL_RR_REJECTED",
                terminal_state="CLOSED_POST_FILL_RR_REJECTED",
            )
        return self._protect_fill(candidate_id, symbol, filled_qty)

    @staticmethod
    def _algo_order_missing(exc: Exception) -> bool:
        return isinstance(exc, DemoBinanceError) and exc.binance_code in {-2011, -2013}

    def _save_algo_leg(
        self, candidate_id: str, leg: str, client_id: str, order: dict,
    ) -> dict:
        algo_id = _algo_order_id(order)
        if not algo_id:
            raise DemoBinanceError(f"ALGO_PROTECTION_ID_MISSING:{leg}")
        return self.store.update(
            candidate_id, "PROTECTION_PENDING",
            **{
                f"{leg}_order_id": algo_id,
                f"{leg}_client_id": client_id,
                f"{leg}_response": order,
                f"{leg}_proven_at": _utc_now(),
                f"{leg}_submission_ambiguous": False,
            },
        )

    def _ensure_algo_protection_leg(
        self,
        candidate_id: str,
        symbol: str,
        quantity: str,
        trigger_price: str,
        *,
        leg: str,
        kind: str,
        client_id: str,
    ) -> dict:
        """Create one Algo protection leg exactly once, resolving ambiguity by GET."""
        payload = self.store.get(candidate_id)["payload"]
        known_algo_id = payload.get(f"{leg}_order_id")
        if known_algo_id:
            return self.client.query_algo_order(algo_id=known_algo_id)

        if payload.get(f"{leg}_post_attempted"):
            try:
                recovered = self.client.query_algo_order(client_id=client_id)
            except Exception as exc:
                if not self._algo_order_missing(exc):
                    raise
            else:
                self._save_algo_leg(candidate_id, leg, client_id, recovered)
                return recovered

        self.store.update(
            candidate_id, "PROTECTION_PENDING",
            **{
                f"{leg}_client_id": client_id,
                f"{leg}_post_attempted": True,
                f"{leg}_submission_intent_at": _utc_now(),
            },
        )
        try:
            order = self.client.create_protective(
                symbol, quantity, trigger_price, kind=kind, client_id=client_id,
            )
        except Exception as submit_exc:
            self.store.update(
                candidate_id, "PROTECTION_PENDING",
                **{
                    f"{leg}_submission_ambiguous": True,
                    f"{leg}_submit_failure": (
                        f"{type(submit_exc).__name__}:{submit_exc}"
                    ),
                },
            )
            try:
                order = self.client.query_algo_order(client_id=client_id)
            except Exception as query_exc:
                if self._algo_order_missing(query_exc):
                    raise submit_exc
                raise

        if not _algo_order_id(order):
            order = self.client.query_algo_order(client_id=client_id)
        self._save_algo_leg(candidate_id, leg, client_id, order)
        return order

    def _cancel_algo_leg_if_present(self, payload: Mapping[str, Any], leg: str) -> None:
        algo_id = payload.get(f"{leg}_order_id")
        client_id = payload.get(f"{leg}_client_id")
        if not algo_id and not client_id:
            return
        try:
            order = self.client.query_algo_order(
                algo_id=algo_id or None, client_id=client_id,
            )
        except Exception as exc:
            if self._algo_order_missing(exc):
                return
            raise
        if _algo_status(order) in TERMINAL_ALGO_STATES:
            return
        self.client.cancel_algo_order(
            algo_id=_algo_order_id(order) or algo_id or None,
            client_id=client_id,
        )

    def _cleanup_protection_after_flat(
        self, payload: Mapping[str, Any],
    ) -> tuple[list[str], list[str]]:
        canceled = []
        failures = []
        for leg in ("stop", "tp"):
            try:
                self._cancel_algo_leg_if_present(payload, leg)
                identity = str(
                    payload.get(f"{leg}_order_id")
                    or payload.get(f"{leg}_client_id")
                    or ""
                )
                if identity:
                    canceled.append(identity)
            except Exception as exc:
                failures.append(f"{leg}:{type(exc).__name__}:{exc}")
        return canceled, failures

    @staticmethod
    def _confirmed_exit_fill(
        order: Mapping[str, Any] | None,
        requested_quantity: float,
    ) -> tuple[bool, str, float]:
        order = dict(order or {})
        status = str(order.get("status") or "").upper()
        try:
            executed_quantity = float(order.get("executedQty") or 0.0)
        except (TypeError, ValueError):
            executed_quantity = 0.0
        confirmed = (
            status == "FILLED"
            and requested_quantity > 0
            and executed_quantity + 1e-12 >= requested_quantity
        )
        return confirmed, status, executed_quantity

    def _query_emergency_exit(
        self,
        symbol: str,
        *,
        order: Mapping[str, Any] | None,
        client_id: str,
        requested_quantity: float,
    ) -> tuple[dict | None, str | None]:
        current = dict(order or {})
        confirmed, _status, _executed = self._confirmed_exit_fill(
            current, requested_quantity
        )
        if confirmed:
            return current, None
        try:
            queried = self.client.query_order(
                symbol,
                order_id=current.get("orderId") or None,
                client_id=client_id,
            )
        except Exception as exc:
            return current or None, f"{type(exc).__name__}:{exc}"
        return dict(queried), None

    def _emergency_close_and_confirm(
        self,
        candidate_id: str,
        symbol: str,
        quantity: float,
        *,
        reason: str,
        terminal_state: str,
    ) -> dict:
        """Submit one reduce-only exit and prove flatness before terminal state.

        A POST ambiguity is resolved only by querying the deterministic client
        id. Protection is intentionally retained until the exchange position
        is proven flat.
        """
        self.reconciliation_ok = False
        exit_client_id = _client_id("x", candidate_id)
        self.store.update(
            candidate_id,
            "EMERGENCY_EXIT_PENDING",
            emergency_reason=reason,
            emergency_terminal_state=terminal_state,
            emergency_exit_client_id=exit_client_id,
            emergency_exit_requested_qty=float(quantity),
            emergency_exit_requested_at=_utc_now(),
            reconciliation_required=True,
        )
        submit_failure = None
        try:
            exit_order = self.client.create_reduce_only_market_exit(
                symbol, _format_number(quantity), exit_client_id
            )
        except Exception as submit_exc:
            submit_failure = f"{type(submit_exc).__name__}:{submit_exc}"
            self.store.update(
                candidate_id,
                "EMERGENCY_EXIT_UNKNOWN",
                emergency_exit_submit_failure=submit_failure,
                emergency_exit_post_retried=False,
            )
            exit_order = None

        exit_order, lookup_failure = self._query_emergency_exit(
            symbol,
            order=exit_order,
            client_id=exit_client_id,
            requested_quantity=float(quantity),
        )
        confirmed, exit_status, executed_quantity = self._confirmed_exit_fill(
            exit_order, float(quantity)
        )
        if lookup_failure or not confirmed:
            state = "EMERGENCY_EXIT_UNKNOWN" if submit_failure else "EMERGENCY_EXIT_RECONCILIATION"
            return self.store.update(
                candidate_id,
                state,
                emergency_exit=exit_order,
                emergency_exit_order_id=str((exit_order or {}).get("orderId") or ""),
                emergency_exit_lookup_failure=lookup_failure,
                emergency_exit_confirmed=False,
                emergency_exit_status=exit_status,
                emergency_exit_executed_qty=executed_quantity,
                emergency_exit_post_retried=False,
                reconciliation_required=True,
            )

        self.store.update(
            candidate_id,
            "EMERGENCY_EXIT_RECONCILIATION",
            emergency_exit=exit_order,
            emergency_exit_order_id=str(exit_order.get("orderId") or ""),
            emergency_exit_confirmed=True,
            emergency_exit_status=exit_status,
            emergency_exit_executed_qty=executed_quantity,
            emergency_exit_post_retried=False,
        )
        try:
            remaining = [
                row for row in self.client.position_risk()
                if str(row.get("symbol") or "") == symbol
                and abs(_position_amount(row)) > 0
            ]
        except Exception as exc:
            return self.store.update(
                candidate_id,
                "EMERGENCY_EXIT_RECONCILIATION",
                emergency_flat_check_failure=f"{type(exc).__name__}:{exc}",
                reconciliation_required=True,
            )
        if remaining:
            return self.store.update(
                candidate_id,
                "EMERGENCY_EXIT_RECONCILIATION",
                emergency_position_still_open=True,
                emergency_remaining_position=remaining,
                reconciliation_required=True,
            )

        payload = self.store.get(candidate_id)["payload"]
        canceled, failures = self._cleanup_protection_after_flat(payload)
        if failures:
            return self.store.update(
                candidate_id,
                "EMERGENCY_EXIT_CLEANUP_PENDING",
                emergency_position_confirmed_flat=True,
                protection_canceled_after_flat=canceled,
                protection_cleanup_failures=failures,
                reconciliation_required=True,
            )
        closed = self.store.update(
            candidate_id,
            terminal_state,
            emergency_position_confirmed_flat=True,
            protection_canceled_after_flat=canceled,
            protection_cleanup_failures=[],
            reconciliation_required=True,
            emergency_closed_at=_utc_now(),
        )
        self.store.event(
            "EMERGENCY_EXIT_CONFIRMED",
            self._event_payload({
                "symbol": symbol,
                "reason": reason,
                "terminal_state": terminal_state,
                "exit_order_id": str(exit_order.get("orderId") or ""),
                "position_confirmed_flat": True,
                "protection_canceled_after_flat": canceled,
            }),
            candidate_id,
        )
        return closed

    def _protect_fill(self, candidate_id: str, symbol: str, filled_qty: float) -> dict:
        row = self.store.get(candidate_id)
        if row["state"] == "PROTECTED":
            return row
        payload = row["payload"]
        quantity = _format_number(filled_qty)
        stop_id = str(payload.get("stop_client_id") or _client_id("s", candidate_id))
        tp_id = str(payload.get("tp_client_id") or _client_id("t", candidate_id))
        self.store.update(
            candidate_id, "PROTECTION_PENDING",
            stop_client_id=stop_id,
            tp_client_id=tp_id,
            protection_api="BINANCE_USDM_ALGO_ORDER",
        )
        try:
            stop_order = self._ensure_algo_protection_leg(
                candidate_id, symbol, quantity,
                _format_number(float(payload["stop"])),
                leg="stop", kind="STOP_MARKET", client_id=stop_id,
            )
            tp_order = self._ensure_algo_protection_leg(
                candidate_id, symbol, quantity,
                _format_number(float(payload["target"])),
                leg="tp", kind="TAKE_PROFIT_MARKET", client_id=tp_id,
            )
        except Exception as exc:
            self.reconciliation_ok = False
            self.store.update(
                candidate_id, "PROTECTION_PENDING",
                protection_failure=f"{type(exc).__name__}:{exc}",
            )
            return self._emergency_close_and_confirm(
                candidate_id,
                symbol,
                filled_qty,
                reason="PROTECTION_FAILURE",
                terminal_state="EMERGENCY_CLOSED_PROTECTION_FAILURE",
            )
        protected = self.store.update(
            candidate_id, "PROTECTED",
            stop_order_id=_algo_order_id(stop_order),
            stop_client_id=stop_id,
            stop_response=stop_order,
            tp_order_id=_algo_order_id(tp_order),
            tp_client_id=tp_id,
            tp_response=tp_order,
            protected_at=_utc_now(),
        )
        self.store.event(
            "PROTECTION_SUBMITTED",
            self._event_payload({
                "symbol": symbol, "filled_qty": filled_qty,
                "stop_order_id": protected["payload"]["stop_order_id"],
                "tp_order_id": protected["payload"]["tp_order_id"],
                "reduce_only": True,
                "protection_api": "BINANCE_USDM_ALGO_ORDER",
            }),
            candidate_id,
        )
        return protected

    def reconcile(self) -> dict:
        try:
            return self._reconcile_once()
        except DemoBinanceError as exc:
            return self._reconciliation_waiting(exc, phase="FULL_RECONCILIATION")

    def _reconcile_once(self) -> dict:
        # No entry may pass while any fresh exchange snapshot is incomplete.
        self.reconciliation_ok = False
        mismatches: list[str] = []
        cleaned_orphans: list[str] = []
        emergency_cleaned_algo_ids: set[str] = set()
        retry_history_before = len(
            getattr(self.client, "reconciliation_retry_history", [])
        )
        position_rows = list(self.client.position_risk())
        open_orders = list(self.client.open_orders())
        open_algo_orders = list(self.client.open_algo_orders())
        recovered_during_snapshot = (
            len(getattr(self.client, "reconciliation_retry_history", []))
            > retry_history_before
        )
        if recovered_during_snapshot:
            # Do not combine a pre-timeout response with a post-timeout one.
            # A successful retry is followed by a new complete exchange read
            # before local comparison or any cleanup side effect is allowed.
            position_rows = list(self.client.position_risk())
            open_orders = list(self.client.open_orders())
            open_algo_orders = list(self.client.open_algo_orders())
        positions = {
            str(row.get("symbol")): row
            for row in position_rows
            if abs(_position_amount(row)) > 0
        }
        algo_by_id = {_algo_order_id(row): row for row in open_algo_orders}
        local_active = self.store.active()
        local_by_symbol = {row["symbol"]: row for row in local_active}

        for row in list(local_active):
            candidate_id = row["candidate_id"]
            symbol = row["symbol"]
            state = row["state"]
            payload = row["payload"]
            if state == "CLAIMED":
                self.store.update(
                    candidate_id, "ABORTED_PRE_ENTRY_RESTART",
                    reconciliation_note="CLAIM_EXISTED_WITHOUT_ORDER_INTENT",
                )
                continue
            if state in {"ENTRY_SUBMITTED", "ENTRY_FILLED_UNPRICED"}:
                order = self.client.query_order(
                    symbol,
                    order_id=payload.get("entry_order_id") or None,
                    client_id=payload.get("entry_client_id"),
                )
                if float(order.get("executedQty") or 0) > 0 and symbol not in positions:
                    mismatches.append(f"EXCHANGE_FILL_WITHOUT_POSITION:{candidate_id}")
                elif float(order.get("executedQty") or 0) > 0:
                    recovered = self._finalize_entry(candidate_id, symbol, order)
                    if recovered["state"] == "ENTRY_FILLED_UNPRICED":
                        mismatches.append(
                            f"ENTRY_FILL_PRICE_UNRESOLVED:{candidate_id}"
                        )
                elif str(order.get("status") or "").upper() in TERMINAL_ORDER_STATES:
                    self.store.update(candidate_id, "ENTRY_UNFILLED", entry_final_response=order)
                else:
                    mismatches.append(f"ENTRY_ORDER_UNRESOLVED:{candidate_id}")
                continue
            if state in {"ENTRY_FILLED", "PARTIALLY_FILLED", "PROTECTION_PENDING"}:
                if symbol not in positions:
                    mismatches.append(f"LOCAL_FILL_WITHOUT_EXCHANGE_POSITION:{candidate_id}")
                else:
                    try:
                        self._protect_fill(candidate_id, symbol, float(payload["filled_qty"]))
                    except Exception as exc:
                        mismatches.append(
                            f"PROTECTION_RECOVERY_FAILED:{candidate_id}:"
                            f"{type(exc).__name__}:{exc}"
                        )
                continue
            if state.startswith("EMERGENCY_EXIT_"):
                exit_client_id = str(payload.get("emergency_exit_client_id") or "")
                requested_quantity = float(
                    payload.get("emergency_exit_requested_qty")
                    or payload.get("filled_qty")
                    or 0.0
                )
                exit_order, lookup_failure = self._query_emergency_exit(
                    symbol,
                    order=payload.get("emergency_exit"),
                    client_id=exit_client_id,
                    requested_quantity=requested_quantity,
                )
                confirmed, exit_status, executed_quantity = self._confirmed_exit_fill(
                    exit_order, requested_quantity
                )
                if lookup_failure or not confirmed:
                    self.store.update(
                        candidate_id,
                        "EMERGENCY_EXIT_UNKNOWN" if lookup_failure else state,
                        emergency_exit=exit_order,
                        emergency_exit_lookup_failure=lookup_failure,
                        emergency_exit_confirmed=False,
                        emergency_exit_status=exit_status,
                        emergency_exit_executed_qty=executed_quantity,
                        reconciliation_required=True,
                    )
                    mismatches.append(
                        f"EMERGENCY_EXIT_QUERY_UNKNOWN:{candidate_id}"
                        if lookup_failure
                        else f"EMERGENCY_EXIT_NOT_FILLED:{candidate_id}:"
                        f"{exit_status}:{executed_quantity}/{requested_quantity}"
                    )
                    continue
                if symbol in positions:
                    mismatches.append(
                        f"EMERGENCY_EXIT_UNRESOLVED:{candidate_id}:{state}"
                    )
                    continue
                canceled, failures = self._cleanup_protection_after_flat(payload)
                if failures:
                    self.store.update(
                        candidate_id,
                        "EMERGENCY_EXIT_CLEANUP_PENDING",
                        emergency_position_confirmed_flat=True,
                        protection_canceled_after_flat=canceled,
                        protection_cleanup_failures=failures,
                        reconciliation_required=True,
                    )
                    mismatches.append(
                        f"EMERGENCY_PROTECTION_CLEANUP_UNRESOLVED:{candidate_id}"
                    )
                    continue
                terminal_state = str(
                    payload.get("emergency_terminal_state")
                    or "EMERGENCY_CLOSED_RECONCILED"
                )
                self.store.update(
                    candidate_id,
                    terminal_state,
                    emergency_exit=exit_order,
                    emergency_exit_confirmed=True,
                    emergency_exit_status=exit_status,
                    emergency_exit_executed_qty=executed_quantity,
                    emergency_position_confirmed_flat=True,
                    protection_canceled_after_flat=canceled,
                    protection_cleanup_failures=[],
                    reconciliation_required=False,
                    emergency_reconciled_at=_utc_now(),
                )
                emergency_cleaned_algo_ids.update(canceled)
                continue
            if state == "PROTECTED":
                if symbol in positions:
                    required = {
                        str(payload.get("stop_order_id") or ""),
                        str(payload.get("tp_order_id") or ""),
                    }
                    if "" in required or not required.issubset(algo_by_id):
                        mismatches.append(f"PROTECTION_MISMATCH:{candidate_id}")
                    continue
                lifecycle = self._close_completed_lifecycle(row)
                if not lifecycle["exact"]:
                    mismatches.append(f"EXIT_NOT_EXACTLY_RECONCILED:{candidate_id}")

        for symbol in positions:
            if symbol not in local_by_symbol:
                mismatches.append(f"ORPHAN_EXCHANGE_POSITION:{symbol}")

        active_regular_order_ids = {
            str(value)
            for row in self.store.active()
            for value in (
                row["payload"].get("entry_order_id"),
            )
            if value
        }
        for order in open_orders:
            client_id = str(order.get("clientOrderId") or "")
            order_id = str(order.get("orderId") or "")
            symbol = str(order.get("symbol") or "")
            if client_id.startswith(CLIENT_PREFIX) and order_id not in active_regular_order_ids:
                self.client.cancel_order(symbol, order_id=order_id)
                cleaned_orphans.append(order_id)

        active_algo_ids = {
            str(value)
            for row in self.store.active()
            for value in (
                row["payload"].get("stop_order_id"),
                row["payload"].get("tp_order_id"),
            )
            if value
        }
        for order in open_algo_orders:
            client_id = str(order.get("clientAlgoId") or "")
            algo_id = _algo_order_id(order)
            if algo_id in emergency_cleaned_algo_ids or client_id in emergency_cleaned_algo_ids:
                continue
            if client_id.startswith(CLIENT_PREFIX) and algo_id not in active_algo_ids:
                self.client.cancel_algo_order(algo_id=algo_id or None, client_id=client_id)
                cleaned_orphans.append(f"algo:{algo_id or client_id}")

        self.reconciliation_ok = not mismatches
        result = {
            "ok": self.reconciliation_ok,
            "complete": True,
            "state": "RECONCILED" if self.reconciliation_ok else "MISMATCH_BLOCKED",
            "checked_at": _utc_now(),
            "exchange_open_positions": len(positions),
            "exchange_open_orders": len(open_orders) + len(open_algo_orders),
            "exchange_open_regular_orders": len(open_orders),
            "exchange_open_algo_orders": len(open_algo_orders),
            "local_active": len(self.store.active()),
            "mismatches": mismatches,
            "cleaned_orphan_orders": cleaned_orphans,
            "complete_snapshot_repeated_after_retry": recovered_during_snapshot,
            "retry": getattr(self.client, "reconciliation_retry_state", None),
            "retry_history": list(
                getattr(self.client, "reconciliation_retry_history", [])[-10:]
            ),
        }
        self.store.put_meta("last_reconciliation", result)
        self.store.event("RECONCILIATION", result)
        return result

    def _close_completed_lifecycle(self, row: dict) -> dict:
        candidate_id = row["candidate_id"]
        symbol = row["symbol"]
        payload = row["payload"]
        stop_order = self.client.query_algo_order(
            algo_id=payload.get("stop_order_id"),
            client_id=payload.get("stop_client_id"),
        )
        tp_order = self.client.query_algo_order(
            algo_id=payload.get("tp_order_id"),
            client_id=payload.get("tp_client_id"),
        )
        filled = [
            order for order in (stop_order, tp_order)
            if _algo_triggered(order)
        ]
        exact = len(filled) == 1
        for order in (stop_order, tp_order):
            if _algo_status(order) not in TERMINAL_ALGO_STATES:
                try:
                    self.client.cancel_algo_order(
                        algo_id=order.get("algoId"),
                        client_id=order.get("clientAlgoId"),
                    )
                except Exception:
                    exact = False
        entry_ms = int(
            datetime.fromisoformat(
                str(payload.get("entry_filled_at") or row["created_at"]).replace("Z", "+00:00")
            ).timestamp() * 1000
        )
        trades = self.client.user_trades(symbol, entry_ms)
        funding = self.client.funding_income(symbol, entry_ms)
        fees = sum(float(item.get("commission") or 0) for item in trades)
        realized = sum(float(item.get("realizedPnl") or 0) for item in trades)
        funding_total = sum(float(item.get("income") or 0) for item in funding)
        exit_algo = filled[0] if filled else {}
        exit_order = {}
        if exit_algo.get("actualOrderId"):
            try:
                exit_order = self.client.query_order(
                    symbol, order_id=exit_algo.get("actualOrderId")
                )
            except Exception:
                exact = False
        if not exit_order and exit_algo:
            exit_order = {
                **exit_algo,
                "orderId": exit_algo.get("actualOrderId"),
                "status": "FILLED" if _algo_triggered(exit_algo) else _algo_status(exit_algo),
                "avgPrice": exit_algo.get("actualPrice") or "0",
                "executedQty": exit_algo.get("actualQty") or "0",
            }
        result = self.store.update(
            candidate_id, "CLOSED_RECONCILED" if exact else "CLOSED_UNRESOLVED",
            exit_order=exit_order, exit_algo_order=exit_algo,
            exit_fill=float(exit_order.get("avgPrice") or 0),
            fees=fees, funding=funding_total, realized_pnl=realized,
            reconciliation_exact=exact, closed_at=_utc_now(),
        )
        self.store.event(
            "LIFECYCLE_COMPLETE",
            self._event_payload({
                "exact": exact, "exit_order": exit_order,
                "fees": fees, "funding": funding_total,
                "realized_pnl": realized,
            }),
            candidate_id,
        )
        if exact:
            self.store.put_meta("first_lifecycle_verified", True)
        return {"exact": exact, "row": result}
