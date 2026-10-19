"""Offline replay helpers for RR-qualified runtime candidates.

This module never connects to an exchange and never submits orders.  It reads
the immutable decision-audit data exported by the bot and measures candidate
paths using the recorded closed 15m OHLC bars.  Same-bar SL/TP collisions are
reported as ambiguous rather than guessed.
"""

from __future__ import annotations

import json
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def load_decision_events(zip_paths: Iterable[str | Path]) -> list[dict]:
    """Load and de-duplicate decision events from a split runtime export."""
    events: list[dict] = []
    seen: set[str] = set()
    for raw_path in zip_paths:
        with zipfile.ZipFile(Path(raw_path)) as archive:
            audit_members = sorted(
                name
                for name in archive.namelist()
                if Path(name).name.startswith("decision_audit_")
                and Path(name).suffix == ".jsonl"
            )
            if len(audit_members) != 1:
                raise ValueError(
                    f"expected exactly one decision_audit_*h.jsonl member in "
                    f"{raw_path}, found {audit_members}"
                )
            payload = archive.read(audit_members[0]).decode("utf-8", "replace")
        for line in payload.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            event_id = str(event.get("event_id") or "")
            if event_id and event_id in seen:
                continue
            if event_id:
                seen.add(event_id)
            events.append(event)
    events.sort(key=lambda item: str(item.get("timestamp") or ""))
    return events


def simulate_market_entry(
    *,
    side: str,
    entry: float,
    stop: float,
    target: float,
    bars: list[dict],
) -> dict:
    """Replay a market entry over closed OHLC bars without intrabar guessing."""
    side = str(side or "").upper()
    entry, stop, target = float(entry), float(stop), float(target)
    risk = abs(entry - stop)
    if side not in {"BUY", "SELL"} or min(entry, stop, target, risk) <= 0:
        return {"outcome": "INVALID_PLAN", "mfe_r": None, "mae_r": None}

    mfe_r = 0.0
    mae_r = 0.0
    last_close = entry
    for bar in bars:
        high = float(bar.get("high") or 0)
        low = float(bar.get("low") or 0)
        last_close = float(bar.get("close") or last_close)
        if side == "BUY":
            mfe_r = max(mfe_r, (high - entry) / risk)
            mae_r = max(mae_r, (entry - low) / risk)
            hit_target = high >= target
            hit_stop = low <= stop
        else:
            mfe_r = max(mfe_r, (entry - low) / risk)
            mae_r = max(mae_r, (high - entry) / risk)
            hit_target = low <= target
            hit_stop = high >= stop

        if hit_target and hit_stop:
            return {
                "outcome": "AMBIGUOUS_SAME_BAR",
                "mfe_r": round(mfe_r, 6),
                "mae_r": round(mae_r, 6),
                "exit_time": bar.get("timestamp"),
                "final_r": None,
            }
        if hit_stop:
            return {
                "outcome": "SL_FIRST",
                "mfe_r": round(mfe_r, 6),
                "mae_r": round(mae_r, 6),
                "exit_time": bar.get("timestamp"),
                "final_r": -1.0,
            }
        if hit_target:
            final_r = abs(target - entry) / risk
            return {
                "outcome": "TP_FIRST",
                "mfe_r": round(mfe_r, 6),
                "mae_r": round(mae_r, 6),
                "exit_time": bar.get("timestamp"),
                "final_r": round(final_r, 6),
            }

    direction = 1.0 if side == "BUY" else -1.0
    return {
        "outcome": "OPEN_AT_HORIZON" if bars else "NO_FUTURE_BARS",
        "mfe_r": round(mfe_r, 6) if bars else None,
        "mae_r": round(mae_r, 6) if bars else None,
        "exit_time": bars[-1].get("timestamp") if bars else None,
        "final_r": round(direction * (last_close - entry) / risk, 6) if bars else None,
    }


