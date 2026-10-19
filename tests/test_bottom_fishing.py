import os
import sys

import pandas as pd

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import Config
from strategies.bottom_fishing_strategy import BottomFishingStrategy
from strategies.smc_rsi_strategy import StrategyContext


class MockClient:
    def fetch_ohlcv(self, symbol, timeframe, limit=50):
        dates = pd.date_range(end=pd.Timestamp.now(), periods=limit, freq="4h")
        close_prices = [100 - i * 1.5 for i in range(limit)]
        return pd.DataFrame(
            {
                "timestamp": dates,
                "open": close_prices,
                "high": [price + 0.5 for price in close_prices],
                "low": [price - 0.5 for price in close_prices],
                "close": close_prices,
                "volume": [1000 + i * 10 for i in range(limit)],
            }
        )


def test_bottom_fishing():
    Config.BOTTOM_FISHING_ENABLED = True
    Config.BOTTOM_FISHING_HTF = "4h"
    Config.BOTTOM_FISHING_RSI_THRESHOLD = 30.0

    close_15m = [50 - i * 0.2 for i in range(50)]
    bars_df = pd.DataFrame(
        {
            "timestamp": pd.date_range(end=pd.Timestamp.now(), periods=50, freq="15min"),
            "open": close_15m,
            "high": [price + 0.1 for price in close_15m],
            "low": [price - 0.1 for price in close_15m],
            "close": close_15m,
            "volume": [200 + i for i in range(50)],
            "rsi": [25.0] * 50,
        }
    )

    context = StrategyContext(
        symbol="BTC/USDT",
        price=close_15m[-1],
        bars_df=bars_df,
        ta_data={"engine_trend": "neutral", "engine_reason": "test"},
        coin_data={"coin": "BTC", "price": close_15m[-1]},
        ticker=None,
        client=MockClient(),
    )

    result = BottomFishingStrategy().execute(context)
    assert result["signal"] == "buy"
