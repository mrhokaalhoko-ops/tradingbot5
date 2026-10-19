from scripts.download_launch_klines import launch_tasks, next_month


def test_launch_tasks_include_following_month_for_72h_window():
    universe = {"launches": [
        {"symbol": "NEWUSDT", "archive_month": "2025-12"},
        {"symbol": "NEWUSDT", "archive_month": "2025-12"},
    ]}
    rows = launch_tasks(universe)
    assert next_month("2025-12") == "2026-01"
    assert [(row.symbol, row.month) for row in rows] == [
        ("NEWUSDT", "2025-12"), ("NEWUSDT", "2026-01"),
    ]


def test_launch_tasks_can_download_finer_one_minute_archives():
    rows = launch_tasks({"launches": [{"symbol": "NEWUSDT", "archive_month": "2025-12"}]}, "1m")
    assert [row.timeframe for row in rows] == ["1m", "1m"]
