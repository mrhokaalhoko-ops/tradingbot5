import numpy as np
import pandas as pd

from strategy.market_regime import (
    RANGE,
    STRONG_RISK_OFF,
    STRONG_RISK_ON,
    classify_market_regime,
)


def _bars(start, end, rows=260):
    close = np.linspace(start, end, rows)
    spread = np.maximum(close * 0.003, 0.01)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": np.full(rows, 1000.0),
        }
    )


def test_btc_and_eth_closed_up_structure_is_strong_risk_on():
    result = classify_market_regime(_bars(70, 130), _bars(50, 95))

    assert result["label"] == STRONG_RISK_ON
    assert result["known"] is True


def test_btc_and_eth_closed_down_structure_is_strong_risk_off():
    result = classify_market_regime(_bars(130, 70), _bars(95, 50))

    assert result["label"] == STRONG_RISK_OFF


def test_disagreement_does_not_invent_directional_regime():
    result = classify_market_regime(_bars(70, 130), _bars(95, 50))

    assert result["label"] == RANGE
