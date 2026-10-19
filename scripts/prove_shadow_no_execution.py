"""AST-based proof that the R3 Shadow Runtime contains no order route."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shadow_runtime.identity import RUNTIME_SOURCE_FILES, runtime_identity


BANNED_IMPORT_PREFIXES = (
    "ccxt", "binance.client", "core.exchange", "utils.exchange",
)
BANNED_CALL_NAMES = {
    "create_order", "create_market_order", "create_limit_order", "cancel_order",
    "cancel_all_orders", "set_leverage", "set_margin_mode", "transfer",
    "withdraw", "fetch_balance", "fetch_positions", "fetch_open_orders",
    "post", "put", "patch", "delete",
}
BANNED_TEXT = (
    "/fapi/v1/order", "/fapi/v1/batchOrders", "private_post", "signed_request",
)


def scan(root: Path) -> dict:
    findings = []
    scanned = []
    for relative in RUNTIME_SOURCE_FILES:
        path = root / relative
        if not path.is_file():
            findings.append({"file": relative, "kind": "MISSING_RUNTIME_SOURCE"})
            continue
        source = path.read_text(encoding="utf-8")
        scanned.append({
            "file": relative,
            "sha256": hashlib.sha256(source.encode()).hexdigest().upper(),
        })
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(BANNED_IMPORT_PREFIXES):
                        findings.append({"file": relative, "line": node.lineno, "kind": "BANNED_IMPORT", "value": alias.name})
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(BANNED_IMPORT_PREFIXES):
                    findings.append({"file": relative, "line": node.lineno, "kind": "BANNED_IMPORT", "value": module})
            elif isinstance(node, ast.Call):
                name = None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name in BANNED_CALL_NAMES:
                    findings.append({"file": relative, "line": node.lineno, "kind": "BANNED_CALL", "value": name})
        lowered = source.lower()
        for marker in BANNED_TEXT:
            if marker.lower() in lowered:
                findings.append({"file": relative, "kind": "BANNED_ROUTE_TEXT", "value": marker})
    identity = runtime_identity(root)
    return {
        "proof_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtime_commit": identity["runtime_commit"],
        "runtime_build_hash": identity["runtime_build_hash"],
        "files_scanned": scanned,
        "banned_import_prefixes": list(BANNED_IMPORT_PREFIXES),
        "banned_call_names": sorted(BANNED_CALL_NAMES),
        "findings": findings,
        "passed": not findings,
        "conclusion": "NO_EXCHANGE_ORDER_ROUTE_IN_RUNTIME_SOURCE" if not findings else "FAILED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    result = scan(root)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
