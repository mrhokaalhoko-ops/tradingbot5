from datetime import datetime, timedelta, timezone

import pytest

from trading_v2.evaluation import (
    EvaluatedPlan,
    summarize_by_calendar_month,
    summarize_by_symbol,
)
from trading_v2.models import EntryKind, ReplayResult, ReplayStatus, Side, TradePlan


UTC = timezone.utc


def _record(plan_id: str, symbol: str, created_at: datetime, net_r: float) -> EvaluatedPlan:
    plan = TradePlan(
        plan_id=plan_id,
        strategy_id="test",
        setup_id=plan_id,
        symbol=symbol,
        side=Side.BUY,
        created_at=created_at,
        valid_until=created_at + timedelta(minutes=5),
        exit_by=created_at + timedelta(hours=1),
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=100.0,
        stop=99.0,
        target=102.0,
        thesis="test",
    )
    result = ReplayResult(
        plan_id=plan_id,
        status=ReplayStatus.TP_FIRST if net_r > 0 else ReplayStatus.SL_FIRST,
        filled_at=created_at + timedelta(minutes=5),
        exited_at=created_at + timedelta(minutes=10),
        entry_price=100.0,
        exit_price=102.0 if net_r > 0 else 99.0,
        gross_r=net_r + 0.1,
        net_r=net_r,
        mfe_r=2.0 if net_r > 0 else 0.2,
        mae_r=0.2 if net_r > 0 else 1.0,
    )
    return EvaluatedPlan(plan=plan, result=result)


def test_stability_summaries_preserve_month_and_symbol_boundaries():
    rows = [
        _record("a", "BTC/USDT", datetime(2026, 1, 31, 23, tzinfo=UTC), 2.0),
        _record("b", "ETH/USDT", datetime(2026, 2, 1, 0, tzinfo=UTC), -1.0),
        _record("c", "BTC/USDT", datetime(2026, 2, 2, 0, tzinfo=UTC), -1.0),
    ]

    monthly = summarize_by_calendar_month(reversed(rows))
    symbols = summarize_by_symbol(rows)

    assert list(monthly) == ["2026-01", "2026-02"]
    assert monthly["2026-01"]["total_net_r"] == pytest.approx(2.0)
    assert monthly["2026-02"]["total_net_r"] == pytest.approx(-2.0)
    assert symbols["BTC/USDT"]["total_net_r"] == pytest.approx(1.0)
    assert symbols["ETH/USDT"]["total_net_r"] == pytest.approx(-1.0)
    assert monthly["2026-02"]["gross_expectancy_r"] == pytest.approx(-0.9)
    assert monthly["2026-02"]["average_cost_drag_r"] == pytest.approx(0.1)
    assert monthly["2026-02"]["median_mfe_r"] == pytest.approx(0.2)
    assert monthly["2026-02"]["median_mae_r"] == pytest.approx(1.0)
