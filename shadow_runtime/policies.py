"""Frozen prospective policy adapters.

All adapters consume the same immutable :class:`MarketSnapshot`.  They return
decisions only; none can communicate with an exchange or an account.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any

import pandas as pd

from engines.ta_engine import TAEngine
from strategy.active_altcoin_shadow import advance_setup, new_setup
from strategy.entry_confirmation import evaluate_entry_confirmation
from strategy.filter_profiles import FILTER_PROFILES
from strategy.risk_manager import RiskManager

from .features import (
    active_altcoin_geometry,
    build_level_map,
    ema,
    shared_repriced_geometry,
    top_down_observation,
)
from .models import CandleOpenEvent, ExecutionQuote, Geometry, MarketSnapshot, PolicyDecision, utc_iso


POLICY_IDS = (
    "CONTROL_ON_SHARED_ACTIVE_CANDIDATES_V1",
    "ACTIVE_ALTCOIN_LONG_V1",
    "MOMENTUM_REPRICED_V1",
    "TOP_DOWN_V1",
)


def stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"


def thesis_id(policy_id: str, snapshot: MarketSnapshot) -> str:
    return stable_id("thesis", {"policy": policy_id, "candidate": snapshot.candidate.candidate_id})


def _frame(snapshot: MarketSnapshot, timeframe: str, *, append_live: bool = False) -> pd.DataFrame:
    rows = [{
        "timestamp": row.open_time,
        "open": row.open, "high": row.high, "low": row.low,
        "close": row.close, "volume": row.volume,
        "open_time": row.open_time, "close_time": row.close_time,
        "quote_volume": row.quote_volume,
        "taker_buy_quote_volume": row.taker_buy_quote_volume,
    } for row in snapshot.closed(timeframe)]
    if append_live and rows:
        quote = snapshot.quote
        rows.append({
            "timestamp": snapshot.as_of,
            "open": rows[-1]["close"],
            "high": max(quote.best_ask, rows[-1]["close"]),
            "low": min(quote.best_bid, rows[-1]["close"]),
            "close": quote.mark_price,
            "volume": 0.0,
            "open_time": snapshot.as_of,
            "close_time": snapshot.as_of,
            "quote_volume": 0.0,
            "taker_buy_quote_volume": 0.0,
        })
    return pd.DataFrame(rows)


class FrozenPolicy:
    policy_id = ""

    def evaluate(self, snapshot: MarketSnapshot, state: dict | None = None) -> PolicyDecision:
        raise NotImplementedError

    def resolve_geometry(
        self,
        snapshot: MarketSnapshot,
        decision: PolicyDecision,
        *,
        execution_quote: ExecutionQuote | None = None,
        candle_open: CandleOpenEvent | None = None,
    ) -> tuple[Geometry | None, str]:
        return decision.geometry, "ENTRY_GEOMETRY_VALID" if decision.geometry else "NO_GEOMETRY_RESOLVER"

    def _decision(
        self, snapshot: MarketSnapshot, *, stage: str, status: str,
        state_before: str | None, state_after: str | None, reasons: list[str],
        entry_ready_at: datetime | None = None, geometry: Geometry | None = None,
        level_map: dict | None = None, metadata: dict | None = None,
    ) -> PolicyDecision:
        return PolicyDecision(
            policy_id=self.policy_id,
            candidate_id=snapshot.candidate.candidate_id,
            thesis_id=thesis_id(self.policy_id, snapshot),
            stage=stage,
            status=status,
            state_before=state_before,
            state_after=state_after,
            reason_codes=reasons,
            entry_ready_at=entry_ready_at,
            geometry=geometry,
            level_map=level_map,
            metadata=metadata or {},
        )


class ControlOnSharedActiveCandidatesPolicy(FrozenPolicy):
    """Read-only adapter over the source-locked TA/Risk engines.

    This intentionally stops before the live 5m lifecycle/order boundary.  A
    candidate can become ENTRY_READY only when the frozen production analysis,
    quality gate, and SMC liquidity plan all pass on the same snapshot.
    """

    policy_id = "CONTROL_ON_SHARED_ACTIVE_CANDIDATES_V1"

    def __init__(self) -> None:
        self.ta = TAEngine()
        self.risk = RiskManager()
        self.profile = dict(FILTER_PROFILES["smc_strict"])
        self.profile["key"] = "smc_strict"

    def evaluate(self, snapshot: MarketSnapshot, state: dict | None = None) -> PolicyDecision:
        if state and state.get("status") in {"ENTRY_READY", "REJECTED"}:
            return self._decision(
                snapshot, stage="THESIS_STATE", status=state["status"],
                state_before=state["status"], state_after=state["status"],
                reasons=["IDEMPOTENT_TERMINAL_STATE"], metadata=state,
            )
        if state and state.get("status") == "PENDING_CONFIRMATION":
            created = pd.Timestamp(state.get("pending_created_at"))
            if created.tzinfo is None:
                created = created.tz_localize("UTC")
            if snapshot.as_of > (created + pd.Timedelta(minutes=30)).to_pydatetime():
                return self._decision(
                    snapshot, stage="THESIS_STATE", status="EXPIRED",
                    state_before="PENDING_CONFIRMATION", state_after="EXPIRED",
                    reasons=["CONTROL_STRICT_VIRTUAL_ORDER_30M_EXPIRED"],
                    metadata={"status": "EXPIRED", "pending_created_at": state.get("pending_created_at")},
                )
        frame = _frame(snapshot, "15m", append_live=True)
        hourly = snapshot.closed("1h")
        if len(frame) < 201 or len(hourly) < 200:
            return self._decision(
                snapshot, stage="THESIS_STATE", status="DATA_UNAVAILABLE",
                state_before=None, state_after="DATA_UNAVAILABLE",
                reasons=["CONTROL_REQUIRES_200_CLOSED_BARS"],
                metadata={"status": "DATA_UNAVAILABLE"},
            )
        hour_closes = [row.close for row in hourly]
        ema50 = ema(hour_closes, 50)[-1]
        ema200 = ema(hour_closes, 200)[-1]
        htf = "bullish" if ema50 > ema200 else "bearish" if ema50 < ema200 else "neutral"
        enriched = self.ta.add_indicators(frame.copy(), filter_profile=self.profile)
        details: dict[str, Any] = {}
        direction = self.ta.evaluate_trend(
            enriched, symbol=snapshot.symbol, details=details,
            htf_trend=htf, ema_mode="dual", filter_profile=self.profile,
        )
        if direction != "buy":
            return self._decision(
                snapshot, stage="THESIS_STATE", status="REJECTED",
                state_before=None, state_after="REJECTED",
                reasons=["CONTROL_NO_LONG_SIGNAL", str(details.get("reason") or "NEUTRAL")],
                metadata={"status": "REJECTED", "control_details": _json_safe(details)},
            )
        quality_audit: dict[str, Any] = {}
        passed, reason = self.ta.validate_entry_quality(
            enriched, "buy", current_price=snapshot.quote.best_ask,
            filter_profile=self.profile, audit_details=quality_audit,
        )
        if not passed:
            return self._decision(
                snapshot, stage="THESIS_STATE", status="REJECTED",
                state_before=None, state_after="REJECTED",
                reasons=["CONTROL_ENTRY_QUALITY_REJECT", str(reason)],
                metadata={"status": "REJECTED", "quality_audit": _json_safe(quality_audit)},
            )
        optimal_entry = float(details.get("optimal_entry") or snapshot.quote.best_ask)
        invalidation = float(details.get("invalidation_level") or 0)
        atr15 = float(enriched.iloc[-2].get("atr") or 0)
        stop, target = self.risk.calculate_sl_tp(
            "buy", optimal_entry, atr=atr15,
            invalidation_level=invalidation,
            market_levels=details.get("market_levels") or {},
            filter_profile=self.profile,
        )
        plan = dict(self.risk.last_plan or {})
        if target is None or self.risk.last_block_reason:
            return self._decision(
                snapshot, stage="THESIS_STATE", status="REJECTED",
                state_before=None, state_after="REJECTED",
                reasons=["CONTROL_RISK_PLAN_REJECT", str(self.risk.last_block_reason or plan.get("reason") or "INVALID_PLAN")],
                metadata={"status": "REJECTED", "risk_plan": _json_safe(plan)},
            )
        planned_risk = optimal_entry - float(stop)
        planned_rr = (float(target) - optimal_entry) / planned_risk if planned_risk > 0 else 0.0
        confirmation = evaluate_entry_confirmation(
            side="buy",
            trigger_bars=_frame(snapshot, "5m", append_live=True),
            current_price=snapshot.quote.best_ask,
            optimal_entry=optimal_entry,
            invalidation_level=invalidation,
            atr_15m=atr15,
            ta_context=details,
            filter_profile=self.profile,
            planned_rr=planned_rr,
            zone_touch_recorded=bool((state or {}).get("zone_touch_recorded")),
            now=snapshot.as_of,
        )
        evidence = confirmation.get("evidence") or {}
        zone_touch = bool(
            (state or {}).get("zone_touch_recorded")
            or (evidence.get("candle_touched_zone") and evidence.get("current_in_zone"))
        )
        pending_created = (state or {}).get("pending_created_at") or utc_iso(snapshot.as_of)
        if not confirmation.get("passed"):
            return self._decision(
                snapshot, stage="THESIS_STATE", status="PENDING_CONFIRMATION",
                state_before=(state or {}).get("status"), state_after="PENDING_CONFIRMATION",
                reasons=["CONTROL_WAIT_CLOSED_5M_CONFIRMATION", str(confirmation.get("reason") or "WAIT")],
                metadata={
                    "status": "PENDING_CONFIRMATION",
                    "pending_created_at": pending_created,
                    "zone_touch_recorded": zone_touch,
                    "control_details": _json_safe(details),
                    "risk_plan": _json_safe(plan),
                    "entry_confirmation": _json_safe(confirmation),
                },
            )
        return self._decision(
            snapshot, stage="ENTRY_READY", status="ENTRY_READY",
            state_before=None, state_after="ENTRY_READY", reasons=["CONTROL_ALL_FROZEN_GATES_PASS"],
            entry_ready_at=snapshot.as_of, geometry=None,
            level_map=_json_safe(details.get("market_levels") or {}),
            metadata={
                "status": "ENTRY_READY", "control_details": _json_safe(details),
                "entry_confirmation": _json_safe(confirmation),
                "risk_plan": _json_safe(plan), "optimal_entry": optimal_entry,
                "atr15": atr15, "invalidation": invalidation,
            },
        )

    def resolve_geometry(self, snapshot, decision, *, execution_quote=None, candle_open=None):
        if execution_quote is None:
            return None, "WAITING_FRESH_EXECUTION_QUOTE"
        entry = execution_quote.best_ask * 1.0001
        meta = decision.metadata
        stop, target = self.risk.calculate_sl_tp(
            "buy", entry, atr=float(meta["atr15"]),
            invalidation_level=float(meta["invalidation"]),
            market_levels=decision.level_map or {}, filter_profile=self.profile,
        )
        refreshed = dict(self.risk.last_plan or {})
        if target is None or self.risk.last_block_reason or not 0 < float(stop) < entry < float(target):
            return None, "CONTROL_EXECUTABLE_GEOMETRY_REVALIDATION_REJECT"
        rr = (float(target) - entry) / (entry - float(stop))
        return Geometry(
            executable_quote_time=execution_quote.event_time, executable_entry=entry,
            entry_quote_side="BEST_ASK", entry_price_source="CONTROL_BEST_ASK_PLUS_1_BPS",
            old_reference_entry=float(meta.get("optimal_entry") or 0) or None,
            stop=float(stop), stop_source="CONTROL_REVALIDATED_SMC_INVALIDATION",
            stop_level_id="CONTROL_SOURCE_LOCKED_INVALIDATION",
            target=float(target), target_source="CONTROL_REVALIDATED_FIRST_OPPOSING_LIQUIDITY",
            target_level_id=str(refreshed.get("target_type") or "CONTROL_TARGET"),
            planned_rr=rr, actual_rr_at_entry=rr,
            geometry_change_reason="CONTROL_SHARED_CANDIDATE_ADAPTER_FRESH_QUOTE",
        ), "ENTRY_GEOMETRY_VALID"


class ActiveAltcoinLongPolicy(FrozenPolicy):
    policy_id = "ACTIVE_ALTCOIN_LONG_V1"

    def evaluate(self, snapshot: MarketSnapshot, state: dict | None = None) -> PolicyDecision:
        if state and state.get("status") == "WAITING_NEXT_1M_OPEN":
            ready = pd.Timestamp(state["scheduled_fill_at"])
            if ready.tzinfo is None:
                ready = ready.tz_localize("UTC")
            if snapshot.as_of < ready.to_pydatetime():
                return self._decision(
                    snapshot, stage="THESIS_STATE", status="WAITING_NEXT_1M_OPEN",
                    state_before="WAITING_NEXT_1M_OPEN", state_after="WAITING_NEXT_1M_OPEN",
                    reasons=["A_WAITING_FOR_PROSPECTIVE_OPEN_EVENT"], metadata=state,
                )
            return self._decision(
                snapshot, stage="ENTRY_READY", status="ENTRY_READY",
                state_before="WAITING_NEXT_1M_OPEN", state_after="ENTRY_READY",
                reasons=["A_PROSPECTIVE_OPEN_DUE"], entry_ready_at=ready.to_pydatetime(),
                geometry=None, metadata=state,
            )
        setup = dict((state or {}).get("setup") or new_setup(dict(snapshot.candidate.event)))
        before = str(setup.get("status") or "WAITING_PULLBACK")
        frame = _frame(snapshot, "1m")
        advanced = advance_setup(setup, frame, snapshot.as_of)
        after = str(advanced.get("status") or before)
        metadata = {"status": after, "setup": _json_safe(advanced)}
        if after != "RECLAIM_CONFIRMED":
            terminal = after in {"HARD_INVALIDATED", "UNCONFIRMED", "DATA_GAP"}
            return self._decision(
                snapshot, stage="THESIS_STATE", status=after,
                state_before=before, state_after=after,
                reasons=[after], metadata=metadata,
            )
        confirmation = pd.Timestamp(advanced["confirmation_close_time"])
        expected_open = confirmation.floor("min") + pd.Timedelta(minutes=1)
        metadata = {
            "status": "WAITING_NEXT_1M_OPEN", "setup": _json_safe(advanced),
            "scheduled_fill_at": expected_open.isoformat(),
        }
        return self._decision(
            snapshot, stage="THESIS_STATE", status="WAITING_NEXT_1M_OPEN",
            state_before=before, state_after="WAITING_NEXT_1M_OPEN",
            reasons=["A_RECLAIM_CONFIRMED_SCHEDULE_PROSPECTIVE_OPEN"], metadata=metadata,
        )

    def resolve_geometry(self, snapshot, decision, *, execution_quote=None, candle_open=None):
        return active_altcoin_geometry(snapshot, dict(decision.metadata.get("setup") or {}), candle_open)


class MomentumRepricedPolicy(FrozenPolicy):
    policy_id = "MOMENTUM_REPRICED_V1"

    def evaluate(self, snapshot: MarketSnapshot, state: dict | None = None) -> PolicyDecision:
        levels = build_level_map(snapshot)
        return self._decision(
            snapshot, stage="ENTRY_READY", status="ENTRY_READY",
            state_before=None, state_after="ENTRY_READY", reasons=["B_INITIAL_CANDIDATE_READY"],
            entry_ready_at=snapshot.candidate.emitted_at,
            geometry=None, level_map=levels, metadata={"status": "ENTRY_READY"},
        )

    def resolve_geometry(self, snapshot, decision, *, execution_quote=None, candle_open=None):
        return shared_repriced_geometry(snapshot, decision.level_map or {}, execution_quote)


class TopDownPolicy(FrozenPolicy):
    policy_id = "TOP_DOWN_V1"
    TERMINAL = {"ENTRY_READY", "INVALIDATED", "BIAS_LOST", "EXPIRED", "DATA_UNAVAILABLE"}

    def evaluate(self, snapshot: MarketSnapshot, state: dict | None = None) -> PolicyDecision:
        prior = dict(state or {})
        before = str(prior.get("status") or "WATCHLIST")
        current_4h_id = snapshot.closed("4h")[-1].candle_id if snapshot.closed("4h") else None
        prior_levels = prior.get("level_map") or {}
        levels = dict(
            build_level_map(snapshot)
            if not prior_levels or prior_levels.get("built_from_candle_id") != current_4h_id
            else prior_levels
        )
        observation = top_down_observation(snapshot, levels)
        after, reasons = self._transition(before, observation, prior, snapshot)
        metadata = {
            **prior,
            "status": after,
            "level_map": levels,
            "observation": _json_safe(observation),
            "candidate_event": dict(snapshot.candidate.event),
            "last_snapshot_id": snapshot.snapshot_id,
        }
        if observation["breakout_1h"] and before == "WAIT_FOR_PULLBACK":
            resistance = levels.get("near_resistance") or {}
            metadata.update({
                "breakout_level": resistance.get("price"),
                "breakout_level_id": resistance.get("id"),
                "breakout_detected_at": utc_iso(snapshot.closed("1h")[-1].close_time),
                "breakout_bars_seen": 0,
            })
        if before == "BREAKOUT_WAIT_RETEST":
            metadata["breakout_bars_seen"] = int(prior.get("breakout_bars_seen") or 0) + 1
        if after != "ENTRY_READY":
            return self._decision(
                snapshot, stage="THESIS_STATE", status=after,
                state_before=before, state_after=after, reasons=reasons,
                level_map=levels, metadata=metadata,
            )
        metadata["status"] = "ENTRY_READY"
        return self._decision(
            snapshot, stage="ENTRY_READY", status="ENTRY_READY",
            state_before=before, state_after="ENTRY_READY",
            reasons=["C_WAIT_FOR_TRIGGER_TO_ENTRY_READY"],
            entry_ready_at=snapshot.as_of, geometry=None,
            level_map=levels, metadata=metadata,
        )

    def resolve_geometry(self, snapshot, decision, *, execution_quote=None, candle_open=None):
        return shared_repriced_geometry(snapshot, decision.level_map or {}, execution_quote)

    @staticmethod
    def _transition(before: str, obs: dict, prior: dict, snapshot: MarketSnapshot) -> tuple[str, list[str]]:
        if obs["missing_timeframes"] or not obs["location_available"]:
            return "DATA_UNAVAILABLE", ["C_DATA_OR_CAUSAL_LOCATION_UNAVAILABLE"]
        if obs["closed_4h_below_invalidation"]:
            return "INVALIDATED", ["C_CLOSED_4H_BELOW_INVALIDATION"]
        if not obs["weekly_long"] or not obs["daily_long"]:
            return "BIAS_LOST", ["C_WEEKLY_OR_DAILY_LONG_BIAS_LOST"]
        if before == "BREAKOUT_WAIT_RETEST":
            breakout_level = prior.get("breakout_level")
            hourly = snapshot.closed("1h")
            tolerance = float(obs.get("zone_tolerance") or 0)
            if breakout_level and hourly and hourly[-1].low <= float(breakout_level) + tolerance and hourly[-1].close >= float(breakout_level):
                return "WAIT_FOR_TRIGGER", ["C_LATER_1H_RETEST_VALID"]
            if int(prior.get("breakout_bars_seen") or 0) >= 4:
                return "EXPIRED", ["C_RETEST_WINDOW_EXPIRED"]
            return before, ["C_WAITING_FOR_LATER_RETEST"]
        if obs["too_extended"]:
            return "TOO_EXTENDED", ["C_DISTANCE_GT_1_5_ATR4H"]
        if before == "TOO_EXTENDED" and obs["recovered_from_extension"] and obs["bias_structure_location_valid"]:
            return "WAIT_FOR_PULLBACK", ["C_EXTENSION_RECOVERED_LTE_1_25_ATR4H"]
        if not obs["bias_structure_location_valid"]:
            return "WATCHLIST", ["C_WAITING_BIAS_STRUCTURE_LOCATION"]
        if before == "WATCHLIST":
            return "WAIT_FOR_PULLBACK", ["C_TOP_DOWN_CONTEXT_VALID"]
        if before == "WAIT_FOR_PULLBACK" and obs["breakout_1h"]:
            return "BREAKOUT_WAIT_RETEST", ["C_NEW_1H_BREAKOUT_WAIT_RETEST"]
        if before == "WAIT_FOR_PULLBACK" and obs["pullback_at_zone"]:
            return "AT_ZONE", ["C_1H_PULLBACK_AT_ZONE"]
        if before == "AT_ZONE" and obs["pullback_at_zone"]:
            return "WAIT_FOR_TRIGGER", ["C_LOCATION_REMAINS_VALID"]
        if before == "AT_ZONE":
            return "WAIT_FOR_PULLBACK", ["C_LOCATION_NO_LONGER_WITHIN_ZONE"]
        if before == "WAIT_FOR_TRIGGER" and obs["trigger_15m"]:
            return "ENTRY_READY", ["C_15M_TRIGGER_AFTER_LOCATION"]
        if before == "WAIT_FOR_TRIGGER" and not obs["pullback_at_zone"]:
            return "WAIT_FOR_PULLBACK", ["C_LOCATION_NO_LONGER_VALID"]
        return before, ["C_STATE_UNCHANGED"]


def default_policies() -> list[FrozenPolicy]:
    return [
        ControlOnSharedActiveCandidatesPolicy(),
        ActiveAltcoinLongPolicy(),
        MomentumRepricedPolicy(),
        TopDownPolicy(),
    ]


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, float) and (pd.isna(value) or value in (float("inf"), float("-inf"))):
        return None
    return value
