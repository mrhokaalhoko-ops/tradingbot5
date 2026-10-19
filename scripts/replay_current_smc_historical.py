"""Causal historical replay of the production SMC setup and entry geometry.

This is a research tool only.  It imports the production TA/RR implementations,
but it has no exchange credentials and contains no order path.  The monthly
universe is selected from the previous month's quote volume, so a symbol cannot
enter the sample because of future liquidity.

The replay deliberately counts one row per structural thesis (OB anchor), not
one row per scanner pass.  Repeated scans of the same OB are therefore not
misrepresented as independent opportunities.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import hashlib
import logging
from pathlib import Path
import sys
import warnings

import pandas as pd
import ta


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines.ta_engine import TAEngine
from strategy.entry_confirmation import build_setup_thesis_id
from strategy.filter_profiles import FILTER_PROFILES
from strategy.risk_manager import RiskManager
from trading_v2.evaluation import (
    EvaluatedPlan,
    enforce_portfolio_capacity,
    summarize,
    summarize_by_calendar_month,
    summarize_by_symbol,
    summarize_time_splits,
)
from trading_v2.models import (
    Bar,
    EntryKind,
    ReplayResult,
    ReplayStatus,
    Side,
    TradePlan,
)
from trading_v2.replay import CausalReplayEngine
from core.logger import logger as production_logger


STRATEGY_VERSION = "CURRENT_SMC_HISTORICAL_V1"
ENTRY_TTL_MINUTES = 20
HOLD_HOURS = 4
PROXIMAL_STOP_ATR = (0.1, 0.2)

warnings.filterwarnings(
    "ignore",
    message="The behavior of DataFrame concatenation with empty or all-NA entries is deprecated.*",
    category=FutureWarning,
)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _month(value) -> str:
    return pd.Timestamp(value).strftime("%Y-%m")


def _resample_ohlcv(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate complete 5m bars without bridging a missing source bar."""
    if frame.empty:
        return frame.copy()
    expected = int(minutes // 5)
    indexed = frame.sort_values("open_time").set_index("open_time")
    result = indexed.resample(
        f"{minutes}min", origin="epoch", label="left", closed="left"
    ).agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum", "close_time": "max", "global_regime": "last",
        "target_month": "last", "selection_rank": "last",
    })
    counts = indexed["close"].resample(
        f"{minutes}min", origin="epoch", label="left", closed="left"
    ).count()
    result = result[counts == expected].dropna(subset=["open", "high", "low", "close"])
    result = result.reset_index()
    result["timestamp"] = result["open_time"]
    return result


def _split_contiguous(frame: pd.DataFrame) -> list[pd.DataFrame]:
    if frame.empty:
        return []
    ordered = frame.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    gap = ordered["open_time"].diff().dt.total_seconds().fillna(300).ne(300)
    return [part.reset_index(drop=True) for _, part in ordered.groupby(gap.cumsum())]


def _htf_table(frame_5m: pd.DataFrame) -> pd.DataFrame:
    hourly = _resample_ohlcv(frame_5m, 60)
    if hourly.empty:
        return hourly
    hourly["ema_50"] = ta.trend.EMAIndicator(hourly["close"], window=50).ema_indicator()
    hourly["ema_200"] = ta.trend.EMAIndicator(hourly["close"], window=200).ema_indicator()
    bullish = (
        (hourly["ema_50"] > hourly["ema_200"])
        & (hourly["close"] > hourly["ema_50"])
    )
    bearish = (
        (hourly["ema_50"] < hourly["ema_200"])
        & (hourly["close"] < hourly["ema_50"])
    )
    hourly["htf_trend"] = "neutral"
    hourly.loc[bullish, "htf_trend"] = "bullish"
    hourly.loc[bearish, "htf_trend"] = "bearish"
    hourly.loc[hourly[["ema_50", "ema_200"]].isna().any(axis=1), "htf_trend"] = "unknown"
    return hourly


