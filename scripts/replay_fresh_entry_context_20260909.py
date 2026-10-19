"""Replay fresh Sniper context/plan with production TA and RiskManager code."""
from __future__ import annotations
import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pandas as pd
from strategy.entry_confirmation import evaluate_entry_confirmation
from strategy.market_regime import detect_market_regime
from utils.sequential_confirmation_research import Context, Observation, evaluate
from scripts.run_sequential_confirmation_research import utc, write_json, records, simulate

OUT=ROOT/"research_data/sequential_confirmation_20260909/fresh_revalidation"
STEPS={"1m":60000,"5m":300000,"15m":900000,"1h":3600000}

class Quiet:
    def __getattr__(self,name):
        return lambda *args,**kwargs:None

def production_classes():
    # Retain the production algorithms verbatim, excluding runtime config and
    # logging initialization so this process cannot load credentials or state.
    config=SimpleNamespace(RISK_PER_TRADE_PERCENT=.25,ATR_SL_MULTIPLIER=1.8,
        MAX_SL_PCT=.05,MAX_SL_ATR_MULTIPLE=4,LEVERAGE=5,MAX_NOTIONAL_BALANCE_PCT=.30,
        MIN_SL_DISTANCE_PCT=.003)
    spaces=[]
    for relative in ("engines/ta_engine.py","strategy/risk_manager.py"):
        tree=ast.parse((ROOT/relative).read_text(encoding="utf-8-sig"))
        tree.body=[n for n in tree.body if not (isinstance(n,ast.ImportFrom) and n.module in ("core.logger","core.config"))]
        ns={"Config":config,"logger":Quiet(),"__name__":"isolated_production_replay"}
        exec(compile(tree,str(ROOT/relative),"exec"),ns)
        spaces.append(ns)
    profile_tree=ast.parse((ROOT/"strategy/filter_profiles.py").read_text(encoding="utf-8-sig"))
    node=next(n for n in profile_tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="FILTER_PROFILES" for t in n.targets))
    profiles=ast.literal_eval(node.value)
    return spaces[0],spaces[1]["RiskManager"],profiles["smc_strict"]

def fetch(symbol,tf,start,end):
    path=OUT/"public_demo"/f"{symbol}_{tf}_{start}_{end}.json"
    if path.exists():
        return json.loads(path.read_text())["rows"]
    url="https://demo-fapi.binance.com/fapi/v1/klines?"+urlencode(
        {"symbol":symbol,"interval":tf,"startTime":start,"endTime":end-1,"limit":1500})
    base=["curl.exe","--silent","--show-error","--fail-with-body","--connect-timeout","8","--max-time","18"]
    old=json.loads((ROOT/"research_data/runtime_review_20260908/demo_1m_manifest.json").read_text())
    attempts=[base+[url],base+["--resolve","demo-fapi.binance.com:443:"+old["resolved_ip"],url]]
    for cmd in attempts:
        response=subprocess.run(cmd,capture_output=True,text=True,timeout=23)
        if response.returncode==0:
            rows=json.loads(response.stdout)
            if isinstance(rows,list) and rows:
                write_json(path,{"url":url,"tls_verified":True,"cached_dns":"--resolve" in cmd,
                    "fetched_at":datetime.now(timezone.utc).isoformat(),"rows":rows})
                print("Cached",symbol,tf,len(rows),flush=True)
                return rows
    raise RuntimeError(f"Public Demo data unavailable: {symbol} {tf}")

