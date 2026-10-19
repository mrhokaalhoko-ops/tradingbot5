"""Public Binance USD-M historical data and causal in-memory access."""

from __future__ import annotations

import gzip
import json
import time
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

import requests

from .models import Bar, MarketSnapshot


TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
}


def _utc_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").upper().replace("/", "").split(":", 1)[0]


class BinanceUsdMFuturesDataSource:
    """Download public klines only; this class has no credentials or orders."""

    def __init__(
        self,
        *,
        base_url: str = "https://fapi.binance.com",
        session: requests.Session | None = None,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.5,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        if isinstance(max_retries, bool) or int(max_retries) < 0:
            raise ValueError("max_retries must be a non-negative integer")
        self.max_retries = int(max_retries)
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))

    def _request_klines(self, params: dict):
        retryable_statuses = {429, 500, 502, 503, 504}
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    f"{self.base_url}/fapi/v1/klines",
                    params=params,
                    timeout=self.timeout_seconds,
                )
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    if getattr(response, "status_code", None) not in retryable_statuses:
                        raise
                    if attempt >= self.max_retries:
                        raise
                else:
                    return response
            except (requests.Timeout, requests.ConnectionError):
                if attempt >= self.max_retries:
                    raise
            if self.retry_backoff_seconds:
                time.sleep(self.retry_backoff_seconds * (2 ** attempt))
        raise RuntimeError("Binance kline request exhausted retries")

    @staticmethod
    def parse_kline(symbol: str, timeframe: str, row: list) -> Bar:
        if len(row) < 7:
            raise ValueError("Binance kline row is incomplete")
        return Bar(
            symbol=symbol,
            timeframe=timeframe,
            open_time=_utc_from_ms(int(row[0])),
            close_time=_utc_from_ms(int(row[6])),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )

    def fetch(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int = 1500,
    ) -> tuple[Bar, ...]:
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("start/end must be aware and end must follow start")
        clean = normalize_symbol(symbol)
        unified = f"{clean[:-4]}/USDT" if clean.endswith("USDT") else symbol
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        bars: dict[int, Bar] = {}
        page_limit = max(1, min(1500, int(limit)))

        while cursor < end_ms:
            response = self._request_klines(
                {
                    "symbol": clean,
                    "interval": timeframe,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": page_limit,
                }
            )
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(f"unexpected Binance response: {payload!r}")
            if not payload:
                break
            for row in payload:
                bar = self.parse_kline(unified, timeframe, row)
                if bar.open_time < end:
                    bars[int(row[0])] = bar
            next_cursor = int(payload[-1][0]) + TIMEFRAME_MS[timeframe]
            if next_cursor <= cursor:
                raise RuntimeError("Binance kline pagination did not advance")
            cursor = next_cursor
            if len(payload) < page_limit:
                break
        return tuple(sorted(bars.values(), key=lambda bar: bar.open_time))

    @staticmethod
    def write_cache(path: str | Path, bars: Iterable[Bar]) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
            for bar in bars:
                handle.write(json.dumps({
                    "symbol": bar.symbol,
                    "timeframe": bar.timeframe,
                    "open_time": bar.open_time.isoformat(),
                    "close_time": bar.close_time.isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }, separators=(",", ":")) + "\n")
        return path

    @staticmethod
    def read_cache(path: str | Path) -> tuple[Bar, ...]:
        bars = []
        with gzip.open(Path(path), "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                item = json.loads(line)
                bars.append(Bar(
                    symbol=item["symbol"],
                    timeframe=item["timeframe"],
                    open_time=datetime.fromisoformat(item["open_time"]),
                    close_time=datetime.fromisoformat(item["close_time"]),
                    open=item["open"],
                    high=item["high"],
                    low=item["low"],
                    close=item["close"],
                    volume=item.get("volume", 0.0),
                ))
        return tuple(sorted(bars, key=lambda bar: bar.open_time))


class CausalBarStore:
    """Efficient snapshots that expose only bars closed by ``as_of``."""

    def __init__(self, symbol: str, bars_by_timeframe: Mapping[str, Iterable[Bar]]):
        self.symbol = symbol
        self._bars = {
            timeframe: tuple(sorted(bars, key=lambda bar: bar.open_time))
            for timeframe, bars in bars_by_timeframe.items()
        }
        for timeframe, bars in self._bars.items():
            if any(bar.symbol != symbol or bar.timeframe != timeframe for bar in bars):
                raise ValueError("CausalBarStore received mixed symbol/timeframe data")
        self._close_times = {
            timeframe: tuple(bar.close_time for bar in bars)
            for timeframe, bars in self._bars.items()
        }
        self._open_times = {
            timeframe: tuple(bar.open_time for bar in bars)
            for timeframe, bars in self._bars.items()
        }

    def bars(self, timeframe: str) -> tuple[Bar, ...]:
        return self._bars.get(timeframe, ())

    def snapshot(
        self,
        *,
        as_of: datetime,
        regime: str = "UNKNOWN",
        history_limit: int = 260,
    ) -> MarketSnapshot:
        visible = {}
        for timeframe, bars in self._bars.items():
            end = bisect_right(self._close_times[timeframe], as_of)
            start = max(0, end - int(history_limit))
            visible[timeframe] = bars[start:end]
        return MarketSnapshot(
            symbol=self.symbol,
            as_of=as_of,
            bars=visible,
            regime=regime,
        )

    def between(self, timeframe: str, start: datetime, end: datetime) -> tuple[Bar, ...]:
        bars = self._bars.get(timeframe, ())
        if not bars:
            return ()
        left = bisect_right(self._open_times[timeframe], start)
        while left > 0 and bars[left - 1].open_time >= start:
            left -= 1
        right = bisect_right(self._open_times[timeframe], end)
        return tuple(bar for bar in bars[left:right] if start <= bar.open_time <= end)
