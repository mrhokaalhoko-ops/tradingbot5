"""Causal replay adapter for the production momentum-continuation path.

This module deliberately reuses the production detector, confirmation and
plan builder.  It has no exchange client and cannot place an order.  Historical
decisions are formed from candles that were closed at the decision timestamp;
execution is then replayed on 1m bars with the normal V2 fee/slippage model.

The historical hot-list candidate is an OHLC approximation of the live public
ticker observer.  That limitation is reported in the result metadata instead
of being hidden as an exact market-wide reproduction.
"""

from __future__ import annotations

from collections import Counter
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Mapping

import pandas as pd

from strategy.momentum_continuation import (
    build_fresh_continuation_plan,
    candidate_side,
    detect_momentum_impulse,
    evaluate_continuation_confirmation,
)
from utils.hot_list_monitor import classify_hot_candidate

from .data import CausalBarStore
from .evaluation import EvaluatedPlan
from .models import Bar, EntryKind, Side, TradePlan
from .replay import CausalReplayEngine


@dataclass(frozen=True)
class MomentumResearchConfig:
    impulse_policy: str = "production_all_gates"
    confirmation_policy: str = "production_all_gates"
    max_wait_bars: int = 3
    max_horizon_minutes: int = 240
    min_quote_volume: float = 10_000_000.0
    min_volume_ratio: float = 1.30
    min_range_atr: float = 0.80
    max_range_atr: float = 2.50
    max_abs_24h_change_pct: float = 30.0
    confirmation_retest_buffer_atr: float = 0.35
    confirmation_max_extension_atr: float = 1.25
    confirmation_min_volume_ratio: float = 0.75
    min_net_rr: float = 1.50
    round_trip_fee_pct: float = 0.0008
    slippage_pct: float = 0.0004
    hot_momentum_1m_pct: float = 0.8
    hot_momentum_5m_pct: float = 2.0


@dataclass(frozen=True)
class MomentumResearchRun:
    symbol: str
    start: datetime
    end: datetime
    records: tuple[EvaluatedPlan, ...]
    funnel: Mapping[str, object]
    metadata: Mapping[str, object]


def _bar_id(bar: Bar) -> str:
    return str(int(bar.open_time.timestamp() * 1000))


def closed_bars_to_live_frame(bars: tuple[Bar, ...]) -> pd.DataFrame:
    """Build the REST-shaped frame expected by production functions.

    Production candle frames include an unfinished row at ``-1`` and all
    decision functions deliberately use ``iloc[:-1]``.  Historical snapshots
    contain closed bars only, so a flat placeholder is appended.  It is never
    exposed as evidence and prevents accidentally dropping the newest closed
    historical bar.
    """
    if not bars:
        return pd.DataFrame(columns=("timestamp", "open", "high", "low", "close", "volume"))
    rows = [
        {
            "timestamp": bar.open_time,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
        }
        for bar in bars
    ]
    last = bars[-1]
    rows.append({
        "timestamp": last.close_time + timedelta(milliseconds=1),
        "open": float(last.close),
        "high": float(last.close),
        "low": float(last.close),
        "close": float(last.close),
        "volume": 0.0,
    })
    return pd.DataFrame(rows)


def historical_hot_candidate(
    bars_1m: tuple[Bar, ...],
    index: int,
    *,
    config: MomentumResearchConfig,
) -> dict | None:
    """Approximate one live hot-list sample from closed 1m futures bars."""
    lookback = 24 * 60
    if index < lookback or index >= len(bars_1m):
        return None
    current = bars_1m[index]
    previous_1m = bars_1m[index - 1]
    previous_5m = bars_1m[index - 5]
    window = bars_1m[index - lookback:index + 1]
    first = window[0]
    change_1m = ((current.close / previous_1m.close) - 1.0) * 100.0
    change_5m = ((current.close / previous_5m.close) - 1.0) * 100.0
    change_24h = ((current.close / first.close) - 1.0) * 100.0
    ticker = {
        "symbol": current.symbol.replace("/", ""),
        "lastPrice": current.close,
        "lowPrice": min(bar.low for bar in window),
        "highPrice": max(bar.high for bar in window),
        # Binance ticker exposes quote volume. Cached klines contain base
        # volume, so close*volume is the causal approximation.
        "quoteVolume": sum(float(bar.close) * float(bar.volume) for bar in window),
        "priceChangePercent": change_24h,
    }
    return classify_hot_candidate(
        ticker,
        change_1m=change_1m,
        change_5m=change_5m,
        min_quote_volume=2_000_000.0,
        momentum_1m_pct=config.hot_momentum_1m_pct,
        momentum_5m_pct=config.hot_momentum_5m_pct,
    )