def visible_frame(rows,at,tf,limit,live_price=None):
    """Require contiguous closed bars; append a synthetic, never-used live row."""
    step=STEPS[tf]
    cut=int(at.timestamp()*1000)//step*step
    selected=[r for r in rows if r[0]+step<=cut][-limit+1:]
    expected=list(range(cut-(limit-1)*step,cut,step))
    if [r[0] for r in selected]!=expected:
        raise ValueError(f"Missing/duplicate {tf} closed bars at {at.isoformat()}")
    for r in selected:
        values=list(map(float,r[1:6]))
        op,hi,lo,cl,vol=values
        if not all(math.isfinite(v) for v in values) or not 0<lo<=min(op,cl)<=max(op,cl)<=hi or vol<0:
            raise ValueError("Invalid closed OHLCV")
    data=[[pd.to_datetime(r[0],unit="ms",utc=True).tz_localize(None),*map(float,r[1:6])] for r in selected]
    price=float(live_price if live_price is not None else selected[-1][4])
    data.append([pd.to_datetime(cut,unit="ms",utc=True).tz_localize(None),price,price,price,price,0.0])
    return pd.DataFrame(data,columns=["timestamp","open","high","low","close","volume"])

class Client:
    def __init__(self,datasets,at):
        self.datasets,self.at=datasets,at
    def fetch_ohlcv(self,symbol,timeframe="1h",limit=260):
        return visible_frame(self.datasets[(symbol.replace("/",""),timeframe)],self.at,timeframe,limit)

def fresh_checkpoint(ta_space, manager_type, profile, data, case, setup_context, at, price):
    client=Client(data,at)
    ta_space["_htf_cache"].clear()
    engine=ta_space["TAEngine"]()
    bars=engine.add_indicators(client.fetch_ohlcv(case["symbol"],"15m",260),filter_profile=profile)
    latest=bars.iloc[-2]
    levels=engine.extract_market_levels(bars,max_age=profile["max_ob_age"])
    context={**setup_context,"ema_50":float(latest["ema_50"]),"ema_200":float(latest["ema_200"]),
        "adx":float(latest["adx"]),"htf_trend":engine.get_htf_trend(case["symbol"],client),
        "market_regime":detect_market_regime(client),"invalidation_state":"VALID"}
    atr=float(latest["atr"])
    old=case["plan"]
    confirmation=evaluate_entry_confirmation(side=old["stop_plan"]["side"],
        trigger_bars=client.fetch_ohlcv(case["symbol"],"5m",80),current_price=price,
        optimal_entry=old["entry"],invalidation_level=old["stop_plan"]["invalidation_level"],
        atr_15m=atr,ta_context=context,filter_profile=profile,planned_rr=old["nearest_opposing_target"]["rr"],
        zone_touch_recorded=True,now=at)
    manager=manager_type()
    sl,tp=manager.calculate_sl_tp(old["stop_plan"]["side"],price,atr=atr,
        invalidation_level=old["stop_plan"]["invalidation_level"],market_levels=levels,filter_profile=profile)
    return {"at":at.isoformat(),"price":price,"atr":atr,"context":context,"market_levels":levels,
        "confirmation":confirmation,"fresh_plan":deepcopy(manager.last_plan),
        "fresh_sl":sl,"fresh_tp":tp,"block_reason":manager.last_block_reason}

