from datetime import datetime, timezone

from scripts.build_launch_event_universe import detect_launches


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_launch_detection_requires_intramonth_first_bar_and_crypto_type():
    summaries = [
        {"symbol": "NEWUSDT", "month": "2026-01", "first_open_time_ms": _ms("2026-01-10T12:00:00"), "rows": 500},
        {"symbol": "OLDUSDT", "month": "2026-01", "first_open_time_ms": _ms("2026-01-01T00:00:00"), "rows": 744},
        {"symbol": "STOCKUSDT", "month": "2026-01", "first_open_time_ms": _ms("2026-01-10T12:00:00"), "rows": 500},
    ]
    info = {"symbols": [
        {"symbol": "NEWUSDT", "underlyingType": "COIN", "contractType": "PERPETUAL", "status": "TRADING"},
        {"symbol": "OLDUSDT", "underlyingType": "COIN", "contractType": "PERPETUAL", "status": "TRADING"},
        {"symbol": "STOCKUSDT", "underlyingType": "EQUITY", "contractType": "PERPETUAL", "status": "TRADING"},
    ]}
    result = detect_launches(summaries, info, baseline_month="2025-07")
    assert [row["symbol"] for row in result] == ["NEWUSDT"]
    assert result[0]["launch_time"] == "2026-01-10T12:00:00+00:00"