def _production_context(
    store: CausalBarStore,
    *,
    as_of: datetime,
    ta_engine,
    profile: Mapping[str, object],
) -> tuple[dict | None, dict | None]:
    bars = store.snapshot(as_of=as_of, history_limit=260).closed_bars("15m")
    if len(bars) < 210:
        return None, None
    frame = closed_bars_to_live_frame(bars)
    enriched = ta_engine.add_indicators(frame, filter_profile=dict(profile))
    if enriched is None or len(enriched) < 210:
        return None, None
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
    levels = ta_engine.extract_market_levels(
        enriched,
        max_age=int(profile.get("max_ob_age", 100)),
    )
    return context, levels


def _record_reasons(counter: Counter, values) -> None:
    for value in values or ():
        counter[str(value or "UNKNOWN")] += 1


def apply_research_impulse_policy(
    impulse: dict,
    *,
    context_15m: Mapping[str, object],
    policy: str,
) -> dict:
    """Apply an explicit counterfactual policy without altering production."""
    if policy == "production_all_gates":
        return impulse
    if policy != "scored_v2":
        raise ValueError(f"unsupported impulse policy: {policy}")
    result = copy.deepcopy(impulse)
    gates = dict((result.get("evidence") or {}).get("gates") or {})
    side = str(result.get("side") or "").lower()
    opposite_15m = bool(
        (side == "buy" and context_15m.get("down_structure"))
        or (side == "sell" and context_15m.get("up_structure"))
    )
    hard = {
        "not_new_contract": bool(gates.get("not_new_contract")),
        "liquidity": bool(gates.get("liquidity")),
        "not_extreme_24h": bool(gates.get("not_extreme_24h")),
        "regime_known": bool(gates.get("regime_known")),
        "regime_policy": bool(gates.get("regime_policy")),
        "15m_not_opposed": not opposite_15m,
        "directional_body": bool(gates.get("directional_body")),
        "breakout": bool(gates.get("breakout")),
    }
    soft = {
        "local_15m_structure": bool(gates.get("local_15m_structure")),
        "ema_5m_alignment": bool(gates.get("ema_5m_alignment")),
        "close_location": bool(gates.get("close_location")),
        "volume_expansion": bool(gates.get("volume_expansion")),
        "displacement_range": bool(gates.get("displacement_range")),
    }
    soft_score = sum(soft.values())
    failed_hard = [name for name, passed in hard.items() if not passed]
    qualified = not failed_hard and soft_score >= 2
    result.update(
        qualified=qualified,
        reason=(
            "SCORED_V2_IMPULSE_CONFIRMED"
            if qualified
            else "SCORED_V2_FAILED:"
            + ",".join(failed_hard + ([] if soft_score >= 2 else ["soft_score<2"]))
        ),
        failed_gates=(
            [] if qualified else failed_hard + ([] if soft_score >= 2 else ["soft_score<2"])
        ),
    )
    result.setdefault("evidence", {})["research_policy"] = {
        "name": policy,
        "hard_gates": hard,
        "soft_gates": soft,
        "soft_score": soft_score,
        "minimum_soft_score": 2,
    }
    return result


def apply_research_confirmation_policy(confirmation: dict, *, policy: str) -> dict:
    if policy == "production_all_gates":
        return confirmation
    if policy != "scored_v2":
        raise ValueError(f"unsupported confirmation policy: {policy}")
    result = copy.deepcopy(confirmation)
    gates = dict((result.get("evidence") or {}).get("gates") or {})
    if not gates:
        return result
    hard = {
        "not_invalidated": bool(gates.get("not_invalidated")),
        "holds_breakout": bool(gates.get("holds_breakout")),
        "not_extended": bool(gates.get("not_extended")),
    }
    trigger = {
        "directional_close": bool(gates.get("directional_close")),
        "close_location": bool(gates.get("close_location")),
        "participation": bool(gates.get("participation")),
    }
    trigger_score = sum(trigger.values())
    failed_hard = [name for name, passed in hard.items() if not passed]
    passed = not failed_hard and trigger_score >= 2
    result.update(
        passed=passed,
        status="PASS" if passed else result.get("status", "WAIT"),
        reason=(
            "SCORED_V2_CONTINUATION_CONFIRMED"
            if passed
            else "SCORED_V2_FAILED:"
            + ",".join(failed_hard + ([] if trigger_score >= 2 else ["trigger_score<2"]))
        ),
        failed_gates=(
            [] if passed else failed_hard + ([] if trigger_score >= 2 else ["trigger_score<2"])
        ),
    )
    result.setdefault("evidence", {})["research_policy"] = {
        "name": policy,
        "hard_gates": hard,
        "trigger_gates": trigger,
        "trigger_score": trigger_score,
        "minimum_trigger_score": 2,
    }
    return result


