"""Reprice frozen SMC plans with old/new replay code; no signal tuning or network."""
from __future__ import annotations
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import pandas as pd
from trading_v2.models import Bar, TradePlan, ReplayResult, Side, EntryKind, ReplayStatus
from trading_v2.replay import CausalReplayEngine
from trading_v2.evaluation import EvaluatedPlan, enforce_portfolio_capacity, summarize

CHECKPOINT = ROOT / 'research_data/causal_futures_dataset/current_smc_historical_replay_full.json.checkpoint.json'
FEATURES = ROOT / 'research_data/causal_futures_dataset/features'
BEFORE = ROOT / 'audit_exports/replay_integrity_20260909_before'
OUTPUT = ROOT / 'research_data/replay_integrity_20260909'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


def dt(value):
    return datetime.fromisoformat(value) if value else None


def plan_from(row):
    return TradePlan(
        plan_id=row['plan_id'], strategy_id=row['model'], setup_id=row['thesis_id'],
        symbol=row['symbol'], side=Side(row['side']), created_at=dt(row['created_at']),
        valid_until=dt(row['valid_until']), exit_by=dt(row['exit_by']), entry_kind=EntryKind(row['entry_kind']),
        entry=row['entry'], stop=row['stop'], target=row['target'], thesis=row['thesis'],
        score=row['score'], evidence=row.get('evidence') or {},
    )


def encoded(result):
    return {k: (v.value if isinstance(v, ReplayStatus) else v.isoformat() if isinstance(v, datetime) else v)
            for k, v in asdict(result).items()}


