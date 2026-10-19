"""Frozen, order-free diagnostics after every recorded Quality/RR/confirmation rejection.

Reads local public Demo caches. Loads only the RiskManager AST class with
explicit non-secret configuration; never imports live config, clients or main.
"""
from __future__ import annotations
import ast
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.audit_runtime_theses_1m import (
    _event_thesis_maps, _thesis_id, load_cached_1m, parse_utc,
)
from utils.runtime_candidate_replay import load_decision_events

RUN = "e2be2e86774d4b1ba62fe3748cc98cf4"
OUT = ROOT / "research_data/runtime_review_20260908"
MINUTE = timedelta(minutes=1)
PROTOCOL = {
    "name": "recorded_rejection_diagnostic_v1",
    "run_id": RUN,
    "cohort": "30 independent setup_thesis_id; all rejection observations retained",
    "selection": "First rejection per thesis and stage is primary; repeated observations diagnostic only",
    "models": ["PLANNED_LIMIT_WITHOUT_CONFIRMATION", "FORCED_NEXT_OPEN"],
    "decision_clock": "Exclude the partially elapsed decision minute; never use its OHLC",
    "entry_ttl_minutes_from_decision": 30,
    "holding_end": "60 minutes after the first complete minute open following the decision",
    "limit_boundary": "Use only minutes fully closed before the exact TTL",
    "limit_policy": "Cancel if target precedes entry or gap opens through stop; exclude entry+terminal same-bar",
    "exit_policy": "First TP/SL; same-bar ambiguous excluded from PnL; time exit at horizon",
    "gap_policy": "No PnL across missing/duplicate 1m bars; stops use worse of open or stop",
    "fees_bps_per_side": 5,
    "friction_bps": {"entry": 3, "stop": 8, "target": 2, "time_exit": 3},
    "friction_policy": "Deduct entry friction as a cost for both models; not an assumed improved limit fill",
    "risk_config": {"ATR_SL_MULTIPLIER": 1.8, "MAX_SL_PCT": 0.05, "MAX_SL_ATR_MULTIPLE": 4.0},
    "profile": {"min_rr": 1.5, "allow_rr_fallback": False},
    "source_quarantine": "Any symbol with a mismatched recorded candle is excluded",
    "limitations": [
        "Counterfactual path diagnostics, not a reproduction of the full production execution policy",
        "Quality bypass uses same-decision market context; reconstruction checked against all recorded RR plans",
        "Both models bypass later execution gates; not isolated causal effects of removing a single gate",
        "No sizing, exchange book/mark-price, capacity, funding or actual orders; R is not account return",
        "60-minute time exit is a diagnostic assumption, not production exit management",
        "No test-set tuning or strategy promotion",
    ],
}

class NullLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None

def risk_manager_class():
    path = ROOT / "strategy/risk_manager.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RiskManager")
    namespace = {
        "Config": SimpleNamespace(RISK_PER_TRADE_PERCENT=0.25, **PROTOCOL["risk_config"]),
        "logger": NullLogger(), "math": math, "threading": threading,
    }
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["RiskManager"]

def reconstruct(manager_class, side, context):
    setup = context.get("setup_context") or {}
    entry = float(setup.get("optimal_entry") or 0)
    atr = float((context.get("indicators") or {}).get("atr") or 0)
    if entry <= 0 or atr <= 0:
        return {}
    manager = manager_class()
    manager.calculate_sl_tp(
        side.lower(), entry, atr=atr,
        invalidation_level=setup.get("invalidation_level"),
        market_levels=context.get("market_levels") or {},
        filter_profile=PROTOCOL["profile"],
    )
    return manager.last_plan

def geometry(plan):
    return (
        float(plan.get("entry") or 0), float(plan.get("sl") or 0),
        float(plan.get("tp") or (plan.get("blocking_target") or {}).get("tp") or 0),
    )

def next_minute(value):
    return value.replace(second=0, microsecond=0) + (MINUTE if value.second or value.microsecond else timedelta())

