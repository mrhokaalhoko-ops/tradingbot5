"""Causal 1m audit for runtime setup theses exported by TradingBotv3.

This is a research-only CLI.  It reads decision-audit ZIP exports and public
Binance USD-M Demo klines; it never imports the live runtime, credentials, or
order methods.  The first phase extracts independent theses, downloads the
matching 1m paths, and proves that those paths aggregate back to the 5m/15m
candles recorded by the bot before any counterfactual replay is attempted.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.data import BinanceUsdMFuturesDataSource, normalize_symbol
from trading_v2.models import Bar
from utils.runtime_candidate_replay import load_decision_events


DEMO_FAPI_BASE_URL = "https://demo-fapi.binance.com"


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("timestamp is required")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _event_thesis_maps(events: Iterable[dict]) -> tuple[dict[str, str], dict[str, str]]:
    by_decision: dict[str, str] = {}
    by_trade: dict[str, str] = {}
    for event in events:
        if event.get("stage") != "HYBRID_DECISION":
            continue
        thesis_id = str((event.get("details") or {}).get("setup_thesis_id") or "")
        if not thesis_id:
            continue
        decision_id = str(event.get("decision_id") or "")
        trade_id = str(event.get("trade_id") or "")
        if decision_id:
            by_decision[decision_id] = thesis_id
        if trade_id:
            by_trade[trade_id] = thesis_id
    return by_decision, by_trade


def _thesis_id(event: dict, by_decision: dict[str, str], by_trade: dict[str, str]) -> str:
    direct = str(event.get("thesis_id") or event.get("setup_id") or "")
    if direct.startswith("setup_"):
        return direct
    decision_id = str(event.get("decision_id") or "")
    trade_id = str(event.get("trade_id") or "")
    return by_decision.get(decision_id) or by_trade.get(trade_id) or ""


def extract_theses(events: list[dict]) -> list[dict]:
    """Return one causal timeline per project-defined ``setup_thesis_id``."""
    by_decision, by_trade = _event_thesis_maps(events)
    market_context_by_decision = {
        str(event.get("decision_id")): event
        for event in events
        if event.get("stage") == "MARKET_CONTEXT" and event.get("decision_id")
    }
    grouped: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        thesis_id = _thesis_id(event, by_decision, by_trade)
        if thesis_id:
            grouped[thesis_id].append(event)

    theses: list[dict] = []
    for thesis_id, rows in grouped.items():
        rows.sort(key=lambda item: str(item.get("timestamp") or ""))
        signals = [
            row for row in rows
            if row.get("stage") == "HYBRID_DECISION"
            and row.get("status") in {"BUY", "SELL"}
        ]
        if not signals:
            continue
        first_signal = signals[0]
        first_context = market_context_by_decision.get(
            str(first_signal.get("decision_id") or "")
        )
        context_details = dict((first_context or {}).get("details") or {})
        setup_context = dict(context_details.get("setup_context") or {})
        anchor = dict(setup_context.get("setup_anchor") or {})
        side = str(first_signal.get("status") or "").upper()
        zone_low = float(anchor.get("low") or setup_context.get("ob_low") or 0.0)
        zone_high = float(anchor.get("high") or setup_context.get("ob_high") or 0.0)
        midpoint = float(
            setup_context.get("optimal_entry")
            or ((zone_low + zone_high) / 2.0 if zone_low > 0 and zone_high > 0 else 0.0)
        )
        proximal_edge = zone_high if side == "BUY" else zone_low
        rr_rows = [row for row in rows if row.get("stage") == "RR_FILTER"]
        quality_rows = [row for row in rows if row.get("stage") == "QUALITY_FILTER"]
        touch_rows = [row for row in rows if row.get("stage") == "ENTRY_ZONE_TOUCH"]
        confirmation_rows = [
            row for row in rows if row.get("stage") == "ENTRY_CONFIRMATION"
        ]
        sniper_rows = [row for row in rows if row.get("stage") == "SNIPER"]
        created_at = parse_utc(str(first_signal.get("timestamp")))
        theses.append({
            "thesis_id": thesis_id,
            "symbol": str(first_signal.get("symbol") or ""),
            "side": side,
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "first_decision_id": first_signal.get("decision_id"),
            "signal_event_count": len(signals),
            "zone_low": zone_low,
            "zone_high": zone_high,
            "proximal_edge": proximal_edge,
            "midpoint": midpoint,
            "invalidation": float(setup_context.get("invalidation_level") or 0.0),
            "atr_at_creation": float(
                (context_details.get("indicators") or {}).get("atr") or 0.0
            ),
            "setup_context": setup_context,
            "market_levels": context_details.get("market_levels") or {},
            "quality_evaluations": len(quality_rows),
            "quality_passes": sum(row.get("status") == "PASS" for row in quality_rows),
            "rr_evaluations": len(rr_rows),
            "rr_passes": sum(row.get("status") == "PASS" for row in rr_rows),
            "touches": len(touch_rows),
            "confirmation_evaluations": len(confirmation_rows),
            "confirmation_passes": sum(
                row.get("status") == "PASS" for row in confirmation_rows
            ),
            "sniper_terminal_statuses": [
                row.get("status") for row in sniper_rows
                if row.get("status") != "PENDING"
            ],
            "rr_snapshots": [
                {
                    "timestamp": row.get("timestamp"),
                    "sequence": row.get("sequence"),
                    "decision_id": row.get("decision_id"),
                    "trade_id": row.get("trade_id"),
                    "status": row.get("status"),
                    "details": row.get("details") or {},
                    "market_context": (
                        market_context_by_decision.get(
                            str(row.get("decision_id") or ""), {}
                        ).get("details")
                        or {}
                    ),
                }
                for row in rr_rows
            ],
            "touch_events": [
                {
                    "timestamp": row.get("timestamp"),
                    "status": row.get("status"),
                    "details": row.get("details") or {},
                }
                for row in touch_rows
            ],
            "confirmation_events": [
                {
                    "timestamp": row.get("timestamp"),
                    "status": row.get("status"),
                    "details": row.get("details") or {},
                }
                for row in confirmation_rows
            ],
        })
    return sorted(theses, key=lambda row: (row["created_at"], row["thesis_id"]))


def required_symbol_windows(
    theses: Iterable[dict], *, warmup_hours: float, horizon_hours: float
) -> dict[str, tuple[datetime, datetime]]:
    windows: dict[str, tuple[datetime, datetime]] = {}
    for thesis in theses:
        symbol = str(thesis["symbol"])
        created = parse_utc(thesis["created_at"])
        start = created - timedelta(hours=float(warmup_hours))
        end = created + timedelta(hours=float(horizon_hours))
        previous = windows.get(symbol)
        windows[symbol] = (
            min(start, previous[0]) if previous else start,
            max(end, previous[1]) if previous else end,
        )
    return windows


def download_demo_1m(
    windows: dict[str, tuple[datetime, datetime]], *, cache_root: Path
) -> dict:
    source = BinanceUsdMFuturesDataSource(base_url=DEMO_FAPI_BASE_URL)
    rows = []
    for index, (symbol, (start, end)) in enumerate(sorted(windows.items()), start=1):
        clean = normalize_symbol(symbol)
        output = cache_root / f"{clean}_1m.jsonl.gz"
        bars = source.fetch(
            symbol=symbol,
            timeframe="1m",
            start=start,
            end=end,
        )
        source.write_cache(output, bars)
        rows.append({
            "symbol": symbol,
            "normalized_symbol": clean,
            "requested_start": start.isoformat().replace("+00:00", "Z"),
            "requested_end": end.isoformat().replace("+00:00", "Z"),
            "rows": len(bars),
            "first_open_time": (
                bars[0].open_time.isoformat().replace("+00:00", "Z") if bars else None
            ),
            "last_close_time": (
                bars[-1].close_time.isoformat().replace("+00:00", "Z") if bars else None
            ),
            "path": str(output),
            "sha256": _sha256(output),
        })
        print(f"downloaded {index}/{len(windows)}: {symbol} ({len(bars)} bars)", flush=True)
    return {
        "source": "BINANCE_USDM_DEMO_REST",
        "base_url": DEMO_FAPI_BASE_URL,
        "timeframe": "1m",
        "symbols": rows,
    }


def load_cached_1m(manifest: dict) -> dict[str, tuple[Bar, ...]]:
    source = BinanceUsdMFuturesDataSource(base_url=DEMO_FAPI_BASE_URL)
    result = {}
    for item in manifest.get("symbols", []):
        path = Path(item["path"])
        if _sha256(path) != item.get("sha256"):
            raise ValueError(f"cache checksum mismatch: {path}")
        result[str(item["symbol"])] = source.read_cache(path)
    return result


def aggregate_bars(
    bars: Iterable[Bar], *, opened_at: datetime, minutes: int
) -> dict | None:
    opened_at = parse_utc(opened_at)
    end = opened_at + timedelta(minutes=int(minutes))
    selected = [bar for bar in bars if opened_at <= bar.open_time < end]
    if len(selected) != int(minutes):
        return None
    selected.sort(key=lambda bar: bar.open_time)
    expected = [opened_at + timedelta(minutes=index) for index in range(int(minutes))]
    if [bar.open_time for bar in selected] != expected:
        return None
    return {
        "open": float(selected[0].open),
        "high": max(float(bar.high) for bar in selected),
        "low": min(float(bar.low) for bar in selected),
        "close": float(selected[-1].close),
        "volume": sum(float(bar.volume) for bar in selected),
        "rows": len(selected),
    }


def _close_enough(left: float, right: float) -> bool:
    scale = max(abs(float(left)), abs(float(right)), 1.0)
    return abs(float(left) - float(right)) <= max(1e-10, scale * 1e-9)


def verify_recorded_candles(events: list[dict], cached: dict[str, tuple[Bar, ...]]) -> dict:
    comparisons = []
    for event in events:
        stage = event.get("stage")
        symbol = str(event.get("symbol") or "")
        if symbol not in cached:
            continue
        recorded = None
        minutes = None
        if stage == "MARKET_BAR":
            details = event.get("details") or {}
            if details.get("timeframe") != "15m":
                continue
            recorded = details
            minutes = 15
        elif stage == "MARKET_CONTEXT":
            details = event.get("details") or {}
            closed_bar = details.get("closed_bar") or {}
            if details.get("timeframe") != "15m" or not closed_bar.get("open_time"):
                continue
            recorded = closed_bar
            minutes = 15
        elif stage == "ENTRY_CONFIRMATION":
            confirmation = (event.get("details") or {}).get("confirmation") or {}
            closed_bar = confirmation.get("closed_bar") or {}
            if confirmation.get("timeframe") != "5m" or not closed_bar.get("open_time"):
                continue
            recorded = closed_bar
            minutes = 5
        if not recorded or not recorded.get("open_time"):
            continue
        opened_at = parse_utc(recorded["open_time"])
        aggregated = aggregate_bars(
            cached[symbol], opened_at=opened_at, minutes=int(minutes)
        )
        if aggregated is None:
            continue
        fields = ("open", "high", "low", "close", "volume")
        mismatched = [
            field for field in fields
            if not _close_enough(float(recorded.get(field) or 0.0), aggregated[field])
        ]
        comparisons.append({
            "symbol": symbol,
            "stage": stage,
            "timeframe": f"{minutes}m",
            "open_time": opened_at.isoformat().replace("+00:00", "Z"),
            "matched": not mismatched,
            "mismatched_fields": mismatched,
            "recorded": {field: float(recorded.get(field) or 0.0) for field in fields},
            "aggregated_1m": aggregated,
        })
    return {
        "comparison_count": len(comparisons),
        "matched": sum(row["matched"] for row in comparisons),
        "mismatched": sum(not row["matched"] for row in comparisons),
        "by_timeframe": {
            timeframe: {
                "compared": sum(row["timeframe"] == timeframe for row in comparisons),
                "matched": sum(
                    row["timeframe"] == timeframe and row["matched"]
                    for row in comparisons
                ),
            }
            for timeframe in ("5m", "15m")
        },
        "comparisons": comparisons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract runtime theses and validate Binance Demo 1m paths."
    )
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("research_data/runtime_thesis_replay_20260830"),
    )
    parser.add_argument("--warmup-hours", type=float, default=3.0)
    parser.add_argument("--horizon-hours", type=float, default=24.0)
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Reuse the existing checksummed cache manifest.",
    )
    args = parser.parse_args()

    output_root = ROOT / args.output_root
    events = load_decision_events(args.archives)
    theses = extract_theses(events)
    windows = required_symbol_windows(
        theses,
        warmup_hours=args.warmup_hours,
        horizon_hours=args.horizon_hours,
    )
    thesis_payload = {
        "schema_version": 1,
        "source_archives": [str(path) for path in args.archives],
        "event_count": len(events),
        "thesis_count": len(theses),
        "symbol_count": len(windows),
        "warmup_hours": args.warmup_hours,
        "horizon_hours": args.horizon_hours,
        "theses": theses,
    }
    _atomic_json(output_root / "theses.json", thesis_payload)

    manifest_path = output_root / "demo_1m_manifest.json"
    if args.skip_download:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = download_demo_1m(windows, cache_root=output_root / "bars_1m")
        _atomic_json(manifest_path, manifest)

    verification = verify_recorded_candles(events, load_cached_1m(manifest))
    _atomic_json(output_root / "source_verification.json", verification)
    print(
        f"theses={len(theses)} symbols={len(windows)} "
        f"comparisons={verification['comparison_count']} "
        f"matched={verification['matched']} mismatched={verification['mismatched']}",
        flush=True,
    )
    return 0 if verification["mismatched"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
