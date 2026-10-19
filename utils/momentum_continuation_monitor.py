"""Coordinator for the fresh-continuation shadow state machine.

The coordinator consumes the existing market-wide hot list, but remains
incapable of creating an order intent or calling the exchange execution API.
Its output is durable decision-audit evidence that can later be replayed and
compared with the SMC pullback funnel.
"""

from __future__ import annotations

import copy
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

from core.logger import logger
from strategy.filter_profiles import get_filter_profile
from strategy.market_regime import detect_market_regime
from strategy.momentum_continuation import (
    build_external_thesis_continuation_setup,
    build_fresh_continuation_plan,
    candidate_side,
    detect_momentum_impulse,
    evaluate_continuation_confirmation,
    evaluate_shadow_plan_outcome,
)
from utils.decision_audit import audit_decision_event
from utils.hot_list_monitor import get_latest_hot_list_snapshot


_snapshot_lock = threading.Lock()
_external_thesis_lock = threading.Lock()
_external_thesis_queue = deque(maxlen=200)
_latest_snapshot = {
    "mode": "FRESH_CONTINUATION_SHADOW",
    "execution_enabled": False,
    "status": "NOT_STARTED",
    "active_setups": 0,
    "active_plans": 0,
    "paired_active_setups": 0,
    "paired_active_plans": 0,
    "completed": [],
}


