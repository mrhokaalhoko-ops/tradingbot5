"""Prospective market-wide monitor for the frozen active-altcoin candidate.

The monitor uses Binance USD-M public candles and writes decision-audit shadow
events. It cannot create an order or call the production execution pipeline.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import os
import threading
import time

import pandas as pd
import requests

from core.logger import logger
from strategy.active_altcoin_shadow import (
    ActiveAltcoinConfig,
    advance_setup,
    build_shadow_plan,
    detect_active_event,
    evaluate_shadow_plan,
    new_setup,
)
from utils.decision_audit import audit_decision_event
from utils.hot_list_monitor import BINANCE_USDM_PUBLIC_URL, get_latest_hot_list_snapshot


_snapshot_lock = threading.Lock()
_latest_snapshot = {
    "mode": "ACTIVE_ALTCOIN_PROSPECTIVE_SHADOW",
    "execution_enabled": False,
    "status": "NOT_STARTED",
    "active_setups": 0,
    "active_plans": 0,
    "completed": [],
}


def _utc_now(now_ts=None):
    timestamp = time.time() if now_ts is None else float(now_ts)
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value):
    """Normalize persisted ISO timestamps before applying retention limits."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _bool_env(name, default=False):
    value = os.getenv(name)
    return bool(default) if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int_env(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def get_latest_active_altcoin_shadow_snapshot():
    with _snapshot_lock:
        return copy.deepcopy(_latest_snapshot)


def _publish_snapshot(snapshot):
    global _latest_snapshot
    with _snapshot_lock:
        _latest_snapshot = copy.deepcopy(snapshot)


class ActiveAltcoinShadowMonitor:
    """Causal prospective validation; deliberately has no execution handle."""

    STATE_KEY = "active_altcoin_prospective_shadow_state"

    def __init__(self, state_manager=None, session=None, *, now_fn=time.time):
        self.state_manager = state_manager
        self.session = session or requests.Session()
        self.now_fn = now_fn
        self.enabled = _bool_env("ACTIVE_ALTCOIN_SHADOW_ENABLED", True)
        self.interval_seconds = _bounded_int_env(
            "ACTIVE_ALTCOIN_SHADOW_INTERVAL_SECONDS", 60, 30, 300
        )
        self.candidate_limit = _bounded_int_env(
            "ACTIVE_ALTCOIN_SHADOW_CANDIDATE_LIMIT", 50, 2, 50
        )
        self._state = self._load_state()

    @staticmethod
    def _empty_state():
        return {
            "schema_version": 1,
            "setups": {},
            "plans": {},
            "seen": {},
            "cooldown_until": {},
            "bar_capacity": {},
            "completed": [],
        }

    def _load_state(self):
        if self.state_manager is None:
            return self._empty_state()
        saved = self.state_manager.get(self.STATE_KEY, {})
        if not isinstance(saved, dict) or saved.get("schema_version") != 1:
            return self._empty_state()
        state = self._empty_state()
        for key in ("setups", "plans", "seen", "cooldown_until", "bar_capacity"):
            if isinstance(saved.get(key), dict):
                state[key] = copy.deepcopy(saved[key])
        if isinstance(saved.get("completed"), list):
            state["completed"] = copy.deepcopy(saved["completed"][-200:])
        return state

    def _persist(self):
        cutoff = float(self.now_fn()) - 6 * 60 * 60
        self._state["seen"] = {
            key: value for key, value in self._state["seen"].items()
            if float(value or 0) >= cutoff
        }
        self._state["cooldown_until"] = {
            key: value for key, value in self._state["cooldown_until"].items()
            if float(value or 0) > float(self.now_fn())
        }
        self._state["bar_capacity"] = {
            key: value for key, value in self._state["bar_capacity"].items()
            if _timestamp(key).timestamp() >= cutoff
        }
        self._state["completed"] = self._state["completed"][-200:]
        if self.state_manager is None:
            return True
        return bool(self.state_manager.set(self.STATE_KEY, self._state))

    def _fetch_1m(self, symbol, *, start_time=None):
        params = {"symbol": str(symbol).upper(), "interval": "1m", "limit": 180}
        if start_time is not None:
            params["startTime"] = int(_timestamp(start_time).timestamp() * 1000)
        response = self.session.get(
            f"{BINANCE_USDM_PUBLIC_URL}/fapi/v1/klines",
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Binance USD-M kline payload is not a list")
        rows = []
        for item in payload:
            if not isinstance(item, list) or len(item) < 11:
                continue
            rows.append({
                "open_time": pd.to_datetime(int(item[0]), unit="ms", utc=True),
                "close_time": pd.to_datetime(int(item[6]), unit="ms", utc=True),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
                "quote_volume": float(item[7]),
                "trades": int(item[8]),
                "taker_buy_quote_volume": float(item[10]),
            })
        return pd.DataFrame(rows)

    def _audit(self, *, status, symbol=None, setup_id=None, plan_id=None, details=None, durable=False):
        identity = plan_id or setup_id or f"aas:{symbol}:{status}"
        return audit_decision_event(
            decision_id=identity,
            signal_id=identity,
            setup_id=setup_id,
            thesis_id=setup_id,
            virtual_order_id=plan_id,
            symbol=symbol,
            stage="ACTIVE_ALTCOIN_SHADOW",
            status=status,
            details={"execution_enabled": False, **(details or {})},
            durable=durable,
        )

    def _archive(self, record):
        self._state["completed"].append({
            **copy.deepcopy(record), "completed_at": _utc_now(self.now_fn())
        })

    def _record_outcome(self, symbol, plan, outcome):
        previous = plan.get("last_outcome")
        plan["last_outcome"] = outcome
        if outcome.get("terminal"):
            self._audit(
                status=f"OUTCOME_{outcome['status']}", symbol=symbol,
                setup_id=plan["setup_id"], plan_id=plan["plan_id"],
                details={"plan": plan, "outcome": outcome}, durable=True,
            )
            self._archive({"kind": "PLAN", "symbol": symbol, "plan": plan, "outcome": outcome})
            self._state["plans"].pop(symbol, None)
        elif outcome.get("status") == "DATA_GAP" and outcome != previous:
            self._audit(
                status="DATA_GAP", symbol=symbol, setup_id=plan["setup_id"],
                plan_id=plan["plan_id"], details={"outcome": outcome},
            )

    def _capacity_available(self, plan, outcome):
        # Fills may be reconstructed after a poll/restart. A position already
        # closed now still occupied a slot at the simulated entry timestamp.
        start = _timestamp(plan["filled_at"])
        end = (
            _timestamp(outcome["exited_at"]) if outcome.get("terminal")
            else pd.Timestamp.max.tz_localize("UTC")
        )
        records = [
            (p, p.get("last_outcome", {})) for p in self._state["plans"].values()
        ] + [
            (r["plan"], r["outcome"]) for r in self._state["completed"]
            if r.get("kind") == "PLAN"
        ]
        intervals = []
        for existing, result in records:
            left = _timestamp(existing["filled_at"])
            right = (
                _timestamp(result["exited_at"]) if result.get("terminal")
                else pd.Timestamp.max.tz_localize("UTC")
            )
            if left < end and right > start:
                if existing["symbol"] == plan["symbol"]:
                    return False
                intervals.append((left, right))
        points = [start] + [left for left, _ in intervals if start < left < end]
        return all(
            sum(left <= point < right for left, right in intervals)
            < ActiveAltcoinConfig().max_open_positions
            for point in points
        )

    def _process_existing(self, bars_by_symbol, now_iso):
        changed = False
        pending_plans = []
        # Resolve existing outcomes before admitting new fills; order by fill
        # time below, matching the historical portfolio's tie break.
        symbols = sorted(
            set(self._state["setups"]) | set(self._state["plans"]),
            key=lambda symbol: (symbol not in self._state["plans"], symbol),
        )
        for symbol in symbols:
            try:
                bars = bars_by_symbol.get(symbol)
                if bars is None:
                    if symbol in self._state["plans"]:
                        start_time = self._state["plans"][symbol]["filled_at"]
                    else:
                        setup = self._state["setups"][symbol]
                        start_time = (
                            _timestamp(setup["last_checked_close_time"]).floor("min")
                            + pd.Timedelta(minutes=1)
                        )
                    bars = self._fetch_1m(symbol, start_time=start_time)
                    bars_by_symbol[symbol] = bars
            except Exception as exc:
                self._audit(
                    status="DATA_UNAVAILABLE", symbol=symbol,
                    details={"error_type": type(exc).__name__, "message": str(exc)[:300]},
                )
                continue
            if symbol in self._state["plans"]:
                plan = self._state["plans"][symbol]
                self._record_outcome(symbol, plan, evaluate_shadow_plan(plan, bars, now_iso))
                changed = True
                continue

            setup = advance_setup(self._state["setups"][symbol], bars, now_iso)
            self._state["setups"][symbol] = setup
            changed = True
            if setup["status"] in {"HARD_INVALIDATED", "UNCONFIRMED"}:
                self._audit(
                    status=setup["status"], symbol=symbol, setup_id=setup["setup_id"],
                    details={"setup": setup}, durable=True,
                )
                self._archive({"kind": "SETUP", "symbol": symbol, "setup": setup})
                self._state["setups"].pop(symbol, None)
            elif setup["status"] == "RECLAIM_CONFIRMED":
                plan = build_shadow_plan(setup, bars, now=now_iso)
                if plan.get("valid"):
                    pending_plans.append((symbol, setup, plan, bars))
                elif plan.get("reason") != "WAITING_NEXT_1M_OPEN":
                    self._audit(
                        status="PLAN_REJECTED", symbol=symbol, setup_id=setup["setup_id"],
                        details={"setup": setup, "plan": plan}, durable=True,
                    )
                    self._archive({"kind": "PLAN_REJECTED", "symbol": symbol, "setup": setup, "plan": plan})
                    self._state["setups"].pop(symbol, None)

        for symbol, setup, plan, bars in sorted(
            pending_plans, key=lambda item: (_timestamp(item[2]["filled_at"]), item[0])
        ):
            outcome = evaluate_shadow_plan(plan, bars, now_iso)
            if not self._capacity_available(plan, outcome):
                plan.update(valid=False, reason="PORTFOLIO_CAP")
                self._audit(
                    status="PLAN_REJECTED", symbol=symbol, setup_id=setup["setup_id"],
                    details={"setup": setup, "plan": plan}, durable=True,
                )
                self._archive({"kind": "PLAN_REJECTED", "symbol": symbol, "setup": setup, "plan": plan})
            else:
                plan["event"] = copy.deepcopy(setup["event"])
                plan["observed_at"] = now_iso
                plan["observation_delay_seconds"] = max(
                    0.0, (_timestamp(now_iso) - _timestamp(plan["filled_at"])).total_seconds()
                )
                plan["last_outcome"] = {"terminal": False, "status": "OPEN"}
                self._state["plans"][symbol] = plan
                self._audit(
                    status="PLAN_QUALIFIED_SHADOW", symbol=symbol,
                    setup_id=setup["setup_id"], plan_id=plan["plan_id"],
                    details={"setup": setup, "plan": plan}, durable=True,
                )
                self._record_outcome(symbol, plan, outcome)
            self._state["setups"].pop(symbol, None)
        return changed

    def _candidate_events(self, candidates, bars_by_symbol, now_iso):
        events = []
        active_candidates = [
            candidate for candidate in candidates
            if {
                "ACTIVE_EVENT_PREFILTER", "ACTIVE_EVENT_WATCH"
            } & {str(tag).upper() for tag in candidate.get("tags", [])}
        ]
        for candidate in active_candidates[: self.candidate_limit]:
            tags = {str(tag).upper() for tag in candidate.get("tags", [])}
            if not ({"ACTIVE_EVENT_PREFILTER", "ACTIVE_EVENT_WATCH"} & tags):
                continue
            symbol = str(candidate.get("symbol") or "").upper()
            if not symbol or symbol in self._state["setups"] or symbol in self._state["plans"]:
                continue
            try:
                bars = bars_by_symbol.get(symbol)
                if bars is None:
                    bars = self._fetch_1m(symbol)
                    bars_by_symbol[symbol] = bars
                event = detect_active_event(symbol, bars, now_iso)
            except Exception as exc:
                self._audit(
                    status="DATA_UNAVAILABLE", symbol=symbol,
                    details={"error_type": type(exc).__name__, "message": str(exc)[:300]},
                )
                continue
            event_id = event.get("event_id")
            if not event_id or event_id in self._state["seen"]:
                continue
            event["hot_candidate"] = copy.deepcopy(candidate)
            events.append(event)
        return events

    def _admit_events(self, events, now_ts):
        changed = False
        by_close = {}
        for event in events:
            by_close.setdefault(event["close_time"], []).append(event)
        for close_time, same_bar in sorted(by_close.items()):
            ranked = sorted(same_bar, key=lambda row: row.get("event_score", 0), reverse=True)
            base_ranked = [event for event in ranked if event.get("base_qualified")]
            previously_admitted = list(self._state["bar_capacity"].get(close_time, []))
            remaining = max(0, 2 - len(previously_admitted))
            newly_admitted = [event["event_id"] for event in base_ranked[:remaining]]
            admitted_ids = set(newly_admitted)
            if newly_admitted:
                self._state["bar_capacity"][close_time] = (
                    previously_admitted + newly_admitted
                )
            for event in ranked:
                event_id, symbol = event["event_id"], event["symbol"]
                self._state["seen"][event_id] = now_ts
                changed = True
                if not event.get("base_qualified"):
                    self._audit(status="EVENT_REJECTED", symbol=symbol, details={"event": event})
                    continue
                if event_id not in admitted_ids:
                    self._audit(status="CROSS_SECTION_CAP", symbol=symbol, details={"event": event})
                    continue
                cooldown_until = float(self._state["cooldown_until"].get(symbol, 0.0) or 0.0)
                if now_ts < cooldown_until:
                    self._audit(status="SIGNAL_COOLDOWN", symbol=symbol, details={"event": event})
                    continue
                # Match the historical 60-minute thesis recycling policy even
                # if this base event later fails the score or micro gate.
                self._state["cooldown_until"][symbol] = now_ts + 60 * 60
                if not event.get("qualified"):
                    self._audit(status="RESEARCH_GATE_REJECTED", symbol=symbol, details={"event": event})
                    continue
                setup = new_setup(event)
                self._state["setups"][symbol] = setup
                self._audit(
                    status="SETUP_CREATED", symbol=symbol, setup_id=setup["setup_id"],
                    details={"event": event, "setup": setup}, durable=True,
                )
        return changed

    def collect_once(self, hot_snapshot=None):
        now_ts = float(self.now_fn())
        now_iso = _utc_now(now_ts)
        if not self.enabled:
            snapshot = {
                "mode": "ACTIVE_ALTCOIN_PROSPECTIVE_SHADOW", "execution_enabled": False,
                "status": "DISABLED", "timestamp": now_iso, "active_setups": 0,
                "active_plans": 0, "completed": [],
            }
            _publish_snapshot(snapshot)
            return snapshot
        hot_snapshot = hot_snapshot or get_latest_hot_list_snapshot()
        discovery_candidates = (hot_snapshot or {}).get("active_event_candidates")
        candidates = list(
            discovery_candidates
            if isinstance(discovery_candidates, list)
            else ((hot_snapshot or {}).get("candidates") or [])
        )
        bars_by_symbol = {}
        changed = self._process_existing(bars_by_symbol, now_iso)
        events = self._candidate_events(candidates, bars_by_symbol, now_iso)
        changed = self._admit_events(events, now_ts) or changed
        if changed:
            self._persist()
        snapshot = {
            "mode": "ACTIVE_ALTCOIN_PROSPECTIVE_SHADOW",
            "execution_enabled": False,
            "status": "OK",
            "timestamp": now_iso,
            "market_symbols": int((hot_snapshot or {}).get("total_market_symbols", 0)),
            "hot_candidates": len(candidates),
            "discovery_candidates": len(candidates),
            "exact_events_checked": len(events),
            "active_setups": len(self._state["setups"]),
            "active_plans": len(self._state["plans"]),
            "setups": copy.deepcopy(list(self._state["setups"].values())),
            "plans": copy.deepcopy(list(self._state["plans"].values())),
            "completed": copy.deepcopy(self._state["completed"][-10:]),
        }
        _publish_snapshot(snapshot)
        return snapshot

    def run_forever(self, stop_event):
        if not self.enabled:
            self.collect_once()
            logger.info("[ActiveAltcoinShadow] المسار التجريبي معطل من البيئة.")
            return
        logger.info(
            "[ActiveAltcoinShadow] بدأ المسار السببي prospectively؛ interval=%ss execution=false.",
            self.interval_seconds,
        )
        self._audit(status="SHADOW_STARTED", details={
            "interval_seconds": self.interval_seconds,
            "candidate_limit": self.candidate_limit,
            "protocol": "ACTIVE_ALTCOIN_MICRO_GATE_PROTOCOL_2026-09-04.md",
        })
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                snapshot = self.collect_once()
                logger.info(
                    "[ActiveAltcoinShadow] discovery=%s checked=%s setups=%s plans=%s execution=false",
                    snapshot["discovery_candidates"], snapshot["exact_events_checked"],
                    snapshot["active_setups"], snapshot["active_plans"],
                )
            except Exception as exc:
                logger.warning("[ActiveAltcoinShadow] تعذر تحديث shadow: %s", exc)
                self._audit(status="DATA_UNAVAILABLE", details={
                    "error_type": type(exc).__name__, "message": str(exc)[:500],
                })
            stop_event.wait(max(1.0, self.interval_seconds - (time.monotonic() - started)))
