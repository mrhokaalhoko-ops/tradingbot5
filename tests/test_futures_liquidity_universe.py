from trading_v2.binance_archive import ArchiveSummary
from scripts.build_futures_liquidity_universe import select_monthly_universe


def _summary(symbol: str, volume: float, rows: int = 744) -> ArchiveSummary:
    return ArchiveSummary(symbol, "1h", "2026-01", rows, volume, 1, 2)


def test_universe_uses_quote_volume_and_rejects_partial_months():
    selected = select_monthly_universe(
        [
            _summary("AUSDT", 100),
            _summary("BUSDT", 300),
            _summary("CUSDT", 1000, rows=20),
        ],
        size=2,
        minimum_coverage=0.75,
    )

    assert [row.symbol for row in selected] == ["BUSDT", "AUSDT"]
