from scripts.download_causal_klines import selected_tasks


def test_selected_tasks_include_warmup_month_without_future_months():
    universe = {
        "monthly": {
            "2026-01": {"symbols": [{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}]},
            "2026-02": {"symbols": [{"symbol": "ETHUSDT"}]},
        }
    }

    tasks = selected_tasks(universe)

    assert {(row.symbol, row.month) for row in tasks} == {
        ("BTCUSDT", "2025-12"),
        ("BTCUSDT", "2026-01"),
        ("ETHUSDT", "2025-12"),
        ("ETHUSDT", "2026-01"),
        ("ETHUSDT", "2026-02"),
    }
