"""Prospective, no-order Shadow Runtime engine."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker

from strategy.prospective_shadow_spec import EXPERIMENT_ID, FREEZE_ID, build_event_schema, build_freeze_spec

from .identity import runtime_identity
from .models import Candle, ExecutionQuote, Geometry, MarketSnapshot, PolicyDecision, utc_iso
from .policies import FrozenPolicy, default_policies, thesis_id
from .portfolio import PortfolioSimulator, terminal_costs
from .store import ShadowStore


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest().upper()


class ShadowRuntime:
    """Fan one immutable market snapshot into Control/A/B/C.

    The runtime has no exchange client and accepts only a prebuilt public market
    snapshot plus public exchange filters.  Its only mutation target is SQLite.
    """

    def __init__(
        self,
        *,
        root: str | Path,
        store: ShadowStore,
        policies: Iterable[FrozenPolicy] | None = None,
        initial_equity: float = 1_000.0,
    ):
        self.root = Path(root)
        self.store = store
        self.spec = build_freeze_spec(self.root)
        self.identity = runtime_identity(self.root)
        self.policy_specs = {row["policy_id"]: row for row in self.spec["policies"]}
        self.policies = list(policies) if policies is not None else default_policies()
        self.validator = Draft202012Validator(
            build_event_schema(self.root), format_checker=FormatChecker()
        )
        self.portfolio = PortfolioSimulator(store, initial_equity=initial_equity)
        self.store.put_meta("runtime_identity", self.identity)
        self.store.put_meta("freeze_hash", self.spec["freeze_hash"])

    def process_snapshot(
        self,
        snapshot: MarketSnapshot,
        *,
        exchange_filter: dict[str, float] | None = None,
        execution_provider=None,
    ) -> list[dict]:
        """Process one immutable object through every frozen policy exactly once."""
        emitted: list[dict] = []
        snapshot_payload = self._snapshot_payload(snapshot)
        self.store.insert_snapshot(
            snapshot.snapshot_id, snapshot.symbol, utc_iso(snapshot.as_of),
            snapshot_payload, canonical_hash(snapshot_payload),
        )
        self.store.insert_candidate(
            snapshot.candidate.candidate_id, snapshot.symbol,
            utc_iso(snapshot.candidate.emitted_at), dict(snapshot.candidate.event),
        )
        exchange_filter = exchange_filter or {
            "tick_size": 0.00000001, "step_size": 0.00000001,
            "min_qty": 0.0, "min_notional": 0.0,
        }

        for policy in self.policies:
            tid = thesis_id(policy.policy_id, snapshot)
            existing_position = self.store.position_for_thesis(policy.policy_id, tid)
            if existing_position:
                continue

            state = self.store.thesis_for_candidate(policy.policy_id, snapshot.candidate.candidate_id)
            if state and state.get("_last_processed_snapshot_id") == snapshot.snapshot_id:
                continue
            if state and not self._needs_evaluation(policy.policy_id, str(state.get("status") or "")):
                continue
            candidate_event = self._candidate_event(policy.policy_id, snapshot, tid)
            if self._append(candidate_event, f"{policy.policy_id}:{tid}:CANDIDATE"):
                emitted.append(candidate_event)

            decision = policy.evaluate(snapshot, state)
            if (
                policy.policy_id == "ACTIVE_ALTCOIN_LONG_V1"
                and decision.status == "WAITING_NEXT_1M_OPEN"
                and not decision.metadata.get("wait_registered_at")
            ):
                # Persist the wall-clock instant at which A was actually armed.
                # A boundary event captured after this instant remains causal
                # even when the next strategy scan materializes the fill later.
                decision.metadata["wait_registered_at"] = utc_iso(
                    datetime.now(timezone.utc)
                )
            execution_quote = None
            if decision.stage == "ENTRY_READY":
                decision, execution_quote = self._resolve_entry(
                    policy, snapshot, decision, execution_provider
                )
            decision.metadata["_last_processed_snapshot_id"] = snapshot.snapshot_id
            self.store.upsert_thesis(
                policy.policy_id, decision.thesis_id, decision.candidate_id,
                snapshot.symbol, decision.status, decision.metadata,
                utc_iso(snapshot.as_of),
            )
            event = self._decision_event(snapshot, decision)
            transition_key = (
                f"{policy.policy_id}:{tid}:{decision.stage}:{decision.state_before}:"
                f"{decision.state_after}:{snapshot.closed_candle_ids}"
            )
            if self._append(event, transition_key):
                emitted.append(event)

            if decision.stage != "ENTRY_READY" or decision.geometry is None or execution_quote is None:
                continue
            simulation, costs, position, fill_status = self.portfolio.simulate_fill(
                policy_id=policy.policy_id, thesis_id=decision.thesis_id,
                snapshot=snapshot, geometry=decision.geometry,
                execution_quote=execution_quote,
                exchange_filter=exchange_filter,
            )
            if position is None or simulation is None:
                rejected = deepcopy(event)
                rejected["event_id"] = self._event_id(policy.policy_id, tid, "FILL_REJECT", fill_status)
                rejected["stage"] = "THESIS_STATE"
                rejected["status"] = fill_status
                rejected["decision"]["state_after"] = fill_status
                rejected["decision"]["reason_codes"] = [fill_status]
                rejected.pop("entry_ready_at", None)
                rejected.pop("geometry", None)
                if self._append(rejected, f"{policy.policy_id}:{tid}:FILL_REJECT:{fill_status}"):
                    emitted.append(rejected)
                continue
            fill_event = deepcopy(event)
            fill_event.update({
                "event_id": self._event_id(policy.policy_id, tid, "SIMULATED_FILL", position["position_id"]),
                "stage": "SIMULATED_FILL",
                "status": "OPEN",
                "portfolio_simulation": simulation,
                "execution_costs": costs,
            })
            position["event_context"] = {
                "market_snapshot_id": event["market_snapshot_id"],
                "candidate_id": event["candidate_id"],
                "candidate_emitted_at": event["candidate_emitted_at"],
                "market": event["market"],
                "entry_ready_at": event["entry_ready_at"],
                "level_map": event.get("level_map"),
            }
            self.store.upsert_position(position)
            if self._append(fill_event, f"{policy.policy_id}:{tid}:SIMULATED_FILL"):
                emitted.append(fill_event)
            if (
                policy.policy_id == "ACTIVE_ALTCOIN_LONG_V1"
                and execution_provider is not None
                and hasattr(execution_provider, "quotes_since")
            ):
                # A can be filled from a prospectively captured boundary event
                # on the next scan. Replay the already persisted ordered quotes
                # from the real fill boundary so intervening TP/SL/MFE/MAE is not
                # lost. Store/event dedupe keeps this restart-idempotent.
                for replay_quote in execution_provider.quotes_since(
                    snapshot.symbol, decision.geometry.executable_quote_time
                ):
                    emitted.extend(self.process_execution_quote(replay_quote))
        return emitted

    def _resolve_entry(self, policy, snapshot, decision, provider):
        """Bind a readiness decision to causal execution observations."""
        ready_at = decision.entry_ready_at
        if provider is None or ready_at is None:
            return self._entry_unavailable(decision, "DATA_UNAVAILABLE_NO_EXECUTION_STREAM"), None
        resolution_started = datetime.now(timezone.utc)
        candle_open = None
        if policy.policy_id == "ACTIVE_ALTCOIN_LONG_V1":
            candle_open = provider.wait_candle_open(
                snapshot.symbol, ready_at, timeout_seconds=5.0
            )
            quote = provider.first_quote_near(snapshot.symbol, ready_at, window_seconds=2.0)
            registered_raw = decision.metadata.get("wait_registered_at")
            registered_at = (
                datetime.fromisoformat(str(registered_raw).replace("Z", "+00:00"))
                if registered_raw
                else None
            )
            causal = bool(
                registered_at and candle_open and quote
                and candle_open.open_time == ready_at
                and 0 <= (candle_open.received_at - ready_at).total_seconds() <= 2.0
                and candle_open.received_at >= registered_at
                and ready_at <= quote.event_time <= ready_at + timedelta(seconds=2)
                and 0 <= (quote.received_at - quote.event_time).total_seconds() <= 2.0
                and quote.received_at >= registered_at
            )
            if not causal:
                return self._entry_unavailable(decision, "DATA_UNAVAILABLE_NO_PROSPECTIVE_1M_OPEN"), None
        else:
            quote_not_before = max(ready_at, resolution_started)
            quote = provider.wait_fresh_quote(snapshot.symbol, quote_not_before, timeout_seconds=5.0)
            waited_ms = (datetime.now(timezone.utc) - resolution_started).total_seconds() * 1000.0
            quote_is_causal = bool(
                quote is not None
                and quote.event_time >= quote_not_before
                and 0 <= (quote.received_at - quote.event_time).total_seconds() <= 2.0
                and waited_ms <= 5_050.0
            )
            if not quote_is_causal:
                return self._entry_unavailable(decision, "DATA_UNAVAILABLE_NO_FRESH_QUOTE_WITHIN_5S"), None
            decision.metadata["quote_wait_ms"] = waited_ms
        decision.metadata["execution_resolution_started_at"] = utc_iso(resolution_started)
        geometry, reason = policy.resolve_geometry(
            snapshot, decision, execution_quote=quote, candle_open=candle_open
        )
        if geometry is None:
            return self._entry_unavailable(decision, reason), None
        decision.geometry = geometry
        self.store.insert_execution_quote(quote)
        if candle_open is not None:
            self.store.insert_candle_open(candle_open)
        decision.metadata.update({
            "execution_quote_event_id": quote.event_id,
            "execution_quote_observed_at": utc_iso(quote.event_time),
            "execution_quote_received_at": utc_iso(quote.received_at),
            "quote_age_ms_at_receipt": max(
                0.0, (quote.received_at - quote.event_time).total_seconds() * 1000.0
            ),
            "execution_source": quote.source,
        })
        if candle_open:
            decision.metadata["candle_open_event_id"] = candle_open.event_id
            decision.metadata["candle_open_received_at"] = utc_iso(candle_open.received_at)
        return decision, quote

    @staticmethod
    def _entry_unavailable(decision: PolicyDecision, reason: str) -> PolicyDecision:
        decision.stage = "THESIS_STATE"
        decision.status = reason
        decision.state_after = reason
        decision.reason_codes = [reason]
        decision.entry_ready_at = None
        decision.geometry = None
        decision.metadata["status"] = reason
        return decision

    def process_execution_quote(self, quote: ExecutionQuote) -> list[dict]:
        """Advance every open position from ordered quote events, between scans."""
        self.store.insert_execution_quote(quote)
        emitted = []
        for position in self.store.open_positions():
            if position["symbol"] != quote.symbol:
                continue
            updated, outcome = self.portfolio.update_position_quote(position, quote)
            self.store.upsert_position(updated)
            if outcome:
                event = self._terminal_event_from_quote(updated, outcome, quote)
                key = f"{updated['policy_id']}:{updated['thesis_id']}:TERMINAL:{outcome['terminal_status']}"
                if self._append(event, key):
                    emitted.append(event)
        return emitted

    def reconcile_closed_bar(
        self, symbol: str, candle: Candle, *, ordered_quote_coverage: bool,
    ) -> list[dict]:
        emitted = []
        for position in self.store.open_positions():
            if position["symbol"] != symbol:
                continue
            updated, outcome = self.portfolio.reconcile_closed_bar(
                position, candle, ordered_quote_coverage=ordered_quote_coverage
            )
            self.store.upsert_position(updated)
            if outcome:
                quote = ExecutionQuote(
                    symbol=symbol, event_time=candle.close_time, received_at=candle.close_time,
                    best_bid=float(updated["exit_price"]) / 0.9999,
                    best_ask=float(updated["exit_price"]) / 0.9999 * 1.00001,
                    update_id=int(candle.close_time.timestamp() * 1000),
                    source="CLOSED_1M_GAP_RECONCILIATION",
                )
                event = self._terminal_event_from_quote(updated, outcome, quote)
                key = f"{updated['policy_id']}:{updated['thesis_id']}:TERMINAL:{outcome['terminal_status']}"
                if self._append(event, key):
                    emitted.append(event)
        return emitted

    def candidate_complete(self, candidate_id: str) -> bool:
        rows = self.store.theses_for_candidate(candidate_id)
        if len(rows) != len(self.policies):
            return False
        for state in rows:
            policy_id = str(state.get("policy_id") or "")
            status = str(state.get("status") or "")
            position = self.store.position_for_thesis(policy_id, str(state.get("thesis_id") or ""))
            if position and position.get("status") == "OPEN":
                return False
            if not position and self._needs_evaluation(policy_id, status):
                return False
        return True

    def _append(self, event: dict, dedupe_key: str) -> bool:
        errors = sorted(self.validator.iter_errors(event), key=lambda item: list(item.path))
        if errors:
            rendered = "; ".join(f"{'.'.join(map(str, error.path))}: {error.message}" for error in errors[:8])
            raise ValueError(f"shadow event schema violation: {rendered}")
        return self.store.append_event(event, canonical_hash(dedupe_key))

    def _candidate_event(self, policy_id: str, snapshot: MarketSnapshot, tid: str) -> dict:
        return self._base_event(
            policy_id, snapshot, tid,
            event_id=self._event_id(policy_id, tid, "CANDIDATE", snapshot.candidate.candidate_id),
            stage="CANDIDATE_EMITTED", status="SEEN",
            decision={
                "side": snapshot.candidate.side,
                "state_before": None,
                "state_after": "CANDIDATE_EMITTED",
                "reason_codes": ["SHARED_ACTIVE_ALTCOIN_CANDIDATE"],
            },
        )

    def _decision_event(self, snapshot: MarketSnapshot, decision: PolicyDecision) -> dict:
        event = self._base_event(
            decision.policy_id, snapshot, decision.thesis_id,
            event_id=self._event_id(
                decision.policy_id, decision.thesis_id, decision.stage,
                f"{decision.state_before}>{decision.state_after}:{snapshot.snapshot_id}",
            ),
            stage=decision.stage, status=decision.status,
            decision={
                "side": snapshot.candidate.side,
                "state_before": decision.state_before,
                "state_after": decision.state_after,
                "reason_codes": list(decision.reason_codes),
            },
        )
        event["level_map"] = decision.level_map
        if decision.entry_ready_at:
            event["entry_ready_at"] = utc_iso(decision.entry_ready_at)
        if decision.geometry:
            event["geometry"] = decision.geometry.as_dict()
        evidence_keys = (
            "execution_quote_event_id", "execution_quote_observed_at",
            "execution_quote_received_at", "quote_age_ms_at_receipt", "quote_wait_ms",
            "execution_resolution_started_at", "execution_source",
            "candle_open_event_id", "candle_open_received_at",
        )
        evidence = {key: decision.metadata[key] for key in evidence_keys if key in decision.metadata}
        if evidence:
            event["runtime_evidence"] = evidence
        return event

    def _terminal_event_from_quote(self, position: dict, outcome: dict, quote: ExecutionQuote) -> dict:
        policy_id = str(position["policy_id"])
        tid = str(position["thesis_id"])
        context = position["event_context"]
        policy = self.policy_specs[policy_id]
        market = deepcopy(context["market"])
        market.update({
            "timestamp_utc": utc_iso(quote.event_time), "best_bid": quote.best_bid,
            "best_ask": quote.best_ask, "spread": quote.best_ask - quote.best_bid,
            "spread_bps": quote.spread_bps,
        })
        event = {
            "schema_version": 3, "experiment_id": EXPERIMENT_ID, "freeze_id": FREEZE_ID,
            "event_id": self._event_id(policy_id, tid, "TERMINAL", outcome["terminal_status"]),
            "emitted_at_utc": utc_iso(quote.event_time),
            "identity": {
                "policy_id": policy_id, "policy_version": policy["policy_version"],
                "policy_freeze_commit": self.identity["policy_freeze_commit"],
                "runtime_commit": self.identity["runtime_commit"],
                "runtime_build_hash": self.identity["runtime_build_hash"],
                "freeze_hash": self.spec["freeze_hash"],
                "policy_config_hash": policy["config_hash"],
            },
            "market_snapshot_id": context["market_snapshot_id"],
            "candidate_id": context["candidate_id"],
            "candidate_emitted_at": context["candidate_emitted_at"],
            "thesis_id": tid, "stage": "TERMINAL_OUTCOME",
            "status": f"TERMINAL_{outcome['terminal_status']}", "market": market,
            "decision": {
                "side": "LONG", "state_before": "OPEN",
                "state_after": outcome["terminal_status"],
                "reason_codes": [outcome["terminal_reason"]],
            },
        }
        event.update({
            "entry_ready_at": context["entry_ready_at"],
            "geometry": position["geometry"],
            "portfolio_simulation": position["portfolio_simulation"],
            "execution_costs": terminal_costs(position["execution_costs"], quote),
            "outcome": outcome,
            "level_map": context.get("level_map"),
        })
        return event

    def _base_event(
        self, policy_id: str, snapshot: MarketSnapshot, tid: str, *,
        event_id: str, stage: str, status: str, decision: dict,
    ) -> dict:
        policy = self.policy_specs[policy_id]
        quote = snapshot.quote
        return {
            "schema_version": 3,
            "experiment_id": EXPERIMENT_ID,
            "freeze_id": FREEZE_ID,
            "event_id": event_id,
            "emitted_at_utc": utc_iso(snapshot.as_of),
            "identity": {
                "policy_id": policy_id,
                "policy_version": policy["policy_version"],
                "policy_freeze_commit": self.identity["policy_freeze_commit"],
                "runtime_commit": self.identity["runtime_commit"],
                "runtime_build_hash": self.identity["runtime_build_hash"],
                "freeze_hash": self.spec["freeze_hash"],
                "policy_config_hash": policy["config_hash"],
            },
            "market_snapshot_id": snapshot.snapshot_id,
            "candidate_id": snapshot.candidate.candidate_id,
            "candidate_emitted_at": utc_iso(snapshot.candidate.emitted_at),
            "thesis_id": tid,
            "stage": stage,
            "status": status,
            "market": {
                "symbol": snapshot.symbol,
                "timestamp_utc": utc_iso(snapshot.as_of),
                "best_bid": quote.best_bid,
                "best_ask": quote.best_ask,
                "spread": quote.spread,
                "spread_bps": quote.spread_bps,
                "mark_price": quote.mark_price,
                "index_price": quote.index_price,
                "funding_rate": quote.funding_rate,
                "next_funding_time": utc_iso(quote.next_funding_time) if quote.next_funding_time else None,
                "closed_candle_ids": {
                    timeframe: snapshot.closed_candle_ids.get(timeframe)
                    for timeframe in ("1w", "1d", "4h", "1h", "15m", "5m", "1m")
                },
            },
            "decision": decision,
        }

    @staticmethod
    def _event_id(policy_id: str, thesis: str, stage: str, discriminator: str) -> str:
        return "shadow_" + hashlib.sha256(
            f"{policy_id}|{thesis}|{stage}|{discriminator}".encode()
        ).hexdigest()[:28]

    @staticmethod
    def _snapshot_payload(snapshot: MarketSnapshot) -> dict:
        return {
            "snapshot_id": snapshot.snapshot_id,
            "source": snapshot.source,
            "symbol": snapshot.symbol,
            "as_of": utc_iso(snapshot.as_of),
            "candidate_id": snapshot.candidate.candidate_id,
            "quote": {
                "observed_at": utc_iso(snapshot.quote.observed_at),
                "best_bid": snapshot.quote.best_bid,
                "best_ask": snapshot.quote.best_ask,
                "mark_price": snapshot.quote.mark_price,
                "index_price": snapshot.quote.index_price,
                "funding_rate": snapshot.quote.funding_rate,
                "next_funding_time": utc_iso(snapshot.quote.next_funding_time) if snapshot.quote.next_funding_time else None,
            },
            "closed_candle_ids": snapshot.closed_candle_ids,
        }

    @staticmethod
    def _needs_evaluation(policy_id: str, status: str) -> bool:
        if policy_id == "CONTROL_ON_SHARED_ACTIVE_CANDIDATES_V1":
            return status == "PENDING_CONFIRMATION"
        if policy_id == "MOMENTUM_REPRICED_V1":
            return False
        if policy_id == "ACTIVE_ALTCOIN_LONG_V1":
            return status in {
                "WAITING_PULLBACK", "SWEEP_PENDING", "RECLAIM_CONFIRMED",
                "WAITING_NEXT_1M_OPEN",
            }
        if policy_id == "TOP_DOWN_V1":
            return status in {
                "WATCHLIST", "WAIT_FOR_PULLBACK", "AT_ZONE", "WAIT_FOR_TRIGGER",
                "TOO_EXTENDED", "BREAKOUT_WAIT_RETEST",
            }
        return False
