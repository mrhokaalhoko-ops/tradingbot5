from trading_v2.binance_archive import AggTradeRow
from scripts.build_v5_aggtrade_pilot import (
    aggregate_and_validate_trades,
    aggregate_trades,
    validate_rows,
)


def _trade(identifier, timestamp, price, quantity, buyer_maker):
    return AggTradeRow(
        aggregate_trade_id=identifier,
        price=price,
        quantity=quantity,
        first_trade_id=identifier * 10,
        last_trade_id=identifier * 10,
        transact_time_ms=timestamp,
        is_buyer_maker=buyer_maker,
    )


def test_aggregation_computes_causal_aggressor_imbalance():
    rows = [
        _trade(1, 1_780_000_000_000, 100.0, 2.0, False),
        _trade(2, 1_780_000_010_000, 100.0, 1.0, True),
    ]
    frame = aggregate_trades(rows)
    assert len(frame) == 1
    assert frame.iloc[0]["buy_notional"] == 200.0
    assert frame.iloc[0]["sell_notional"] == 100.0
    assert abs(frame.iloc[0]["aggressor_imbalance"] - 1 / 3) < 1e-12


def test_validation_rejects_duplicate_ids_and_decreasing_time():
    rows = [
        _trade(1, 1_780_000_010_000, 100.0, 1.0, False),
        _trade(1, 1_780_000_000_000, 100.0, 1.0, True),
    ]
    report = validate_rows(rows)
    assert not report["passed"]
    assert "DUPLICATE_AGGREGATE_TRADE_ID" in report["errors"]
    assert "NON_INCREASING_AGGREGATE_TRADE_ID" in report["errors"]
    assert "DECREASING_TIMESTAMP" in report["errors"]


def test_streaming_aggregation_matches_batch_aggregation():
    rows = [
        _trade(1, 1_780_000_000_000, 100.0, 2.0, False),
        _trade(2, 1_780_000_010_000, 100.0, 1.0, True),
        _trade(3, 1_780_000_070_000, 101.0, 1.0, False),
    ]
    batch = aggregate_trades(rows)
    streamed, validation = aggregate_and_validate_trades(iter(rows))
    assert validation["passed"]
    assert validation["rows"] == 3
    assert len(streamed) == len(batch) == 2
    columns = [
        "total_notional", "buy_notional", "sell_notional",
        "signed_notional", "vwap", "aggressor_imbalance",
    ]
    for column in columns:
        assert all(abs(streamed[column] - batch[column]) < 1e-12)
