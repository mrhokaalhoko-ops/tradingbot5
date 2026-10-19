import pandas as pd

from scripts.download_active_event_klines import event_tasks


def test_event_tasks_deduplicate_and_include_midnight_context():
    rows = pd.DataFrame([
        {"variant": "IMMEDIATE_CONTINUATION", "event_score": 9.0, "symbol": "ALTUSDT", "signal_time": "2026-01-02T00:05:00Z"},
        {"variant": "PULLBACK_RECLAIM", "event_score": 9.0, "symbol": "ALTUSDT", "signal_time": "2026-01-02T00:05:00Z"},
        {"variant": "IMMEDIATE_CONTINUATION", "event_score": 7.0, "symbol": "LOWUSDT", "signal_time": "2026-01-03T12:00:00Z"},
    ])
    tasks = event_tasks(rows, min_score=8.0)
    assert [(task.symbol, task.day) for task in tasks] == [
        ("ALTUSDT", "2026-01-01"),
        ("ALTUSDT", "2026-01-02"),
    ]


def test_raw_event_source_does_not_drop_events_rejected_by_portfolio_capacity():
    rows = pd.DataFrame([
        {"event_score": 9.0, "symbol": "CRCLUSDT", "close_time": "2026-06-01T13:34:59.999Z"},
    ])

    tasks = event_tasks(rows, min_score=8.0)

    assert [(task.symbol, task.day) for task in tasks] == [
        ("CRCLUSDT", "2026-06-01"),
    ]