def _preselect_signal_indices(frame: pd.DataFrame, max_ob_age: int) -> list[int]:
    """Cheap equivalent of the OB portion of ``TAEngine.evaluate_trend``.

    Every selected row is still passed through the real production method;
    this function only avoids millions of expensive market-level rebuilds.
    """
    close = frame["close"]
    atr = frame["atr"]
    up = (frame["ema_50"] > frame["ema_200"]) & frame["ema_200"].gt(0)
    down = (frame["ema_50"] < frame["ema_200"]) & frame["ema_200"].gt(0)
    bull_valid = (
        frame["ob_bullish_low"].gt(0)
        & close.ge(frame["ob_bullish_low"] * 0.995)
        & frame["ob_bullish_age"].le(max_ob_age)
        & ~frame["ob_bullish_mitigated"].fillna(True).astype(bool)
    )
    bear_valid = (
        frame["ob_bearish_high"].gt(0)
        & close.le(frame["ob_bearish_high"] * 1.005)
        & frame["ob_bearish_age"].le(max_ob_age)
        & ~frame["ob_bearish_mitigated"].fillna(True).astype(bool)
    )
    bull_near = (
        (close - frame["ob_bullish_high"]).abs()
        <= pd.concat([close * 0.035, atr * 1.5], axis=1).min(axis=1)
    ) | close.between(frame["ob_bullish_low"], frame["ob_bullish_high"])
    bear_near = (
        (close - frame["ob_bearish_low"]).abs()
        <= pd.concat([close * 0.035, atr * 1.5], axis=1).min(axis=1)
    ) | close.between(frame["ob_bearish_low"], frame["ob_bearish_high"])
    mask = (up & bull_valid & bull_near) | (down & bear_valid & bear_near)
    return [int(index) for index in frame.index[mask.fillna(False)]]


def _preselected_side(row: pd.Series, max_ob_age: int) -> str | None:
    """Return the side using the same BUY-first precedence as production."""
    close = float(row.get("close") or 0)
    atr = float(row.get("atr") or 0)
    ema_50 = float(row.get("ema_50") or 0)
    ema_200 = float(row.get("ema_200") or 0)
    bull_low = float(row.get("ob_bullish_low") or 0)
    bull_high = float(row.get("ob_bullish_high") or 0)
    bear_low = float(row.get("ob_bearish_low") or 0)
    bear_high = float(row.get("ob_bearish_high") or 0)
    bull_valid = (
        bull_low > 0 and close >= bull_low * 0.995
        and float(row.get("ob_bullish_age") or 999) <= max_ob_age
        and not bool(row.get("ob_bullish_mitigated", True))
    )
    bear_valid = (
        bear_high > 0 and close <= bear_high * 1.005
        and float(row.get("ob_bearish_age") or 999) <= max_ob_age
        and not bool(row.get("ob_bearish_mitigated", True))
    )
    bull_near = abs(close - bull_high) <= min(close * 0.035, atr * 1.5) or bull_low <= close <= bull_high
    bear_near = abs(close - bear_low) <= min(close * 0.035, atr * 1.5) or bear_low <= close <= bear_high
    if ema_50 > ema_200 > 0 and bull_valid and bull_near:
        return "buy"
    if 0 < ema_50 < ema_200 and bear_valid and bear_near:
        return "sell"
    return None


def _geometry_key(symbol: str, side: str, row: pd.Series) -> tuple:
    low = row.get("ob_bullish_low") if side == "buy" else row.get("ob_bearish_low")
    high = row.get("ob_bullish_high") if side == "buy" else row.get("ob_bearish_high")
    return symbol, side, f"{float(low or 0):.10g}", f"{float(high or 0):.10g}"


def _live_frame(history: pd.DataFrame, current_price: float, opened_at) -> pd.DataFrame:
    """Append the live placeholder expected by production's ``[-2]`` policy."""
    live = {column: pd.NA for column in history.columns}
    live.update({
        "open": current_price, "high": current_price, "low": current_price,
        "close": current_price, "volume": 0.0,
        "open_time": opened_at, "timestamp": opened_at,
    })
    return pd.concat([history, pd.DataFrame([live])], ignore_index=True)


