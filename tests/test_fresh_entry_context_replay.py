from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import pandas as pd
import pytest
from scripts.replay_fresh_entry_context_20260909 import visible_frame, production_classes

AT=datetime(2026,9,8,16,5,18,tzinfo=timezone.utc)
CUT=int(AT.replace(second=0,microsecond=0).timestamp()*1000)

def minutes():
    return [[CUT+i*300000,"100","101","99","100","10"] for i in range(-3,2)]

def test_frame_contains_three_closed_bars_and_a_synthetic_live_bar():
    f=visible_frame(minutes(),AT,"5m",4)
    assert len(f)==4
    assert f.iloc[-2]["timestamp"]==pd.Timestamp("2026-09-08T16:00:00")
    assert f.iloc[-1]["timestamp"]==pd.Timestamp("2026-09-08T16:05:00")
    assert f.iloc[-1]["volume"]==0

def test_future_prices_never_enter_indicator_window():
    data=minutes()
    original=visible_frame(data,AT,"5m",4)
    for row in data:
        if row[0]>=CUT:row[1:]=["1","9000","0.01","2000","999999"]
    pd.testing.assert_frame_equal(original,visible_frame(data,AT,"5m",4))

@pytest.mark.parametrize("kind",["missing","duplicate","bad_ohlc"])
def test_incomplete_or_invalid_history_is_rejected(kind):
    data=minutes()
    if kind=="missing":del data[1]
    elif kind=="duplicate":data.insert(2,deepcopy(data[1]))
    else:data[1][2]="98"
    with pytest.raises(ValueError):visible_frame(data,AT,"5m",4)

@pytest.mark.parametrize("key,expected",[
    ("decision_checkpoint",1.0782434847844156),("modeled_entry_checkpoint",.9397777886230145)])
def test_production_risk_manager_rejects_recorded_fresh_plan(key,expected):
    f=json.loads((Path(__file__).parent/"fixtures/dash_fresh_plan_20260908.json").read_text(encoding="utf-8"))[key]
    _,manager_type,profile=production_classes()
    manager=manager_type()
    sl,tp=manager.calculate_sl_tp("sell",f["price"],atr=f["atr"],invalidation_level=64.89,
        market_levels=f["market_levels"],filter_profile=profile)
    assert tp is None and sl==pytest.approx(65.10818830915474)
    assert manager.last_plan["rr"]==pytest.approx(expected)
    assert manager.last_plan["min_rr"]==1.5
