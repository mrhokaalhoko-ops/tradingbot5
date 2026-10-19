"""Summarize frozen repriced plans using the original calendar split boundaries."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.audit_replay_integrity_20260909 import plan_from
from trading_v2.evaluation import EvaluatedPlan, enforce_portfolio_capacity, summarize_time_splits
from trading_v2.models import ReplayResult, ReplayStatus


def main():
    directory=ROOT/'research_data/replay_integrity_20260909'
    source=directory/'ledger.json'
    ledger=json.loads(source.read_text())
    grouped=defaultdict(list)
    for row in ledger:
        plan=plan_from(row['plan'])
        for variant in ['old','new']:
            data=dict(row[variant])
            data['status']=ReplayStatus(data['status'])
            for k in ['filled_at','exited_at']:
                if data[k]: data[k]=datetime.fromisoformat(data[k])
            grouped[(row['plan']['model'],variant)].append(EvaluatedPlan(plan,ReplayResult(**data)))
    output={}
    for (model,variant),records in grouped.items():
        selected=enforce_portfolio_capacity(records,max_open_positions=5)
        splits=summarize_time_splits(selected.records,
            start=datetime(2025,8,1,tzinfo=timezone.utc),end=datetime(2026,8,1,tzinfo=timezone.utc))
        output.setdefault(model,{})[variant]={name:{k:v for k,v in metrics.items() if k in
            ['realized','expectancy_r','profit_factor','max_drawdown_r','total_net_r','incomplete','ambiguous']} for name,metrics in splits.items()}
    result={'method':'Original 60/20/20 calendar split over 2025-08-01 to 2026-08-01, capacity=5; previously inspected data, not an untouched test.',
            'ledger_sha256':hashlib.sha256(source.read_bytes()).hexdigest(), 'models':output}
    (directory/'time_splits.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(output,ensure_ascii=True))


if __name__=='__main__':
    main()