def replay(bars, *, decision, side, plan, model):
    entry, stop, target = geometry(plan)
    direction = 1 if side == "BUY" else -1
    if not all(math.isfinite(v) and v > 0 for v in (entry, stop, target)):
        return {"status": "NO_VALID_RECORDED_GEOMETRY"}
    if not direction * (entry-stop) > 0 or not direction * (target-entry) > 0:
        return {"status": "INVALID_PLANNED_GEOMETRY"}
    start = next_minute(decision)
    expires = decision + timedelta(minutes=30)
    end = start + timedelta(minutes=60)
    path = sorted([b for b in bars if start <= b.open_time < end], key=lambda b:b.open_time)
    expected = start
    filled = False
    filled_at = None
    risk = direction * (entry-stop)
    def finish(status, bar, price):
        slip = 8 if status == "SL_FIRST" else 2 if status == "TP_FIRST" else 3
        exit_price = price * (1-direction*slip/10000)
        gross = direction * (price-entry) / risk
        friction = entry*3/10000 + abs(price-exit_price)
        fees = (entry+exit_price)*5/10000
        return {
            "status": status, "filled_at": filled_at.isoformat(),
            "exited_at": bar.close_time.isoformat(), "entry": entry, "stop": stop, "target": target,
            "raw_exit": price, "exit_after_friction": exit_price, "risk": risk,
            "gross_r": gross, "net_r": gross - (friction+fees)/risk,
        }
    for i, bar in enumerate(path):
        if not filled and model.startswith("PLANNED") and bar.close_time >= expires:
            if expected < expires.replace(second=0, microsecond=0):
                return {"status": "DATA_GAP", "expected_open": expected.isoformat()}
            return {"status": "EXPIRED_UNFILLED"}
        if bar.open_time != expected or (i+1<len(path) and path[i+1].open_time == bar.open_time):
            return {"status": "DATA_GAP", "expected_open": expected.isoformat()}
        if bar.close_time != bar.open_time + MINUTE - timedelta(milliseconds=1):
            return {"status": "INVALID_MINUTE_CLOSE"}
        if not all(math.isfinite(x) for x in (bar.open,bar.high,bar.low,bar.close,bar.volume)):
            return {"status": "INVALID_BAR"}
        if not 0 < bar.low <= min(bar.open,bar.close) <= max(bar.open,bar.close) <= bar.high or bar.volume < 0:
            return {"status": "INVALID_BAR"}
        expected += MINUTE
        sl_hit = bar.low <= stop if direction == 1 else bar.high >= stop
        tp_hit = bar.high >= target if direction == 1 else bar.low <= target
        if not filled:
            if model == "FORCED_NEXT_OPEN":
                entry = float(bar.open)
                risk = direction*(entry-stop)
                if risk <= 0 or direction*(target-entry) <= 0:
                    return {"status": "INVALID_NEXT_OPEN_GEOMETRY", "next_open": entry}
            else:
                if direction*(bar.open-stop) <= 0:
                    return {"status": "INVALIDATED_BEFORE_ENTRY"}
                touch = bar.low <= entry if direction == 1 else bar.high >= entry
                if touch and (sl_hit or tp_hit):
                    return {"status": "AMBIGUOUS_ENTRY_BAR"}
                if tp_hit:
                    return {"status": "TARGET_BEFORE_ENTRY"}
                if not touch:
                    continue
            filled = True
            filled_at = bar.open_time
        if sl_hit and tp_hit:
            return {"status": "AMBIGUOUS_SAME_BAR", "filled_at": filled_at.isoformat()}
        if sl_hit:
            raw = min(stop,bar.open) if direction == 1 else max(stop,bar.open)
            return finish("SL_FIRST",bar,raw)
        if tp_hit:
            return finish("TP_FIRST",bar,target)
        if expected == end:
            return finish("TIME_EXIT",bar,bar.close)
    if expected < (end if filled or model == "FORCED_NEXT_OPEN" else expires.replace(second=0,microsecond=0)):
        return {"status": "DATA_GAP", "expected_open": expected.isoformat()}
    return {"status": "EXPIRED_UNFILLED"}

