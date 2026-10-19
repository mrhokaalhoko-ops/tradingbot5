"""Run a descriptive, leakage-safe 72h study around USD-M contract launches.

The output is research evidence only.  It does not replay orders and must not
be connected to the live decision path without a separately preregistered,
out-of-sample strategy test.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
import statistics
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.binance_archive import MonthlyArchive, parse_kline_archive
from trading_v2.launch_events import HORIZONS_HOURS, LaunchEvent, measure_launch_event


def _parse_time(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("launch timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _event_dict(event: LaunchEvent) -> dict:
    return {
        "symbol": event.symbol,
        "launch_time": event.launch_time.isoformat(),
        "anchor_time": event.anchor_time.isoformat() if event.anchor_time else None,
        "anchor_delay_minutes": event.anchor_delay_minutes,
        "status": event.status,
        "bars_observed": event.bars_observed,
        "first_30m_return_pct": event.first_30m_return_pct,
        "first_30m_range_pct": event.first_30m_range_pct,
        "first_30m_close_location": event.first_30m_close_location,
        "first_30m_taker_buy_ratio": event.first_30m_taker_buy_ratio,
        "candidate_side": event.candidate_side,
        "returns_pct": event.returns_pct,
        "candidate_returns_pct": event.candidate_returns_pct,
        "mfe_pct_24h": event.mfe_pct_24h,
        "mae_pct_24h": event.mae_pct_24h,
        "data_gap_count_72h": event.data_gap_count_72h,
    }


def _summary(events: list[LaunchEvent]) -> dict:
    usable = [event for event in events if event.status == "OK"]
    result = {
        "events": len(events),
        "usable_events": len(usable),
        "candidate_counts": {
            side: sum(event.candidate_side == side for event in usable)
            for side in ("LONG", "SHORT", "NONE")
        },
        "anchor_delay_minutes": {
            "median": _median([event.anchor_delay_minutes for event in usable if event.anchor_delay_minutes is not None]),
            "p95": _percentile([event.anchor_delay_minutes for event in usable if event.anchor_delay_minutes is not None], 0.95),
        },
        "72h_data_gaps": {
            "events_with_any_gap": sum(event.data_gap_count_72h > 0 for event in usable),
            "total_missing_bars": sum(event.data_gap_count_72h for event in usable),
        },
        "raw_returns_pct": {},
        "candidate_returns_pct": {},
        "candidate_outcomes": {},
        "first_30m_return_buckets": {},
        "excursion_pct": {
            "mfe_24h_median": _median([event.mfe_pct_24h for event in usable if event.mfe_pct_24h is not None]),
            "mae_24h_median": _median([event.mae_pct_24h for event in usable if event.mae_pct_24h is not None]),
        },
    }
    for hours in HORIZONS_HOURS:
        key = f"{hours}h"
        raw = [event.returns_pct[key] for event in usable if event.returns_pct[key] is not None]
        result["raw_returns_pct"][key] = {
            "n": len(raw),
            "mean": _mean(raw),
            "median": _median(raw),
            "positive_rate": sum(value > 0 for value in raw) / len(raw) if raw else None,
        }
        for side in ("LONG", "SHORT"):
            values = [
                event.candidate_returns_pct[key]
                for event in usable
                if event.candidate_side == side and event.candidate_returns_pct[key] is not None
            ]
            result["candidate_returns_pct"].setdefault(key, {})[side] = {
                "n": len(values),
                "mean": _mean(values),
                "median": _median(values),
                "positive_rate": sum(value > 0 for value in values) / len(values) if values else None,
            }
    result["candidate_outcomes"] = {
        side: {
            "events": sum(event.candidate_side == side for event in usable),
            "mean_first_30m_return_pct": _mean([
                event.first_30m_return_pct
                for event in usable
                if event.candidate_side == side and event.first_30m_return_pct is not None
            ]),
            "mean_first_30m_range_pct": _mean([
                event.first_30m_range_pct
                for event in usable
                if event.candidate_side == side and event.first_30m_range_pct is not None
            ]),
        }
        for side in ("LONG", "SHORT")
    }
    buckets = (
        ("<=-20%", None, -20.0),
        ("-20..-10%", -20.0, -10.0),
        ("-10..-5%", -10.0, -5.0),
        ("-5..-2%", -5.0, -2.0),
        ("-2..2%", -2.0, 2.0),
        ("2..5%", 2.0, 5.0),
        ("5..10%", 5.0, 10.0),
        ("10..20%", 10.0, 20.0),
        (">=20%", 20.0, None),
    )
    for label, lower, upper in buckets:
        # Keep the boundary convention explicit: lower is inclusive except
        # for the first bucket, upper is exclusive except for the last.
        if label.startswith("<="):
            selected = [event for event in usable if event.first_30m_return_pct is not None and event.first_30m_return_pct <= upper]
        elif label.startswith(">="):
            selected = [event for event in usable if event.first_30m_return_pct is not None and event.first_30m_return_pct >= lower]
        else:
            selected = [event for event in usable if event.first_30m_return_pct is not None and lower <= event.first_30m_return_pct < upper]
        result["first_30m_return_buckets"][label] = {
            "events": len(selected),
            "mean_return_4h_pct": _mean([
                event.returns_pct["4h"] for event in selected if event.returns_pct["4h"] is not None
            ]),
            "mean_return_24h_pct": _mean([
                event.returns_pct["24h"] for event in selected if event.returns_pct["24h"] is not None
            ]),
            "positive_24h_rate": (
                sum(event.returns_pct["24h"] > 0 for event in selected if event.returns_pct["24h"] is not None)
                / sum(event.returns_pct["24h"] is not None for event in selected)
                if any(event.returns_pct["24h"] is not None for event in selected)
                else None
            ),
        }
    return result


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _split(events: list[LaunchEvent]) -> dict[str, list[LaunchEvent]]:
    # Calendar splits are fixed before viewing outcomes.  This prevents a
    # launch rule from being selected on the same dates used to assess it.
    rows = {"TRAIN": [], "VALIDATION": [], "TEST": []}
    for event in events:
        month = event.launch_time.strftime("%Y-%m")
        if month <= "2025-12":
            rows["TRAIN"].append(event)
        elif month <= "2026-03":
            rows["VALIDATION"].append(event)
        else:
            rows["TEST"].append(event)
    return rows


def _load_events(
    universe: dict,
    manifest: dict,
    archive_root: Path,
    threshold: float,
    timeframe: str,
) -> list[LaunchEvent]:
    indexed = {(row["symbol"], row["month"]): row for row in manifest.get("results", [])}
    archive_dirs = {
        path.name: path
        for path in (archive_root / "archives" / timeframe).iterdir()
        if path.is_dir()
    }
    path_cache: dict[tuple[str, str], Path | None] = {}

    def resolve_path(symbol: str, month: str) -> Path | None:
        key = (symbol, month)
        if key in path_cache:
            return path_cache[key]
        exact = (
            archive_root / "archives" / timeframe / symbol
            / MonthlyArchive(symbol, timeframe, month).filename
        )
        if exact.exists():
            path_cache[key] = exact
            return exact
        # A few Binance symbols contain non-ASCII text.  The current
        # ExchangeInfo JSON has occasionally exposed replacement characters;
        # resolve only the local archive path by close Unicode match, never by
        # changing the symbol in the event metadata.
        def normalized(value: str) -> str:
            return "".join(
                char for char in unicodedata.normalize("NFKC", value).casefold()
                if char.isalnum()
            )
        target = normalized(symbol)
        ranked = sorted(
            ((difflib.SequenceMatcher(None, target, normalized(name)).ratio(), name) for name in archive_dirs),
            reverse=True,
        )
        for score, name in ranked:
            if score < 0.80:
                break
            candidate = archive_dirs[name] / f"{name}-{timeframe}-{month}.zip"
            if candidate.exists():
                path_cache[key] = candidate
                return candidate
        path_cache[key] = None
        return None

    events: list[LaunchEvent] = []
    for row in universe.get("launches", []):
        symbol = str(row["symbol"]).upper()
        launch_time = _parse_time(row["launch_time"])
        months = (row["archive_month"], _next_month(row["archive_month"]))
        bars = []
        missing = []
        for month in months:
            item = indexed.get((symbol, month))
            path = resolve_path(symbol, month)
            if not item or item.get("status") != "OK" or path is None:
                missing.append(month)
                continue
            bars.extend(parse_kline_archive(path))
        if missing:
            # Still measure any available month; an incomplete status is more
            # useful than silently dropping a launch from the denominator.
            pass
        events.append(measure_launch_event(
            symbol,
            launch_time,
            bars,
            classification_return_threshold_pct=threshold,
            bar_minutes=int(timeframe[:-1]),
        ))
    return events


def _next_month(month: str) -> str:
    year, number = (int(value) for value in month.split("-"))
    number += 1
    if number == 13:
        year, number = year + 1, 1
    return f"{year:04d}-{number:02d}"


def _markdown(payload: dict) -> str:
    lines = [
        "# USD-M launch event study (research only)",
        "",
        "This report is descriptive evidence. It does not authorize live orders or claim a profitable launch strategy.",
        "",
        f"- Events: **{payload['summary']['events']}**; usable: **{payload['summary']['usable_events']}**",
        f"- Frozen event gate: first 30m return >= ±{payload['method']['classification_return_threshold_pct']:.1f}% + close location/taker-buy confirmation",
        f"- Source interval: {payload['method']['timeframe']}; entry for labels is the next bar open after the 30m observation.",
        "- Calendar split: TRAIN through 2025-12, VALIDATION 2026-01..03, TEST 2026-04 onward.",
        "",
        "## Candidate counts and forward directional returns",
        "",
        "| Horizon | LONG n / mean / positive | SHORT n / mean / positive |",
        "|---|---:|---:|",
    ]
    for hours in HORIZONS_HOURS:
        row = payload["summary"]["candidate_returns_pct"][f"{hours}h"]
        def fmt(side: str) -> str:
            item = row[side]
            return f"{item['n']} / {item['mean']:.3f}% / {item['positive_rate']:.1%}" if item["n"] else "0 / n/a / n/a"
        lines.append(f"| {hours}h | {fmt('LONG')} | {fmt('SHORT')} |")
    lines.extend(["", "## Calendar split summary", ""])
    for name, summary in payload["splits"].items():
        lines.append(f"- **{name}**: {summary['events']} events, {summary['usable_events']} usable; candidates {summary['candidate_counts']}")
        for hours in HORIZONS_HOURS:
            values = summary["candidate_returns_pct"][f"{hours}h"]
            lines.append(f"  - {hours}h LONG mean={values['LONG']['mean']}, SHORT mean={values['SHORT']['mean']}")
    lines.extend(["", "## First-30m return buckets (raw descriptive view)", "", "| Bucket | Events | Mean 4h | Mean 24h | Positive 24h |", "|---|---:|---:|---:|---:|"])
    for bucket, row in payload["summary"]["first_30m_return_buckets"].items():
        fmt = lambda value: f"{value:.3f}%" if value is not None else "n/a"
        rate = f"{row['positive_24h_rate']:.1%}" if row["positive_24h_rate"] is not None else "n/a"
        lines.append(f"| {bucket} | {row['events']} | {fmt(row['mean_return_4h_pct'])} | {fmt(row['mean_return_24h_pct'])} | {rate} |")
    lines.extend(["", "## Limitations", "", "- The launch universe is based on the first available Vision 1h archive and current ExchangeInfo; delisted contracts are a survivorship limitation.", "- A 5m OHLC bar cannot determine intrabar ordering. No SL/TP win rate is inferred here.", "- Thresholds are frozen for measurement, not optimized on these outcomes. A strategy would still require a separate walk-forward and cost-aware replay.", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, default=Path("research_data/launch_event_dataset/launch_universe.json"))
    parser.add_argument("--manifest", type=Path, default=Path("research_data/launch_event_dataset/klines_manifest.json"))
    parser.add_argument("--archive-root", type=Path, default=Path("research_data/causal_futures_dataset"))
    parser.add_argument("--output", type=Path, default=Path("research_data/launch_event_dataset/launch_event_study.json"))
    parser.add_argument("--report", type=Path, default=Path("research_data/launch_event_dataset/launch_event_study.md"))
    parser.add_argument("--threshold-pct", type=float, default=5.0)
    parser.add_argument("--timeframe", choices=("1m", "5m"), default="5m")
    args = parser.parse_args()
    universe = json.loads((ROOT / args.universe).read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / args.manifest).read_text(encoding="utf-8"))
    archive_root = ROOT / args.archive_root
    events = _load_events(
        universe,
        manifest,
        archive_root,
        args.threshold_pct,
        args.timeframe,
    )
    splits = _split(events)
    payload = {
        "dataset": "Binance USD-M launch event study",
        "source": "https://data.binance.vision/data/futures/um/monthly/klines/",
        "method": {
            "timeframe": args.timeframe,
            "decision_information": f"first 30 minutes of closed {args.timeframe} bars after first observed 1h launch bar",
            "entry_label": f"next {args.timeframe} open after observation",
            "horizons_hours": list(HORIZONS_HOURS),
            "classification_return_threshold_pct": args.threshold_pct,
            "classification": "LONG if return>=threshold, close_location>=0.60, taker_buy_ratio>=0.55; SHORT symmetric",
            "warning": "Descriptive research only; no live execution authority",
        },
        "summary": _summary(events),
        "splits": {name: _summary(rows) for name, rows in splits.items()},
        "events": [_event_dict(event) for event in events],
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report = ROOT / args.report
    report.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps({"events": len(events), "usable": payload["summary"]["usable_events"], "candidates": payload["summary"]["candidate_counts"]}, ensure_ascii=True))
    print(output)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
