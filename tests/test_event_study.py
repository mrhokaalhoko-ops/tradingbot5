from datetime import datetime, timedelta, timezone

from trading_v2.event_study import (
    DirectionalEvent,
    calendar_split_events,
    summarize_events,
)
from trading_v2.models import Side
from scripts.run_cross_sectional_event_study import is_sample_boundary


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(hours, net_bps, opposite_bps):
    return DirectionalEvent(
        symbol=f"S{hours}/USDT",
        side=Side.BUY,
        timestamp=T0 + timedelta(hours=hours),
        entry_price=100,
        exit_price=101,
        directional_gross_bps=100,
        directional_net_bps=net_bps,
        opposite_net_bps=opposite_bps,
        mfe_pct=2,
        mae_pct=1,
    )


def test_event_summary_keeps_original_and_opposite_costs_separate():
    summary = summarize_events((_event(1, 88, -112), _event(2, -62, 38)))

    assert summary["events"] == 2
    assert summary["original_mean_net_bps"] == 13
    assert summary["opposite_mean_net_bps"] == -37


def test_event_calendar_split_uses_shared_boundaries():
    events = (_event(24, 1, -1), _event(48, 1, -1), _event(168, 1, -1), _event(216, 1, -1))
    splits = calendar_split_events(events, start=T0, end=T0 + timedelta(days=10))

    assert [len(splits[name]) for name in ("TRAIN", "VALIDATION", "TEST")] == [2, 1, 1]


def test_binance_close_timestamp_aligns_to_next_sampling_minute():
    close_time = T0 + timedelta(minutes=59, seconds=59, milliseconds=999)

    assert is_sample_boundary(close_time, 60) is True
    assert is_sample_boundary(close_time, 120) is False
