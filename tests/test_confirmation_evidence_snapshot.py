"""Decision evidence must be reproducible without exposing unrelated state."""
from datetime import datetime, timezone
import json
import pytest
from strategy.entry_confirmation import evaluate_entry_confirmation
from utils import test_session

NOW=datetime(2026,9,8,16,5,18,tzinfo=timezone.utc)

def snapshot(**changes):
    args=dict(side="sell",trigger_bars=None,current_price=63.72,optimal_entry=64.78,
        invalidation_level=64.89,atr_15m=.7272943638491306,planned_rr=1.998006,
        ta_context={"score":3,"ema_50":65,"ema_200":66,"adx":15,"setup_anchor":{"type":"bearish_ob"},
            "market_regime":{"label":"RANGE"},"htf_trend":"neutral","invalidation_state":"VALID",
            "unrelated_private_state":"DO_NOT_RECORD"},
        filter_profile={"key":"smc_strict","min_rr":1.5,"require_volume":True,"require_entry_sweep":True,
            "trigger_evidence_min":2,"unrelated_private_state":"DO_NOT_RECORD"},
        zone_touch_recorded=True,now=NOW)
    args.update(changes)
    return evaluate_entry_confirmation(**args)

def test_early_data_rejection_keeps_exact_reference_and_plan_inputs():
    result=snapshot()
    assert not result["passed"] and result["reason"]=="INSUFFICIENT_5M_DATA"
    inputs=result["inputs"]
    assert inputs["reference_price"]==63.72 and inputs["optimal_entry"]==64.78
    assert inputs["reference_price"]!=inputs["optimal_entry"]
    assert inputs["invalidation_level"]==64.89 and inputs["zone_touch_recorded"]
    assert inputs["decision_time"]=="2026-09-08T16:05:18Z"
    assert inputs["context"]["setup_anchor_present"]
    assert inputs["context"]["ema_200"]==66
    assert inputs["price_role"]=="DECISION_REFERENCE_NOT_EXECUTABLE_QUOTE"

@pytest.mark.parametrize("value",[float("nan"),float("inf"),None,"not-a-price"])
def test_unavailable_price_is_null_never_a_fabricated_zero_quote(value):
    result=snapshot(current_price=value)
    assert result["inputs"]["reference_price"] is None
    json.dumps(result,allow_nan=False)

def test_context_and_profile_are_whitelisted_not_serialized_wholesale():
    result=snapshot()
    assert "DO_NOT_RECORD" not in json.dumps(result)
    assert result["inputs"]["profile"]["require_entry_sweep"]

def test_configuration_records_margin_separately_from_stop_risk(monkeypatch):
    from core.config import Config
    monkeypatch.setattr(Config,"ENTRY_MARGIN_PERCENT",1.0)
    monkeypatch.setattr(Config,"RISK_PER_TRADE_PERCENT",.25)
    monkeypatch.setattr(Config,"LEVERAGE",5)
    monkeypatch.setattr(Config,"MAX_OPEN_TRADES",10)
    monkeypatch.setattr(Config,"ATR_SL_MULTIPLIER",1.8)
    monkeypatch.setattr(Config,"BINANCE_API_SECRET","DO_NOT_RECORD",raising=False)
    monkeypatch.setattr(test_session,"_safe_profile",lambda state: {"key":"smc_strict"})
    result=test_session.configuration_snapshot()
    assert result["entry_margin_percent"]==1.0 and result["risk_percent"]==.25
    assert result["leverage"]==5 and result["max_open_trades"]==10
    assert result["risk_model"]["atr_sl_multiplier"]==1.8
    assert result["risk_model"]["max_sl_atr_multiple"]==4.0
    assert "DO_NOT_RECORD" not in json.dumps(result)

def test_real_profile_sweep_and_fallback_fields_are_preserved(monkeypatch):
    from strategy import filter_profiles
    monkeypatch.setattr(filter_profiles,"get_filter_profile",lambda state: {
        "key":"smc_strict","require_sweep":True,"enable_fallback":False,"private":"DO_NOT_RECORD"})
    result=test_session._safe_profile()
    assert result["require_sweep"] is True and result["enable_fallback"] is False
    assert "private" not in result

def test_margin_and_risk_model_changes_affect_configuration_identity(monkeypatch):
    from core.config import Config
    monkeypatch.setattr(test_session,"_safe_profile",lambda state: {"key":"smc_strict"})
    a=test_session.configuration_snapshot()
    monkeypatch.setattr(Config,"ENTRY_MARGIN_PERCENT",a["entry_margin_percent"]+.1)
    b=test_session.configuration_snapshot()
    assert test_session.configuration_hash(a)!=test_session.configuration_hash(b)
    monkeypatch.setattr(Config,"ATR_SL_MULTIPLIER",a["risk_model"]["atr_sl_multiplier"]+.1)
    c=test_session.configuration_snapshot()
    assert test_session.configuration_hash(b)!=test_session.configuration_hash(c)
