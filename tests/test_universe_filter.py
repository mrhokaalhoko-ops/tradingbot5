from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from core.config import Config
from core.universe_filter import UniverseFilter


class _Exchange:
    def __init__(self, ticker):
        self.ticker = ticker

    def fetch_tickers(self):
        return {"LOW/USDT:USDT": self.ticker}

    def load_markets(self):
        return {
            "LOW/USDT:USDT": {
                "type": "swap",
                "active": True,
            }
        }


def _filter(ticker):
    client = SimpleNamespace(exchange=_Exchange(ticker))
    result = UniverseFilter(client)
    result.min_volume_24h = 10_000_000
    result.min_volume_24h_relaxed = 2_000_000
    return result


def test_price_percentage_cannot_masquerade_as_volume_spike():
    ticker = {
        "quoteVolume": 3_000_000,
        "percentage": 500.0,
        "bid": 10.0,
        "ask": 10.001,
        "info": {},
    }
    with patch.object(Config, "USE_TESTNET", False):
        eligible = _filter(ticker).filter_and_get_tickers()

    assert eligible == []


def test_explicit_quote_volume_change_can_unlock_relaxed_liquidity_tier():
    ticker = {
        "quoteVolume": 3_000_000,
        "percentage": 5.0,
        "quoteVolumeChangePercent": 250.0,
        "bid": 10.0,
        "ask": 10.001,
        "info": {},
    }
    with patch.object(Config, "USE_TESTNET", False):
        eligible = _filter(ticker).filter_and_get_tickers()

    assert [row[0] for row in eligible] == ["LOW/USDT:USDT"]


def test_dynamic_blacklist_does_not_learn_from_five_losses():
    history = pd.DataFrame(
        {
            "symbol": ["NOISY/USDT"] * 5,
            "close_time": pd.date_range("2026-08-01", periods=5, freq="D"),
            "pnl": [-1.0] * 5,
            "pnl_pct": [-1.0] * 5,
        }
    )

    with (
        patch.object(Config, "USE_TESTNET", False),
        patch.object(Config, "DYNAMIC_BLACKLIST_ACTIVE", True),
        patch.object(Config, "DYNAMIC_BLACKLIST_MIN_TRADES", 20),
        patch.object(Config, "DATA_DIR", "/fake"),
        patch("os.path.exists", return_value=True),
        patch("pandas.read_csv", return_value=history),
    ):
        blacklist = UniverseFilter(SimpleNamespace()).get_dynamic_blacklist()

    assert "NOISY/USDT" not in blacklist
