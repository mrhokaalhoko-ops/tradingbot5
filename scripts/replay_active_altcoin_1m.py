"""Replay the frozen active-altcoin events on official 1m OHLC archives.

Research only.  The script never imports an exchange client or an order API.
It intentionally evaluates a small, pre-registered event cohort instead of
searching thresholds after seeing the outcomes.
"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.active_altcoin_scalper import (  # noqa: E402
    ActiveAltcoinConfig,
    enforce_capacity,
    select_hot_events,
    summarize_records,
)
from trading_v2.binance_archive import parse_kline_archive  # noqa: E402


FEATURE_ROOT = ROOT / "research_data" / "causal_futures_dataset" / "features"
ARCHIVE_ROOT = ROOT / "research_data" / "active_altcoin_1m" / "archives"
MANIFEST = ROOT / "research_data" / "active_altcoin_1m" / "manifest.json"
OUTPUT = ROOT / "research_data" / "active_altcoin_1m" / "replay_result.json"
LEDGER_OUTPUT = ROOT / "research_data" / "active_altcoin_1m" / "replay_ledger.parquet"
MONTHS = tuple(f"2025-{month:02d}" for month in range(8, 13)) + tuple(
    f"2026-{month:02d}" for month in range(1, 8)
)
SPLITS = {
    "TRAIN": set(MONTHS[:8]),
    "VALIDATION": set(MONTHS[8:10]),
    "TEST": set(MONTHS[10:]),
}


def _feature_events(month: str, config: ActiveAltcoinConfig) -> pd.DataFrame:
    columns = [
        "open_time", "close_time", "open", "high", "low", "close", "symbol",
        "observation_valid", "return_1_pct", "return_4_pct", "volume_ratio",
        "quote_volume", "body_pct", "close_location", "atr", "global_regime",
    ]
    paths = sorted((FEATURE_ROOT / month).glob("*.parquet"))
    frames = [pd.read_parquet(path, columns=columns) for path in paths]
    if not frames:
        return pd.DataFrame(columns=columns)
    frame = pd.concat(frames, ignore_index=True)
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True)
    return select_hot_events(frame, config).query("event_score >= 8").copy()


def _archive_map() -> dict[tuple[str, str], Path]:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    result: dict[tuple[str, str], Path] = {}
    for row in payload["results"]:
        if row.get("status") != "OK":
            continue
        symbol, day = str(row["symbol"]), str(row["day"])
        path = ARCHIVE_ROOT / symbol / f"{symbol}-1m-{day}.zip"
        if path.exists():
            result[(symbol, day)] = path
    return result


def _read_archive(path: Path) -> pd.DataFrame:
    rows = list(parse_kline_archive(path))
    frame = pd.DataFrame(
        [
            {
                "open_time": pd.to_datetime(row.open_time_ms, unit="ms", utc=True),
                "close_time": pd.to_datetime(row.close_time_ms, unit="ms", utc=True),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.base_volume),
                "quote_volume": float(row.quote_volume),
                "trades": int(row.trades),
                "taker_buy_quote_volume": float(row.taker_buy_quote_volume),
            }
            for row in rows
        ]
    )
    return frame.sort_values("open_time").reset_index(drop=True)


def _bar_outcome(side: str, bar: pd.Series, stop: float, target: float) -> str | None:
    if side == "LONG":
        stop_hit = float(bar["low"]) <= stop
        target_hit = float(bar["high"]) >= target
    else:
        stop_hit = float(bar["high"]) >= stop
        target_hit = float(bar["low"]) <= target
    if stop_hit and target_hit:
        return "AMBIGUOUS_AS_LOSS"
    if stop_hit:
        return "SL_FIRST"
    if target_hit:
        return "TP_FIRST"
    return None


def _finish(event: pd.Series, bars: pd.DataFrame, entry_index: int, entry: float,
            stop: float, target: float, variant: str, config: ActiveAltcoinConfig) -> dict:
    side = str(event["side"])
    risk = abs(entry - stop)
    direction = 1.0 if side == "LONG" else -1.0
    # The frozen parent protocol expresses holding_bars in 5m bars (12 = 60m).
    # This replay operates on 1m bars, so preserve the same wall-clock horizon.
    holding_minutes = config.holding_bars * 5
    path = bars.iloc[entry_index:entry_index + holding_minutes]
    if path.empty:
        return {"variant": variant, "symbol": event["symbol"], "side": side,
                "signal_time": event["close_time"], "status": "NO_FORWARD_DATA"}
    status = "TIME_EXIT"
    exit_price = float(path.iloc[-1]["close"])
    exited_at = path.iloc[-1]["close_time"]
    mfe_r = 0.0
    mae_r = 0.0
    for _, bar in path.iterrows():
        if side == "LONG":
            mfe_r = max(mfe_r, (float(bar["high"]) - entry) / risk)
            mae_r = max(mae_r, (entry - float(bar["low"])) / risk)
        else:
            mfe_r = max(mfe_r, (entry - float(bar["low"])) / risk)
            mae_r = max(mae_r, (float(bar["high"]) - entry) / risk)
        outcome = _bar_outcome(side, bar, stop, target)
        if outcome:
            status = outcome
            exit_price = stop if outcome != "TP_FIRST" else target
            exited_at = bar["close_time"]
            break
    gross_r = direction * (exit_price - entry) / risk
    if status == "AMBIGUOUS_AS_LOSS":
        gross_r = -1.0
    cost_r = (config.round_trip_cost_bps / 10_000.0 * entry) / risk
    return {
        "variant": variant,
        "symbol": str(event["symbol"]),
        "side": side,
        "signal_time": event["close_time"],
        "filled_at": bars.iloc[entry_index]["open_time"],
        "exited_at": exited_at,
        "entry": entry,
        "stop": stop,
        "target": target,
        "status": status,
        "gross_r": float(gross_r),
        "net_r": float(gross_r - cost_r),
        "mfe_r": float(mfe_r),
        "mae_r": float(mae_r),
        "global_regime": str(event.get("global_regime", "UNKNOWN")),
        "event_score": float(event["event_score"]),
        "return_5m_pct": float(event["return_1_pct"]),
        "return_20m_pct": float(event["return_4_pct"]),
        "volume_ratio": float(event["volume_ratio"]),
        "quote_volume_5m": float(event["quote_volume"]),
        "atr_pct": float(event["atr"]) / float(event["close"]) * 100.0,
    }


def _micro_features(event: pd.Series, bars: pd.DataFrame, signal_index: int) -> dict:
    """Return features available at the completed event close only."""
    recent = bars.iloc[max(0, signal_index - 4):signal_index + 1]
    history = bars.iloc[max(0, signal_index - 20):signal_index + 1]
    if recent.empty:
        return {}
    direction = 1.0 if str(event["side"]) == "LONG" else -1.0
    closes = recent["close"].astype(float)
    opens = recent["open"].astype(float)
    ranges = (recent["high"].astype(float) - recent["low"].astype(float)).clip(lower=0)
    volumes = recent["volume"].astype(float).clip(lower=0)
    hist_volumes = history["volume"].astype(float).clip(lower=0)
    close_location = ((recent["close"].astype(float) - recent["low"].astype(float)) /
                      ranges.replace(0, np.nan)).fillna(0.5)
    bar_returns = (closes / opens - 1.0) * 100.0
    signed_bar_returns = direction * bar_returns
    close_to_close_returns = direction * closes.pct_change().fillna(0.0) * 100.0
    quote_volumes = recent.get("quote_volume", volumes).astype(float).clip(lower=0)
    taker_buy_quote = recent.get(
        "taker_buy_quote_volume", quote_volumes * 0.5
    ).astype(float).clip(lower=0)
    raw_imbalance = pd.Series(
        np.where(
            quote_volumes > 0,
            2.0 * taker_buy_quote / quote_volumes - 1.0,
            0.0,
        ),
        index=recent.index,
    ).clip(-1.0, 1.0)
    directional_imbalance = direction * raw_imbalance
    # Positive when aggressive flow and the candle body agree with the setup.
    flow_price_product = directional_imbalance * signed_bar_returns
    # Aggressive flow against the setup while price nevertheless holds in its
    # direction is a compact proxy for absorption. It is descriptive only and
    # is never used with future bars.
    directional_absorption = (
        (-directional_imbalance).clip(lower=0.0)
        * signed_bar_returns.clip(lower=0.0)
    )
    return {
        "micro_5m_range_pct": float((recent["high"].max() / recent["low"].min() - 1.0) * 100.0),
        "micro_5m_return_pct": float((closes.iloc[-1] / opens.iloc[0] - 1.0) * 100.0),
        "micro_directional_5m_return_pct": float(direction * (closes.iloc[-1] / opens.iloc[0] - 1.0) * 100.0),
        "micro_1m_return_pct": float(direction * (closes.iloc[-1] / opens.iloc[-1] - 1.0) * 100.0),
        "micro_directional_bar_fraction": float((signed_bar_returns > 0).mean()),
        "micro_close_location_mean": float((close_location if direction > 0 else 1.0 - close_location).mean()),
        "micro_last_close_location": float(close_location.iloc[-1] if direction > 0 else 1.0 - close_location.iloc[-1]),
        "micro_volume_concentration": float(volumes.max() / volumes.sum()) if volumes.sum() > 0 else 0.0,
        "micro_last_volume_ratio": float(volumes.iloc[-1] / hist_volumes.iloc[:-1].mean()) if len(hist_volumes) > 1 and hist_volumes.iloc[:-1].mean() > 0 else 0.0,
        "micro_range_atr": float((recent["high"].max() - recent["low"].min()) / float(event["atr"])) if float(event["atr"]) > 0 else 0.0,
        "micro_signed_return_dispersion": float(close_to_close_returns.std(ddof=0)),
        "micro_directional_taker_imbalance_mean": float(directional_imbalance.mean()),
        "micro_last_directional_taker_imbalance": float(directional_imbalance.iloc[-1]),
        "micro_flow_price_agreement": float(flow_price_product.mean()),
        "micro_directional_absorption": float(directional_absorption.mean()),
    }


def replay_one(event: pd.Series, bars: pd.DataFrame, variant: str,
               config: ActiveAltcoinConfig) -> dict:
    locations = bars.index[bars["close_time"].eq(event["close_time"])].tolist()
    if not locations:
        return {"variant": variant, "symbol": event["symbol"], "side": event["side"],
                "signal_time": event["close_time"], "status": "MISSING_SIGNAL_BAR"}
    signal_index = locations[0]
    atr = float(event["atr"])
    side = str(event["side"])
    if variant == "IMMEDIATE_1M":
        entry_index = signal_index + 1
        if entry_index >= len(bars):
            return {"variant": variant, "symbol": event["symbol"], "side": side,
                    "signal_time": event["close_time"], "status": "NO_FORWARD_DATA"}
        entry = float(bars.iloc[entry_index]["open"])
        stop = entry - atr if side == "LONG" else entry + atr
        target = entry + config.target_r * atr if side == "LONG" else entry - config.target_r * atr
        result = _finish(event, bars, entry_index, entry, stop, target, variant, config)
        result.update(_micro_features(event, bars, signal_index))
        return result
    if variant != "PULLBACK_RECLAIM_1M":
        raise ValueError(f"unknown variant: {variant}")
    event_close = float(event["close"])
    touched = False
    extremes: list[float] = []
    max_index = min(len(bars) - 1, signal_index + config.pullback_wait_bars * 5)
    for check_index in range(signal_index + 1, max_index + 1):
        bar = bars.iloc[check_index]
        if side == "LONG":
            if float(bar["low"]) <= event_close - config.hard_invalidation_atr * atr:
                return {"variant": variant, "symbol": event["symbol"], "side": side,
                        "signal_time": event["close_time"], "status": "HARD_INVALIDATED"}
            touched = touched or float(bar["low"]) <= event_close - config.pullback_atr * atr
            extremes.append(float(bar["low"]))
            reclaimed = (
                touched and float(bar["close"]) > float(bar["open"])
                and float(bar["close"]) >= event_close - config.reclaim_distance_atr * atr
                and (float(bar["high"]) - float(bar["low"])) > 0
                and (float(bar["close"]) - float(bar["low"])) /
                (float(bar["high"]) - float(bar["low"])) >= config.directional_close_boundary
            )
        else:
            if float(bar["high"]) >= event_close + config.hard_invalidation_atr * atr:
                return {"variant": variant, "symbol": event["symbol"], "side": side,
                        "signal_time": event["close_time"], "status": "HARD_INVALIDATED"}
            touched = touched or float(bar["high"]) >= event_close + config.pullback_atr * atr
            extremes.append(float(bar["high"]))
            reclaimed = (
                touched and float(bar["close"]) < float(bar["open"])
                and float(bar["close"]) <= event_close + config.reclaim_distance_atr * atr
                and (float(bar["high"]) - float(bar["low"])) > 0
                and (float(bar["high"]) - float(bar["close"])) /
                (float(bar["high"]) - float(bar["low"])) >= config.directional_close_boundary
            )
        if not reclaimed:
            continue
        entry_index = check_index + 1
        if entry_index >= len(bars):
            break
        entry = float(bars.iloc[entry_index]["open"])
        stop = (min(extremes) - config.stop_buffer_atr * atr if side == "LONG"
                else max(extremes) + config.stop_buffer_atr * atr)
        risk = entry - stop if side == "LONG" else stop - entry
        if not config.min_risk_atr * atr <= risk <= config.max_risk_atr * atr:
            return {"variant": variant, "symbol": event["symbol"], "side": side,
                    "signal_time": event["close_time"], "status": "INVALID_RISK"}
        target = entry + config.target_r * risk if side == "LONG" else entry - config.target_r * risk
        result = _finish(event, bars, entry_index, entry, stop, target, variant, config)
        result.update(_micro_features(event, bars, signal_index))
        return result
    return {"variant": variant, "symbol": event["symbol"], "side": side,
            "signal_time": event["close_time"], "status": "UNCONFIRMED"}


def _json_safe(rows: list[dict]) -> list[dict]:
    safe = []
    for row in rows:
        item = dict(row)
        for key, value in item.items():
            if isinstance(value, (pd.Timestamp,)):
                item[key] = value.isoformat()
        safe.append(item)
    return safe


def run() -> dict:
    config = ActiveAltcoinConfig()
    events = pd.concat([_feature_events(month, config) for month in MONTHS], ignore_index=True)
    archive_paths = _archive_map()
    grouped_events: dict[tuple[str, str], list[pd.Series]] = defaultdict(list)
    for _, event in events.iterrows():
        day = pd.Timestamp(event["close_time"]).strftime("%Y-%m-%d")
        grouped_events[(str(event["symbol"]), day)].append(event)
    records = {variant: [] for variant in ("IMMEDIATE_1M", "PULLBACK_RECLAIM_1M")}
    missing_archives = 0
    for key, rows in sorted(grouped_events.items(), key=lambda item: item[0]):
        symbol, day = key
        current = archive_paths.get(key)
        if current is None:
            missing_archives += len(rows)
            continue
        bars = _read_archive(current)
        next_day = (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        following = archive_paths.get((symbol, next_day))
        if following is not None:
            bars = pd.concat([bars, _read_archive(following)], ignore_index=True)
            bars = bars.sort_values("open_time").reset_index(drop=True)
        for event in rows:
            for variant in records:
                row = replay_one(event, bars, variant, config)
                row["month"] = str(event["close_time"])[:7]
                records[variant].append(row)
    variants = {}
    ledger_rows = []
    for variant, rows in records.items():
        adjusted, rejected_capacity = enforce_capacity(rows, config.max_open_positions)
        ledger_rows.extend(adjusted)
        split_rows = {
            split: summarize_records(row for row in adjusted if row["month"] in months)
            for split, months in SPLITS.items()
        }
        variants[variant] = {
            "raw_records": len(rows),
            "rejected_capacity": rejected_capacity,
            "splits": split_rows,
        }
    return {
        "method": {
            "strategy_id": "ACTIVE_ALTCOIN_SCALPER_1M_EVENT_REPLAY_V1",
            "authority": "RESEARCH_ONLY",
            "protocol": "ACTIVE_ALTCOIN_SCALPER_FROZEN_PROTOCOL_2026-09-03.md",
            "event_score_floor": 8.0,
            "official_1m_archives": str(MANIFEST),
            "costs_bps": config.round_trip_cost_bps,
        },
        "events": int(len(events)),
        "missing_archives_for_events": int(missing_archives),
        "variants": variants,
        "ledger": _json_safe(ledger_rows),
        "verdict": "RESEARCH_ONLY",
        "execution_authority": False,
    }


def main() -> int:
    result = run()
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(result["ledger"]).to_parquet(LEDGER_OUTPUT, index=False)
    print(json.dumps({
        "events": result["events"],
        "missing_archives_for_events": result["missing_archives_for_events"],
        "variants": result["variants"],
    }, ensure_ascii=True))
    print(OUTPUT)
    print(LEDGER_OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
