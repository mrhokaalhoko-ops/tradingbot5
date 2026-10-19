"""Feature engineering for the historical causal Futures dataset."""

from __future__ import annotations

from typing import Iterable

import pandas as pd


def aggregate_timeframe(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate closed 5m bars into aligned higher-timeframe OHLCV bars."""
    if minutes not in {15, 60}:
        raise ValueError("only 15m and 1h aggregation is supported")
    required = {"open_time", "open", "high", "low", "close", "volume", "quote_volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"missing OHLCV columns: {sorted(missing)}")
    source = frame.sort_values("open_time").set_index("open_time")
    rule = f"{minutes}min"
    bars = source.resample(rule, origin="epoch", label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        quote_volume=("quote_volume", "sum"),
        source_bars=("close", "count"),
    )
    bars = bars.dropna(subset=["open", "high", "low", "close"]).reset_index()
    bars["close_time"] = bars["open_time"] + pd.Timedelta(minutes=minutes) - pd.Timedelta(milliseconds=1)
    bars["expected_source_bars"] = minutes // 5
    bars["complete"] = bars["source_bars"] == bars["expected_source_bars"]
    return bars


def _atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period, min_periods=period).mean()


def add_timeframe_features(frame: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    """Add indicators using current and preceding bars only."""
    result = frame.sort_values("open_time").copy()
    close = result["close"]
    atr = _atr(result)
    label = f"{prefix}_" if prefix else ""
    result[f"{label}return_1_pct"] = close.pct_change(1) * 100.0
    result[f"{label}return_4_pct"] = close.pct_change(4) * 100.0
    result[f"{label}return_6_pct"] = close.pct_change(6) * 100.0
    result[f"{label}return_24_pct"] = close.pct_change(24) * 100.0
    result[f"{label}ema20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    result[f"{label}ema50"] = close.ewm(span=50, adjust=False, min_periods=50).mean()
    result[f"{label}atr"] = atr
    result[f"{label}atr_pct"] = atr / close * 100.0
    volume_base = result["quote_volume"].rolling(20, min_periods=20).mean().shift(1)
    result[f"{label}volume_ratio"] = result["quote_volume"] / volume_base
    result[f"{label}ema20_distance_atr"] = (close - result[f"{label}ema20"]) / atr
    result[f"{label}ema_alignment"] = (
        (close > result[f"{label}ema20"]) & (result[f"{label}ema20"] > result[f"{label}ema50"])
    ).astype("int8") - (
        (close < result[f"{label}ema20"]) & (result[f"{label}ema20"] < result[f"{label}ema50"])
    ).astype("int8")
    return result


def add_structure_features(frame: pd.DataFrame, lookback: int = 24) -> pd.DataFrame:
    """Add non-forward-looking swing, displacement and sweep observations."""
    result = frame.sort_values("open_time").copy()
    prior_high = result["high"].rolling(lookback, min_periods=lookback).max().shift(1)
    prior_low = result["low"].rolling(lookback, min_periods=lookback).min().shift(1)
    result["prior_high_24"] = prior_high
    result["prior_low_24"] = prior_low
    result["breakout_up_24"] = (result["close"] > prior_high).astype("int8")
    result["breakdown_down_24"] = (result["close"] < prior_low).astype("int8")
    result["wick_sweep_high_24"] = (
        (result["high"] > prior_high) & (result["close"] <= prior_high)
    ).astype("int8")
    result["wick_sweep_low_24"] = (
        (result["low"] < prior_low) & (result["close"] >= prior_low)
    ).astype("int8")
    candle_range = result["high"] - result["low"]
    result["close_location"] = (result["close"] - result["low"]) / candle_range.where(candle_range > 0)
    result["body_pct"] = (result["close"] - result["open"]).abs() / result["open"] * 100.0
    return result


def add_forward_labels(frame: pd.DataFrame, horizons: Iterable[int] = (1, 3, 12, 48)) -> pd.DataFrame:
    """Add future labels after the current closed 5m bar, never inputs."""
    result = frame.sort_values("open_time").copy()
    close = result["close"]
    high = result["high"]
    low = result["low"]
    entry_open = result["open"].shift(-1)
    result["future_entry_open_1x5m"] = entry_open
    for bars in horizons:
        result[f"future_close_{bars}x5m_pct"] = (
            close.shift(-bars).div(close).sub(1.0) * 100.0
        )
        future_high = high.iloc[::-1].rolling(bars, min_periods=bars).max().iloc[::-1].shift(-1)
        future_low = low.iloc[::-1].rolling(bars, min_periods=bars).min().iloc[::-1].shift(-1)
        result[f"future_high_{bars}x5m_pct"] = future_high.div(close).sub(1.0) * 100.0
        result[f"future_low_{bars}x5m_pct"] = future_low.div(close).sub(1.0) * 100.0
        # Execution labels are relative to the next 5m open, matching a
        # causal signal that is evaluated only after the current bar closes.
        result[f"future_close_{bars}x5m_from_entry_pct"] = (
            close.shift(-bars).div(entry_open).sub(1.0) * 100.0
        )
        result[f"future_high_{bars}x5m_from_entry_pct"] = (
            future_high.div(entry_open).sub(1.0) * 100.0
        )
        result[f"future_low_{bars}x5m_from_entry_pct"] = (
            future_low.div(entry_open).sub(1.0) * 100.0
        )
    return result


def valid_observation_mask(frame: pd.DataFrame) -> pd.Series:
    """Rows with enough warm-up and a complete 4h forward horizon."""
    required = [
        "atr",
        "ema20",
        "ema50",
        "prior_high_24",
        "prior_low_24",
        "future_close_48x5m_pct",
        "future_high_48x5m_pct",
        "future_low_48x5m_pct",
        "future_entry_open_1x5m",
        "future_close_48x5m_from_entry_pct",
        "future_high_48x5m_from_entry_pct",
        "future_low_48x5m_from_entry_pct",
    ]
    return frame[required].notna().all(axis=1)
