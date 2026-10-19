import pytest

from scripts.build_v5_aggtrade_dataset import (
    causal_monthly_symbols,
    iter_days,
)


def test_iter_days_is_inclusive_and_validates_order():
    assert iter_days("2026-05-30", "2026-06-01") == (
        "2026-05-30", "2026-05-31", "2026-06-01"
    )
    with pytest.raises(ValueError):
        iter_days("2026-06-01", "2026-05-31")


def test_monthly_symbols_preserve_prior_month_rank_order():
    universe = {
        "monthly": {
            "2026-05": {
                "symbols": [
                    {"symbol": "EQUITYUSDT", "rank": 1},
                    {"symbol": "BBBUSDT", "rank": 3},
                    {"symbol": "AAAUSDT", "rank": 2},
                ]
            }
        }
    }
    metadata = {
        "symbols": [
            {"symbol": "AAAUSDT", "underlying_type": "COIN"},
            {"symbol": "BBBUSDT", "underlying_type": "COIN"},
            {"symbol": "EQUITYUSDT", "underlying_type": "EQUITY"},
        ]
    }
    selected = causal_monthly_symbols(
        universe, metadata, {"2026-05"}, limit=2
    )
    assert selected["2026-05"] == ("AAAUSDT", "BBBUSDT")
