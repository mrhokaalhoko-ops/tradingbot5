"""Replay independent runtime theses against verified Binance Demo 1m paths.

Research only: this module has no exchange credentials and no order methods.
It deliberately separates geometry-only limit counterfactuals from the stricter
touch -> later closed-5m confirmation -> next-open execution experiment.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Iterable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_runtime_theses_1m import load_cached_1m, parse_utc
from strategy.entry_confirmation import entry_zone_tolerance, evaluate_entry_confirmation
from trading_v2.models import Bar, EntryKind, ReplayStatus, Side, TradePlan
from trading_v2.replay import CausalReplayEngine, ReplayCosts


MIN_RR = 1.5
DISCOVERY_MINUTES = 20
CONFIRMATION_MINUTES = 12
HOLD_HOURS = 24
STOP_MULTIPLIERS = (0.1, 0.2, 0.3)
ZERO_COSTS = ReplayCosts(0, 0, 0, 0, 0)
CONSERVATIVE_COSTS = ReplayCosts(5, 5, 3, 8, 2)
# Minimal immutable snapshot of the production smc_balanced fields consumed by
# evaluate_entry_confirmation().  Importing filter_profiles configures the live
# logger as a module side effect, which a research-only CLI must not do.
BALANCED_CONFIRMATION_PROFILE = {
    "require_ob": True,
    "allow_countertrend_reversal": True,
    "trigger_evidence_min": 1,
    "require_volume": False,
    "require_entry_sweep": False,
    "min_rr": MIN_RR,
}


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def snapshot_plan_inputs(thesis: dict, snapshot: dict) -> dict | None:
    details = snapshot.get("details") or {}
    target_plan = details.get("target_plan") or {}
    stop_plan = target_plan.get("stop_plan") or {}
    market_context = snapshot.get("market_context") or {}
    setup_context = market_context.get("setup_context") or {}
    anchor = setup_context.get("setup_anchor") or {}
    side = str(thesis.get("side") or "").upper()

    entry = _float(target_plan.get("entry") or thesis.get("midpoint"))
    current_stop = _float(target_plan.get("sl") or stop_plan.get("final_sl"))
    if target_plan.get("valid"):
        target = _float(target_plan.get("tp"))
        target_type = str(target_plan.get("target_type") or "unknown")
    else:
        blocking = target_plan.get("blocking_target") or {}
        target = _float(blocking.get("tp"))
        target_type = str(blocking.get("type") or "unknown")

    invalidation = _float(
        stop_plan.get("invalidation_level")
        or setup_context.get("invalidation_level")
        or thesis.get("invalidation")
    )
    atr = _float(
        stop_plan.get("atr")
        or (market_context.get("indicators") or {}).get("atr")
        or thesis.get("atr_at_creation")
    )
    zone_low = _float(anchor.get("low") or setup_context.get("ob_low") or thesis.get("zone_low"))
    zone_high = _float(anchor.get("high") or setup_context.get("ob_high") or thesis.get("zone_high"))
    proximal_edge = zone_high if side == "BUY" else zone_low

    if not all(value > 0 for value in (entry, current_stop, target, invalidation, atr)):
        return None
    if side == "BUY" and not target > entry:
        return None
    if side == "SELL" and not target < entry:
        return None
    return {
        "side": side,
        "entry": entry,
        "current_stop": current_stop,
        "target": target,
        "target_type": target_type,
        "invalidation": invalidation,
        "atr": atr,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "proximal_edge": proximal_edge,
        "setup_context": setup_context,
        "recorded_plan_valid": bool(target_plan.get("valid")),
        "recorded_rr": _float(target_plan.get("rr")),
    }


def invalidation_buffer_stop(
    *, side: str, invalidation: float, atr: float, multiplier: float
) -> float:
    distance = float(atr) * float(multiplier)
    return (
        float(invalidation) - distance
        if str(side).upper() == "BUY"
        else float(invalidation) + distance
    )


def planned_rr(*, side: str, entry: float, stop: float, target: float) -> float:
    if str(side).upper() == "BUY" and not (stop < entry < target):
        return 0.0
    if str(side).upper() == "SELL" and not (target < entry < stop):
        return 0.0
    risk = abs(float(entry) - float(stop))
    return abs(float(target) - float(entry)) / risk if risk > 0 else 0.0


def aggregate_1m(bars: Iterable[Bar], *, minutes: int) -> tuple[Bar, ...]:
    buckets: dict[datetime, list[Bar]] = {}
    for bar in sorted(bars, key=lambda item: item.open_time):
        opened = bar.open_time.astimezone(timezone.utc)
        bucket_minute = opened.minute - opened.minute % int(minutes)
        bucket = opened.replace(minute=bucket_minute, second=0, microsecond=0)
        buckets.setdefault(bucket, []).append(bar)
    result = []
    for opened, rows in sorted(buckets.items()):
        rows.sort(key=lambda item: item.open_time)
        expected = [opened + timedelta(minutes=index) for index in range(int(minutes))]
        if len(rows) != int(minutes) or [row.open_time for row in rows] != expected:
            continue
        result.append(Bar(
            symbol=rows[0].symbol,
            timeframe=f"{minutes}m",
            open_time=opened,
            close_time=opened + timedelta(minutes=int(minutes)),
            open=rows[0].open,
            high=max(row.high for row in rows),
            low=min(row.low for row in rows),
            close=rows[-1].close,
            volume=sum(row.volume for row in rows),
        ))
    return tuple(result)


def confirmation_frame(closed_bars: tuple[Bar, ...], candidate: Bar) -> pd.DataFrame | None:
    visible = [bar for bar in closed_bars if bar.close_time <= candidate.close_time]
    if len(visible) < 21 or visible[-1].open_time != candidate.open_time:
        return None
    rows = [
        {
            "timestamp": bar.open_time,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in visible[-79:]
    ]
    # Production fetches include the current still-open candle at [-1].  The
    # evaluator reads only [-2]; this inert placeholder preserves that shape.
    rows.append({
        "timestamp": candidate.close_time,
        "open": candidate.close,
        "high": candidate.close,
        "low": candidate.close,
        "close": candidate.close,
        "volume": 0.0,
    })
    return pd.DataFrame(rows)


def _result_payload(result, *, available_end: datetime, exit_by: datetime) -> dict:
    payload = asdict(result)
    payload["status"] = result.status.value
    payload["filled_at"] = _iso(result.filled_at)
    payload["exited_at"] = _iso(result.exited_at)
    payload["right_censored"] = (
        result.status is ReplayStatus.OPEN_AT_HORIZON and available_end < exit_by
    )
    return payload


def replay_limit(
    *, thesis: dict, snapshot: dict, inputs: dict, entry: float, stop: float,
    bars: tuple[Bar, ...], model: str,
) -> dict:
    created = parse_utc(snapshot["timestamp"])
    valid_until = created + timedelta(minutes=DISCOVERY_MINUTES)
    exit_by = created + timedelta(hours=HOLD_HOURS)
    rr = planned_rr(
        side=inputs["side"], entry=entry, stop=stop, target=inputs["target"]
    )
    base = {
        "model": model,
        "thesis_id": thesis["thesis_id"],
        "symbol": thesis["symbol"],
        "side": inputs["side"],
        "snapshot_at": _iso(created),
        "entry": entry,
        "stop": stop,
        "target": inputs["target"],
        "target_type": inputs["target_type"],
        "planned_rr": rr,
        "touch_evidence": "TRADE_PRICE_1M_RANGE_APPROXIMATION",
    }
    if rr < MIN_RR:
        return {**base, "status": "RR_REJECTED"}
    available = tuple(bar for bar in bars if bar.open_time >= created)
    if not available or available[-1].close_time < valid_until:
        return {**base, "status": "INSUFFICIENT_DISCOVERY_DATA"}
    side = Side.BUY if inputs["side"] == "BUY" else Side.SELL
    plan = TradePlan(
        plan_id=f"{model}:{thesis['thesis_id']}",
        strategy_id=model,
        setup_id=thesis["thesis_id"],
        symbol=thesis["symbol"],
        side=side,
        created_at=created,
        valid_until=valid_until,
        exit_by=exit_by,
        entry_kind=EntryKind.LIMIT,
        entry=entry,
        stop=stop,
        target=inputs["target"],
        thesis="runtime thesis geometry counterfactual",
    )
    available_end = available[-1].close_time
    gross = CausalReplayEngine(ZERO_COSTS).replay_plan(
        plan, available, horizon_end=available_end
    )
    conservative = CausalReplayEngine(CONSERVATIVE_COSTS).replay_plan(
        plan, available, horizon_end=available_end
    )
    return {
        **base,
        "status": gross.status.value,
        "gross": _result_payload(gross, available_end=available_end, exit_by=exit_by),
        "conservative": _result_payload(
            conservative, available_end=available_end, exit_by=exit_by
        ),
    }


def _bar_hits(side: str, bar: Bar, *, stop: float, target: float) -> tuple[bool, bool]:
    if side == "BUY":
        return bar.low <= stop, bar.high >= target
    return bar.high >= stop, bar.low <= target


def replay_touch_reclaim(
    *, thesis: dict, snapshot: dict, inputs: dict, multiplier: float,
    bars_1m: tuple[Bar, ...], bars_5m: tuple[Bar, ...],
) -> dict:
    model = f"TOUCH_RECLAIM_INV_{multiplier:.1f}_ATR"
    created = parse_utc(snapshot["timestamp"])
    discovery_end = created + timedelta(minutes=DISCOVERY_MINUTES)
    entry_reference = inputs["entry"]
    stop = invalidation_buffer_stop(
        side=inputs["side"], invalidation=inputs["invalidation"],
        atr=inputs["atr"], multiplier=multiplier,
    )
    rr_at_reference = planned_rr(
        side=inputs["side"], entry=entry_reference, stop=stop,
        target=inputs["target"],
    )
    base = {
        "model": model,
        "thesis_id": thesis["thesis_id"],
        "symbol": thesis["symbol"],
        "side": inputs["side"],
        "snapshot_at": _iso(created),
        "entry_reference": entry_reference,
        "stop": stop,
        "target": inputs["target"],
        "target_type": inputs["target_type"],
        "planned_rr_at_reference": rr_at_reference,
        "touch_evidence": "TRADE_PRICE_1M_RANGE_APPROXIMATION",
    }
    if rr_at_reference < MIN_RR:
        return {**base, "status": "RR_REJECTED"}
    eligible_1m = tuple(
        bar for bar in bars_1m
        if bar.open_time >= created and bar.open_time < discovery_end
    )
    if not eligible_1m or eligible_1m[-1].close_time < discovery_end:
        return {**base, "status": "INSUFFICIENT_DISCOVERY_DATA"}

    tolerance = entry_zone_tolerance(entry_reference, inputs["atr"])
    touch = None
    for bar in eligible_1m:
        stop_hit, target_hit = _bar_hits(
            inputs["side"], bar, stop=stop, target=inputs["target"]
        )
        touched = bar.low <= entry_reference + tolerance and bar.high >= entry_reference - tolerance
        if touched:
            if stop_hit or target_hit:
                return {
                    **base, "status": "AMBIGUOUS_TOUCH_TERMINAL_BAR",
                    "touch_bar": _iso(bar.open_time),
                }
            touch = bar
            break
        if stop_hit:
            return {**base, "status": "INVALIDATED_BEFORE_TOUCH"}
        if target_hit:
            return {**base, "status": "TARGET_BEFORE_TOUCH"}
    if touch is None:
        return {**base, "status": "EXPIRED_UNTOUCHED"}

    confirmation_end = touch.close_time + timedelta(minutes=CONFIRMATION_MINUTES)
    candidate_bars = [
        bar for bar in bars_5m
        if bar.close_time > touch.close_time and bar.close_time <= confirmation_end
    ]
    if not candidate_bars:
        return {
            **base, "status": "NO_CLOSED_5M_IN_CONFIRMATION_WINDOW",
            "touch_at": _iso(touch.close_time),
        }

    last_checked_1m = touch.close_time
    profile = dict(BALANCED_CONFIRMATION_PROFILE)
    confirmation = None
    confirmed_bar = None
    for candidate in candidate_bars:
        between = [
            bar for bar in bars_1m
            if last_checked_1m <= bar.open_time < candidate.close_time
        ]
        for bar in between:
            stop_hit, target_hit = _bar_hits(
                inputs["side"], bar, stop=stop, target=inputs["target"]
            )
            if stop_hit and target_hit:
                return {**base, "status": "AMBIGUOUS_TERMINALS_BEFORE_CONFIRMATION"}
            if stop_hit:
                return {**base, "status": "INVALIDATED_BEFORE_CONFIRMATION"}
            if target_hit:
                return {**base, "status": "TARGET_BEFORE_CONFIRMATION"}
        frame = confirmation_frame(bars_5m, candidate)
        if frame is None:
            continue
        confirmation = evaluate_entry_confirmation(
            side=inputs["side"].lower(),
            trigger_bars=frame,
            current_price=candidate.close,
            optimal_entry=entry_reference,
            invalidation_level=inputs["invalidation"],
            atr_15m=inputs["atr"],
            ta_context=inputs["setup_context"],
            filter_profile=profile,
            planned_rr=rr_at_reference,
            zone_touch_recorded=True,
            now=candidate.close_time + timedelta(seconds=1),
        )
        last_checked_1m = candidate.close_time
        if confirmation.get("passed"):
            confirmed_bar = candidate
            break
    if confirmed_bar is None:
        return {
            **base,
            "status": "CONFIRMATION_FAILED",
            "touch_at": _iso(touch.close_time),
            "last_confirmation": confirmation,
        }

    next_bars = [bar for bar in bars_1m if bar.open_time >= confirmed_bar.close_time]
    if not next_bars:
        return {**base, "status": "NO_NEXT_OPEN_AFTER_CONFIRMATION"}
    nominal_fill = next_bars[0].open
    conservative_fill = nominal_fill * (
        1.0003 if inputs["side"] == "BUY" else 0.9997
    )
    execution_rr = planned_rr(
        side=inputs["side"], entry=conservative_fill, stop=stop,
        target=inputs["target"],
    )
    if execution_rr < MIN_RR:
        return {
            **base,
            "status": "RR_REJECTED_AT_EXECUTION",
            "touch_at": _iso(touch.close_time),
            "confirmed_at": _iso(confirmed_bar.close_time),
            "conservative_fill": conservative_fill,
            "execution_rr": execution_rr,
            "confirmation": confirmation,
        }

    exit_by = confirmed_bar.close_time + timedelta(hours=HOLD_HOURS)
    plan = TradePlan(
        plan_id=f"{model}:{thesis['thesis_id']}",
        strategy_id=model,
        setup_id=thesis["thesis_id"],
        symbol=thesis["symbol"],
        side=Side.BUY if inputs["side"] == "BUY" else Side.SELL,
        created_at=confirmed_bar.close_time,
        valid_until=confirmed_bar.close_time + timedelta(minutes=1),
        exit_by=exit_by,
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=nominal_fill,
        stop=stop,
        target=inputs["target"],
        thesis="touch then subsequent closed-5m confirmation",
    )
    available_end = bars_1m[-1].close_time
    gross = CausalReplayEngine(ZERO_COSTS).replay_plan(
        plan, bars_1m, horizon_end=available_end
    )
    conservative = CausalReplayEngine(CONSERVATIVE_COSTS).replay_plan(
        plan, bars_1m, horizon_end=available_end
    )
    return {
        **base,
        "status": gross.status.value,
        "touch_at": _iso(touch.close_time),
        "confirmed_at": _iso(confirmed_bar.close_time),
        "execution_rr": execution_rr,
        "confirmation": confirmation,
        "gross": _result_payload(gross, available_end=available_end, exit_by=exit_by),
        "conservative": _result_payload(
            conservative, available_end=available_end, exit_by=exit_by
        ),
    }


def summarize(rows: list[dict]) -> dict:
    by_model = {}
    for model in sorted({row["model"] for row in rows}):
        selected = [row for row in rows if row["model"] == model]
        statuses = Counter(row["status"] for row in selected)
        resolved = [
            row for row in selected
            if row.get("conservative", {}).get("status")
            in {ReplayStatus.TP_FIRST.value, ReplayStatus.SL_FIRST.value}
        ]
        net_values = [
            _float(row["conservative"].get("net_r")) for row in resolved
            if row["conservative"].get("net_r") is not None
        ]
        wins = sum(
            row["conservative"]["status"] == ReplayStatus.TP_FIRST.value
            for row in resolved
        )
        positive = sum(value for value in net_values if value > 0)
        negative = abs(sum(value for value in net_values if value < 0))
        by_model[model] = {
            "theses_evaluated": len(selected),
            "statuses": dict(statuses),
            "resolved_tp_sl": len(resolved),
            "wins": wins,
            "win_rate_resolved": wins / len(resolved) if resolved else None,
            "net_r_sum_resolved": sum(net_values) if net_values else None,
            "net_r_mean_resolved": (
                sum(net_values) / len(net_values) if net_values else None
            ),
            "profit_factor_resolved": (
                positive / negative if negative > 0 else None
            ),
            "right_censored_fills": sum(
                bool(row.get("conservative", {}).get("right_censored"))
                for row in selected
                if row.get("conservative", {}).get("filled_at")
            ),
        }
    return by_model


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay verified runtime theses on Demo 1m.")
    parser.add_argument(
        "--research-root",
        type=Path,
        default=Path("research_data/runtime_thesis_replay_20260830"),
    )
    parser.add_argument(
        "--allow-source-mismatches",
        action="store_true",
        help=(
            "Run as a quarantined diagnostic when some recorded candles do not "
            "match the downloaded 1m source. The mismatch count remains embedded "
            "in every result and must not be treated as validated backtest evidence."
        ),
    )
    args = parser.parse_args()
    root = ROOT / args.research_root
    theses_payload = json.loads((root / "theses.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "demo_1m_manifest.json").read_text(encoding="utf-8"))
    verification = json.loads((root / "source_verification.json").read_text(encoding="utf-8"))
    if verification.get("comparison_count", 0) <= 0:
        raise RuntimeError("Demo 1m source has not passed recorded-candle verification")
    if verification.get("mismatched") and not args.allow_source_mismatches:
        raise RuntimeError(
            "Demo 1m source has recorded-candle mismatches; rerun only with "
            "--allow-source-mismatches for a quarantined diagnostic"
        )
    cached = load_cached_1m(manifest)
    aggregated_5m = {symbol: aggregate_1m(bars, minutes=5) for symbol, bars in cached.items()}

    rows = []
    excluded = []
    for thesis in theses_payload["theses"]:
        snapshots = thesis.get("rr_snapshots") or []
        if not snapshots:
            excluded.append({
                "thesis_id": thesis["thesis_id"],
                "symbol": thesis["symbol"],
                "reason": "NO_RR_SNAPSHOT_QUALITY_DID_NOT_REACH_RR",
            })
            continue
        first = snapshots[0]
        inputs = snapshot_plan_inputs(thesis, first)
        if inputs is None:
            excluded.append({
                "thesis_id": thesis["thesis_id"],
                "symbol": thesis["symbol"],
                "reason": "NO_CAUSAL_OPPOSING_MARKET_TARGET",
            })
            continue
        symbol_bars = cached.get(thesis["symbol"], ())

        first_pass = next(
            (snapshot for snapshot in snapshots if snapshot.get("status") == "PASS"),
            None,
        )
        if first_pass is not None:
            pass_inputs = snapshot_plan_inputs(thesis, first_pass)
            if pass_inputs is not None:
                rows.append(replay_limit(
                    thesis=thesis, snapshot=first_pass, inputs=pass_inputs,
                    entry=pass_inputs["entry"], stop=pass_inputs["current_stop"],
                    bars=symbol_bars,
                    model="LIMIT_CURRENT_GEOMETRY_FIRST_RR_PASS",
                ))

        for multiplier in STOP_MULTIPLIERS:
            stop = invalidation_buffer_stop(
                side=inputs["side"], invalidation=inputs["invalidation"],
                atr=inputs["atr"], multiplier=multiplier,
            )
            rows.append(replay_limit(
                thesis=thesis, snapshot=first, inputs=inputs,
                entry=inputs["entry"], stop=stop, bars=symbol_bars,
                model=f"LIMIT_MIDPOINT_INV_{multiplier:.1f}_ATR",
            ))
            rows.append(replay_limit(
                thesis=thesis, snapshot=first, inputs=inputs,
                entry=inputs["proximal_edge"], stop=stop, bars=symbol_bars,
                model=f"LIMIT_PROXIMAL_INV_{multiplier:.1f}_ATR",
            ))
            rows.append(replay_touch_reclaim(
                thesis=thesis, snapshot=first, inputs=inputs, multiplier=multiplier,
                bars_1m=symbol_bars, bars_5m=aggregated_5m.get(thesis["symbol"], ()),
            ))

    summary = summarize(rows)
    payload = {
        "schema_version": 1,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "source_verification": {
            "comparison_count": verification["comparison_count"],
            "matched": verification["matched"],
            "mismatched": verification["mismatched"],
            "mismatch_override": bool(
                verification.get("mismatched") and args.allow_source_mismatches
            ),
        },
        "assumptions": {
            "minimum_rr": MIN_RR,
            "discovery_minutes": DISCOVERY_MINUTES,
            "confirmation_minutes": CONFIRMATION_MINUTES,
            "holding_hours": HOLD_HOURS,
            "touch_evidence": "1m trade-range approximation except recorded runtime events",
            "conservative_costs": asdict(CONSERVATIVE_COSTS),
        },
        "row_count": len(rows),
        "excluded": excluded,
        "summary": summary,
        "rows": rows,
    }
    _atomic_json(root / "replay_results.json", payload)

    markdown_lines = [
        "# Runtime Thesis Causal Replay — Binance Demo 1m",
        "",
        f"Independent theses: **{theses_payload['thesis_count']}**  ",
        f"Verified source candles: **{verification['matched']}/{verification['comparison_count']}**  ",
        f"Recorded/source mismatches: **{verification['mismatched']}** "
        f"({'QUARANTINED OVERRIDE' if verification['mismatched'] else 'none'})  ",
        "Actual runtime executions: **0**",
        "",
        "| Model | Evaluated | Resolved | Wins | Win rate | Net R | Censored |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model, item in summary.items():
        win_rate = "—" if item["win_rate_resolved"] is None else f"{item['win_rate_resolved']:.1%}"
        net_r = "—" if item["net_r_sum_resolved"] is None else f"{item['net_r_sum_resolved']:.3f}"
        markdown_lines.append(
            f"| {model} | {item['theses_evaluated']} | {item['resolved_tp_sl']} | "
            f"{item['wins']} | {win_rate} | {net_r} | {item['right_censored_fills']} |"
        )
    markdown_lines.extend([
        "",
        "## Interpretation constraints",
        "",
        "- Limit variants diagnose entry/stop geometry only; they bypass production confirmation.",
        "- A 1m candle touching entry and a terminal level is ambiguous, never forced into a win.",
        "- Available Demo history ends at extraction time, so open trades are right-censored.",
        "- This one runtime window generates hypotheses; it cannot prove durable edge.",
    ])
    (root / "replay_summary.md").write_text(
        "\n".join(markdown_lines) + "\n", encoding="utf-8"
    )
    print(f"rows={len(rows)} models={len(summary)}")
    for model, item in summary.items():
        print(model, item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
