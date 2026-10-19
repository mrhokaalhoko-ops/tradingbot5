"""Measure directional edge at each runtime funnel stage on verified Demo 1m."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_runtime_theses_1m import load_cached_1m, parse_utc
from trading_v2.models import Bar


HORIZONS_MINUTES = (30, 60, 120)
ROUND_TRIP_COST_BPS = 16.0


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def directional_path(
    bars: tuple[Bar, ...], *, at: datetime, side: str, minutes: int
) -> dict | None:
    at = parse_utc(at)
    entry_bar = next((bar for bar in bars if bar.open_time >= at), None)
    if entry_bar is None:
        return None
    end = at + timedelta(minutes=int(minutes))
    visible = [
        bar for bar in bars
        if bar.open_time >= entry_bar.open_time and bar.close_time <= end
    ]
    if not visible or visible[-1].close_time < end - timedelta(minutes=1):
        return None
    entry = float(entry_bar.open)
    exit_price = float(visible[-1].close)
    direction = 1.0 if str(side).upper() == "BUY" else -1.0
    signed_return = direction * (exit_price / entry - 1.0)
    if direction > 0:
        mfe = max(float(bar.high) / entry - 1.0 for bar in visible)
        mae = max(1.0 - float(bar.low) / entry for bar in visible)
    else:
        mfe = max(1.0 - float(bar.low) / entry for bar in visible)
        mae = max(float(bar.high) / entry - 1.0 for bar in visible)
    return {
        "entry_at": _iso(entry_bar.open_time),
        "end_at": _iso(visible[-1].close_time),
        "entry": entry,
        "exit": exit_price,
        "signed_return_pct": signed_return * 100.0,
        "net_after_assumed_cost_pct": signed_return * 100.0 - ROUND_TRIP_COST_BPS / 100.0,
        "mfe_pct": max(0.0, mfe) * 100.0,
        "mae_pct": max(0.0, mae) * 100.0,
    }


def cohort_rows(theses: list[dict]) -> list[dict]:
    rows = []
    for thesis in theses:
        rows.append({
            "cohort": "RAW_THESIS",
            "thesis_id": thesis["thesis_id"],
            "symbol": thesis["symbol"],
            "side": thesis["side"],
            "at": thesis["created_at"],
        })
        snapshots = thesis.get("rr_snapshots") or []
        if snapshots:
            rows.append({
                "cohort": "QUALITY_PASSED_TO_RR",
                "thesis_id": thesis["thesis_id"],
                "symbol": thesis["symbol"],
                "side": thesis["side"],
                "at": snapshots[0]["timestamp"],
            })
        first_pass = next(
            (snapshot for snapshot in snapshots if snapshot.get("status") == "PASS"),
            None,
        )
        if first_pass:
            rows.append({
                "cohort": "RR_PASSED",
                "thesis_id": thesis["thesis_id"],
                "symbol": thesis["symbol"],
                "side": thesis["side"],
                "at": first_pass["timestamp"],
            })
    return rows


def summarize(rows: list[dict]) -> dict:
    grouped = defaultdict(list)
    for row in rows:
        path = row.get("path")
        if path is not None:
            grouped[(row["cohort"], row["side"], row["horizon_minutes"])].append(path)
            grouped[(row["cohort"], "ALL", row["horizon_minutes"])].append(path)
    summary = {}
    for (cohort, side, horizon), paths in sorted(grouped.items()):
        returns = [item["signed_return_pct"] for item in paths]
        net = [item["net_after_assumed_cost_pct"] for item in paths]
        key = f"{cohort}|{side}|{horizon}m"
        summary[key] = {
            "cohort": cohort,
            "side": side,
            "horizon_minutes": horizon,
            "count": len(paths),
            "mean_signed_return_pct": statistics.fmean(returns),
            "median_signed_return_pct": statistics.median(returns),
            "directional_hit_rate": sum(value > 0 for value in returns) / len(returns),
            "mean_net_after_assumed_cost_pct": statistics.fmean(net),
            "mean_mfe_pct": statistics.fmean(item["mfe_pct"] for item in paths),
            "mean_mae_pct": statistics.fmean(item["mae_pct"] for item in paths),
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze runtime funnel directional edge.")
    parser.add_argument(
        "--research-root", type=Path,
        default=Path("research_data/runtime_thesis_replay_20260830"),
    )
    args = parser.parse_args()
    root = ROOT / args.research_root
    theses_payload = json.loads((root / "theses.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "demo_1m_manifest.json").read_text(encoding="utf-8"))
    verification = json.loads((root / "source_verification.json").read_text(encoding="utf-8"))
    if verification.get("mismatched"):
        raise RuntimeError("source verification failed")
    cached = load_cached_1m(manifest)

    rows = []
    for cohort in cohort_rows(theses_payload["theses"]):
        bars = cached.get(cohort["symbol"], ())
        for horizon in HORIZONS_MINUTES:
            rows.append({
                **cohort,
                "horizon_minutes": horizon,
                "path": directional_path(
                    bars, at=parse_utc(cohort["at"]), side=cohort["side"], minutes=horizon
                ),
            })
    summary = summarize(rows)
    payload = {
        "schema_version": 1,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "round_trip_cost_bps_assumption": ROUND_TRIP_COST_BPS,
        "summary": summary,
        "rows": rows,
    }
    _atomic_json(root / "funnel_forward_returns.json", payload)

    lines = [
        "# Funnel Forward Returns — Verified Binance Demo 1m",
        "",
        f"Assumed round-trip cost for the net diagnostic: **{ROUND_TRIP_COST_BPS:.1f} bps**.",
        "",
        "| Cohort | Side | Horizon | N | Mean signed | Median | Hit rate | Mean net | MFE | MAE |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary.values():
        lines.append(
            f"| {item['cohort']} | {item['side']} | {item['horizon_minutes']}m | "
            f"{item['count']} | {item['mean_signed_return_pct']:.3f}% | "
            f"{item['median_signed_return_pct']:.3f}% | {item['directional_hit_rate']:.1%} | "
            f"{item['mean_net_after_assumed_cost_pct']:.3f}% | "
            f"{item['mean_mfe_pct']:.3f}% | {item['mean_mae_pct']:.3f}% |"
        )
    (root / "funnel_forward_returns.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    for key, item in summary.items():
        if item["side"] == "ALL":
            print(key, item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
