from datetime import datetime, timedelta, timezone

import pytest
import requests

from trading_v2.data import BinanceUsdMFuturesDataSource, CausalBarStore
from trading_v2.evaluation import (
    EvaluatedPlan,
    chronological_split,
    chronological_time_split,
    enforce_portfolio_capacity,
    summarize,
)
from trading_v2.models import (
    Bar,
    EntryKind,
    ReplayResult,
    ReplayStatus,
    Side,
    TradePlan,
)


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(minutes=0, timeframe="5m"):
    duration = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}[timeframe]
    opened = T0 + timedelta(minutes=minutes)
    return Bar(
        symbol="TEST/USDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=opened + timedelta(minutes=duration),
        open=100,
        high=101,
        low=99,
        close=100.5,
        volume=10,
    )


def _plan(index=0):
    created = T0 + timedelta(hours=index)
    return TradePlan(
        plan_id=f"plan-{index}",
        strategy_id="test",
        setup_id=f"setup-{index}",
        symbol="TEST/USDT",
        side=Side.BUY,
        created_at=created,
        valid_until=created + timedelta(minutes=10),
        exit_by=created + timedelta(hours=1),
        entry_kind=EntryKind.LIMIT,
        entry=100,
        stop=99,
        target=102,
        thesis="test",
    )


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payloads.pop(0))


class _FlakySession(_Session):
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if len(self.calls) == 1:
            raise requests.Timeout("temporary timeout")
        return _Response(self.payloads.pop(0))


def test_public_data_source_parses_and_caches_without_credentials(tmp_path):
    open_ms = int(T0.timestamp() * 1000)
    session = _Session([[[open_ms, "100", "101", "99", "100.5", "12", open_ms + 59_999]]])
    source = BinanceUsdMFuturesDataSource(session=session)

    bars = source.fetch(
        symbol="TESTUSDT",
        timeframe="1m",
        start=T0,
        end=T0 + timedelta(minutes=2),
    )

    assert len(bars) == 1
    assert bars[0].symbol == "TEST/USDT"
    assert session.calls[0][0].endswith("/fapi/v1/klines")
    assert "headers" not in session.calls[0][1]
    path = source.write_cache(tmp_path / "bars.jsonl.gz", bars)
    assert source.read_cache(path) == bars


def test_public_data_source_retries_a_transient_timeout():
    open_ms = int(T0.timestamp() * 1000)
    session = _FlakySession(
        [[[open_ms, "100", "101", "99", "100.5", "12", open_ms + 59_999]]]
    )
    source = BinanceUsdMFuturesDataSource(
        session=session,
        max_retries=1,
        retry_backoff_seconds=0,
    )

    bars = source.fetch(
        symbol="TESTUSDT",
        timeframe="1m",
        start=T0,
        end=T0 + timedelta(minutes=2),
    )

    assert len(bars) == 1
    assert len(session.calls) == 2


def test_causal_store_excludes_still_open_bar_and_between_is_inclusive():
    bars = (_bar(0), _bar(5))
    store = CausalBarStore("TEST/USDT", {"5m": bars})
    snapshot = store.snapshot(as_of=T0 + timedelta(minutes=7))
    assert snapshot.closed_bars("5m") == (bars[0],)
    assert store.between("5m", T0, T0 + timedelta(minutes=5)) == bars


def test_trade_plan_rejects_holding_window_that_ends_before_entry_window():
    with pytest.raises(ValueError, match="exit_by cannot precede"):
        TradePlan(
            plan_id="bad",
            strategy_id="test",
            setup_id="setup",
            symbol="TEST/USDT",
            side=Side.BUY,
            created_at=T0,
            valid_until=T0 + timedelta(hours=2),
            exit_by=T0 + timedelta(hours=1),
            entry_kind=EntryKind.LIMIT,
            entry=100,
            stop=99,
            target=102,
            thesis="bad lifecycle",
        )


def test_evaluation_uses_true_even_sample_median_and_chronological_split():
    rows = tuple(
        EvaluatedPlan(
            _plan(index),
            ReplayResult(
                plan_id=f"plan-{index}",
                status=ReplayStatus.TP_FIRST if value > 0 else ReplayStatus.SL_FIRST,
                net_r=value,
            ),
        )
        for index, value in enumerate((-1.0, 3.0, -0.5, 1.0, 2.0))
    )
    metrics = summarize(rows[:2])
    assert metrics["median_r"] == 1.0
    splits = chronological_split(rows)
    assert [len(splits[key]) for key in ("TRAIN", "VALIDATION", "TEST")] == [3, 1, 1]


def test_calendar_split_uses_shared_time_boundaries_not_record_counts():
    indices = (24, 48, 24 * 7, 24 * 9)
    rows = tuple(
        EvaluatedPlan(
            _plan(index),
            ReplayResult(
                plan_id=f"plan-{index}",
                status=ReplayStatus.TP_FIRST,
                net_r=1.0,
            ),
        )
        for index in indices
    )

    splits = chronological_time_split(
        rows,
        start=T0,
        end=T0 + timedelta(days=10),
    )

    assert [len(splits[key]) for key in ("TRAIN", "VALIDATION", "TEST")] == [2, 1, 1]
    assert splits["VALIDATION"][0].plan.created_at == T0 + timedelta(days=7)


def _portfolio_row(
    plan_id,
    symbol,
    *,
    created_minutes,
    filled_minutes=None,
    exited_minutes=None,
):
    created = T0 + timedelta(minutes=created_minutes)
    plan = TradePlan(
        plan_id=plan_id,
        strategy_id="portfolio-test",
        setup_id=f"setup-{plan_id}",
        symbol=symbol,
        side=Side.BUY,
        created_at=created,
        valid_until=created + timedelta(minutes=10),
        exit_by=created + timedelta(hours=2),
        entry_kind=EntryKind.LIMIT,
        entry=100,
        stop=99,
        target=102,
        thesis="portfolio capacity test",
    )
    if filled_minutes is None:
        result = ReplayResult(
            plan_id=plan_id,
            status=ReplayStatus.EXPIRED_UNFILLED,
            exited_at=plan.valid_until,
        )
    else:
        result = ReplayResult(
            plan_id=plan_id,
            status=ReplayStatus.TP_FIRST,
            filled_at=T0 + timedelta(minutes=filled_minutes),
            exited_at=T0 + timedelta(minutes=exited_minutes),
            net_r=1.0,
        )
    return EvaluatedPlan(plan, result)


def test_portfolio_capacity_counts_actual_overlapping_fills_only():
    rows = (
        _portfolio_row("p1", "A/USDT", created_minutes=0, filled_minutes=1, exited_minutes=4),
        _portfolio_row("p2", "B/USDT", created_minutes=1, filled_minutes=2, exited_minutes=3),
        _portfolio_row("p3", "C/USDT", created_minutes=2, filled_minutes=2.5, exited_minutes=5),
        _portfolio_row("p4", "A/USDT", created_minutes=2, filled_minutes=3, exited_minutes=5),
        _portfolio_row("p5", "C/USDT", created_minutes=4, filled_minutes=4, exited_minutes=6),
        _portfolio_row("unfilled", "D/USDT", created_minutes=1),
    )

    selection = enforce_portfolio_capacity(rows, max_open_positions=2)

    assert {row.plan.plan_id for row in selection.records} == {"p1", "p2", "p5", "unfilled"}
    assert selection.rejected_capacity == 1
    assert selection.rejected_symbol_busy == 1
