from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.evaluate_causal_rules import _episodes, _stats


def test_episode_cooldown_deduplicates_same_symbol():
    frame = pd.DataFrame(
        {
            "symbol": ["A", "A", "B"],
            "open_time": pd.to_datetime(
                ["2026-01-01 00:55", "2026-01-01 01:55", "2026-01-01 00:55"], utc=True
            ),
            "net_bps": [1.0, 2.0, -1.0],
            "net_r_proxy": [1.0, 2.0, -1.0],
            "tp_hit": [True, True, False],
            "sl_hit": [False, False, True],
        }
    )

    result = _episodes(frame, 4)

    assert len(result) == 2
    assert result["symbol"].tolist() == ["A", "B"]


def test_stats_does_not_call_ambiguous_bar_a_win():
    frame = pd.DataFrame(
        {
            "symbol": ["A", "B"],
            "net_bps": [1.0, -1.0],
            "net_r_proxy": [0.1, -0.1],
            "tp_hit": [True, False],
            "sl_hit": [True, True],
        }
    )

    result = _stats(frame)

    assert result["ambiguous_proxy"] == 1
    assert result["tp_hit_proxy"] == 0