def _context(details: dict, regime: str) -> dict:
    anchor = details.get("setup_anchor") or {}
    safe_anchor = ({
        "type": str(anchor.get("type") or ""),
        "created_at": str(anchor.get("created_at") or ""),
        "age": int(float(anchor.get("age") or 0)),
        "low": float(anchor.get("low") or 0),
        "high": float(anchor.get("high") or 0),
    } if anchor else None)
    return {
        "score": int(float(details.get("score") or 0)),
        "htf_trend": str(details.get("htf_trend") or "unknown"),
        "ema_50": float(details.get("ema_50") or 0),
        "ema_200": float(details.get("ema_200") or 0),
        "adx": float(details.get("adx") or 0),
        "rsi": float(details.get("rsi") or 0),
        "optimal_entry": float(details.get("optimal_entry") or 0),
        "invalidation_level": float(details.get("invalidation_level") or 0),
        "ob_low": float(details.get("ob_low") or 0),
        "ob_high": float(details.get("ob_high") or 0),
        "countertrend": bool(details.get("countertrend", False)),
        "market_regime": {"label": str(regime or "UNKNOWN"), "known": True},
        "recent_sell_side_sweep": bool(details.get("recent_sell_side_sweep", False)),
        "recent_buy_side_sweep": bool(details.get("recent_buy_side_sweep", False)),
        "setup_anchor": safe_anchor,
    }


def _bars(frame: pd.DataFrame, symbol: str) -> tuple[Bar, ...]:
    unified = f"{symbol[:-4]}/USDT"
    return tuple(Bar(
        symbol=unified,
        timeframe="5m",
        open_time=row.open_time.to_pydatetime(),
        close_time=row.close_time.to_pydatetime(),
        open=float(row.open), high=float(row.high), low=float(row.low),
        close=float(row.close), volume=float(row.volume),
    ) for row in frame.itertuples())


def _execution_bars(
    frame: pd.DataFrame,
    symbol: str,
    *,
    start,
    end,
) -> tuple[Bar, ...]:
    """Build only the forward bars needed by one plan.

    The source segment is chronologically sorted.  Binary search avoids
    rescanning a year of 5m rows for every thesis while preserving the exact
    inclusive time window used by the original implementation.
    """
    opened = frame["open_time"]
    left = int(opened.searchsorted(pd.Timestamp(start), side="left"))
    right = int(opened.searchsorted(pd.Timestamp(end), side="right"))
    return _bars(frame.iloc[left:right], symbol)


def _nearest_next_row(frame: pd.DataFrame, as_of) -> pd.Series | None:
    rows = frame[frame["open_time"] > as_of]
    return None if rows.empty else rows.iloc[0]


def _plan(
    *, model: str, thesis_id: str, symbol: str, side: str, created_at,
    entry: float, stop: float, target: float, score: float,
    evidence: dict | None = None,
) -> TradePlan:
    enum_side = Side.BUY if side == "buy" else Side.SELL
    return TradePlan(
        plan_id=f"{model}:{thesis_id}", strategy_id=model,
        setup_id=thesis_id, symbol=f"{symbol[:-4]}/USDT", side=enum_side,
        created_at=created_at,
        valid_until=created_at + timedelta(minutes=ENTRY_TTL_MINUTES),
        exit_by=created_at + timedelta(hours=HOLD_HOURS),
        entry_kind=EntryKind.LIMIT, entry=entry, stop=stop, target=target,
        thesis="Production SMC structure with independently replayed entry geometry",
        score=score,
        evidence=evidence or {},
    )


def _record(row: EvaluatedPlan) -> dict:
    return {
        "model": row.plan.strategy_id, "thesis_id": row.plan.setup_id,
        "plan_id": row.plan.plan_id,
        "symbol": row.plan.symbol, "side": row.plan.side.value,
        "entry_kind": row.plan.entry_kind.value,
        "thesis": row.plan.thesis,
        "created_at": row.plan.created_at.isoformat(),
        "valid_until": row.plan.valid_until.isoformat(),
        "exit_by": row.plan.exit_by.isoformat(),
        "entry": row.plan.entry, "stop": row.plan.stop,
        "target": row.plan.target, "planned_rr": row.plan.planned_rr,
        "score": row.plan.score,
        "status": row.result.status.value,
        "filled_at": row.result.filled_at.isoformat() if row.result.filled_at else None,
        "exited_at": row.result.exited_at.isoformat() if row.result.exited_at else None,
        "entry_price": row.result.entry_price,
        "exit_price": row.result.exit_price,
        "gross_r": row.result.gross_r,
        "net_r": row.result.net_r, "mfe_r": row.result.mfe_r,
        "mae_r": row.result.mae_r,
        "bars_observed": row.result.bars_observed,
        "reason": row.result.reason,
        "htf_trend": row.plan.evidence.get("htf_trend"),
        "market_regime": row.plan.evidence.get("market_regime"),
        "countertrend": row.plan.evidence.get("countertrend"),
        "evidence": dict(row.plan.evidence),
    }


