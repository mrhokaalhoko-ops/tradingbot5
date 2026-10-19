from scripts.profile_causal_dataset import _bucket, _summary


def test_profile_side_bucket_is_signed_and_not_absolute():
    assert _bucket(10.0, 10.0) == "BUY"
    assert _bucket(-10.0, 10.0) == "SELL"
    assert _bucket(9.99, 10.0) is None


def test_profile_marks_same_window_stop_and_target_ambiguous():
    result = _summary([
        {"net_bps": 1, "mfe_pct": 2, "mae_pct": 1, "tp_hit": True, "sl_hit": True},
        {"net_bps": -1, "mfe_pct": 1, "mae_pct": 2, "tp_hit": False, "sl_hit": True},
    ])

    assert result["events"] == 2
    assert result["atr_1r_target_2r"]["ambiguous"] == 1
    assert result["atr_1r_target_2r"]["sl_first_proxy"] == 1
