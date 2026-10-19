import numpy as np
import pandas as pd

from engines.ta_engine import TAEngine
from strategy.candidate_scoring import score_candidate


def test_rvol_uses_prior_window_and_not_the_forming_candle():
    rows = 80
    close = np.linspace(100.0, 105.0, rows)
    volume = np.ones(rows)
    volume[-2] = 2.0
    volume[-1] = 999.0
    frame = pd.DataFrame({
        "open": close - 0.1,
        "high": close + 0.3,
        "low": close - 0.3,
        "close": close,
        "volume": volume,
    })

    enriched = TAEngine().add_indicators(frame)
    last_closed = enriched.iloc[-2]

    assert last_closed["volume_avg_20"] == 1.0
    assert last_closed["volume_median_20"] == 1.0
    assert last_closed["rvol_20"] == 2.0


def test_large_opposing_volume_is_not_scored_as_full_confirmation():
    base = {
        "side": "buy",
        "open": 101.0,
        "price": 100.0,
        "rvol_20": 2.0,
    }
    result = score_candidate(base)
    volume = next(item for item in result["components"] if item["name"] == "volume")

    assert volume["available"] is True
    assert volume["value"] == 0.4
    assert "معاكس" in volume["evidence"]