def summarize(rows):
    counts = Counter(r["result"]["status"] for r in rows)
    pnl = [r["result"]["net_r"] for r in rows if "net_r" in r["result"]]
    return {
        "observations": len(rows), "independent_theses":len({r["thesis_id"] for r in rows}),
        "statuses":dict(counts), "resolved_pnl_n":len(pnl),
        "mean_net_r_resolved_only":sum(pnl)/len(pnl) if pnl else None,
        "sum_net_r_not_portfolio":sum(pnl) if pnl else None,
    }

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    protocol_path = OUT/"rejection_replay_protocol.json"
    encoded = json.dumps(PROTOCOL,ensure_ascii=False,indent=2)+"\n"
    if protocol_path.exists():
        assert protocol_path.read_text(encoding="utf-8") == encoded, "Frozen protocol changed"
    else:
        protocol_path.write_text(encoded,encoding="utf-8")
    archives = sorted(ROOT.glob("runtime_logs_24h_20260907_1905_part*.zip"))
    events = [e for e in load_decision_events(archives) if e["run_id"]==RUN]
    events.sort(key=lambda e:(e["timestamp"],e.get("event_sequence",0)))
    by_decision, by_trade = _event_thesis_maps(events)
    theses = json.loads((OUT/"theses.json").read_text(encoding="utf-8"))["theses"]
    assert len(theses)==30
    manifest = json.loads((OUT/"demo_1m_manifest.json").read_text())
    bars = load_cached_1m(manifest)
    verification = json.loads((OUT/"source_verification.json").read_text())
    quarantined = sorted({r["symbol"] for r in verification["comparisons"] if not r["matched"]})
    contexts = {e["decision_id"]:e for e in events if e["stage"]=="MARKET_CONTEXT"}
    sides = {t["thesis_id"]:t["side"] for t in theses}
    manager_class = risk_manager_class()
    parity=[]
    for e in events:
        if e["stage"]!="RR_FILTER": continue
        context = contexts[e["decision_id"]]
        assert parse_utc(context["timestamp"])<=parse_utc(e["timestamp"])
        tid = _thesis_id(e,by_decision,by_trade)
        rebuilt = reconstruct(manager_class,sides[tid],context["details"])
        recorded = e["details"]["target_plan"]
        match = rebuilt.get("valid")==recorded.get("valid") and all(
            math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-12)
            for a,b in zip(geometry(rebuilt),geometry(recorded))
        )
        parity.append({"event_id":e["event_id"],"matched":match,"recorded":geometry(recorded),"rebuilt":geometry(rebuilt)})
    if not all(p["matched"] for p in parity):
        (OUT/"risk_reconstruction_parity.json").write_text(json.dumps(parity,indent=2))
        raise ValueError("Risk reconstruction does not match recorded plans; no Quality replay accepted")
    rows=[]; first=set(); latest_rr={}
    confirmations=[]
    for e in events:
        tid = _thesis_id(e,by_decision,by_trade)
        if tid not in sides: continue
        stage=e["stage"]; stamp=parse_utc(e["timestamp"])
        if stage=="RR_FILTER":
            latest_rr[tid]=e["details"]["target_plan"]
        if stage=="ENTRY_CONFIRMATION":
            conf=e["details"].get("confirmation") or {}
            confirmations.append({"thesis_id":tid,"timestamp":e["timestamp"],"failed_gates":conf.get("failed_gates",[]),"passed":conf.get("passed"),"evidence":conf.get("evidence")})
        if stage not in {"QUALITY_FILTER","RR_FILTER","ENTRY_CONFIRMATION"} or e["status"]=="PASS":
            continue
        if stage=="QUALITY_FILTER":
            context=contexts[e["decision_id"]]
            assert parse_utc(context["timestamp"])<=stamp
            plan=reconstruct(manager_class,sides[tid],context["details"])
            origin="RECONSTRUCTED_FROM_SAME_DECISION_CONTEXT"
        else:
            plan=e["details"].get("target_plan") or latest_rr.get(tid,{})
            origin="RECORDED_PLAN_AT_OR_BEFORE_DECISION"
        key=(tid,stage); primary=key not in first; first.add(key)
        for model in PROTOCOL["models"]:
            result = {"status":"SOURCE_MISMATCH_QUARANTINED"} if e["symbol"] in quarantined else replay(
                bars[e["symbol"]],decision=stamp,side=sides[tid],plan=plan,model=model)
            rows.append({
                "event_id":e["event_id"],"timestamp":e["timestamp"],"thesis_id":tid,"symbol":e["symbol"],"side":sides[tid],
                "stage":stage,"primary_first_rejection":primary,"model":model,"plan_origin":origin,
                "plan":plan,"rr_gate_would_pass":bool(plan.get("valid")),"result":result,
            })
    summary={}
    for model in PROTOCOL["models"]:
        summary[model]={stage:summarize([r for r in rows if r["model"]==model and r["stage"]==stage and r["primary_first_rejection"]])
                        for stage in ("QUALITY_FILTER","RR_FILTER","ENTRY_CONFIRMATION")}
    coverage=[]
    for t in theses:
        coverage.append({
            "thesis_id":t["thesis_id"],"symbol":t["symbol"],"side":t["side"],
            "quality_passes":t["quality_passes"],"rr_passes":t["rr_passes"],"touches":t["touches"],"confirmation_passes":t["confirmation_passes"],
            "source_accepted":t["symbol"] not in quarantined,
            "primary_results":[{"stage":r["stage"],"model":r["model"],**r["result"]} for r in rows if r["thesis_id"]==t["thesis_id"] and r["primary_first_rejection"]],
        })
    unique_bad={(r["symbol"],r["timeframe"],r["open_time"]) for r in verification["comparisons"] if not r["matched"]}
    payload={
        "protocol":PROTOCOL,"protocol_sha256":hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
        "script_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "risk_source_sha256":hashlib.sha256((ROOT/"strategy/risk_manager.py").read_bytes()).hexdigest(),
        "risk_parity":{"comparisons":len(parity),"matched":sum(p["matched"] for p in parity)},
        "source":{"comparisons":verification["comparison_count"],"matched":verification["matched"],"mismatched":verification["mismatched"],
                  "unique_bad_candles":len(unique_bad),"quarantined_symbols":quarantined},
        "rejection_events":len(rows)//2,"cohort":coverage,"primary_summary":summary,
        "confirmation_gate_observations":confirmations,"all_observation_results":rows,
    }
    (OUT/"rejection_replay_result.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:payload[k] for k in ("risk_parity","source","rejection_events","primary_summary")},indent=2))
if __name__=="__main__":
    main()

