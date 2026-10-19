from datetime import datetime, timedelta, timezone

from trading_v2.evaluation import EvaluatedPlan, summarize
from trading_v2.models import EntryKind, Side, TradePlan, ReplayResult, ReplayStatus


def _record(name: str, minutes: int, net_r: float) -> EvaluatedPlan:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    plan = TradePlan(
        plan_id=name,
        strategy_id="test",
        setup_id=name,
        symbol=f"{name}/USDT",
        side=Side.BUY,
        created_at=start,
        valid_until=start + timedelta(minutes=5),
        exit_by=start + timedelta(hours=1),
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=100,
        stop=99,
        target=102,
        thesis="chronology",
    )
    result = ReplayResult(plan_id=name, status=ReplayStatus.TP_FIRST,
                          exited_at=start + timedelta(minutes=5), net_r=net_r)
    return EvaluatedPlan(plan, result)


def test_summary_drawdown_is_chronological_not_input_order():
    result = summarize((_record("late", 10, 1.0), _record("early", 0, -1.0)))

    assert result["max_drawdown_r"] == 1.0
