"""Run B from Mainnet public signals with execution isolated to Binance Demo."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo_execution.client import DemoBinanceClient
from demo_execution.config import DemoExecutionConfig
from demo_execution.cross_environment import (
    EXECUTION_ENVIRONMENT,
    MAINNET_USDM_PUBLIC_URL,
    SIGNAL_MARKET_ENVIRONMENT,
    audit_cross_environment_candidate,
    compare_contracts,
    demo_execution_quote,
    validate_mainnet_signal_endpoint,
)
from demo_execution.executor import DemoBExecutor, POLICY_ID
from demo_execution.identity import demo_runtime_identity
from demo_execution.store import DemoExecutionStore
from shadow_runtime.market import PublicBinanceDemoMarket
from strategy.prospective_shadow_spec import build_freeze_spec


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_artifacts(
    config: DemoExecutionConfig,
    store: DemoExecutionStore,
    executor: DemoBExecutor,
    *,
    status: str,
    cycle: dict | None = None,
) -> dict:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    freeze = build_freeze_spec(ROOT)
    identity = demo_runtime_identity(ROOT)
    summary = {
        "generated_at_utc": _utc_now(),
        "service_mode": "BINANCE_USDM_MAINNET_SIGNAL_DEMO_B_R4_EXECUTOR",
        "status": status,
        "execution_enabled": config.enabled,
        "credentials_present": bool(config.api_key and config.api_secret),
        "endpoint_proof": executor.environment_proof or config.validate(),
        "signal_market": {
            **validate_mainnet_signal_endpoint(MAINNET_USDM_PUBLIC_URL),
            "detector_input": True,
        },
        "execution_market": {
            "execution_environment": EXECUTION_ENVIRONMENT,
            "base_url": config.base_url,
            "account_and_order_authority": True,
        },
        "policy_id": POLICY_ID,
        "policy_version": next(
            row["policy_version"] for row in freeze["policies"]
            if row["policy_id"] == POLICY_ID
        ),
        "freeze_id": freeze["freeze_id"],
        "freeze_hash": freeze["freeze_hash"],
        "runtime_identity": identity,
        "effective_position_cap": executor.effective_position_cap if config.enabled else 0,
        "frozen_position_cap": config.frozen_max_open_positions,
        "risk": {
            "leverage": config.leverage,
            "target_initial_margin_fraction": config.target_initial_margin_fraction,
            "base_stop_risk_fraction": config.base_stop_risk_fraction,
            "hard_stop_risk_fraction": config.hard_stop_risk_fraction,
            "minimum_actual_rr": config.min_actual_rr,
        },
        "last_reconciliation": store.get_meta("last_reconciliation"),
        "last_cycle": cycle,
        "executions": store.all(),
    }
    (config.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (config.output_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in store.events():
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return summary


def main() -> int:
    config = DemoExecutionConfig.from_env()
    store = DemoExecutionStore(config.database_path)
    client = DemoBinanceClient(config)
    executor = DemoBExecutor(
        config,
        client,
        store,
        signal_market_environment=SIGNAL_MARKET_ENVIRONMENT,
        execution_environment=EXECUTION_ENVIRONMENT,
    )
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if not config.enabled:
            startup = executor.startup()
            try:
                public_endpoint_probe = client.prove_environment()
            except Exception as exc:
                public_endpoint_probe = {
                    **config.validate(),
                    "probe_ok": False,
                    "error": f"{type(exc).__name__}:{exc}",
                }
            else:
                public_endpoint_probe["probe_ok"] = True
            summary = _write_artifacts(config, store, executor, status="DISABLED_SAFE")
            print(
                "DEMO_B_R4_STARTUP "
                + json.dumps(
                    {
                        **startup,
                        "base_url": config.base_url,
                        "mainnet": False,
                        "signal_market_environment": SIGNAL_MARKET_ENVIRONMENT,
                        "signal_base_url": MAINNET_USDM_PUBLIC_URL,
                        "execution_environment": EXECUTION_ENVIRONMENT,
                        "execution_base_url": config.base_url,
                        "order_calls_possible": False,
                        "public_endpoint_probe": public_endpoint_probe,
                        "freeze_id": summary["freeze_id"],
                        "freeze_hash": summary["freeze_hash"],
                        "policy_id": summary["policy_id"],
                        "runtime_identity": summary["runtime_identity"],
                        "database": str(config.database_path),
                        "events": str(config.output_dir / "events.jsonl"),
                        "summary": str(config.output_dir / "summary.json"),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            while not stopped:
                time.sleep(1.0)
            return 0

        while not stopped:
            startup = executor.startup()
            print("DEMO_B_R4_STARTUP " + json.dumps(startup, sort_keys=True), flush=True)
            if startup["status"] == "READY":
                break
            _write_artifacts(
                config, store, executor,
                status=startup["status"],
            )
            print(
                "DEMO_B_R4_FAIL_CLOSED "
                + json.dumps(
                    {
                        "status": startup["status"],
                        "order_calls_possible": False,
                        "reconciliation": startup.get("reconciliation"),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            deadline = time.monotonic() + config.poll_seconds
            while not stopped and time.monotonic() < deadline:
                time.sleep(0.25)
        if stopped:
            return 0

        signal_endpoint_proof = validate_mainnet_signal_endpoint(
            MAINNET_USDM_PUBLIC_URL
        )
        signal_market = PublicBinanceDemoMarket(
            base_url=MAINNET_USDM_PUBLIC_URL
        )
        execution_market = PublicBinanceDemoMarket(base_url=config.base_url)
        signal_filters = signal_market.exchange_contracts()
        execution_filters = client.exchange_contracts()
        cycles = int(store.get_meta("cycles_completed", 0))
        while not stopped:
            cycle_started = time.monotonic()
            reconciliation = executor.reconcile()
            cycle = {
                "started_at_utc": _utc_now(),
                "reconciliation": reconciliation,
                "symbols_scanned": 0,
                "raw_active_events": 0,
                "qualified_candidates": 0,
                "api_errors": 0,
                "results": [],
                "signal_market_environment": SIGNAL_MARKET_ENVIRONMENT,
                "execution_environment": EXECUTION_ENVIRONMENT,
                "signal_endpoint_proof": signal_endpoint_proof,
            }
            if reconciliation["ok"]:
                try:
                    scan = signal_market.scan_candidates(
                        as_of=signal_market.server_now()
                    )
                    cycle.update({
                        "symbols_scanned": scan.symbols_scanned,
                        "raw_active_events": scan.raw_active_events,
                        "qualified_candidates": scan.qualified_candidates,
                        "api_errors": scan.api_errors,
                    })
                    for candidate, cached_1m in scan.candidates:
                        snapshot = signal_market.snapshot(
                            candidate, cached_1m=cached_1m
                        )
                        signal_contract = signal_filters.get(candidate.symbol) or {}
                        execution_contract = (
                            execution_filters.get(candidate.symbol) or {}
                        )
                        contract_check = compare_contracts(
                            candidate.symbol,
                            signal_contract,
                            execution_contract,
                        )
                        quote = None
                        if contract_check["compatible"]:
                            quote = demo_execution_quote(
                                candidate.symbol,
                                execution_market.quote(candidate.symbol),
                            )
                        audit = audit_cross_environment_candidate(
                            snapshot,
                            signal_contract,
                            execution_contract,
                            quote,
                        )
                        result = executor.execute_candidate(
                            snapshot,
                            execution_contract,
                            execution_quote=quote,
                            cross_environment_audit=audit,
                        )
                        cycle["results"].append({
                            "candidate_id": candidate.candidate_id,
                            "symbol": candidate.symbol,
                            "status": result.get("state") or result.get("status"),
                            "signal_market_environment": SIGNAL_MARKET_ENVIRONMENT,
                            "execution_environment": EXECUTION_ENVIRONMENT,
                            "cross_environment_ok": audit["ok"],
                            "cross_environment_reasons": audit["reasons"],
                        })
                except Exception as exc:
                    cycle["api_errors"] += 1
                    cycle["failure"] = f"{type(exc).__name__}:{exc}"
                    executor.reconciliation_ok = False
            cycles += 1
            store.put_meta("cycles_completed", cycles)
            cycle["cycles_completed"] = cycles
            cycle["elapsed_ms"] = (time.monotonic() - cycle_started) * 1000.0
            if not reconciliation.get("complete", True):
                runtime_status = "RECONCILIATION_WAITING"
            elif not reconciliation["ok"]:
                runtime_status = "BLOCKED_RECONCILIATION"
            else:
                runtime_status = "RUNNING"
            summary = _write_artifacts(
                config, store, executor, status=runtime_status, cycle=cycle
            )
            print(
                "DEMO_B_R4_CYCLE "
                + json.dumps(
                    {
                        "generated_at_utc": summary["generated_at_utc"],
                        "cycle": cycle,
                        "effective_position_cap": executor.effective_position_cap,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            deadline = time.monotonic() + config.poll_seconds
            while not stopped and time.monotonic() < deadline:
                time.sleep(0.25)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