def main():
    OUTPUT.mkdir(exist_ok=True)
    before_manifest = json.loads((BEFORE / 'manifest.json').read_text())
    for f in ('trading_v2/replay.py', 'trading_v2/evaluation.py'):
        assert sha(BEFORE / f) == before_manifest[f]
    old_module = module('trading_v2._replay_before_integrity', BEFORE / 'trading_v2/replay.py')
    old_evaluation = module('trading_v2._evaluation_before_integrity', BEFORE / 'trading_v2/evaluation.py')
    old, new = old_module.CausalReplayEngine(), CausalReplayEngine()
    saved = json.loads(CHECKPOINT.read_text(encoding='utf-8'))
    symbols = defaultdict(list)
    for model, rows in saved['records'].items():
        for row in rows:
            symbols[row['symbol']].append((model, row))
    assert sum(map(len, symbols.values())) == 8098
    old_records, new_records = defaultdict(list), defaultdict(list)
    ledger, mismatches, feature_hashes = [], [], {}
    for i, (symbol, rows) in enumerate(sorted(symbols.items()), 1):
        clean = symbol.replace('/', '')
        parts = []
        for path in sorted(FEATURES.glob(f'*/{clean}.parquet')):
            parts.append(pd.read_parquet(path, columns=['open_time','close_time','open','high','low','close','volume']))
            feature_hashes[str(path.relative_to(ROOT))] = sha(path)
        frame = pd.concat(parts, ignore_index=True).sort_values('open_time').reset_index(drop=True)
        frame['open_time'] = pd.to_datetime(frame['open_time'], utc=True)
        frame['close_time'] = pd.to_datetime(frame['close_time'], utc=True)
        assert not frame['open_time'].duplicated().any(), symbol
        for model, raw in rows:
            plan = plan_from(raw)
            left = int(frame.open_time.searchsorted(plan.created_at, side='left'))
            right = int(frame.open_time.searchsorted(plan.exit_by, side='right'))
            window = frame.iloc[left:right]
            # Historical generator split its input at gaps and passed only the
            # plan's contiguous segment. Reconstruct that exact input here.
            if len(window):
                gaps = window.open_time.diff().dt.total_seconds().iloc[1:].ne(300)
                if gaps.any():
                    window = window.loc[:gaps[gaps].index[0] - 1]
            bars = tuple(Bar(symbol, '5m', r.open_time.to_pydatetime(), r.close_time.to_pydatetime(),
                             float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
                         for r in window.itertuples())
            old_result = old.replay_plan(plan, bars, horizon_end=plan.exit_by)
            new_result = new.replay_plan(plan, bars, horizon_end=plan.exit_by)
            old_data, new_data = encoded(old_result), encoded(new_result)
            fields = ['status','filled_at','exited_at','entry_price','exit_price','gross_r','net_r','bars_observed','mfe_r','mae_r']
            differences = [key for key in fields if old_data[key] != raw.get(key)]
            if differences:
                mismatches.append({'plan_id':plan.plan_id,'fields':differences,'saved':{k:raw.get(k) for k in differences},
                                   'old_rerun':{k:old_data[k] for k in differences}})
            old_records[model].append(EvaluatedPlan(plan, old_result))
            new_records[model].append(EvaluatedPlan(plan, new_result))
            changed = [key for key in fields if old_data[key] != new_data[key]]
            ledger.append({'plan':raw, 'old':old_data, 'new':new_data, 'changed_fields':changed})
        if i % 25 == 0 or i == len(symbols):
            print(f'Replayed {i}/{len(symbols)} symbols; {len(ledger)} frozen plans; old mismatches {len(mismatches)}', flush=True)
    models = {}
    for model in saved['records']:
        before_selected = enforce_portfolio_capacity(old_records[model], max_open_positions=5)
        after_selected = enforce_portfolio_capacity(new_records[model], max_open_positions=5)
        paired = [x for x in ledger if x['plan']['model'] == model]
        models[model] = {
            'frozen_plans':len(paired),
            'before':old_evaluation.summarize(before_selected.records),
            'old_results_corrected_drawdown':summarize(before_selected.records)['max_drawdown_r'],
            'after':summarize(after_selected.records),
            'before_capacity_rejected':before_selected.rejected_capacity,
            'after_capacity_rejected':after_selected.rejected_capacity,
            'changed_status_or_fill_or_pnl':sum(any(k in x['changed_fields'] for k in ['status','filled_at','entry_price','exit_price','gross_r','net_r']) for x in paired),
            'status_transitions':dict(Counter(x['old']['status']+' -> '+x['new']['status'] for x in paired if x['old']['status'] != x['new']['status'])),
        }
    result = {
        'method':'Same 8098 frozen plans and cached 5m data, no regenerated signals or rule optimization. Old code replay reproduction checked first.',
        'source_profile':saved['profile'], 'source_checkpoint_sha256':sha(CHECKPOINT),
        'code_sha256':{f:sha(ROOT/f) for f in ['trading_v2/replay.py','trading_v2/evaluation.py','trading_v2/models.py','scripts/audit_replay_integrity_20260909.py']},
        'old_code_sha256':before_manifest, 'feature_sha256':feature_hashes,
        'symbols':len(symbols),'plans':len(ledger), 'old_reproduction_mismatches':mismatches,
        'models':models,
        'limitations':[
            'Historical smc_balanced geometry plans, not current smc_strict deployment or a replay of its fresh revalidation and execution.',
            'Existing historical universe selection and feature generation are held fixed, not independently validated by this repricing.',
            'OHLC gives no exact intrabar limit fill/exit ordering. MFE/MAE may include pre-fill or post-exit extremes.',
            'Incomplete/ambiguous results are excluded from realized metrics; their exclusion can bias profitability and bars cannot prove fills.',
            'Drawdown is cumulative realized R by recorded exits, not account equity including open PnL, sizing, margin and funding.',
            'No financial success verdict follows from this audit. Old reports using affected code require separate regeneration.',
        ],
    }
    (OUTPUT/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    (OUTPUT/'ledger.json').write_text(json.dumps(ledger,indent=2),encoding='utf-8')
    print(json.dumps({'old_reproduction_mismatches':len(mismatches),'models':models},ensure_ascii=True),flush=True)
    if mismatches:
        raise SystemExit('Old results did not reproduce; investigate before interpreting corrected metrics')


if __name__ == '__main__':
    main()