def main():
    previous=json.loads((OUT.parent/"gate_comparison.json").read_text())
    cases=previous["additional_confirmations"]
    ta_space,manager_type,profile=production_classes()
    proto={"frozen_at":datetime.now(timezone.utc).isoformat(),"cases":[c["event_id"] for c in cases],
        "selection":"Every additional confirmation from the previously fixed rule; no new thresholds",
        "algorithm_files_sha256":{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
            ["engines/ta_engine.py","strategy/risk_manager.py","strategy/entry_confirmation.py","strategy/market_regime.py"]},
        "clock":"Only fully closed 5m/15m/1h candles; synthetic live row; 80/260/500 bars as production requests",
        "prices":"Current closed-5m close (known at decision) and prior diagnostic next-minute adverse entry; neither is a historical executable quote",
        "risk_config":{"ATR_SL_MULTIPLIER":1.8,"MAX_SL_PCT":.05,"MAX_SL_ATR_MULTIPLE":4},
        "limits":"No historical mark tick/order book, exchange filters, account state, funding or full trade-management reproduction"}
    if not (OUT/"protocol.json").exists():write_json(OUT/"protocol.json",proto)
    results=[]
    for case in cases:
        at=utc(case["timestamp"]);prior_at=utc(case["previous_observed_at"])
        data={}
        for symbol,tf,limit in [(case["symbol"].replace("/",""),"5m",85),
                               (case["symbol"].replace("/",""),"15m",265),
                               (case["symbol"].replace("/",""),"1h",505),("BTCUSDT","1h",265),("ETHUSDT","1h",265)]:
            step=STEPS[tf];end=int(at.timestamp()*1000)//step*step
            data[(symbol,tf)]=fetch(symbol,tf,end-limit*step,end)
        recorded={}
        for path in sorted(ROOT.glob("runtime_logs_24h_20260908_1939_part*.zip")):
            for e in records(path):
                if e.get("trade_id")==case["trade_id"] and e["stage"] in ("SETUP_CREATED","HYBRID_DECISION"):
                    recorded[e["stage"]]=e["details"]
        setup={"score":recorded["SETUP_CREATED"]["setup_score"],"setup_anchor":recorded["SETUP_CREATED"]["setup_anchor"]}
        old=case["plan"]
        manager=manager_type()
        manager.calculate_sl_tp(old["stop_plan"]["side"],old["entry"],atr=old["stop_plan"]["atr"],
            invalidation_level=old["stop_plan"]["invalidation_level"],market_levels=recorded["HYBRID_DECISION"]["market_levels"],filter_profile=profile)
        original_check={k:math.isclose(float(manager.last_plan[k]),float(old[k]),rel_tol=1e-10) for k in ("entry","sl","tp","rr")}
        assert all(original_check.values()),original_check
        prev=fresh_checkpoint(ta_space,manager_type,profile,data,case,setup,prior_at,case["previous_confirmation"]["closed_bar"]["close"])
        current=fresh_checkpoint(ta_space,manager_type,profile,data,case,setup,at,case["current_confirmation"]["closed_bar"]["close"])
        current_ctx=Context(case["trade_id"],at,utc(case["zone_touch"]),utc(case["deadline"]))
        prev_ctx=Context(case["trade_id"],prior_at,current_ctx.zone_touched_at,current_ctx.deadline)
        seq,_=evaluate(current["confirmation"],current_ctx,Observation(prev["confirmation"],prev_ctx))
        diagnostic=json.loads((OUT.parent/"price_diagnostics.json").read_text())
        pp=next(x for x in diagnostic["cases"] if x["trade_id"]==case["trade_id"])
        modeled_price=pp["diagnostic"]["entry"]
        entry_at=at.replace(second=0,microsecond=0)+timedelta(minutes=1)
        entry=fresh_checkpoint(ta_space,manager_type,profile,data,case,setup,entry_at,modeled_price)
        result={"case_event_id":case["event_id"],"symbol":case["symbol"],"original_plan_reproduced":original_check,
            "previous_checkpoint":prev,"decision_checkpoint":current,"sequential_after_fresh_context":seq,
            "modeled_entry_checkpoint":entry}
        if seq["passed"] and entry["fresh_plan"].get("valid"):
            newcase=deepcopy(case);newcase["plan"]=entry["fresh_plan"]
            rows=json.loads((ROOT/pp["source"]["cache"]).read_text())["rows"]
            result["path_diagnostic"]=simulate(rows,newcase)
        else:result["path_diagnostic"]={"status":"FRESH_CONTEXT_REJECTED" if not seq["passed"] else "FRESH_PLAN_REJECTED"}
        results.append(result)
        print(json.dumps({"symbol":case["symbol"],"original_plan_reproduced":original_check,
            "fresh_context_gates":current["confirmation"]["failed_gates"],"sequential_fresh_pass":seq["passed"],
            "fresh_plan_at_decision":current["fresh_plan"],"fresh_plan_at_modeled_entry":entry["fresh_plan"],
            "path":result["path_diagnostic"]},ensure_ascii=True),flush=True)
    write_json(OUT/"result.json",{"cases":results,"limits":proto["limits"],"promotion":"NOT_ELIGIBLE"})
if __name__=="__main__":main()
