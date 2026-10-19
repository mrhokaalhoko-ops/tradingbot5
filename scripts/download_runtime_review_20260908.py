"""Download the exact recorded Demo cohort using TLS-verified curl DNS resolution."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.audit_runtime_theses_1m import extract_theses, load_cached_1m, verify_recorded_candles
from trading_v2.data import BinanceUsdMFuturesDataSource, normalize_symbol
from utils.runtime_candidate_replay import load_decision_events
from urllib.parse import urlencode

class CurlSession:
    def __init__(self, address):
        self.address = address
    def get(self, url, *, params, timeout):
        if url != "https://demo-fapi.binance.com/fapi/v1/klines":
            raise ValueError("Only the public Demo kline endpoint is allowed")
        command = [
            "curl.exe", "--resolve", "demo-fapi.binance.com:443:" + self.address,
            "--silent", "--show-error", "--fail-with-body", "--connect-timeout", "10",
            "--max-time", str(int(timeout)), "--retry", "2", "--retry-delay", "1",
            url + "?" + urlencode(params),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=100)
        if result.returncode:
            raise RuntimeError("curl: " + result.stderr[:250] + " " + result.stdout[:250])
        payload = json.loads(result.stdout)
        class Response:
            def raise_for_status(self): pass
            def json(self): return payload
        return Response()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--resolved-ip", required=True)
    args=parser.parse_args()
    out=ROOT/"research_data/runtime_review_20260908"
    out.mkdir(parents=True,exist_ok=True)
    archives=sorted(ROOT.glob("runtime_logs_24h_20260907_1905_part*.zip"))
    events=load_decision_events(archives)
    run="e2be2e86774d4b1ba62fe3748cc98cf4"
    events=[e for e in events if e["run_id"]==run]
    theses=extract_theses(events)
    assert len(theses)==30, len(theses)
    cutoff=datetime.now(timezone.utc).replace(second=0,microsecond=0)
    windows={}
    for thesis in theses:
        symbol=thesis["symbol"]
        decisions=[e for e in events if e["symbol"]==symbol]
        start=min(datetime.fromisoformat(e["timestamp"].replace("Z","+00:00")) for e in decisions).replace(second=0,microsecond=0)-timedelta(hours=3)
        end=min(cutoff,max(datetime.fromisoformat(e["timestamp"].replace("Z","+00:00")) for e in decisions)+timedelta(minutes=62))
        windows[symbol]=(start,end)
    (out/"theses.json").write_text(json.dumps({"run_id":run,"thesis_count":len(theses),"theses":theses},ensure_ascii=False,indent=2),encoding="utf-8")
    source=BinanceUsdMFuturesDataSource(base_url="https://demo-fapi.binance.com",session=CurlSession(args.resolved_ip),max_retries=0)
    entries=[]
    for i,(symbol,(start,end)) in enumerate(sorted(windows.items()),1):
        cache=out/"bars_1m"/(normalize_symbol(symbol)+"_1m.jsonl.gz")
        marker=cache.with_suffix(".metadata.json")
        spec={"start":start.isoformat(),"end":end.isoformat()}
        if cache.exists() and marker.exists() and json.loads(marker.read_text())==spec:
            bars=source.read_cache(cache)
        else:
            bars=source.fetch(symbol=symbol,timeframe="1m",start=start,end=end)
            source.write_cache(cache,bars)
            marker.write_text(json.dumps(spec),encoding="utf-8")
        entries.append({
            "symbol":symbol,"normalized_symbol":normalize_symbol(symbol),
            "requested_start":start.isoformat(),"requested_end":end.isoformat(),
            "rows":len(bars),"path":str(cache),"sha256":hashlib.sha256(cache.read_bytes()).hexdigest(),
            "first_open_time":bars[0].open_time.isoformat() if bars else None,
            "last_close_time":bars[-1].close_time.isoformat() if bars else None,
        })
        manifest={"source":"BINANCE_USDM_DEMO_REST","base_url":"https://demo-fapi.binance.com","timeframe":"1m",
                  "tls_verified":True,"dns_source":"RAILWAY_RUNTIME_RESOLVER","resolved_ip":args.resolved_ip,
                  "run_id":run,"download_cutoff":cutoff.isoformat(),"symbols":entries,"complete":i==len(windows)}
        (out/"demo_1m_manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
        print(f"{i}/{len(windows)} {symbol} rows={len(bars)}",flush=True)
        time.sleep(0.2)
    verification=verify_recorded_candles(events,load_cached_1m(manifest))
    (out/"source_verification.json").write_text(json.dumps(verification,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:verification[k] for k in ("comparison_count","matched","mismatched","by_timeframe")}),flush=True)

if __name__=="__main__":
    main()
