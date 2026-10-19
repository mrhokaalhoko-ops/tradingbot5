from scripts.build_derivatives_feature_dataset import OUTPUT_COLUMNS


def test_derivatives_partition_keeps_source_timestamps_and_staleness():
    required = {
        "close_time", "metrics_time", "funding_time",
        "metrics_staleness_seconds", "funding_staleness_seconds",
        "oi_value_change_1h_pct", "funding_rate_bps",
    }
    assert required.issubset(OUTPUT_COLUMNS)
    assert len(OUTPUT_COLUMNS) == len(set(OUTPUT_COLUMNS))
