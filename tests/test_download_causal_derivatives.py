from scripts.download_causal_derivatives import _month_days, selected_derivatives_tasks


def test_month_days_handles_leap_year():
    rows = _month_days("2024-02")
    assert rows[0] == "2024-02-01"
    assert rows[-1] == "2024-02-29"
    assert len(rows) == 29


def test_derivatives_tasks_are_deduplicated_and_include_warmup():
    universe = {
        "monthly": {
            "2026-01": {"symbols": [{"symbol": "BTCUSDT"}]},
            "2026-02": {"symbols": [{"symbol": "BTCUSDT"}]},
        }
    }
    metrics, funding = selected_derivatives_tasks(universe)
    metric_keys = {(row.symbol, row.day) for row in metrics}
    funding_keys = {(row.symbol, row.month) for row in funding}
    assert ("BTCUSDT", "2025-12-31") in metric_keys
    assert ("BTCUSDT", "2026-01-31") in metric_keys
    assert ("BTCUSDT", "2026-02-28") in metric_keys
    assert len(metric_keys) == 60
    assert funding_keys == {
        ("BTCUSDT", "2025-12"),
        ("BTCUSDT", "2026-01"),
        ("BTCUSDT", "2026-02"),
    }
