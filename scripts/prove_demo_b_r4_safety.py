"""Machine-readable static safety proof for the dedicated Demo B executor."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo_execution.config import ALLOWED_DEMO_REST_HOSTS, DemoExecutionConfig
from demo_execution.identity import DEMO_RUNTIME_SOURCE_FILES, demo_runtime_identity


FORBIDDEN_IMPORTS = ("core.exchange", "main", "utils.telegram_bot")


def prove(root: Path) -> dict:
    findings = []
    for relative in DEMO_RUNTIME_SOURCE_FILES:
        path = root / relative
        if not path.exists():
            findings.append({"file": relative, "kind": "MISSING_SOURCE"})
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        if not relative.startswith("demo_execution/") and relative != "scripts/run_demo_b_r4.py":
            continue
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(FORBIDDEN_IMPORTS):
                        findings.append({
                            "file": relative, "line": node.lineno,
                            "kind": "FORBIDDEN_PRODUCTION_IMPORT", "value": alias.name,
                        })
            if module.startswith(FORBIDDEN_IMPORTS):
                findings.append({
                    "file": relative, "line": node.lineno,
                    "kind": "FORBIDDEN_PRODUCTION_IMPORT", "value": module,
                })
    defaults = DemoExecutionConfig()
    identity = demo_runtime_identity(root)
    return {
        "proof_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtime_identity": identity,
        "allowed_rest_hosts": sorted(ALLOWED_DEMO_REST_HOSTS),
        "mainnet_allowed": False,
        "execution_enabled_default": defaults.enabled,
        "policy_id": identity["policy_id"],
        "other_policy_execution_enabled": False,
        "frozen_risk": {
            "max_open_positions": defaults.frozen_max_open_positions,
            "initial_validation_cap": defaults.initial_validation_cap,
            "leverage": defaults.leverage,
            "target_initial_margin_fraction": defaults.target_initial_margin_fraction,
            "base_stop_risk_fraction": defaults.base_stop_risk_fraction,
            "hard_stop_risk_fraction": defaults.hard_stop_risk_fraction,
            "minimum_actual_rr": defaults.min_actual_rr,
        },
        "findings": findings,
        "passed": not findings and defaults.enabled is False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--output")
    args = parser.parse_args()
    result = prove(Path(args.root).resolve())
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
