from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pytest
from scripts.replay_runtime_rejections_20260908 import replay
from trading_v2.models import Bar

T=datetime(2026,9,7,tzinfo=timezone.utc)
PLAN={"entry":100,"sl":90,"tp":120}
def bar(i,**kw):
    values=dict(symbol="X/USDT",timeframe="1m",open_time=T+timedelta(minutes=i),close_time=T+timedelta(minutes=i+1)-timedelta(milliseconds=1),open=100,high=101,low=99,close=100,volume=10)
    values.update(kw); return Bar(**values)
def run(rows,**kw):
    return replay(rows,decision=kw.pop("decision",T),side=kw.pop("side","BUY"),plan=kw.pop("plan",PLAN),model=kw.pop("model","FORCED_NEXT_OPEN"),**kw)
def test_excludes_partly_elapsed_decision_minute():
    result=run([bar(0,high=125),bar(1,low=89)],decision=T+timedelta(seconds=10))
    assert result["status"]=="SL_FIRST"
    assert result["filled_at"]==(T+timedelta(minutes=1)).isoformat()
def test_requires_exact_next_open():
    assert run([bar(1,high=125)])["status"]=="DATA_GAP"
def test_gap_before_exit_cannot_score():
    assert run([bar(0),bar(2,high=125)])["status"]=="DATA_GAP"
def test_duplicate_cannot_score():
    assert run([bar(0,high=125),bar(0,low=89)])["status"]=="DATA_GAP"
def test_gap_after_observed_exit_does_not_invalidate():
    assert run([bar(0,high=125),bar(3)])["status"]=="TP_FIRST"
@pytest.mark.parametrize("side,plan,changed",[("BUY",PLAN,{"high":125,"low":85}),("SELL",{"entry":100,"sl":110,"tp":80},{"high":115,"low":75})])
def test_same_bar_excluded(side,plan,changed):
    result=run([bar(0,**changed)],side=side,plan=plan)
    assert result["status"]=="AMBIGUOUS_SAME_BAR" and "net_r" not in result
def test_limit_entry_and_target_is_ambiguous():
    assert run([bar(0,high=125)],model="PLANNED_LIMIT_WITHOUT_CONFIRMATION")["status"]=="AMBIGUOUS_ENTRY_BAR"
def test_stop_gap_is_charged_at_worse_open():
    result=run([bar(0),bar(1,open=80,low=79,high=81,close=80)])
    assert result["raw_exit"]==80 and result["net_r"] < -2
def test_target_costs():
    result=run([bar(0,high=125)])
    assert result["gross_r"]==2
    assert result["net_r"]==pytest.approx(2-(100*.0003+120*.0002+(100+120*(1-.0002))*.0005)/10)
def test_time_exit_needs_full_60_minutes():
    assert run([bar(i) for i in range(59)])["status"]=="DATA_GAP"
    result=run([bar(i) for i in range(60)])
    assert result["status"]=="TIME_EXIT" and result["net_r"] < 0
def test_limit_expiry_does_not_use_later_touch():
    rows=[bar(i,open=105,high=106,low=104,close=105) for i in range(30)]+[bar(30)]
    assert run(rows,model="PLANNED_LIMIT_WITHOUT_CONFIRMATION")["status"]=="EXPIRED_UNFILLED"
def test_invalid_next_open_geometry():
    assert run([bar(0,open=85,low=84,high=86,close=85)])["status"]=="INVALID_NEXT_OPEN_GEOMETRY"


def test_missing_discovery_data_cannot_be_called_expired():
    result=run([bar(31,open=105,high=106,low=104,close=105)],model="PLANNED_LIMIT_WITHOUT_CONFIRMATION")
    assert result["status"]=="DATA_GAP"
