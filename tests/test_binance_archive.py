from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from trading_v2.binance_archive import (
    DailyKlineArchive,
    DailyAggTradesArchive,
    DailyMetricsArchive,
    MonthlyFundingArchive,
    BinanceVisionArchiveClient,
    MonthlyArchive,
    expected_bars_in_month,
    iter_months,
    parse_kline_archive,
    parse_agg_trades_archive,
    parse_funding_archive,
    parse_metrics_archive,
    previous_month,
)


def _zip_payload() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as bundle:
        bundle.writestr(
            "BTCUSDT-5m-2026-01.csv",
            "open_time,open,high,low,close,volume,close_time,quote_volume,trades,taker_base,taker_quote,ignore\n"
            "1735689600000,100,102,99,101,10,1735689899999,1005,20,6,603,0\n",
        )
    return output.getvalue()


def _csv_zip(name: str, content: str) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as bundle:
        bundle.writestr(name, content)
    return output.getvalue()


class _Response:
    def __init__(self, *, content=b"", text="", status=200):
        self.content = content
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_content(self, chunk_size=0):
        yield self.content


class _Session:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if url.endswith(".CHECKSUM"):
            return _Response(text=f"{sha256(self.payload).hexdigest()}  fixture.zip")
        return _Response(content=self.payload)


class _IndexSession:
    def get(self, url, **kwargs):
        xml = b"""<?xml version='1.0' encoding='UTF-8'?>
        <ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>
          <Contents><Key>data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-01.zip</Key></Contents>
          <Contents><Key>data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-01.zip.CHECKSUM</Key></Contents>
          <Contents><Key>data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-02.zip</Key></Contents>
        </ListBucketResult>"""
        return _Response(content=xml)


def test_month_helpers_are_inclusive_and_cross_year():
    assert iter_months("2025-11", "2026-02") == (
        "2025-11", "2025-12", "2026-01", "2026-02"
    )
    assert previous_month("2026-01") == "2025-12"
    assert expected_bars_in_month("2024-02", 5) == 29 * 24 * 12


def test_archive_parser_skips_header_and_reads_quote_volume(tmp_path: Path):
    path = tmp_path / "fixture.zip"
    path.write_bytes(_zip_payload())

    rows = list(parse_kline_archive(path))

    assert len(rows) == 1
    assert rows[0].close == 101
    assert rows[0].quote_volume == 1005


def test_verified_download_is_atomic_and_reusable(tmp_path: Path):
    payload = _zip_payload()
    session = _Session(payload)
    client = BinanceVisionArchiveClient(session=session, retries=0)
    archive = MonthlyArchive("BTCUSDT", "5m", "2026-01")
    destination = tmp_path / archive.filename

    assert client.download(archive, destination) == destination
    assert destination.read_bytes() == payload
    assert destination.with_suffix(".zip.sha256").exists()
    assert not destination.with_suffix(".zip.part").exists()
    assert client.download(archive, destination) == destination
    assert session.calls == 2


def test_monthly_index_ignores_checksum_sidecars():
    client = BinanceVisionArchiveClient(session=_IndexSession(), retries=0)

    rows = client.list_monthly_archives("BTCUSDT", "1h")

    assert [row.month for row in rows] == ["2026-01", "2026-02"]


def test_metrics_and_funding_archive_paths_are_official_layouts():
    kline = DailyKlineArchive("BTCUSDT", "1m", "2026-07-15")
    agg_trades = DailyAggTradesArchive("BTCUSDT", "2026-07-15")
    metrics = DailyMetricsArchive("BTCUSDT", "2026-07-15")
    funding = MonthlyFundingArchive("BTCUSDT", "2026-07")
    assert kline.key.endswith("daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-07-15.zip")
    assert agg_trades.key.endswith(
        "daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-07-15.zip"
    )
    assert metrics.key.endswith("daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-07-15.zip")
    assert funding.key.endswith("monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-07.zip")


def test_metrics_parser_reads_5m_open_interest_snapshot(tmp_path: Path):
    path = tmp_path / "metrics.zip"
    path.write_bytes(_csv_zip(
        "BTCUSDT-metrics-2026-07-15.csv",
        "create_time,symbol,sum_open_interest,sum_open_interest_value,"
        "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
        "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
        "2026-07-15 00:00:00,BTCUSDT,105.5,6858.6,1.2,1.4,1.1,1.5\n",
    ))
    rows = list(parse_metrics_archive(path))
    assert len(rows) == 1
    assert rows[0].symbol == "BTCUSDT"
    assert rows[0].sum_open_interest == 105.5
    assert rows[0].sum_taker_long_short_vol_ratio == 1.5


def test_metrics_parser_preserves_empty_ratio_as_missing(tmp_path: Path):
    path = tmp_path / "metrics-empty.zip"
    path.write_bytes(_csv_zip(
        "BTCUSDT-metrics-2026-07-15.csv",
        "create_time,symbol,sum_open_interest,sum_open_interest_value,"
        "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
        "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
        "2026-07-15 00:00:00,BTCUSDT,105.5,6858.6,1.2,1.4,1.1,\n",
    ))
    rows = list(parse_metrics_archive(path))
    assert rows[0].sum_taker_long_short_vol_ratio != rows[0].sum_taker_long_short_vol_ratio


def test_funding_parser_reads_interval_and_rate(tmp_path: Path):
    path = tmp_path / "funding.zip"
    path.write_bytes(_csv_zip(
        "BTCUSDT-fundingRate-2026-07.csv",
        "calc_time,funding_interval_hours,last_funding_rate\n"
        "1782864000000,8,0.00005532\n",
    ))
    rows = list(parse_funding_archive(path))
    assert len(rows) == 1
    assert rows[0].funding_interval_hours == 8
    assert rows[0].last_funding_rate == 0.00005532


def test_agg_trade_parser_reads_aggressor_side_and_notional(tmp_path: Path):
    path = tmp_path / "aggTrades.zip"
    path.write_bytes(_csv_zip(
        "BTCUSDT-aggTrades-2026-07-15.csv",
        "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
        "100,65000.0,0.010,200,201,1784073600123,false\n"
        "101,64999.0,0.020,202,202,1784073600456,true\n",
    ))
    rows = list(parse_agg_trades_archive(path))
    assert len(rows) == 2
    assert rows[0].aggressor_side == "BUY"
    assert rows[1].aggressor_side == "SELL"
    assert rows[0].quote_notional == 650.0
