"""Research-only replay of the production closed-5m entry confirmation gate.

The source plans are the causal midpoint plans emitted by
``replay_current_smc_historical.py``.  This diagnostic asks whether the live
5m gate selects a better subset; it never connects to an exchange.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sys

import pandas as pd
import ta


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.logger import logger as production_logger
from scripts.replay_current_smc_historical import (
    HOLD_HOURS,
    _atomic_json,
    _execution_bars,
    _report,
    _resample_ohlcv,
)
from strategy.entry_confirmation import entry_zone_tolerance, evaluate_entry_confirmation
from strategy.filter_profiles import FILTER_PROFILES
from trading_v2.evaluation import EvaluatedPlan
from trading_v2.models import EntryKind, Side, TradePlan
from trading_v2.replay import CausalReplayEngine


MODEL = "CURRENT_5M_CONFIRMATION_DIAGNOSTIC_V1"
ALL_CONFIRMED_MODEL = "CURRENT_5M_CONFIRMATION_ALL_VALID_GEOMETRY_V1"
CONFIRMATION_WINDOW_MINUTES = 12


def _load_symbol(features_root: Path, exchange_symbol: str) -> pd.DataFrame:
    paths = sorted(features_root.glob(f"*/{exchange_symbol}.parquet"))
    parts = [pd.read_parquet(path, columns=[
        "open_time", "close_time", "open", "high", "low", "close",
        "volume", "global_regime", "target_month", "selection_rank",
    ]) for path in paths]
    if not parts:
        return pd.DataFrame()
    frame = pd.concat(parts, ignore_index=True)
    for column in ("open_time", "close_time"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    frame = frame.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    frame["timestamp"] = frame["open_time"]
    return frame


def _confirmation_indicators(frame_5m: pd.DataFrame) -> pd.DataFrame:
    frame = _resample_ohlcv(frame_5m, 15)
    if frame.empty:
        return frame
    frame["ema_50"] = ta.trend.EMAIndicator(frame["close"], window=50).ema_indicator()
    frame["ema_200"] = ta.trend.EMAIndicator(frame["close"], window=200).ema_indicator()
    frame["atr"] = ta.volatility.AverageTrueRange(
        frame["high"], frame["low"], frame["close"], window=14
    ).average_true_range()
    frame["adx"] = ta.trend.ADXIndicator(
        frame["high"], frame["low"], frame["close"], window=14
    ).adx().clip(upper=99.0)
    return frame


def _latest_closed(frame_15m: pd.DataFrame, at) -> pd.Series | None:
    visible = frame_15m[frame_15m["close_time"] <= pd.Timestamp(at)]
    return None if visible.empty else visible.iloc[-1]


def _trigger_window(frame_5m: pd.DataFrame, closed_index: int) -> pd.DataFrame:
    """Place the candidate closed bar at [-2], matching production REST shape."""
    history = frame_5m.iloc[max(0, closed_index - 78):closed_index + 1].copy()
    live = history.iloc[[-1]].copy()
    next_open = pd.Timestamp(history.iloc[-1]["open_time"]) + timedelta(minutes=5)
    close = float(history.iloc[-1]["close"])
    for column in ("open", "high", "low", "close"):
        live.loc[:, column] = close
    live.loc[:, "volume"] = 0.0
    live.loc[:, "open_time"] = next_open
    live.loc[:, "close_time"] = next_open + timedelta(minutes=5) - timedelta(milliseconds=1)
    live.loc[:, "timestamp"] = next_open
    return pd.concat([history, live], ignore_index=True)


def _touches_zone(row: pd.Series, entry: float, tolerance: float) -> bool:
    return (
        float(row["low"]) <= entry + tolerance
        and float(row["high"]) >= entry - tolerance
    )


def _source_rows(source_report: Path) -> dict[str, list[dict]]:
    source = json.loads(source_report.read_text(encoding="utf-8"))
    rows = source["models"]["MIDPOINT_PRODUCTION_RISK"]["records"]
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["symbol"]).replace("/", ""), []).append(row)
    return grouped


def _context(source_plan: dict, fresh: pd.Series) -> dict:
    entry = float(source_plan["entry"])
    stop = float(source_plan["stop"])
    regime = source_plan.get("market_regime") or "UNKNOWN"
    return {
        "setup_anchor": {
            "created_at": source_plan["created_at"],
            "low": min(entry, stop),
            "high": max(entry, stop),
        },
        "score": float(source_plan.get("score") or 0),
        "htf_trend": source_plan.get("htf_trend") or "unknown",
        "market_regime": {"label": regime, "known": regime != "UNKNOWN"},
        "ema_50": float(fresh.get("ema_50") or 0),
        "ema_200": float(fresh.get("ema_200") or 0),
        "adx": float(fresh.get("adx") or 0),
    }


def _confirmed_index(
    *, source_plan: dict, frame_5m: pd.DataFrame, frame_15m: pd.DataFrame,
    touch_index: int, profile: dict, counters: Counter, failed_gates: Counter,
) -> tuple[int | None, dict | None]:
    entry = float(source_plan["entry"])
    stop = float(source_plan["stop"])
    side = str(source_plan["side"]).lower()
    touch_close = pd.Timestamp(frame_5m.iloc[touch_index]["open_time"]) + timedelta(minutes=5)
    deadline = touch_close + timedelta(minutes=CONFIRMATION_WINDOW_MINUTES)
    last_result = None
    for closed_index in range(touch_index, min(len(frame_5m), touch_index + 4)):
        closed_open = pd.Timestamp(frame_5m.iloc[closed_index]["open_time"])
        closed_at = closed_open + timedelta(minutes=5)
        if closed_at > deadline:
            break
        fresh = _latest_closed(frame_15m, closed_at)
        if fresh is None:
            continue
        last_result = evaluate_entry_confirmation(
            side=side,
            trigger_bars=_trigger_window(frame_5m, closed_index),
            current_price=float(frame_5m.iloc[closed_index]["close"]),
            optimal_entry=entry,
            invalidation_level=stop,
            atr_15m=float(fresh.get("atr") or 0),
            ta_context=_context(source_plan, fresh),
            filter_profile=profile,
            planned_rr=float(source_plan["planned_rr"]),
            zone_touch_recorded=True,
            now=closed_at.to_pydatetime(),
        )
        counters["CONFIRMATION_ATTEMPT"] += 1
        if last_result.get("passed"):
            return closed_index, last_result
        failed_gates.update(last_result.get("failed_gates") or [])
    return None, last_result


def replay_confirmation(
    *, source_report: Path, features_root: Path,
    profile_key: str = "smc_balanced",
) -> dict:
    grouped = _source_rows(source_report)
    profile = dict(FILTER_PROFILES[profile_key])
    profile["key"] = profile_key
    counters: Counter[str] = Counter()
    failed_gates: Counter[str] = Counter()
    evaluated: list[EvaluatedPlan] = []
    all_confirmed: list[EvaluatedPlan] = []
    pass_diagnostics: list[dict] = []
    replayer = CausalReplayEngine()
    previous_level = production_logger.level
    production_logger.setLevel(logging.ERROR)
    try:
        for number, (exchange_symbol, plans) in enumerate(sorted(grouped.items()), 1):
            frame_5m = _load_symbol(features_root, exchange_symbol)
            if frame_5m.empty:
                counters["MISSING_SYMBOL_DATA"] += len(plans)
                continue
            frame_15m = _confirmation_indicators(frame_5m)
            opened = frame_5m["open_time"]
            unified_symbol = str(plans[0]["symbol"])
            for source_plan in plans:
                counters["SOURCE_PLAN"] += 1
                created = datetime.fromisoformat(source_plan["created_at"])
                valid_until = datetime.fromisoformat(source_plan["valid_until"])
                entry = float(source_plan["entry"])
                stop = float(source_plan["stop"])
                target = float(source_plan["target"])
                side_text = str(source_plan["side"]).lower()
                side = Side.BUY if side_text == "buy" else Side.SELL

                setup_row = _latest_closed(frame_15m, created)
                if setup_row is None or float(setup_row.get("atr") or 0) <= 0:
                    counters["MISSING_SETUP_ATR"] += 1
                    continue
                tolerance = entry_zone_tolerance(entry, float(setup_row["atr"]))
                left = int(opened.searchsorted(pd.Timestamp(created), side="left"))
                right = int(opened.searchsorted(pd.Timestamp(valid_until), side="right"))
                touch_index = next((
                    index for index in range(left, right)
                    if _touches_zone(frame_5m.iloc[index], entry, tolerance)
                ), None)
                if touch_index is None:
                    counters["NO_ZONE_TOUCH"] += 1
                    continue
                counters["ZONE_TOUCH"] += 1

                confirmed_index, confirmation = _confirmed_index(
                    source_plan=source_plan,
                    frame_5m=frame_5m,
                    frame_15m=frame_15m,
                    touch_index=touch_index,
                    profile=profile,
                    counters=counters,
                    failed_gates=failed_gates,
                )
                if confirmed_index is None:
                    counters["CONFIRMATION_TIMEOUT"] += 1
                    continue
                counters["CONFIRMATION_PASS"] += 1
                next_index = confirmed_index + 1
                if next_index >= len(frame_5m):
                    counters["NO_NEXT_BAR"] += 1
                    continue
                next_open = float(frame_5m.iloc[next_index]["open"])
                risk = next_open - stop if side is Side.BUY else stop - next_open
                reward = target - next_open if side is Side.BUY else next_open - target
                trigger_rr = reward / risk if risk > 0 else 0.0
                pass_diagnostics.append({
                    "thesis_id": source_plan["thesis_id"],
                    "symbol": source_plan["symbol"],
                    "side": source_plan["side"],
                    "setup_created_at": source_plan["created_at"],
                    "confirmed_at": pd.Timestamp(
                        frame_5m.iloc[confirmed_index]["open_time"]
                    ).isoformat(),
                    "trigger_rr_original_levels": trigger_rr,
                    "executable_original_levels": bool(
                        risk > 0 and reward > 0
                        and trigger_rr >= float(profile["min_rr"])
                    ),
                })
                if risk <= 0 or reward <= 0:
                    counters["INVALID_GEOMETRY_AT_TRIGGER"] += 1
                    continue

                execution_start = pd.Timestamp(
                    frame_5m.iloc[next_index]["open_time"]
                ).to_pydatetime()
                execution = _execution_bars(
                    frame_5m, exchange_symbol,
                    start=execution_start,
                    end=execution_start + timedelta(hours=HOLD_HOURS),
                )
                all_plan = TradePlan(
                    plan_id=f"{ALL_CONFIRMED_MODEL}:{source_plan['thesis_id']}",
                    strategy_id=ALL_CONFIRMED_MODEL,
                    setup_id=str(source_plan["thesis_id"]),
                    symbol=unified_symbol, side=side,
                    created_at=execution_start,
                    valid_until=execution_start + timedelta(minutes=1),
                    exit_by=execution_start + timedelta(hours=HOLD_HOURS),
                    entry_kind=EntryKind.MARKET_NEXT_OPEN,
                    entry=next_open, stop=stop, target=target,
                    thesis="Closed-5m confirmation without the trigger RR threshold",
                    score=float((confirmation or {}).get("score") or 0),
                    evidence={
                        "htf_trend": source_plan.get("htf_trend"),
                        "market_regime": source_plan.get("market_regime"),
                        "countertrend": bool(source_plan.get("countertrend", False)),
                        "trigger_rr": trigger_rr,
                    },
                )
                all_confirmed.append(EvaluatedPlan(
                    plan=all_plan,
                    result=replayer.replay_plan(all_plan, execution, horizon_end=all_plan.exit_by),
                ))
                counters["VALID_GEOMETRY_AFTER_CONFIRMATION"] += 1

                if trigger_rr < float(profile["min_rr"]):
                    counters["RR_INVALID_AT_TRIGGER"] += 1
                    continue
                plan = TradePlan(
                    plan_id=f"{MODEL}:{source_plan['thesis_id']}",
                    strategy_id=MODEL,
                    setup_id=str(source_plan["thesis_id"]),
                    symbol=unified_symbol,
                    side=side,
                    created_at=execution_start,
                    valid_until=execution_start + timedelta(minutes=1),
                    exit_by=execution_start + timedelta(hours=HOLD_HOURS),
                    entry_kind=EntryKind.MARKET_NEXT_OPEN,
                    entry=next_open,
                    stop=stop,
                    target=target,
                    thesis="Midpoint setup selected by production closed-5m confirmation",
                    score=float((confirmation or {}).get("score") or 0),
                    evidence={
                        "htf_trend": source_plan.get("htf_trend"),
                        "market_regime": source_plan.get("market_regime"),
                        "countertrend": bool(source_plan.get("countertrend", False)),
                        "trigger_rr": trigger_rr,
                        "confirmation": confirmation,
                    },
                )
                evaluated.append(EvaluatedPlan(
                    plan=plan,
                    result=replayer.replay_plan(plan, execution, horizon_end=plan.exit_by),
                ))
                counters["EXECUTABLE_AFTER_CONFIRMATION"] += 1

            safe = exchange_symbol.encode("ascii", errors="backslashreplace").decode("ascii")
            print(
                f"[{number}/{len(grouped)}] {safe} "
                f"touch={counters['ZONE_TOUCH']} pass={counters['CONFIRMATION_PASS']}",
                flush=True,
            )
    finally:
        production_logger.setLevel(previous_level)

    start = datetime(2025, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return {
        "method": {
            "model": MODEL,
            "research_only": True,
            "source_report": str(source_report),
            "touch_model": "5m OHLC overlap with production adaptive midpoint zone",
            "confirmation": "production evaluate_entry_confirmation on closed 5m",
            "confirmation_window_minutes": CONFIRMATION_WINDOW_MINUTES,
            "execution": "next 5m open with default conservative replay costs",
            "limitations": [
                "5m OHLC cannot prove intrabar touch time or live bid/ask alignment",
                "trigger RR reuses original stop/target instead of fresh SMC level rebuild",
                "structural sweep state transitions are not replayed",
            ],
        },
        "counters": dict(sorted(counters.items())),
        "failed_gates": dict(sorted(failed_gates.items())),
        "confirmation_passes": pass_diagnostics,
        "model": _report(evaluated, start, end),
        "all_confirmed_model": _report(all_confirmed, start, end),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-report", type=Path,
        default=Path("research_data/causal_futures_dataset/current_smc_historical_replay_full.json"),
    )
    parser.add_argument(
        "--features-root", type=Path,
        default=Path("research_data/causal_futures_dataset/features"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("research_data/causal_futures_dataset/current_5m_confirmation_replay.json"),
    )
    args = parser.parse_args()
    payload = replay_confirmation(
        source_report=args.source_report,
        features_root=args.features_root,
    )
    _atomic_json(args.output, payload)
    print(json.dumps(payload["counters"], ensure_ascii=False, indent=2))
    print(json.dumps(payload["failed_gates"], ensure_ascii=False, indent=2))
    print(json.dumps(payload["model"]["summary"], ensure_ascii=False, indent=2))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