def _evaluated_from_record(payload: dict) -> EvaluatedPlan:
    created_at = datetime.fromisoformat(payload["created_at"])
    model = str(payload["model"])
    thesis_id = str(payload["thesis_id"])
    plan = TradePlan(
        plan_id=str(payload.get("plan_id") or f"{model}:{thesis_id}"),
        strategy_id=model,
        setup_id=thesis_id,
        symbol=str(payload["symbol"]),
        side=Side(str(payload["side"])),
        created_at=created_at,
        valid_until=datetime.fromisoformat(payload["valid_until"]),
        exit_by=datetime.fromisoformat(payload["exit_by"]),
        entry_kind=EntryKind(str(payload.get("entry_kind") or EntryKind.LIMIT.value)),
        entry=float(payload["entry"]),
        stop=float(payload["stop"]),
        target=float(payload["target"]),
        thesis=str(payload.get("thesis") or (
            "Production SMC structure with independently replayed entry geometry"
        )),
        score=float(payload.get("score") or 0),
        evidence=payload.get("evidence") or {
            "htf_trend": payload.get("htf_trend"),
            "market_regime": payload.get("market_regime"),
            "countertrend": bool(payload.get("countertrend", False)),
        },
    )
    result = ReplayResult(
        plan_id=plan.plan_id,
        status=ReplayStatus(str(payload["status"])),
        filled_at=(datetime.fromisoformat(payload["filled_at"])
                   if payload.get("filled_at") else None),
        exited_at=(datetime.fromisoformat(payload["exited_at"])
                   if payload.get("exited_at") else None),
        entry_price=payload.get("entry_price"),
        exit_price=payload.get("exit_price"),
        gross_r=payload.get("gross_r"),
        net_r=payload.get("net_r"),
        mfe_r=payload.get("mfe_r"),
        mae_r=payload.get("mae_r"),
        bars_observed=int(payload.get("bars_observed") or 0),
        reason=str(payload.get("reason") or ""),
    )
    return EvaluatedPlan(plan=plan, result=result)


def _checkpoint_code_identity() -> dict[str, str]:
    # A completed cache must not be relabelled as a new replay after changes
    # to the simulator, plan generator, risk rules or metrics.
    files = (
        "scripts/replay_current_smc_historical.py", "trading_v2/replay.py",
        "trading_v2/models.py", "trading_v2/evaluation.py", "engines/ta_engine.py",
        "strategy/risk_manager.py", "strategy/filter_profiles.py",
        "strategy/entry_confirmation.py", "strategy/market_regime.py", "core/config.py",
    )
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}


def _validate_checkpoint(saved: dict, profile_key: str) -> None:
    if (saved.get("strategy_version") != STRATEGY_VERSION
            or saved.get("profile") != profile_key):
        raise ValueError("checkpoint strategy/profile does not match this replay")
    if saved.get("code_identity") != _checkpoint_code_identity():
        raise ValueError("checkpoint code identity is missing or changed; use a new output path to recompute")


def _write_checkpoint(
    path: Path,
    *,
    profile_key: str,
    completed_symbols: set[str],
    counters: Counter[str],
    records: dict[str, list[EvaluatedPlan]],
) -> None:
    _atomic_json(path, {
        "strategy_version": STRATEGY_VERSION,
        "code_identity": _checkpoint_code_identity(),
        "profile": profile_key,
        "completed_symbols": sorted(completed_symbols),
        "counters": dict(counters),
        "records": {
            model: [_record(row) for row in rows]
            for model, rows in records.items()
        },
    })


def _summarize_groups(
    records: list[EvaluatedPlan],
    key,
) -> dict[str, dict]:
    groups: dict[str, list[EvaluatedPlan]] = {}
    for row in records:
        groups.setdefault(str(key(row)), []).append(row)
    return {name: summarize(rows) for name, rows in sorted(groups.items())}