def evaluate_production_momentum(
    store: CausalBarStore,
    *,
    start: datetime,
    end: datetime,
    regime_at: Callable[[datetime], str],
    ta_engine,
    profile: Mapping[str, object],
    replay: CausalReplayEngine | None = None,
    config: MomentumResearchConfig | None = None,
) -> MomentumResearchRun:
    """Replay the production momentum state machine on one symbol.

    The loop advances on closed 1m bars to reproduce the live hot observer,
    while impulse and confirmation decisions remain restricted to closed 5m
    candles.  One symbol stays busy until its replayed position exits.
    """
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("start/end must be timezone-aware and ordered")
    cfg = config or MomentumResearchConfig()
    engine = replay or CausalReplayEngine()
    minute_bars = store.bars("1m")
    records: list[EvaluatedPlan] = []
    counts = Counter()
    impulse_reasons = Counter()
    impulse_gate_passes = Counter()
    impulse_gate_totals = Counter()
    impulse_failure_combinations = Counter()
    confirmation_reasons = Counter()
    confirmation_gate_passes = Counter()
    confirmation_gate_totals = Counter()
    plan_reasons = Counter()
    seen_fingerprints: set[str] = set()
    setup: dict | None = None
    busy_until: datetime | None = None
    context_cache: dict[str, tuple[dict | None, dict | None]] = {}

    def context_at(as_of: datetime):
        latest = store.snapshot(as_of=as_of, history_limit=1).closed_bars("15m")
        if not latest:
            return None, None
        cache_key = _bar_id(latest[-1])
        if cache_key not in context_cache:
            context_cache[cache_key] = _production_context(
                store,
                as_of=as_of,
                ta_engine=ta_engine,
                profile=profile,
            )
        return context_cache[cache_key]

    for index, minute_bar in enumerate(minute_bars):
        as_of = minute_bar.close_time
        if as_of < start or as_of > end:
            continue
        if busy_until is not None and as_of <= busy_until:
            counts["minutes_symbol_busy"] += 1
            continue

        five_bars = store.snapshot(as_of=as_of, history_limit=120).closed_bars("5m")
        if not five_bars:
            continue
        latest_5m_id = _bar_id(five_bars[-1])

        if setup is not None and latest_5m_id != setup.get("last_evaluated_5m_bar_id"):
            setup["last_evaluated_5m_bar_id"] = latest_5m_id
            confirmation = evaluate_continuation_confirmation(
                setup=setup,
                bars_5m=closed_bars_to_live_frame(five_bars),
                max_wait_bars=cfg.max_wait_bars,
                retest_buffer_atr=cfg.confirmation_retest_buffer_atr,
                max_extension_atr=cfg.confirmation_max_extension_atr,
                min_volume_ratio=cfg.confirmation_min_volume_ratio,
            )
            confirmation = apply_research_confirmation_policy(
                confirmation,
                policy=cfg.confirmation_policy,
            )
            status = str(confirmation.get("status") or "UNKNOWN")
            reason = str(confirmation.get("reason") or "UNKNOWN")
            counts[f"confirmation_{status.lower()}"] += 1
            confirmation_reasons[reason] += 1
            confirmation_gates = dict(
                (confirmation.get("evidence") or {}).get("gates") or {}
            )
            for gate, passed in confirmation_gates.items():
                confirmation_gate_totals[str(gate)] += 1
                if passed:
                    confirmation_gate_passes[str(gate)] += 1
            if status in {"EXPIRED", "HARD_INVALIDATED"}:
                setup = None
            elif confirmation.get("passed"):
                context, levels = context_at(as_of)
                aligned = bool(
                    context
                    and (
                        (setup.get("side") == "buy" and context.get("up_structure"))
                        or (setup.get("side") == "sell" and context.get("down_structure"))
                    )
                )
                if not aligned:
                    built = {"valid": False, "reason": "15M_STRUCTURE_NO_LONGER_ALIGNED"}
                else:
                    built = build_fresh_continuation_plan(
                        setup=setup,
                        confirmation=confirmation,
                        market_levels=levels,
                        min_net_rr=cfg.min_net_rr,
                        round_trip_fee_pct=cfg.round_trip_fee_pct,
                        slippage_pct=cfg.slippage_pct,
                    )
                plan_reasons[str(built.get("reason") or "UNKNOWN")] += 1
                if built.get("valid"):
                    side = Side.BUY if setup.get("side") == "buy" else Side.SELL
                    created_at = as_of
                    trade_plan = TradePlan(
                        plan_id=str(built["plan_id"]),
                        strategy_id="active_momentum_continuation_v1",
                        setup_id=str(setup["setup_id"]),
                        symbol=store.symbol,
                        side=side,
                        created_at=created_at,
                        valid_until=created_at + timedelta(seconds=120),
                        exit_by=created_at + timedelta(minutes=cfg.max_horizon_minutes),
                        entry_kind=EntryKind.MARKET_NEXT_OPEN,
                        entry=float(built["entry"]),
                        stop=float(built["sl"]),
                        target=float(built["tp"]),
                        thesis="Closed 5m momentum continuation with first real market target",
                        evidence={
                            "regime": regime_at(as_of),
                            "target_type": built.get("target_type"),
                            "planned_net_rr": built.get("net_rr"),
                            "confirmation": confirmation,
                        },
                    )
                    execution_bars = store.between(
                        "1m", trade_plan.created_at, min(trade_plan.exit_by, end)
                    )
                    result = engine.replay_plan(trade_plan, execution_bars, horizon_end=end)
                    records.append(EvaluatedPlan(plan=trade_plan, result=result))
                    counts["plans_qualified"] += 1
                    counts[f"outcome_{result.status.value.lower()}"] += 1
                    busy_until = result.exited_at or trade_plan.exit_by
                else:
                    counts["plans_rejected"] += 1
                setup = None

        candidate = historical_hot_candidate(minute_bars, index, config=cfg)
        if candidate is None:
            continue
        counts["hot_candidates"] += 1
        if setup is not None:
            counts["hot_while_setup_active"] += 1
            continue
        side = candidate_side(candidate)
        if side not in {"buy", "sell"}:
            counts["hot_without_direction"] += 1
            continue
        fingerprint = f"{store.symbol}|{side}|{latest_5m_id}"
        if fingerprint in seen_fingerprints:
            counts["duplicate_fingerprint"] += 1
            continue
        seen_fingerprints.add(fingerprint)
        counts["independent_impulses"] += 1
        context, _ = context_at(as_of)
        if context is None:
            counts["context_unavailable"] += 1
            continue
        regime_label = str(regime_at(as_of) or "UNKNOWN")
        regime = {"label": regime_label, "known": regime_label != "UNKNOWN"}
        impulse = detect_momentum_impulse(
            symbol=store.symbol,
            candidate=candidate,
            bars_5m=closed_bars_to_live_frame(five_bars),
            market_regime=regime,
            context_15m=context,
            min_volume_ratio=cfg.min_volume_ratio,
            min_range_atr=cfg.min_range_atr,
            max_range_atr=cfg.max_range_atr,
            max_abs_24h_change_pct=cfg.max_abs_24h_change_pct,
            min_quote_volume=cfg.min_quote_volume,
        )
        impulse = apply_research_impulse_policy(
            impulse,
            context_15m=context,
            policy=cfg.impulse_policy,
        )
        impulse_gates = dict((impulse.get("evidence") or {}).get("gates") or {})
        for gate, passed in impulse_gates.items():
            impulse_gate_totals[str(gate)] += 1
            if passed:
                impulse_gate_passes[str(gate)] += 1
        if not impulse.get("qualified"):
            counts["impulses_rejected"] += 1
            _record_reasons(impulse_reasons, impulse.get("failed_gates"))
            impulse_failure_combinations[
                ",".join(sorted(str(item) for item in impulse.get("failed_gates") or ()))
                or "UNKNOWN"
            ] += 1
            continue
        counts["setups_created"] += 1
        setup = {
            **impulse,
            "candidate": candidate,
            "market_regime": regime,
            "context_15m": context,
            "created_at": as_of.isoformat(),
            "last_evaluated_5m_bar_id": latest_5m_id,
        }

    funnel = {
        **dict(sorted(counts.items())),
        "impulse_rejection_gates": dict(impulse_reasons.most_common()),
        "impulse_failure_combinations": dict(
            impulse_failure_combinations.most_common()
        ),
        "impulse_gate_pass_rates": {
            gate: (
                impulse_gate_passes[gate] / total if total else None
            )
            for gate, total in sorted(impulse_gate_totals.items())
        },
        "confirmation_reasons": dict(confirmation_reasons.most_common()),
        "confirmation_gate_pass_rates": {
            gate: (
                confirmation_gate_passes[gate] / total if total else None
            )
            for gate, total in sorted(confirmation_gate_totals.items())
        },
        "plan_reasons": dict(plan_reasons.most_common()),
    }
    metadata = {
        "causal": True,
        "execution_enabled": False,
        "decision_clock": "closed_1m hot sample / closed_5m impulse+confirmation",
        "hot_list_fidelity": "OHLC_APPROXIMATION_NOT_FULL_MARKET_RANKING",
        "causal_15m_context_cache_entries": len(context_cache),
        "production_functions_reused": [
            "classify_hot_candidate",
            "detect_momentum_impulse",
            "evaluate_continuation_confirmation",
            "build_fresh_continuation_plan",
        ],
    }
    return MomentumResearchRun(
        symbol=store.symbol,
        start=start,
        end=end,
        records=tuple(records),
        funnel=funnel,
        metadata=metadata,
    )
