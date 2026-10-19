"""Rebuild production entry-plan revalidation from cached Binance Demo 1m bars.

Research only.  The script has no credentials and no order methods.  It uses
the same TAEngine/RiskManager code as production to explain why a candidate
that passed 5m confirmation was accepted or rejected when its 15m context was
refreshed immediately before execution.
"""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import re
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines.ta_engine import TAEngine
from scripts.audit_runtime_theses_1m import load_cached_1m, parse_utc
from scripts.replay_runtime_theses_1m import aggregate_1m
from strategy.filter_profiles import FILTER_PROFILES, normalize_filter_profile
from strategy.risk_manager import RiskManager
from trading_v2.models import Bar


_LOGGED_RR_RE = re.compile(r"\((\d+(?:\.\d+)?)R\s*<")
_LOGGED_TARGET_RE = re.compile(r"النوع:\s*([A-Za-z_]+)", re.IGNORECASE)


def _frame_at(
    bars: tuple[Bar, ...], *, at, trigger_price: float, limit: int = 260
) -> pd.DataFrame | None:
    """Return the REST-like 15m frame visible at ``at``.

    Production receives ``limit-1`` completed candles and one currently open
    candle, then reads ``iloc[-2]`` and excludes ``iloc[-1]`` from its level
    map.  A neutral placeholder therefore reproduces the causal shape without
    pretending to know intrabar ticks that were not recorded.
    """
    at = parse_utc(at)
    closed = [bar for bar in aggregate_1m(bars, minutes=15) if bar.close_time <= at]
    required_closed = max(1, int(limit) - 1)
    if len(closed) < required_closed:
        return None
    closed = closed[-required_closed:]
    rows = [
        {
            "timestamp": bar.open_time,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in closed
    ]
    current_open = closed[-1].close_time
    price = float(trigger_price or closed[-1].close)
    rows.append(
        {
            "timestamp": current_open,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 0.0,
        }
    )
    return pd.DataFrame(rows)


def _logged_terminal(thesis: dict) -> tuple[float | None, str | None, str | None]:
    statuses = thesis.get("sniper_terminal_statuses") or []
    terminal = str(statuses[-1]) if statuses else None
    if not terminal:
        return None, None, None
    rr_match = _LOGGED_RR_RE.search(terminal)
    target_match = _LOGGED_TARGET_RE.search(terminal)
    return (
        float(rr_match.group(1)) if rr_match else None,
        target_match.group(1).lower() if target_match else None,
        terminal,
    )


def rebuild_confirmed_plan(
    *, thesis: dict, confirmation_event: dict, bars: tuple[Bar, ...], profile: dict
) -> dict:
    confirmation = (confirmation_event.get("details") or {}).get("confirmation") or {}
    closed_5m = confirmation.get("closed_bar") or {}
    trigger_price = float(closed_5m.get("close") or 0.0)
    at = parse_utc(confirmation_event["timestamp"])
    frame = _frame_at(bars, at=at, trigger_price=trigger_price)
    base = {
        "thesis_id": thesis.get("thesis_id"),
        "symbol": thesis.get("symbol"),
        "side": thesis.get("side"),
        "confirmation_at": confirmation_event.get("timestamp"),
        "trigger_price_proxy": trigger_price,
        "trigger_price_evidence": "RECORDED_CLOSED_5M_CLOSE",
    }
    if frame is None:
        return {**base, "status": "INSUFFICIENT_15M_WARMUP"}

    ta_engine = TAEngine()
    enriched = ta_engine.add_indicators(frame, filter_profile=profile)
    if enriched is None or len(enriched) < 210:
        return {**base, "status": "INDICATOR_WINDOW_UNAVAILABLE"}
    latest = enriched.iloc[-2]
    atr = float(latest.get("atr") or 0.0)
    levels = ta_engine.extract_market_levels(
        enriched, max_age=int(profile.get("max_ob_age", 100))
    )
    manager = RiskManager()
    sl, tp = manager.calculate_sl_tp(
        str(thesis.get("side") or "").lower(),
        trigger_price,
        atr=atr,
        invalidation_level=float(thesis.get("invalidation") or trigger_price),
        market_levels=levels,
        filter_profile=profile,
    )
    plan = dict(manager.last_plan or {})
    logged_rr, logged_target, terminal = _logged_terminal(thesis)
    reconstructed_rr = plan.get("rr")
    blocking = plan.get("blocking_target") or {}
    reconstructed_target = str(
        plan.get("target_type") or blocking.get("type") or ""
    ).lower() or None
    rr_matches = (
        logged_rr is not None
        and reconstructed_rr is not None
        and round(float(reconstructed_rr), 2) == round(float(logged_rr), 2)
    )
    target_matches = (
        logged_target is not None
        and reconstructed_target is not None
        and logged_target == reconstructed_target
    )
    return {
        **base,
        "status": "REBUILT_ACCEPTED" if sl is not None and tp is not None else "REBUILT_REJECTED",
        "fresh_atr": atr,
        "fresh_sl": sl,
        "fresh_tp": tp,
        "fresh_plan": plan,
        "fresh_block_reason": manager.last_block_reason,
        "level_counts": {key: len(value) for key, value in levels.items()},
        "logged_terminal": terminal,
        "logged_rr": logged_rr,
        "logged_target_type": logged_target,
        "reconstructed_rr_matches_log": rr_matches,
        "reconstructed_target_matches_log": target_matches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild fresh 15m target plans for confirmed runtime entries."
    )
    parser.add_argument(
        "--research-root",
        type=Path,
        default=Path("research_data/runtime_thesis_replay_20260901_warm72"),
    )
    parser.add_argument("--profile", default="smc_balanced")
    parser.add_argument(
        "--allow-source-mismatches",
        action="store_true",
        help=(
            "Run only as a quarantined diagnostic when recorded candles do not "
            "match the downloaded Binance 1m source."
        ),
    )
    args = parser.parse_args()
    root = ROOT / args.research_root
    theses = json.loads((root / "theses.json").read_text(encoding="utf-8"))["theses"]
    manifest = json.loads((root / "demo_1m_manifest.json").read_text(encoding="utf-8"))
    verification = json.loads(
        (root / "source_verification.json").read_text(encoding="utf-8")
    )
    if verification.get("comparison_count", 0) <= 0:
        raise RuntimeError("Demo 1m source has not passed recorded-candle verification")
    if verification.get("mismatched") and not args.allow_source_mismatches:
        raise RuntimeError(
            "Demo 1m source has recorded-candle mismatches; rerun only with "
            "--allow-source-mismatches for a quarantined diagnostic"
        )
    cached = load_cached_1m(manifest)
    profile_name = normalize_filter_profile(args.profile)
    profile = dict(FILTER_PROFILES[profile_name])
    profile["key"] = profile_name

    rows = []
    for thesis in theses:
        for event in thesis.get("confirmation_events") or []:
            if event.get("status") != "PASS":
                continue
            rows.append(
                rebuild_confirmed_plan(
                    thesis=thesis,
                    confirmation_event=event,
                    bars=cached.get(str(thesis.get("symbol")), ()),
                    profile=profile,
                )
            )

    payload = {
        "schema_version": 1,
        "method": "PRODUCTION_CODE_REBUILD_FROM_BINANCE_DEMO_1M",
        "profile": args.profile,
        "source_verification": {
            "comparison_count": verification["comparison_count"],
            "matched": verification["matched"],
            "mismatched": verification["mismatched"],
            "mismatch_override": bool(
                verification.get("mismatched") and args.allow_source_mismatches
            ),
        },
        "row_count": len(rows),
        "rows": rows,
    }
    output = root / "entry_plan_revalidation_replay.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
