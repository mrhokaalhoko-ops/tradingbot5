"""Causal Open Interest, position-ratio and funding feature engineering."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .binance_archive import parse_funding_archive, parse_metrics_archive


METRIC_VALUE_COLUMNS = [
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
]
METRIC_DERIVED_COLUMNS = [
    "oi_value_change_1h_pct",
    "oi_value_change_4h_pct",
    "oi_units_change_1h_pct",
    "oi_value_zscore_24h",
]
FUNDING_FEATURE_COLUMNS = [
    "funding_interval_hours", "last_funding_rate",
    "funding_rate_bps", "funding_rate_24h_bps",
]


def load_metrics_frame(paths: Iterable[str | Path]) -> pd.DataFrame:
    rows = [row for path in paths for row in parse_metrics_archive(path)]
    if not rows:
        return pd.DataFrame(columns=["metrics_time", "symbol", *METRIC_VALUE_COLUMNS])
    frame = pd.DataFrame({
        "metrics_time": pd.to_datetime([row.create_time_ms for row in rows], unit="ms", utc=True),
        "symbol": [row.symbol for row in rows],
        **{
            column: [getattr(row, column) for row in rows]
            for column in METRIC_VALUE_COLUMNS
        },
    })
    frame = frame.sort_values("metrics_time").drop_duplicates("metrics_time", keep="last")
    return frame.reset_index(drop=True)


def add_metrics_features(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=[
            "metrics_time", "symbol", *METRIC_VALUE_COLUMNS, *METRIC_DERIVED_COLUMNS,
        ])
    result = frame.sort_values("metrics_time").copy()
    start, end = result["metrics_time"].min(), result["metrics_time"].max()
    grid = pd.date_range(start.floor("5min"), end.ceil("5min"), freq="5min", tz="UTC")
    indexed = result.set_index("metrics_time").reindex(grid)
    value = indexed["sum_open_interest_value"]
    units = indexed["sum_open_interest"]
    indexed["oi_value_change_1h_pct"] = value.pct_change(12, fill_method=None) * 100.0
    indexed["oi_value_change_4h_pct"] = value.pct_change(48, fill_method=None) * 100.0
    indexed["oi_units_change_1h_pct"] = units.pct_change(12, fill_method=None) * 100.0
    prior_mean = value.rolling(288, min_periods=144).mean().shift(1)
    prior_std = value.rolling(288, min_periods=144).std(ddof=0).shift(1)
    indexed["oi_value_zscore_24h"] = (value - prior_mean) / prior_std.replace(0.0, np.nan)
    indexed.index.name = "metrics_time"
    indexed = indexed[indexed["sum_open_interest_value"].notna()].reset_index()
    return indexed


def load_funding_frame(paths: Iterable[str | Path]) -> pd.DataFrame:
    rows = [row for path in paths for row in parse_funding_archive(path)]
    if not rows:
        return pd.DataFrame(columns=[
            "funding_time", *FUNDING_FEATURE_COLUMNS,
        ])
    frame = pd.DataFrame({
        "funding_time": pd.to_datetime([row.calc_time_ms for row in rows], unit="ms", utc=True),
        "funding_interval_hours": [row.funding_interval_hours for row in rows],
        "last_funding_rate": [row.last_funding_rate for row in rows],
    })
    frame = frame.sort_values("funding_time").drop_duplicates("funding_time", keep="last")
    frame["funding_rate_bps"] = frame["last_funding_rate"] * 10_000.0
    frame["funding_rate_24h_bps"] = (
        frame.set_index("funding_time")["funding_rate_bps"]
        .rolling("24h", min_periods=1)
        .sum()
        .to_numpy()
    )
    return frame.reset_index(drop=True)


def attach_derivatives_features(
    observations: pd.DataFrame,
    metrics: pd.DataFrame,
    funding: pd.DataFrame,
) -> pd.DataFrame:
    """Backward-only as-of joins; future derivatives snapshots are impossible."""

    result = observations.sort_values("close_time").copy()
    if metrics.empty:
        result["metrics_time"] = pd.Series(
            pd.NaT, index=result.index, dtype="datetime64[ns, UTC]"
        )
        for column in [*METRIC_VALUE_COLUMNS, *METRIC_DERIVED_COLUMNS]:
            result[column] = np.nan
    else:
        metrics_for_join = metrics.drop(columns=["symbol"], errors="ignore")
        result = pd.merge_asof(
            result,
            metrics_for_join.sort_values("metrics_time"),
            left_on="close_time",
            right_on="metrics_time",
            direction="backward",
            tolerance=pd.Timedelta(minutes=10),
        )
    if funding.empty:
        result["funding_time"] = pd.Series(
            pd.NaT, index=result.index, dtype="datetime64[ns, UTC]"
        )
        for column in FUNDING_FEATURE_COLUMNS:
            result[column] = np.nan
    else:
        result = pd.merge_asof(
            result.sort_values("close_time"),
            funding.sort_values("funding_time"),
            left_on="close_time",
            right_on="funding_time",
            direction="backward",
            tolerance=pd.Timedelta(days=3),
        )
    result["metrics_staleness_seconds"] = (
        result["close_time"] - result["metrics_time"]
    ).dt.total_seconds()
    result["funding_staleness_seconds"] = (
        result["close_time"] - result["funding_time"]
    ).dt.total_seconds()
    return result