def _bool_env(name, default=False):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int_env(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float_env(name, default, minimum, maximum):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _snapshot_age_seconds(snapshot, now_ts):
    try:
        timestamp = datetime.fromisoformat(
            str((snapshot or {}).get("timestamp") or "").replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return max(0.0, float(now_ts) - timestamp.timestamp())
    except (TypeError, ValueError, OverflowError):
        return float("inf")


def _symbol_to_ccxt(symbol):
    symbol = str(symbol or "").strip().upper().split(":")[0]
    if "/" in symbol:
        return symbol
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    return f"{symbol}/USDT"


def get_latest_momentum_continuation_snapshot():
    with _snapshot_lock:
        return copy.deepcopy(_latest_snapshot)


def register_external_smc_thesis(payload):
    """Queue an RR-qualified SMC thesis for paired shadow evaluation only."""
    payload = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    identity = (
        str(payload.get("source_thesis_id") or ""),
        str(payload.get("symbol") or "").upper(),
        str(payload.get("side") or "").lower(),
    )
    if not identity[0] or not identity[1] or identity[2] not in {"buy", "sell"}:
        return False
    with _external_thesis_lock:
        if any(
            (
                str(item.get("source_thesis_id") or ""),
                str(item.get("symbol") or "").upper(),
                str(item.get("side") or "").lower(),
            )
            == identity
            for item in _external_thesis_queue
        ):
            return True
        _external_thesis_queue.append(payload)
    return True


def _take_external_smc_theses():
    with _external_thesis_lock:
        rows = list(_external_thesis_queue)
        _external_thesis_queue.clear()
    return rows


def _publish_snapshot(snapshot):
    global _latest_snapshot
    with _snapshot_lock:
        _latest_snapshot = copy.deepcopy(snapshot)


class MomentumContinuationShadowMonitor:
    """Closed-candle continuation research with no execution dependency."""

    STATE_KEY = "momentum_continuation_shadow_state"

    def __init__(
        self,
        client,
        ta_engine,
        state_manager=None,
        *,
        now_fn=time.time,
        regime_fn=detect_market_regime,
    ):
        self.client = client
        self.ta_engine = ta_engine
        self.state_manager = state_manager
        self.now_fn = now_fn
        self.regime_fn = regime_fn
        self.enabled = _bool_env("MOMENTUM_CONTINUATION_SHADOW_ENABLED", True)
        self.interval_seconds = _bounded_int_env(
            "MOMENTUM_CONTINUATION_INTERVAL_SECONDS", 60, 30, 300
        )
        self.candidate_limit = _bounded_int_env(
            "MOMENTUM_CONTINUATION_CANDIDATE_LIMIT", 8, 1, 20
        )
        self.max_snapshot_age_seconds = _bounded_int_env(
            "MOMENTUM_CONTINUATION_MAX_SNAPSHOT_AGE_SECONDS", 180, 60, 900
        )
        self.max_wait_bars = _bounded_int_env(
            "MOMENTUM_CONTINUATION_MAX_WAIT_BARS", 3, 1, 12
        )
        self.max_horizon_minutes = _bounded_int_env(
            "MOMENTUM_CONTINUATION_SHADOW_HORIZON_MINUTES", 240, 30, 1440
        )
        self.min_quote_volume = _bounded_float_env(
            "MOMENTUM_CONTINUATION_MIN_QUOTE_VOLUME_USDT",
            10_000_000.0,
            1_000_000.0,
            1_000_000_000.0,
        )
        self.max_abs_24h_change_pct = _bounded_float_env(
            "MOMENTUM_CONTINUATION_MAX_ABS_24H_CHANGE_PCT", 30.0, 5.0, 100.0
        )
        self.min_volume_ratio = _bounded_float_env(
            "MOMENTUM_CONTINUATION_MIN_VOLUME_RATIO", 1.30, 1.0, 5.0
        )
        self.round_trip_fee_pct = _bounded_float_env(
            "MOMENTUM_CONTINUATION_ROUND_TRIP_FEE_PCT", 0.0008, 0.0, 0.01
        )
        self.slippage_pct = _bounded_float_env(
            "MOMENTUM_CONTINUATION_SLIPPAGE_PCT", 0.0004, 0.0, 0.01
        )
        self._last_regime_at = 0.0
        self._market_regime = None
        self._venue_unavailable_audit_at = {}
        self._state = self._load_state()

    def _empty_state(self):
        return {
            "schema_version": 1,
            "setups": {},
            "plans": {},
            "seen": {},
            "completed": [],
        }

    def _load_state(self):
        if self.state_manager is None:
            return self._empty_state()
        saved = self.state_manager.get(self.STATE_KEY, {})
        if not isinstance(saved, dict) or int(saved.get("schema_version", 1)) != 1:
            return self._empty_state()
        state = self._empty_state()
        for key in ("setups", "plans", "seen"):
            if isinstance(saved.get(key), dict):
                state[key] = copy.deepcopy(saved[key])
        if isinstance(saved.get("completed"), list):
            state["completed"] = copy.deepcopy(saved["completed"][-100:])
        return state

    def _persist_state(self):
        cutoff = float(self.now_fn()) - 6 * 60 * 60
        self._state["seen"] = {
            key: value
            for key, value in self._state.get("seen", {}).items()
            if float(value or 0) >= cutoff
        }
        if len(self._state["seen"]) > 500:
            newest = sorted(
                self._state["seen"].items(), key=lambda item: float(item[1]), reverse=True
            )[:500]
            self._state["seen"] = dict(newest)
        self._state["completed"] = self._state.get("completed", [])[-100:]
        if self.state_manager is None:
            return True
        return bool(self.state_manager.set(self.STATE_KEY, self._state))

    def _profile(self):
        return get_filter_profile(self.state_manager) if self.state_manager else {
            "key": "smc_balanced",
            "min_rr": 1.5,
            "max_ob_age": 100,
        }

    def _get_market_regime(self, supplied=None):
        if supplied is not None:
            self._market_regime = supplied
            self._last_regime_at = float(self.now_fn())
            return supplied
        now_ts = float(self.now_fn())
        if self._market_regime is None or now_ts - self._last_regime_at >= 300:
            self._market_regime = self.regime_fn(self.client)
            self._last_regime_at = now_ts
        return self._market_regime

    def _fetch_5m(self, symbol):
        return self.client.fetch_ohlcv(symbol, timeframe="5m", limit=120)

    def _execution_venue_supports(self, symbol):
        """Fail fast for hot-list symbols unavailable on the execution venue.

        The hot list intentionally watches Binance USD-M Mainnet, while the bot
        may execute on Binance Demo.  New or pre-trading Mainnet contracts are
        not necessarily present on Demo, so fetching their candles through the
        execution client only creates repeated ExchangeError noise.

        If CCXT markets have not been loaded yet, availability is unknown and
        the normal fetch path remains authoritative (fail-open for data only;
        this shadow monitor has no execution capability).
        """
        exchange = getattr(self.client, "exchange", None)
        markets = getattr(exchange, "markets", None)
        if not isinstance(markets, dict) or not markets:
            return True, "MARKETS_NOT_LOADED"

        requested = _symbol_to_ccxt(symbol).upper().split(":", 1)[0]
        matching = []
        for market_key, market in markets.items():
            if not isinstance(market, dict):
                continue
            unified = str(market.get("symbol") or market_key or "").upper()
            if unified.split(":", 1)[0] != requested:
                continue
            is_contract = bool(
                market.get("contract")
                or market.get("swap")
                or market.get("future")
            )
            quote = str(market.get("quote") or "").upper()
            settle = str(market.get("settle") or "").upper()
            if not is_contract or quote != "USDT" or settle not in {"", "USDT"}:
                continue
            if market.get("linear") is False:
                continue
            matching.append(market)

        if not matching:
            return False, "SYMBOL_NOT_LISTED"

        reasons = []
        for market in matching:
            raw_status = str((market.get("info") or {}).get("status") or "").upper()
            if market.get("active") is False:
                reasons.append("INACTIVE")
                continue
            if raw_status and raw_status != "TRADING":
                reasons.append(raw_status)
                continue
            return True, "TRADING"
        return False, ",".join(sorted(set(reasons))) or "NOT_TRADING"

    def _audit_unavailable_symbol(self, symbol, candidate, reason):
        now_ts = float(self.now_fn())
        last_audit = float(self._venue_unavailable_audit_at.get(symbol, 0.0) or 0.0)
        if now_ts - last_audit < 15 * 60:
            return
        self._venue_unavailable_audit_at[symbol] = now_ts
        self._audit(
            symbol=symbol,
            status="SYMBOL_UNAVAILABLE_ON_EXECUTION_VENUE",
            details={
                "reason": reason,
                "candidate_tags": list(candidate.get("tags") or []),
            },
        )
        logger.debug(
            "[MomentumContinuation] تجاهل %s: غير متاح للتداول على بيئة التنفيذ (%s).",
            symbol,
            reason,
        )

    def _fetch_15m_context(self, symbol, profile):
        bars = self.client.fetch_ohlcv(symbol, timeframe="15m", limit=260)
        if bars is None or len(bars) < 210:
            return None, None, None
        enriched = self.ta_engine.add_indicators(bars, filter_profile=profile)
        if enriched is None or len(enriched) < 210:
            return None, None, None
        latest = enriched.iloc[-2]
        close = float(latest.get("close") or 0.0)
        ema_50 = float(latest.get("ema_50") or 0.0)
        ema_200 = float(latest.get("ema_200") or 0.0)
        context = {
            "close": close,
            "ema_50": ema_50,
            "ema_200": ema_200,
            "adx": float(latest.get("adx") or 0.0),
            "atr": float(latest.get("atr") or 0.0),
            "up_structure": close > ema_50 > ema_200 > 0,
            "down_structure": 0 < close < ema_50 < ema_200,
        }
        levels = self.ta_engine.extract_market_levels(
            enriched, max_age=int(profile.get("max_ob_age", 100))
        )
        return enriched, context, levels

    @staticmethod
    def _closed_bar_fingerprint(symbol, side, bars_5m):
        if bars_5m is None or len(bars_5m) < 2:
            return None
        row = bars_5m.iloc[-2]
        timestamp = row.get("timestamp", row.get("time", row.name))
        return f"{symbol}|{side}|{timestamp}"

    def _audit(self, *, symbol, status, details, setup_id=None, plan_id=None, durable=False):
        identity = plan_id or setup_id or f"mc:{symbol}:{status}"
        return audit_decision_event(
            decision_id=identity,
            signal_id=identity,
            setup_id=setup_id,
            thesis_id=setup_id,
            virtual_order_id=plan_id,
            symbol=symbol,
            stage="MOMENTUM_CONTINUATION",
            status=status,
            details={"execution_enabled": False, **(details or {})},
            durable=durable,
        )

    def _archive(self, record):
        self._state.setdefault("completed", []).append(
            {**copy.deepcopy(record), "completed_at": _utc_now()}
        )

    def _process_plan(self, symbol, plan, bars_5m):
        outcome = evaluate_shadow_plan_outcome(
            plan=plan,
            bars_5m=bars_5m,
            max_horizon_minutes=self.max_horizon_minutes,
            now=datetime.fromtimestamp(float(self.now_fn()), tz=timezone.utc),
        )
        plan["last_outcome"] = outcome
        if not outcome.get("terminal"):
            return False
        self._audit(
            symbol=symbol,
            status=f"OUTCOME_{outcome.get('status')}",
            setup_id=plan.get("setup_id"),
            plan_id=plan.get("plan_id"),
            details={"plan": plan, "outcome": outcome},
            durable=True,
        )
        self._archive({"kind": "PLAN", "symbol": symbol, "plan": plan, "outcome": outcome})
        self._state["plans"].pop(symbol, None)
        return True

    def _process_setup(self, symbol, setup, bars_5m, profile):
        confirmation = evaluate_continuation_confirmation(
            setup=setup,
            bars_5m=bars_5m,
            max_wait_bars=self.max_wait_bars,
        )
        bar_id = confirmation.get("confirmation_bar_id")
        if bar_id and bar_id != setup.get("last_confirmation_bar"):
            setup["last_confirmation_bar"] = bar_id
            setup["last_confirmation"] = confirmation
            self._audit(
                symbol=symbol,
                status=(
                    "CONFIRMATION_PASS"
                    if confirmation.get("passed")
                    else "CONFIRMATION_WAIT"
                ),
                setup_id=setup.get("setup_id"),
                details={"setup": setup, "confirmation": confirmation},
            )
        if confirmation.get("status") in {"EXPIRED", "HARD_INVALIDATED"}:
            self._audit(
                symbol=symbol,
                status=confirmation["status"],
                setup_id=setup.get("setup_id"),
                details={"setup": setup, "confirmation": confirmation},
                durable=True,
            )
            self._archive(
                {"kind": "SETUP", "symbol": symbol, "setup": setup, "confirmation": confirmation}
            )
            self._state["setups"].pop(symbol, None)
            return True
        if not confirmation.get("passed"):
            return bool(bar_id)

        _, context_15m, levels = self._fetch_15m_context(symbol, profile)
        if context_15m is None:
            setup["last_plan_error"] = "INSUFFICIENT_15M_CONTEXT"
            return True
        # A fresh plan must remain aligned at confirmation, not only at impulse.
        side = setup.get("side")
        aligned = context_15m.get("up_structure") if side == "buy" else context_15m.get("down_structure")
        if not aligned:
            rejected = {
                "valid": False,
                "reason": "15M_STRUCTURE_NO_LONGER_ALIGNED",
                "plan_type": "FRESH_CONTINUATION_SHADOW",
            }
        else:
            rejected = build_fresh_continuation_plan(
                setup=setup,
                confirmation=confirmation,
                market_levels=levels,
                min_net_rr=float(profile.get("min_rr", 1.5)),
                round_trip_fee_pct=self.round_trip_fee_pct,
                slippage_pct=self.slippage_pct,
            )
        if not rejected.get("valid"):
            self._audit(
                symbol=symbol,
                status="PLAN_REJECTED",
                setup_id=setup.get("setup_id"),
                details={
                    "setup": setup,
                    "confirmation": confirmation,
                    "context_15m": context_15m,
                    "plan": rejected,
                },
                durable=True,
            )
            self._archive(
                {"kind": "PLAN_REJECTED", "symbol": symbol, "setup": setup, "plan": rejected}
            )
            self._state["setups"].pop(symbol, None)
            return True

        plan = {
            **rejected,
            "symbol": symbol,
            "source": setup.get("source", "HOT_LIST_MOMENTUM"),
            "source_thesis_id": setup.get("source_thesis_id"),
            "context_15m": context_15m,
            # Preserve the exact closed-candle evidence used to qualify the
            # plan so the shadow result can be reproduced causally later.
            "confirmation": copy.deepcopy(confirmation),
            "market_regime": copy.deepcopy(setup.get("market_regime") or {}),
            "candidate": copy.deepcopy(setup.get("candidate") or {}),
            "created_at": _utc_now(),
            "last_outcome": {"status": "OPEN"},
        }
        self._state["plans"][symbol] = plan
        self._state["setups"].pop(symbol, None)
        self._audit(
            symbol=symbol,
            status="PLAN_QUALIFIED_SHADOW",
            setup_id=setup.get("setup_id"),
            plan_id=plan.get("plan_id"),
            details={"setup": setup, "confirmation": confirmation, "plan": plan},
            durable=True,
        )
        return True

    def _process_candidate(self, candidate, regime, profile):
        side = candidate_side(candidate)
        if side is None:
            return False
        symbol = _symbol_to_ccxt(candidate.get("symbol"))
        if symbol in self._state["setups"] or symbol in self._state["plans"]:
            return False
        venue_supported, venue_reason = self._execution_venue_supports(symbol)
        if not venue_supported:
            self._audit_unavailable_symbol(symbol, candidate, venue_reason)
            return False
        bars_5m = self._fetch_5m(symbol)
        fingerprint = self._closed_bar_fingerprint(symbol, side, bars_5m)
        if not fingerprint or fingerprint in self._state["seen"]:
            return False
        _, context_15m, _ = self._fetch_15m_context(symbol, profile)
        if context_15m is None:
            return False
        impulse = detect_momentum_impulse(
            symbol=symbol,
            candidate=candidate,
            bars_5m=bars_5m,
            market_regime=regime,
            context_15m=context_15m,
            min_volume_ratio=self.min_volume_ratio,
            max_abs_24h_change_pct=self.max_abs_24h_change_pct,
            min_quote_volume=self.min_quote_volume,
        )
        self._state["seen"][fingerprint] = float(self.now_fn())
        setup_id = impulse.get("setup_id")
        if not impulse.get("qualified"):
            self._audit(
                symbol=symbol,
                status="IMPULSE_REJECTED",
                setup_id=setup_id,
                details={"candidate": candidate, "impulse": impulse},
            )
            return True
        setup = {
            **impulse,
            "candidate": copy.deepcopy(candidate),
            "market_regime": copy.deepcopy(regime),
            "context_15m": context_15m,
            "created_at": _utc_now(),
            "last_confirmation_bar": None,
        }
        self._state["setups"][symbol] = setup
        self._audit(
            symbol=symbol,
            status="SETUP_CREATED",
            setup_id=setup_id,
            details={"candidate": candidate, "setup": setup},
            durable=True,
        )
        return True

    def _process_external_thesis(self, payload, profile):
        """Turn one production SMC thesis into an isolated paired shadow setup."""
        symbol = _symbol_to_ccxt(payload.get("symbol"))
        source_thesis_id = str(payload.get("source_thesis_id") or "")
        if symbol in self._state["setups"] or symbol in self._state["plans"]:
            self._audit(
                symbol=symbol,
                status="EXTERNAL_SETUP_SKIPPED_SYMBOL_BUSY",
                setup_id=source_thesis_id,
                details={"source": "SMC_RR_PASS_PAIRED_SHADOW"},
            )
            return False
        venue_supported, venue_reason = self._execution_venue_supports(symbol)
        if not venue_supported:
            self._audit_unavailable_symbol(symbol, payload, venue_reason)
            return False

        setup = build_external_thesis_continuation_setup(
            symbol=symbol,
            side=payload.get("side"),
            source_thesis_id=source_thesis_id,
            bars_5m=self._fetch_5m(symbol),
            invalidation_level=payload.get("invalidation_level"),
            market_regime=payload.get("market_regime"),
            context_15m=payload.get("context_15m"),
            source_snapshot=payload.get("source_snapshot"),
        )
        if not setup.get("qualified"):
            self._audit(
                symbol=symbol,
                status="EXTERNAL_SETUP_REJECTED",
                setup_id=source_thesis_id,
                details={"setup": setup},
                durable=True,
            )
            return True

        setup.update(
            candidate={
                "symbol": str(payload.get("symbol") or ""),
                "tags": ["SMC_RR_PASS_PAIRED_SHADOW"],
            },
            market_regime=copy.deepcopy(payload.get("market_regime") or {}),
            context_15m=copy.deepcopy(payload.get("context_15m") or {}),
            created_at=_utc_now(),
            last_confirmation_bar=None,
        )
        self._state["setups"][symbol] = setup
        self._audit(
            symbol=symbol,
            status="SETUP_CREATED_EXTERNAL_SMC",
            setup_id=setup.get("setup_id"),
            details={
                "source_thesis_id": source_thesis_id,
                "source": "SMC_RR_PASS_PAIRED_SHADOW",
                "setup": setup,
            },
            durable=True,
        )
        return True

    def collect_once(self, hot_snapshot=None, market_regime=None):
        if not self.enabled:
            snapshot = {
                "mode": "FRESH_CONTINUATION_SHADOW",
                "execution_enabled": False,
                "status": "DISABLED",
                "timestamp": _utc_now(),
                "active_setups": 0,
                "active_plans": 0,
                "paired_active_setups": 0,
                "paired_active_plans": 0,
                "completed": [],
            }
            _publish_snapshot(snapshot)
            return snapshot

        hot_snapshot = hot_snapshot or get_latest_hot_list_snapshot()
        changed = False
        profile = self._profile()
        regime = self._get_market_regime(market_regime)
        hot_status = str((hot_snapshot or {}).get("status") or "UNKNOWN").upper()
        hot_age_seconds = _snapshot_age_seconds(hot_snapshot, self.now_fn())
        hot_is_fresh = (
            hot_status == "OK"
            and hot_age_seconds <= float(self.max_snapshot_age_seconds)
        )
        candidates = (
            list((hot_snapshot or {}).get("candidates") or [])[: self.candidate_limit]
            if hot_is_fresh
            else []
        )

        external_theses = _take_external_smc_theses()
        for payload in external_theses:
            changed = self._process_external_thesis(payload, profile) or changed

        symbols = set(self._state["plans"]) | set(self._state["setups"])
        for symbol in sorted(symbols):
            venue_supported, venue_reason = self._execution_venue_supports(symbol)
            if not venue_supported:
                self._audit_unavailable_symbol(symbol, {}, venue_reason)
                self._archive({
                    "kind": "VENUE_UNAVAILABLE",
                    "symbol": symbol,
                    "reason": venue_reason,
                })
                self._state["plans"].pop(symbol, None)
                self._state["setups"].pop(symbol, None)
                changed = True
                continue
            bars_5m = self._fetch_5m(symbol)
            if symbol in self._state["plans"]:
                changed = self._process_plan(
                    symbol, self._state["plans"][symbol], bars_5m
                ) or changed
            elif symbol in self._state["setups"]:
                changed = self._process_setup(
                    symbol, self._state["setups"][symbol], bars_5m, profile
                ) or changed

        for candidate in candidates:
            changed = self._process_candidate(candidate, regime, profile) or changed

        if changed:
            self._persist_state()
        snapshot = {
            "mode": "FRESH_CONTINUATION_SHADOW",
            "execution_enabled": False,
            "status": "OK" if hot_is_fresh else "HOT_LIST_STALE",
            "timestamp": _utc_now(),
            "hot_list_status": hot_status,
            "hot_list_age_seconds": hot_age_seconds,
            "market_regime": regime,
            "candidate_count": len(candidates),
            "external_theses_received": len(external_theses),
            "active_setups": len(self._state["setups"]),
            "active_plans": len(self._state["plans"]),
            "paired_active_setups": sum(
                str(item.get("source") or "") == "SMC_RR_PASS_PAIRED_SHADOW"
                for item in self._state["setups"].values()
            ),
            "paired_active_plans": sum(
                str(item.get("source") or "") == "SMC_RR_PASS_PAIRED_SHADOW"
                for item in self._state["plans"].values()
            ),
            "setups": copy.deepcopy(list(self._state["setups"].values())),
            "plans": copy.deepcopy(list(self._state["plans"].values())),
            "completed": copy.deepcopy(self._state["completed"][-10:]),
        }
        _publish_snapshot(snapshot)
        return snapshot

    def run_forever(self, stop_event):
        if not self.enabled:
            self.collect_once()
            logger.info("[MomentumContinuation] مسار shadow معطل من البيئة.")
            return
        logger.info(
            "[MomentumContinuation] بدأ Fresh Continuation shadow كل %ss؛ "
            "execution=false.",
            self.interval_seconds,
        )
        audit_decision_event(
            stage="MOMENTUM_CONTINUATION",
            status="SHADOW_STARTED",
            details={
                "execution_enabled": False,
                "interval_seconds": self.interval_seconds,
                "candidate_limit": self.candidate_limit,
            },
        )
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                snapshot = self.collect_once()
                logger.info(
                    "[MomentumContinuation] candidates=%s setups=%s plans=%s execution=false",
                    snapshot.get("candidate_count", 0),
                    snapshot.get("active_setups", 0),
                    snapshot.get("active_plans", 0),
                )
            except Exception as exc:
                logger.warning(
                    "[MomentumContinuation] تعذر تحديث المسار التجريبي: %s", exc
                )
                audit_decision_event(
                    stage="MOMENTUM_CONTINUATION",
                    status="DATA_UNAVAILABLE",
                    details={
                        "execution_enabled": False,
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:500],
                    },
                )
            elapsed = time.monotonic() - started
            stop_event.wait(max(1.0, self.interval_seconds - elapsed))
