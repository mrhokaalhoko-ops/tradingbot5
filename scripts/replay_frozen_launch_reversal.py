"""Cost-aware 1m replay of the frozen launch extreme/reversal hypothesis."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.binance_archive import MonthlyArchive, parse_kline_archive
from trading_v2.evaluation import (
    EvaluatedPlan,
    enforce_portfolio_capacity,
    summarize,
    summarize_by_calendar_month,
)
from trading_v2.launch_reversal import detect_launch_reversal
from trading_v2.models import Bar
from trading_v2.replay import CausalReplayEngine


def _time(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("launch time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _next_month(month: str) -> str:
    year, number = (int(value) for value in month.split("-"))
    number += 1
    if number == 13:
        year, number = year + 1, 1
    return f"{year:04d}-{number:02d}"


def _normalized(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", value).casefold()
        if char.isalnum()
    )


def _archive_resolver(archive_root: Path):
    directories = {
        path.name: path
        for path in (archive_root / "archives" / "1m").iterdir()
        if path.is_dir()
    }

    def resolve(symbol: str, month: str) -> Path | None:
        exact = (
            archive_root / "archives" / "1m" / symbol
            / MonthlyArchive(symbol, "1m", month).filename
        )
        if exact.exists():
            return exact
        target = _normalized(symbol)
        ranked = sorted(
            (
                (difflib.SequenceMatcher(None, target, _normalized(name)).ratio(), name)
                for name in directories
            ),
            reverse=True,
        )
        for score, name in ranked:
            if score < 0.80:
                break
            candidate = directories[name] / f"{name}-1m-{month}.zip"
            if candidate.exists():
                return candidate
        return None

    return resolve


def _bar(symbol: str, row) -> Bar:
    return Bar(
        symbol=f"{symbol[:-4]}/USDT",
        timeframe="1m",
        open_time=datetime.fromtimestamp(row.open_time_ms / 1000, tz=timezone.utc),
        close_time=datetime.fromtimestamp(row.close_time_ms / 1000, tz=timezone.utc),
        open=float(row.open), high=float(row.high), low=float(row.low), close=float(row.close),
        volume=float(row.base_volume),
    )


def _calendar_splits(records: tuple[EvaluatedPlan, ...]) -> dict[str, dict]:
    groups = {"TRAIN": [], "VALIDATION": [], "TEST": []}
    for row in records:
        month = row.plan.created_at.strftime("%Y-%m")
        if month <= "2025-12":
            groups["TRAIN"].append(row)
        elif month <= "2026-03":
            groups["VALIDATION"].append(row)
        else:
            groups["TEST"].append(row)
    return {name: summarize(rows) for name, rows in groups.items()}


def replay(universe: dict, manifest: dict, archive_root: Path) -> dict:
    manifest_index = {
        (str(row["symbol"]), str(row["month"])): row
        for row in manifest.get("results", [])
    }
    resolve = _archive_resolver(archive_root)
    funnel = Counter()
    decisions = []
    evaluated: list[EvaluatedPlan] = []
    engine = CausalReplayEngine()
    for launch in universe.get("launches", []):
        funnel["launch_events"] += 1
        symbol = str(launch["symbol"])
        months = (str(launch["archive_month"]), _next_month(str(launch["archive_month"])))
        rows = []
        missing = []
        for month in months:
            item = manifest_index.get((symbol, month))
            path = resolve(symbol, month)
            if not item or item.get("status") != "OK" or path is None:
                missing.append(month)
                continue
            rows.extend(parse_kline_archive(path))
        if missing:
            funnel["missing_archives"] += 1
            continue
        decision = detect_launch_reversal(symbol, _time(launch["launch_time"]), rows)
        funnel[decision.status] += 1
        decision_payload = {
            "symbol": symbol,
            "status": decision.status,
            "side": decision.side.value if decision.side else None,
            "first_30m_return_pct": decision.first_30m_return_pct,
            "confirmation_time": decision.confirmation_time.isoformat() if decision.confirmation_time else None,
            "reason": decision.reason,
        }
        if decision.plan is None:
            decisions.append(decision_payload)
            continue
        execution_bars = tuple(_bar(symbol, row) for row in rows)
        result = engine.replay_plan(
            decision.plan,
            execution_bars,
            horizon_end=decision.plan.exit_by,
        )
        evaluated.append(EvaluatedPlan(plan=decision.plan, result=result))
        funnel[f"RESULT_{result.status.value}"] += 1
        decision_payload.update({
            "entry": decision.plan.entry,
            "stop": decision.plan.stop,
            "target": decision.plan.target,
            "planned_rr": decision.plan.planned_rr,
            "risk_pct": decision.plan.evidence.get("risk_pct"),
            "result": result.status.value,
            "filled_at": result.filled_at.isoformat() if result.filled_at else None,
            "exited_at": result.exited_at.isoformat() if result.exited_at else None,
            "net_r": result.net_r,
            "mfe_r": result.mfe_r,
            "mae_r": result.mae_r,
        })
        decisions.append(decision_payload)

    before_capacity = tuple(sorted(evaluated, key=lambda row: row.plan.created_at))
    capacity = enforce_portfolio_capacity(before_capacity, max_open_positions=5)
    records = capacity.records
    splits = _calendar_splits(records)
    monthly = summarize_by_calendar_month(records)
    realized_months = [row for row in monthly.values() if row["realized"]]
    validation = splits["VALIDATION"]
    test = splits["TEST"]
    development_pass = bool(
        validation["realized"] >= 10
        and test["realized"] >= 10
        and (validation["expectancy_r"] or 0) > 0
        and (test["expectancy_r"] or 0) > 0
        and (validation["profit_factor"] or 0) > 1.10
        and (test["profit_factor"] or 0) > 1.10
        and sum((row["total_net_r"] or 0) > 0 for row in realized_months) >= 4
    )
    return {
        "hypothesis": {
            "name": "FROZEN_LAUNCH_REVERSAL_V1",
            "source_interval": "1m",
            "observation": "first 30m absolute move >=10% and close in directional outer 35%",
            "confirmation": "within 60m, directional 1m close reclaims first-30m midpoint",
            "invalidation": "close 0.25 first-range beyond extreme; wick alone is soft",
            "entry": "next 1m open with 3bps slippage",
            "stop": "0.10 first-range beyond observation/confirmation extreme",
            "target": "2R",
            "timeout": "4h market exit",
            "costs": "5bps entry + 5bps exit; stop/target/time slippage modeled",
            "warning": "Development-only because this 12-month window has already been inspected",
        },
        "funnel": dict(sorted(funnel.items())),
        "before_capacity": summarize(before_capacity),
        "capacity": {
            "rejected_capacity": capacity.rejected_capacity,
            "rejected_symbol_busy": capacity.rejected_symbol_busy,
        },
        "portfolio": summarize(records),
        "calendar_splits": splits,
        "calendar_months": monthly,
        "development_assessment": {
            "passed": development_pass,
            "execution_authorized": False,
            "next_step": (
                "Freeze unchanged for a future untouched launch cohort"
                if development_pass
                else "Reject hypothesis; do not retune on this cohort"
            ),
        },
        "decisions": decisions,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, default=Path("research_data/launch_event_dataset/launch_universe.json"))
    parser.add_argument("--manifest", type=Path, default=Path("research_data/launch_event_dataset/klines_1m_manifest.json"))
    parser.add_argument("--archive-root", type=Path, default=Path("research_data/causal_futures_dataset"))
    parser.add_argument("--output", type=Path, default=Path("research_data/launch_event_dataset/frozen_launch_reversal_replay.json"))
    args = parser.parse_args()
    universe = json.loads((ROOT / args.universe).read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / args.manifest).read_text(encoding="utf-8"))
    payload = replay(universe, manifest, ROOT / args.archive_root)
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "funnel": payload["funnel"],
        "portfolio": payload["portfolio"],
        "development_assessment": payload["development_assessment"],
    }, ensure_ascii=True))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

