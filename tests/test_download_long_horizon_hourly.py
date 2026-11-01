import pytest

from scripts.download_long_horizon_hourly import archive_tasks, month_range


def test_month_range_is_inclusive_across_year_boundary():
    assert month_range("2025-11", "2026-02") == (
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    )


def test_month_range_rejects_reverse_interval():
    with pytest.raises(ValueError, match="precedes"):
        month_range("2026-02", "2025-11")


def test_archive_tasks_are_deduplicated_sorted_hourly():
    tasks = archive_tasks(("ethusdt", "BTCUSDT", "ethusdt"), "2026-01", "2026-02")
    assert [(row.symbol, row.timeframe, row.month) for row in tasks] == [
        ("BTCUSDT", "1h", "2026-01"),
        ("BTCUSDT", "1h", "2026-02"),
        ("ETHUSDT", "1h", "2026-01"),
        ("ETHUSDT", "1h", "2026-02"),
    ]