def replay_rr_candidates(
    events: list[dict], *, horizon_hours: float = 4.0
) -> dict:
    """Build a current-Sniper vs signal-close replay for RR-pass events."""
    contexts = {
        event.get("decision_id"): event.get("details") or {}
        for event in events
        if event.get("stage") == "MARKET_CONTEXT"
    }
    signals = {
        event.get("decision_id"): event
        for event in events
        if event.get("stage") == "HYBRID_DECISION"
        and event.get("status") in {"BUY", "SELL"}
    }
    pending = {
        event.get("decision_id"): event
        for event in events
        if event.get("stage") == "SNIPER" and event.get("status") == "PENDING"
    }
    terminals = {
        event.get("virtual_order_id"): event
        for event in events
        if event.get("stage") == "SNIPER" and event.get("status") != "PENDING"
    }
    touched = {
        event.get("virtual_order_id")
        for event in events
        if event.get("stage") == "ENTRY_ZONE_TOUCH"
    }
    bars_by_symbol: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        if event.get("stage") != "MARKET_BAR" or not event.get("symbol"):
            continue
        details = dict(event.get("details") or {})
        details["timestamp"] = event.get("timestamp")
        bars_by_symbol[str(event["symbol"])].append(details)
    for bars in bars_by_symbol.values():
        bars.sort(key=lambda bar: str(bar.get("timestamp") or ""))

    results: list[dict] = []
    rr_passes = [
        event
        for event in events
        if event.get("stage") == "RR_FILTER" and event.get("status") == "PASS"
    ]
    for rr_event in rr_passes:
        decision_id = rr_event.get("decision_id")
        signal = signals.get(decision_id) or {}
        context = contexts.get(decision_id) or {}
        plan = (rr_event.get("details") or {}).get("target_plan") or {}
        side = str(signal.get("status") or "").upper()
        signal_price = float((context.get("closed_bar") or {}).get("close") or 0)
        optimal_entry = float(plan.get("entry") or 0)
        stop = float(plan.get("sl") or 0)
        target = float(plan.get("tp") or 0)
        if side == "BUY":
            effective_rr = (
                (target - signal_price) / (signal_price - stop)
                if signal_price > stop and target > signal_price
                else None
            )
        else:
            effective_rr = (
                (signal_price - target) / (stop - signal_price)
                if stop > signal_price and signal_price > target
                else None
            )

        start = _parse_utc(str(rr_event.get("timestamp")))
        end = start + timedelta(hours=float(horizon_hours))
        future_bars = [
            bar
            for bar in bars_by_symbol.get(str(rr_event.get("symbol")), [])
            if start < _parse_utc(str(bar.get("timestamp"))) <= end
        ]
        replay = simulate_market_entry(
            side=side,
            entry=signal_price,
            stop=stop,
            target=target,
            bars=future_bars,
        )
        sniper_event = pending.get(decision_id) or {}
        virtual_order_id = sniper_event.get("virtual_order_id")
        terminal = terminals.get(virtual_order_id) or {}
        results.append({
            "timestamp": rr_event.get("timestamp"),
            "symbol": rr_event.get("symbol"),
            "side": side,
            "signal_price": signal_price,
            "optimal_entry": optimal_entry,
            "distance_to_optimal_pct": round(
                abs(signal_price - optimal_entry) / optimal_entry * 100, 6
            ) if optimal_entry else None,
            "planned_rr": round(float(plan.get("rr") or 0), 6),
            "signal_close_effective_rr": (
                round(effective_rr, 6) if effective_rr is not None else None
            ),
            "sniper_touched": virtual_order_id in touched,
            "sniper_terminal": terminal.get("status") or "PENDING_AT_EXPORT",
            "signal_close_replay": replay,
        })

    qualifying_market = [
        item for item in results
        if float(item.get("signal_close_effective_rr") or 0) >= 1.5
    ]
    return {
        "schema_version": 1,
        "limitations": [
            "Uses recorded closed 15m OHLC; same-bar SL/TP order is ambiguous.",
            "Signal-close replay reuses the original structural stop and market target.",
            "This is an execution audit, not evidence of durable strategy profitability.",
        ],
        "candidate_count": len(results),
        "sniper_touch_count": sum(bool(x["sniper_touched"]) for x in results),
        "signal_close_rr_qualified_count": len(qualifying_market),
        "signal_close_rr_qualified_outcomes": {
            outcome: sum(
                x["signal_close_replay"]["outcome"] == outcome
                for x in qualifying_market
            )
            for outcome in {
                x["signal_close_replay"]["outcome"] for x in qualifying_market
            }
        },
        "candidates": results,
    }
