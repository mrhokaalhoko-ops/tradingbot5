"""Verified access to Binance Vision USD-M Futures monthly kline archives."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import time
from typing import Iterable, Iterator
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import requests


ARCHIVE_BASE_URL = "https://data.binance.vision"
S3_INDEX_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
MONTHLY_KLINE_PREFIX = "data/futures/um/monthly/klines"
DAILY_KLINE_PREFIX = "data/futures/um/daily/klines"
DAILY_AGGTRADES_PREFIX = "data/futures/um/daily/aggTrades"
DAILY_METRICS_PREFIX = "data/futures/um/daily/metrics"
MONTHLY_FUNDING_PREFIX = "data/futures/um/monthly/fundingRate"


@dataclass(frozen=True)
class MonthlyArchive:
    symbol: str
    timeframe: str
    month: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-{self.timeframe}-{self.month}.zip"

    @property
    def key(self) -> str:
        return (
            f"{MONTHLY_KLINE_PREFIX}/{self.symbol}/{self.timeframe}/"
            f"{self.filename}"
        )

    @property
    def url(self) -> str:
        return f"{ARCHIVE_BASE_URL}/{self.key}"


@dataclass(frozen=True)
class DailyKlineArchive:
    symbol: str
    timeframe: str
    day: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-{self.timeframe}-{self.day}.zip"

    @property
    def key(self) -> str:
        return (
            f"{DAILY_KLINE_PREFIX}/{self.symbol}/{self.timeframe}/"
            f"{self.filename}"
        )

    @property
    def url(self) -> str:
        return f"{ARCHIVE_BASE_URL}/{self.key}"


@dataclass(frozen=True)
class DailyMetricsArchive:
    symbol: str
    day: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-metrics-{self.day}.zip"

    @property
    def key(self) -> str:
        return f"{DAILY_METRICS_PREFIX}/{self.symbol}/{self.filename}"

    @property
    def url(self) -> str:
        return f"{ARCHIVE_BASE_URL}/{self.key}"


@dataclass(frozen=True)
class DailyAggTradesArchive:
    symbol: str
    day: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-aggTrades-{self.day}.zip"

    @property
    def key(self) -> str:
        return f"{DAILY_AGGTRADES_PREFIX}/{self.symbol}/{self.filename}"

    @property
    def url(self) -> str:
        return f"{ARCHIVE_BASE_URL}/{self.key}"


@dataclass(frozen=True)
class MonthlyFundingArchive:
    symbol: str
    month: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-fundingRate-{self.month}.zip"

    @property
    def key(self) -> str:
        return f"{MONTHLY_FUNDING_PREFIX}/{self.symbol}/{self.filename}"

    @property
    def url(self) -> str:
        return f"{ARCHIVE_BASE_URL}/{self.key}"


@dataclass(frozen=True)
class KlineRow:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    close_time_ms: int
    quote_volume: float
    trades: int
    taker_buy_base_volume: float
    taker_buy_quote_volume: float


@dataclass(frozen=True)
class ArchiveSummary:
    symbol: str
    timeframe: str
    month: str
    rows: int
    quote_volume: float
    first_open_time_ms: int | None
    last_open_time_ms: int | None


@dataclass(frozen=True)
class MetricsRow:
    create_time_ms: int
    symbol: str
    sum_open_interest: float
    sum_open_interest_value: float
    count_toptrader_long_short_ratio: float
    sum_toptrader_long_short_ratio: float
    count_long_short_ratio: float
    sum_taker_long_short_vol_ratio: float


@dataclass(frozen=True)
class FundingRow:
    calc_time_ms: int
    funding_interval_hours: int
    last_funding_rate: float


@dataclass(frozen=True)
class AggTradeRow:
    aggregate_trade_id: int
    price: float
    quantity: float
    first_trade_id: int
    last_trade_id: int
    transact_time_ms: int
    is_buyer_maker: bool

    @property
    def quote_notional(self) -> float:
        return self.price * self.quantity

    @property
    def aggressor_side(self) -> str:
        # buyer-maker means the incoming/aggressing order was a SELL.
        return "SELL" if self.is_buyer_maker else "BUY"


def _normalize_timestamp_ms(raw: str | int) -> int:
    value = int(raw)
    # Binance archives may use microseconds in some product families.  Keep
    # downstream code in milliseconds regardless of the upstream unit.
    return value // 1000 if value >= 100_000_000_000_000 else value


def _optional_float(raw: str | None) -> float:
    text = str(raw or "").strip()
    return float(text) if text else float("nan")


def iter_months(start_month: str, end_month: str) -> tuple[str, ...]:
    """Return inclusive YYYY-MM months in chronological order."""
    start = datetime.strptime(start_month, "%Y-%m")
    end = datetime.strptime(end_month, "%Y-%m")
    if end < start:
        raise ValueError("end_month cannot precede start_month")
    rows = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        rows.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month = 1
            year += 1
    return tuple(rows)


def previous_month(month: str) -> str:
    value = datetime.strptime(month, "%Y-%m")
    year, number = value.year, value.month - 1
    if number == 0:
        year -= 1
        number = 12
    return f"{year:04d}-{number:02d}"


def parse_kline_archive(path: str | Path) -> Iterator[KlineRow]:
    """Yield validated rows without extracting the ZIP to disk."""
    archive_path = Path(path)
    with ZipFile(archive_path) as bundle:
        csv_members = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if len(csv_members) != 1:
            raise ValueError(f"expected exactly one CSV in {archive_path.name}")
        with bundle.open(csv_members[0]) as raw:
            text = (line.decode("utf-8-sig") for line in raw)
            for fields in csv.reader(text):
                if not fields or not fields[0].strip().lstrip("-").isdigit():
                    continue
                if len(fields) < 11:
                    raise ValueError(f"incomplete kline row in {archive_path.name}")
                yield KlineRow(
                    open_time_ms=_normalize_timestamp_ms(fields[0]),
                    open=float(fields[1]),
                    high=float(fields[2]),
                    low=float(fields[3]),
                    close=float(fields[4]),
                    base_volume=float(fields[5]),
                    close_time_ms=_normalize_timestamp_ms(fields[6]),
                    quote_volume=float(fields[7]),
                    trades=int(fields[8]),
                    taker_buy_base_volume=float(fields[9]),
                    taker_buy_quote_volume=float(fields[10]),
                )


def _single_csv_member(bundle: ZipFile, path: Path) -> str:
    members = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
    if len(members) != 1:
        raise ValueError(f"expected exactly one CSV in {path.name}")
    return members[0]


def parse_metrics_archive(path: str | Path) -> Iterator[MetricsRow]:
    archive_path = Path(path)
    with ZipFile(archive_path) as bundle:
        member = _single_csv_member(bundle, archive_path)
        with bundle.open(member) as raw:
            text = (line.decode("utf-8-sig") for line in raw)
            for row in csv.DictReader(text):
                if not row.get("create_time") or not row.get("symbol"):
                    continue
                timestamp = datetime.strptime(
                    row["create_time"].strip(), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                yield MetricsRow(
                    create_time_ms=int(timestamp.timestamp() * 1000),
                    symbol=row["symbol"].strip(),
                    sum_open_interest=_optional_float(row.get("sum_open_interest")),
                    sum_open_interest_value=_optional_float(row.get("sum_open_interest_value")),
                    count_toptrader_long_short_ratio=_optional_float(row.get("count_toptrader_long_short_ratio")),
                    sum_toptrader_long_short_ratio=_optional_float(row.get("sum_toptrader_long_short_ratio")),
                    count_long_short_ratio=_optional_float(row.get("count_long_short_ratio")),
                    sum_taker_long_short_vol_ratio=_optional_float(row.get("sum_taker_long_short_vol_ratio")),
                )


def parse_funding_archive(path: str | Path) -> Iterator[FundingRow]:
    archive_path = Path(path)
    with ZipFile(archive_path) as bundle:
        member = _single_csv_member(bundle, archive_path)
        with bundle.open(member) as raw:
            text = (line.decode("utf-8-sig") for line in raw)
            for row in csv.DictReader(text):
                if not row.get("calc_time"):
                    continue
                yield FundingRow(
                    calc_time_ms=_normalize_timestamp_ms(row["calc_time"]),
                    funding_interval_hours=int(row["funding_interval_hours"]),
                    last_funding_rate=float(row["last_funding_rate"]),
                )


def parse_agg_trades_archive(path: str | Path) -> Iterator[AggTradeRow]:
    """Yield official USD-M aggregate trades without extracting the ZIP."""
    archive_path = Path(path)
    with ZipFile(archive_path) as bundle:
        member = _single_csv_member(bundle, archive_path)
        with bundle.open(member) as raw:
            text = (line.decode("utf-8-sig") for line in raw)
            for fields in csv.reader(text):
                if not fields or not fields[0].strip().lstrip("-").isdigit():
                    continue
                if len(fields) < 7:
                    raise ValueError(
                        f"incomplete aggregate-trade row in {archive_path.name}"
                    )
                maker = fields[6].strip().lower()
                if maker not in {"true", "false"}:
                    raise ValueError(
                        f"invalid buyer-maker flag in {archive_path.name}: {fields[6]}"
                    )
                yield AggTradeRow(
                    aggregate_trade_id=int(fields[0]),
                    price=float(fields[1]),
                    quantity=float(fields[2]),
                    first_trade_id=int(fields[3]),
                    last_trade_id=int(fields[4]),
                    transact_time_ms=_normalize_timestamp_ms(fields[5]),
                    is_buyer_maker=maker == "true",
                )


def summarize_archive(path: str | Path, archive: MonthlyArchive) -> ArchiveSummary:
    rows = 0
    quote_volume = 0.0
    first: int | None = None
    last: int | None = None
    for row in parse_kline_archive(path):
        rows += 1
        quote_volume += row.quote_volume
        first = row.open_time_ms if first is None else min(first, row.open_time_ms)
        last = row.open_time_ms if last is None else max(last, row.open_time_ms)
    return ArchiveSummary(
        symbol=archive.symbol,
        timeframe=archive.timeframe,
        month=archive.month,
        rows=rows,
        quote_volume=quote_volume,
        first_open_time_ms=first,
        last_open_time_ms=last,
    )


class BinanceVisionArchiveClient:
    """Download public archives with official SHA-256 validation and resume."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 45.0,
        retries: int = 3,
        backoff_seconds: float = 0.5,
    ):
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.retries = max(0, int(retries))
        self.backoff_seconds = max(0.0, float(backoff_seconds))

    def _get(self, url: str, **kwargs):
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout_seconds, **kwargs)
                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt >= self.retries:
                        response.raise_for_status()
                else:
                    return response
            except (requests.Timeout, requests.ConnectionError):
                if attempt >= self.retries:
                    raise
            if self.backoff_seconds:
                time.sleep(self.backoff_seconds * (2 ** attempt))
        raise RuntimeError("archive request exhausted retries")

    def list_symbols(self) -> tuple[str, ...]:
        response = self._get(
            S3_INDEX_URL,
            params={"delimiter": "/", "prefix": f"{MONTHLY_KLINE_PREFIX}/"},
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        prefixes = [
            node.text or ""
            for node in root.findall(".//{*}CommonPrefixes/{*}Prefix")
        ]
        symbols = {
            prefix.rstrip("/").split("/")[-1]
            for prefix in prefixes
            if prefix.rstrip("/").split("/")[-1].endswith("USDT")
        }
        return tuple(sorted(symbols))

    def list_monthly_archives(
        self,
        symbol: str,
        timeframe: str,
    ) -> tuple[MonthlyArchive, ...]:
        clean = str(symbol).upper().strip()
        prefix = f"{MONTHLY_KLINE_PREFIX}/{clean}/{timeframe}/"
        response = self._get(S3_INDEX_URL, params={"prefix": prefix})
        response.raise_for_status()
        root = ET.fromstring(response.content)
        rows = []
        filename_prefix = f"{clean}-{timeframe}-"
        for node in root.findall(".//{*}Contents/{*}Key"):
            key = node.text or ""
            filename = key.rsplit("/", 1)[-1]
            if not filename.startswith(filename_prefix) or not filename.endswith(".zip"):
                continue
            month = filename[len(filename_prefix):-4]
            try:
                datetime.strptime(month, "%Y-%m")
            except ValueError:
                continue
            rows.append(MonthlyArchive(clean, timeframe, month))
        return tuple(sorted(rows, key=lambda row: row.month))

    @staticmethod
    def _parse_checksum(text: str) -> str:
        token = str(text).strip().split()[0].lower()
        if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
            raise ValueError("invalid Binance checksum payload")
        return token

    @staticmethod
    def sha256(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def download(
        self,
        archive: (
            MonthlyArchive
            | DailyAggTradesArchive
            | DailyMetricsArchive
            | MonthlyFundingArchive
        ),
        destination: str | Path,
    ) -> Path | None:
        """Return None for a genuine 404; raise for corruption/other failures."""
        destination = Path(destination)
        marker = destination.with_suffix(destination.suffix + ".sha256")
        if destination.exists() and marker.exists():
            try:
                locally_verified = self._parse_checksum(marker.read_text(encoding="ascii"))
            except (OSError, ValueError):
                locally_verified = ""
            if locally_verified and self.sha256(destination) == locally_verified:
                return destination
        checksum_response = self._get(f"{archive.url}.CHECKSUM")
        if checksum_response.status_code == 404:
            return None
        checksum_response.raise_for_status()
        expected = self._parse_checksum(checksum_response.text)
        if destination.exists():
            actual = self.sha256(destination)
            if actual == expected:
                marker.write_text(expected + "\n", encoding="ascii")
                return destination

        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._get(archive.url, stream=True)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                digest = hashlib.sha256()
                with partial.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                actual = digest.hexdigest()
                if actual != expected:
                    raise ValueError(
                        f"checksum mismatch for {archive.filename}: {actual} != {expected}"
                    )
                os.replace(partial, destination)
                marker.write_text(expected + "\n", encoding="ascii")
                return destination
            except (requests.Timeout, requests.ConnectionError, ValueError) as exc:
                last_error = exc
                partial.unlink(missing_ok=True)
                if attempt >= self.retries:
                    raise
                if self.backoff_seconds:
                    time.sleep(self.backoff_seconds * (2 ** attempt))
        raise RuntimeError(f"archive download failed: {archive.filename}") from last_error


def expected_bars_in_month(month: str, timeframe_minutes: int) -> int:
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    year, number = start.year, start.month + 1
    if number == 13:
        year += 1
        number = 1
    end = datetime(year, number, 1, tzinfo=timezone.utc)
    return int((end - start).total_seconds() // (timeframe_minutes * 60))
