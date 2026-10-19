from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import pytest
from scripts.run_sequential_confirmation_research import simulate, verify_recorded, utc
from utils.sequential_confirmation_research import Context, Observation, evaluate

FIXTURE = Path(__file__).parent / "fixtures/sequential_confirmation_dash_20260908.json"

def example():
    case = {"timestamp": "2026-09-08T16:05:18Z", "plan": {
        "entry": 100, "sl": 99, "tp": 103, "min_rr": 1.5, "stop_plan": {"side": "buy"}}}
    first = int(utc("2026-09-08T16:06:00Z").timestamp()*1000)
    rows = [[first+i*60000, "100", "100.5", "99.5", "100", "10", first+(i+1)*60000-1] for i in range(60)]
    return case, rows

def test_actual_dash_passes_new_confirmation_but_fails_repriced_rr():
    f=json.loads(FIXTURE.read_text(encoding="utf-8"))
    c=f["case"]
    context=Context("DASH:sell:frozen",utc(c["timestamp"]),utc(c["zone_touch"]),utc(c["deadline"]))
    pc=Context(context.setup_key,utc(c["previous_observed_at"]),context.zone_touched_at,context.deadline)
    changed,_=evaluate(c["current_confirmation"],context,Observation(c["previous_confirmation"],pc))
    assert changed["passed"] and not c["current_confirmation"]["passed"]
    assert verify_recorded(f["minutes"],c)["valid"]
    result=simulate(f["minutes"],c)
    assert result["status"]=="RR_REJECTED_AT_MODELED_ENTRY"
    assert result["rr"]==pytest.approx(0.5731220349591057)
    assert "net_r" not in result

def test_source_mismatch_is_detected_before_performance_reporting():
    f=json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows=deepcopy(f["minutes"])
    rows[0][1]=str(float(rows[0][1])*1.01)
    assert not verify_recorded(rows,f["case"])["valid"]

@pytest.mark.parametrize("kind", ["missing","duplicate"])
def test_missing_and_duplicate_source_minutes_are_rejected(kind):
    f=json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows=f["minutes"]
    rows=rows[1:] if kind=="missing" else [rows[0],*rows]
    assert not verify_recorded(rows,f["case"])["valid"]

def test_entry_uses_next_complete_minute_and_costs_reduce_net():
    case,rows=example()
    rows[0][2]="103.2"
    result=simulate(rows,case)
    expected_entry=100*1.0003
    expected_exit=103*(1-0.0002)
    expected_net=((expected_exit-expected_entry)-(expected_entry+expected_exit)*0.0005)/(expected_entry-99)
    assert result["status"]=="TP_FIRST"
    assert result["entry_at"].endswith("16:06:00+00:00")
    assert result["decision_to_model_entry_seconds"]==42
    assert result["net_r"]==pytest.approx(expected_net)

def test_partial_decision_minute_cannot_supply_entry_or_outcome():
    case,rows=example()
    partial=[rows[0][0]-60000,"101","200","1","101","10",rows[0][0]-1]
    assert simulate([partial,*rows],case)==simulate(rows,case)

def test_ambiguous_intraminute_stop_and_target_has_no_pnl():
    case,rows=example()
    rows[0][2:4]=["104","98"]
    result=simulate(rows,case)
    assert result["status"]=="AMBIGUOUS_BOTH_HIT" and "net_r" not in result

def test_known_stop_gap_uses_worse_open_before_intraminute_extremes():
    case,rows=example()
    rows[1][1:5]=["98","104","97","100"]
    result=simulate(rows,case)
    assert result["status"]=="SL_FIRST"
    assert result["exit_after_friction"]==pytest.approx(98*(1-0.0008))
    assert result["net_r"] < -1

@pytest.mark.parametrize("kind",["missing","duplicate","truncated"])
def test_incomplete_or_duplicate_path_has_no_pnl(kind):
    case,rows=example()
    if kind=="missing":del rows[5]
    elif kind=="duplicate":rows.insert(6,rows[5][:])
    else:rows=rows[:10]
    result=simulate(rows,case)
    assert result["status"] in {"DATA_GAP_OR_DUPLICATE","RIGHT_CENSORED_OR_DATA_GAP"}
    assert "net_r" not in result

def test_time_exit_after_exactly_sixty_complete_minutes():
    case,rows=example()
    result=simulate(rows,case)
    assert result["status"]=="TIME_EXIT" and result["net_r"]<0

def test_wrong_side_stop_gap_cannot_be_counted_as_a_filled_trade():
    case,rows=example()
    rows[0][1]="98"
    result=simulate(rows,case)
    assert result["status"]=="INVALID_GEOMETRY_AT_MODELED_ENTRY"
    assert "net_r" not in result

def test_short_friction_is_adverse_and_net_includes_both_fees():
    case,rows=example()
    case["plan"].update(sl=101,tp=97,stop_plan={"side":"sell"})
    rows[0][3]="96.8"
    result=simulate(rows,case)
    assert result["status"]=="TP_FIRST"
    assert result["entry_after_friction"]<100
    assert result["exit_after_friction"]>97
    assert result["net_r"] < (result["entry_after_friction"]-97)/result["risk_per_unit"]

def test_future_outcome_changes_cannot_change_confirmation():
    f=json.loads(FIXTURE.read_text(encoding="utf-8"));c=f["case"]
    ctx=Context("same",utc(c["timestamp"]),utc(c["zone_touch"]),utc(c["deadline"]))
    prior=Observation(c["previous_confirmation"],Context(ctx.setup_key,utc(c["previous_observed_at"]),ctx.zone_touched_at,ctx.deadline))
    before=evaluate(c["current_confirmation"],ctx,prior)[0]
    for row in f["minutes"]:
        if row[0]>int(ctx.observed_at.timestamp()*1000):row[1:5]=["1","999","0.01","99"]
    assert evaluate(c["current_confirmation"],ctx,prior)[0]==before
