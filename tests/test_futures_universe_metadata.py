from scripts.audit_futures_universe_metadata import audit_universe


def test_audit_universe_classifies_and_preserves_missing_symbols():
    universe = {
        "monthly": {
            "2026-01": {"symbols": [{"symbol": "BTCUSDT"}, {"symbol": "XAUUSDT"}]},
            "2026-02": {"symbols": [{"symbol": "BTCUSDT"}, {"symbol": "OLDUSDT"}]},
        }
    }
    exchange_info = {
        "symbols": [
            {
                "symbol": "BTCUSDT", "underlyingType": "COIN",
                "underlyingSubType": [], "contractType": "PERPETUAL", "status": "TRADING",
            },
            {
                "symbol": "XAUUSDT", "underlyingType": "COMMODITY",
                "underlyingSubType": ["TradFi"], "contractType": "PERPETUAL", "status": "TRADING",
            },
        ]
    }

    result = audit_universe(exchange_info=exchange_info, universe=universe, observed_at="2026-08-30T00:00:00Z")

    assert result["universe_symbols"] == 3
    assert result["found_in_snapshot"] == 2
    assert result["type_counts"] == {"COIN": 1, "COMMODITY": 1, "UNKNOWN": 1}
    assert result["monthly_type_counts"]["2026-01"] == {"COIN": 1, "COMMODITY": 1}
    assert result["monthly_type_counts"]["2026-02"] == {"COIN": 1, "UNKNOWN": 1}
    missing = next(row for row in result["symbols"] if row["symbol"] == "OLDUSDT")
    assert missing["found_in_snapshot"] is False