def _report(records: list[EvaluatedPlan], start: datetime, end: datetime) -> dict:
    capacity = enforce_portfolio_capacity(records, max_open_positions=5)
    selected = list(capacity.records)
    return {
        "capacity": {
            "rejected_capacity": capacity.rejected_capacity,
            "rejected_symbol_busy": capacity.rejected_symbol_busy,
        },
        "summary": summarize(selected),
        "time_splits": summarize_time_splits(selected, start=start, end=end),
        "calendar_months": summarize_by_calendar_month(selected),
        "symbols": summarize_by_symbol(selected),
        "sides": _summarize_groups(selected, lambda row: row.plan.side.value),
        "market_regimes": _summarize_groups(
            selected,
            lambda row: row.plan.evidence.get("market_regime", "UNKNOWN"),
        ),
        "countertrend": _summarize_groups(
            selected,
            lambda row: "COUNTERTREND"
            if row.plan.evidence.get("countertrend") else "ALIGNED",
        ),
        "records": [_record(row) for row in selected],
    }


def replay(
    *, features_root: Path, metadata_path: Path, profile_key: str,
    max_symbols: int | None = None,
    checkpoint_path: Path | None = None,
) -> dict:
    profile = dict(FILTER_PROFILES[profile_key])
    profile["key"] = profile_key
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    crypto = {
        row["symbol"] for row in metadata["symbols"]
        if row.get("underlying_type") == "COIN"
    }
    available = sorted({path.stem for path in features_root.glob("*/*.parquet")} & crypto)
    if max_symbols is not None:
        available = available[:max(0, int(max_symbols))]

    engine = TAEngine()
    risk = RiskManager()
    replayer = CausalReplayEngine()
    records: dict[str, list[EvaluatedPlan]] = {
        "MIDPOINT_PRODUCTION_RISK": [],
        **{f"PROXIMAL_INV_{multiplier:.1f}_ATR": [] for multiplier in PROXIMAL_STOP_ATR},
    }
    counters: Counter[str] = Counter()
    completed_symbols: set[str] = set()
    seen_theses: set[str] = set()
    seen_geometry: set[tuple] = set()
    all_times = []

    if checkpoint_path is not None and checkpoint_path.exists():
        saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        _validate_checkpoint(saved, profile_key)
        completed_symbols = set(saved.get("completed_symbols") or [])
        counters.update(saved.get("counters") or {})
        for model, rows in (saved.get("records") or {}).items():
            if model in records:
                records[model].extend(_evaluated_from_record(row) for row in rows)
        all_times.extend(
            row.plan.created_at
            for model_rows in records.values()
            for row in model_rows
        )

    # Production risk code logs every rejected target.  Those messages are
    # useful live but turn a deterministic historical replay into gigabytes of
    # noise.  The structured counters/report retain every rejection class.
    previous_log_level = production_logger.level
    production_logger.setLevel(logging.ERROR)

    for number, symbol in enumerate(available, 1):
        if symbol in completed_symbols:
            continue
        paths = sorted(features_root.glob(f"*/{symbol}.parquet"))
        raw_parts = [pd.read_parquet(path, columns=[
            "open_time", "close_time", "open", "high", "low", "close",
            "volume", "global_regime", "target_month", "selection_rank",
        ]) for path in paths]
        if not raw_parts:
            continue
        raw = pd.concat(raw_parts, ignore_index=True)
        for column in ("open_time", "close_time"):
            raw[column] = pd.to_datetime(raw[column], utc=True)
        for segment in _split_contiguous(raw):
            if len(segment) < 12 * 24 * 10:
                counters["SEGMENT_TOO_SHORT"] += 1
                continue
            frame_15m = _resample_ohlcv(segment, 15)
            if len(frame_15m) < 260:
                counters["SEGMENT_15M_TOO_SHORT"] += 1
                continue
            enriched = engine.add_indicators(frame_15m.copy(), filter_profile=profile)
            hourly = _htf_table(segment)
            if hourly.empty:
                continue
            indices = _preselect_signal_indices(enriched, int(profile["max_ob_age"]))
            counters["PRESELECTED"] += len(indices)

            for index in indices:
                if index < 210:
                    counters["WARMUP_REJECTED"] += 1
                    continue
                closed_at = pd.Timestamp(enriched.iloc[index]["close_time"])
                htf_visible = hourly[hourly["close_time"] <= closed_at]
                if htf_visible.empty:
                    continue
                htf_trend = str(htf_visible.iloc[-1]["htf_trend"])
                if htf_trend == "unknown":
                    counters["HTF_UNKNOWN"] += 1
                    continue
                next_row = _nearest_next_row(segment, closed_at)
                if next_row is None:
                    continue
                current_price = float(next_row["open"])
                history = enriched.iloc[max(0, index - 259):index + 1].copy()
                live = _live_frame(history, current_price, next_row["open_time"])
                row = enriched.iloc[index]
                side = _preselected_side(row, int(profile["max_ob_age"]))
                if side is None:
                    counters["PRESELECTOR_SIDE_MISMATCH"] += 1
                    continue
                regime = str(row.get("global_regime") or "UNKNOWN")
                quality_audit: dict = {}
                quality_ok, _ = engine.validate_entry_quality(
                    bars_df=live, side=side, current_price=current_price,
                    filter_profile=profile,
                    market_regime={"label": regime, "known": regime != "UNKNOWN"},
                    audit_details=quality_audit,
                )
                if not quality_ok:
                    counters[f"QUALITY_REJECTED:{quality_audit.get('terminal_gate','UNKNOWN')}"] += 1
                    continue
                counters["QUALITY_PASSED"] += 1
                geometry = _geometry_key(symbol, side, row)
                if geometry in seen_geometry:
                    counters["DUPLICATE_STRUCTURAL_GEOMETRY"] += 1
                    continue
                seen_geometry.add(geometry)

                details: dict = {}
                verified_side = engine.evaluate_trend(
                    live, symbol=f"{symbol[:-4]}/USDT", details=details,
                    htf_trend=htf_trend, ema_mode="dual", filter_profile=profile,
                )
                if verified_side != side:
                    counters["REAL_ENGINE_NEUTRAL"] += 1
                    continue
                counters["RAW_SETUP_SIGNAL"] += 1

                context = _context(details, regime)
                thesis_id = build_setup_thesis_id(
                    f"{symbol[:-4]}/USDT", side, context, profile_key
                )
                if thesis_id in seen_theses:
                    counters["DUPLICATE_STRUCTURAL_THESIS"] += 1
                    continue
                seen_theses.add(thesis_id)
                counters["INDEPENDENT_THESIS"] += 1

                entry = float(details.get("optimal_entry") or 0)
                invalidation = float(details.get("invalidation_level") or 0)
                atr_15m = float(details.get("atr") or 0)
                levels = details.get("market_levels") or {}
                if min(entry, invalidation, atr_15m) <= 0:
                    counters["INVALID_GEOMETRY_INPUT"] += 1
                    continue
                created = pd.Timestamp(next_row["open_time"]).to_pydatetime()
                execution = _execution_bars(
                    segment,
                    symbol,
                    start=created,
                    end=created + timedelta(hours=HOLD_HOURS),
                )
                if not execution:
                    counters["NO_EXECUTION_BARS"] += 1
                    continue

                baseline_sl, baseline_tp = risk.calculate_sl_tp(
                    side, entry, atr=atr_15m, invalidation_level=invalidation,
                    market_levels=levels, filter_profile=profile,
                )
                if baseline_sl is not None and baseline_tp is not None:
                    plan = _plan(
                        model="MIDPOINT_PRODUCTION_RISK", thesis_id=thesis_id,
                        symbol=symbol, side=side, created_at=created,
                        entry=entry, stop=float(baseline_sl), target=float(baseline_tp),
                        score=float(details.get("score") or 0),
                        evidence={
                            "htf_trend": htf_trend,
                            "market_regime": regime,
                            "countertrend": bool(details.get("countertrend", False)),
                        },
                    )
                    records["MIDPOINT_PRODUCTION_RISK"].append(EvaluatedPlan(
                        plan=plan,
                        result=replayer.replay_plan(plan, execution, horizon_end=plan.exit_by),
                    ))
                    counters["MIDPOINT_RR_PASSED"] += 1
                else:
                    counters["MIDPOINT_RR_REJECTED"] += 1

                proximal = float(details.get("ob_high") if side == "buy" else details.get("ob_low") or 0)
                for multiplier in PROXIMAL_STOP_ATR:
                    model = f"PROXIMAL_INV_{multiplier:.1f}_ATR"
                    stop = (
                        invalidation - multiplier * atr_15m
                        if side == "buy" else invalidation + multiplier * atr_15m
                    )
                    plan_result = risk.calculate_trade_plan(
                        side, proximal, stop, levels, min_rr=float(profile["min_rr"])
                    ) if proximal > 0 else {"valid": False}
                    if not plan_result.get("valid"):
                        counters[f"{model}_RR_REJECTED"] += 1
                        continue
                    try:
                        plan = _plan(
                            model=model, thesis_id=thesis_id, symbol=symbol, side=side,
                            created_at=created, entry=proximal, stop=stop,
                            target=float(plan_result["tp"]),
                            score=float(details.get("score") or 0),
                            evidence={
                                "htf_trend": htf_trend,
                                "market_regime": regime,
                                "countertrend": bool(details.get("countertrend", False)),
                            },
                        )
                    except ValueError:
                        counters[f"{model}_INVALID_PLAN"] += 1
                        continue
                    records[model].append(EvaluatedPlan(
                        plan=plan,
                        result=replayer.replay_plan(plan, execution, horizon_end=plan.exit_by),
                    ))
                    counters[f"{model}_RR_PASSED"] += 1
                all_times.append(created)
        completed_symbols.add(symbol)
        if checkpoint_path is not None and (
            number % 10 == 0 or number == len(available)
        ):
            _write_checkpoint(
                checkpoint_path,
                profile_key=profile_key,
                completed_symbols=completed_symbols,
                counters=counters,
                records=records,
            )
        safe_symbol = symbol.encode("ascii", errors="backslashreplace").decode("ascii")
        print(
            f"[{number}/{len(available)}] {safe_symbol} "
            f"theses={counters['INDEPENDENT_THESIS']}",
            flush=True,
        )

    production_logger.setLevel(previous_log_level)

    if not all_times:
        return {"method": {"strategy_version": STRATEGY_VERSION}, "counters": dict(counters), "models": {}}
    start = datetime(2025, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    model_reports = {
        model: _report(rows, start, end) for model, rows in records.items()
    }
    return {
        "method": {
            "strategy_version": STRATEGY_VERSION,
            "research_only": True,
            "profile": profile_key,
            "source": "causal monthly top-50 Binance USD-M Futures universe",
            "source_timeframe": "verified 5m monthly archives / feature partitions",
            "decision": "production TAEngine on closed 15m plus production [-2] live placeholder",
            "htf": "closed 1h EMA50/EMA200",
            "identity": "one observation per structural setup_thesis_id",
            "entry_ttl_minutes": ENTRY_TTL_MINUTES,
            "holding_hours": HOLD_HOURS,
            "costs": "CausalReplayEngine defaults",
            "ambiguity": "entry/terminal or SL/TP same 5m candle remains AMBIGUOUS",
            "limitations": [
                "geometry replay does not claim that production 5m confirmation passed",
                "monthly previous-volume universe approximates the live rolling universe",
                "5m bars cannot order multiple intrabar touches",
            ],
        },
        "symbols": len(available),
        "counters": dict(sorted(counters.items())),
        "models": model_reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--features-root", type=Path,
        default=Path("research_data/causal_futures_dataset/features"),
    )
    parser.add_argument(
        "--metadata", type=Path,
        default=Path("research_data/causal_futures_dataset/universe_contract_audit.json"),
    )
    parser.add_argument("--profile", choices=sorted(FILTER_PROFILES), default="smc_balanced")
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument(
        "--output", type=Path,
        default=Path("research_data/causal_futures_dataset/current_smc_historical_replay.json"),
    )
    args = parser.parse_args()
    payload = replay(
        features_root=args.features_root, metadata_path=args.metadata,
        profile_key=args.profile, max_symbols=args.max_symbols,
        checkpoint_path=args.output.with_suffix(args.output.suffix + ".checkpoint.json"),
    )
    _atomic_json(args.output, payload)
    for model, report in payload.get("models", {}).items():
        summary = report["summary"]
        print(
            f"{model}: plans={summary['plans']} realized={summary['realized']} "
            f"expectancy={summary['expectancy_r']} pf={summary['profit_factor']} "
            f"dd={summary['max_drawdown_r']}"
        )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
