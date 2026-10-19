"""Generate the frozen Prospective Shadow Experiment v1 review artifacts."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.prospective_shadow_spec import (
    FORBIDDEN_EXECUTION_IMPORTS,
    build_event_schema,
    build_freeze_spec,
    validate_freeze_spec,
)


SPEC_SOURCE_FILES = (
    "strategy/prospective_shadow_spec.py",
    "scripts/freeze_prospective_shadow_v1.py",
)

REVIEW_SOURCE_FILES = (
    "strategy/prospective_shadow_spec.py",
    "scripts/freeze_prospective_shadow_v1.py",
    "tests/test_prospective_shadow_spec.py",
)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def package_review(root: Path, output_root: Path) -> dict:
    manifest_path = output_root / "REVIEW_PACKAGE_MANIFEST_SHA256_R4.txt"
    artifact_files = sorted(
        path for path in output_root.iterdir()
        if path.is_file() and path != manifest_path
    )
    rows = []
    entries = []
    for path in artifact_files:
        archive_name = f"artifacts/{path.name}"
        rows.append(f"{_sha256(path)}  {archive_name}")
        entries.append((path, archive_name))
    for relative in REVIEW_SOURCE_FILES:
        path = root / relative
        archive_name = f"source/{relative}"
        rows.append(f"{_sha256(path)}  {archive_name}")
        entries.append((path, archive_name))
    manifest_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    entries.append((manifest_path, f"artifacts/{manifest_path.name}"))

    package_path = root / "review_handoff_20261005_shadow_freeze_r4.zip"
    temporary = package_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, archive_name in entries:
            archive.write(path, archive_name)
    temporary.replace(package_path)
    return {
        "package": str(package_path),
        "package_sha256": _sha256(package_path),
        "manifest_entries": len(rows),
        "zip_entries": len(entries),
    }


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.append(node.module or "")
    return sorted(set(result))


def _called_symbols(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = []

    def name(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            result.append(name(node.func))
    return sorted(set(filter(None, result)))


def no_execution_proof(root: Path) -> dict:
    files = {}
    failures = []
    for relative in SPEC_SOURCE_FILES:
        path = root / relative
        imports = _imports(path)
        calls = _called_symbols(path)
        forbidden = []
        for token in FORBIDDEN_EXECUTION_IMPORTS:
            if "." in token and any(module.startswith(token) for module in imports):
                forbidden.append(token)
            if any(call.split(".")[-1] == token for call in calls):
                forbidden.append(token)
        forbidden = sorted(set(forbidden))
        files[relative] = {
            "imports": imports,
            "called_symbols": calls,
            "forbidden_matches": forbidden,
        }
        if forbidden:
            failures.append({"file": relative, "matches": forbidden})
    return {
        "schema_version": 2,
        "scope": "FROZEN_SPECIFICATION_LAYER_ONLY",
        "runtime_engine_present": False,
        "exchange_client_present": False,
        "credentials_read": False,
        "order_route_present": False,
        "files": files,
        "failures": failures,
        "passed": not failures,
        "limitation": (
            "Must be rerun against the future runtime engine before any Shadow process starts."
        ),
    }


def render_markdown(spec: dict) -> str:
    by_id = {row["policy_id"]: row for row in spec["policies"]}
    lines = [
        "# Prospective Shadow Experiment v1 — Frozen Policy Specification",
        "",
        f"- Experiment: `{spec['experiment_id']}`",
        f"- Freeze: `{spec['freeze_id']}`",
        f"- Frozen at UTC: `{spec['frozen_at_utc']}`",
        f"- Policy freeze commit: `{spec['identity']['policy_freeze_commit']}`",
        f"- Runtime commit at freeze: `{spec['identity']['runtime_commit_at_freeze']}`",
        f"- Freeze hash: `{spec['freeze_hash']}`",
        "- Runtime status: **not implemented and not started**",
        "- Execution authority: **none for every policy**",
        "",
        "Any change to a threshold, state transition, level rule, geometry rule, or cost creates a new policy version and a separate sample.",
        "",
        "## Precise differences",
        "",
        "| Policy | What stays fixed | The isolated change |",
        "|---|---|---|",
        "| Control | Current production source, SMC strict, Sniper and closed-5m confirmation | Nothing; baseline mirror only |",
        "| A | Frozen Active Altcoin prospective rules | Candidate/direction hypothesis: LONG only |",
        "| B | Exactly A's candidate IDs and LONG direction | ENTRY_READY at the initial Active Event, then shared execution mechanics |",
        "| C | Exactly B's mechanics and portfolio after ENTRY_READY | ENTRY_READY only after its 1W→1D→4H→1H→15m state machine |",
        "",
        "## Control — CONTROL_ON_SHARED_ACTIVE_CANDIDATES_V1",
        "",
        "The current production decision pipeline is source-locked and mirrored without calling execution. It retains SMC strict, derivatives enforcement, current quality/RR gates, virtual Sniper lifecycle, and closed-5m confirmation. A resolved runtime configuration snapshot is mandatory at experiment start.",
        "",
        "## A — ACTIVE_ALTCOIN_LONG_V1",
        "",
        "This is the existing prospective Active Altcoin policy without threshold optimization, restricted to LONG. It waits for a 0.25 ATR pullback, causal reclaim, and fills at the exact next 1m open. Stop uses the observed pullback extreme plus the frozen buffer; target is 1.5R. Its historical native cap of five is retained only as metadata; the primary experiment applies the common ten-position portfolio to A, B, C and Control alike.",
        "",
        "## B — MOMENTUM_REPRICED_V1",
        "",
        "B consumes the same candidate ID and LONG direction as A before entry modeling, but does not wait for A's pullback/reclaim. Its policy-specific `entry_ready_rule` makes `candidate_emitted_at` and `entry_ready_at` the same initial Active Event decision time. From that instant it invokes the shared execution mechanics: first non-stale ask, causally repriced stop/target, minimum actual RR, common costs and exits.",
        "",
        "## C — TOP_DOWN_V1",
        "",
        "C does not reuse B's timing rule. Its `entry_ready_at` is the timestamp of its own `WAIT_FOR_TRIGGER -> ENTRY_READY` transition after the 15m trigger. Only then does it invoke the exact same shared execution mechanics as B. Weekly and daily establish LONG bias; 4H maps structure and levels; 1H manages pullback or a separately detected breakout followed by a later retest. 5m is observation-only. TOO_EXTENDED recovers at or below 1.25 ATR; breakout retest expires after four later closed 1H bars; loss of bias, invalidation, expiry, or missing causal location terminates the thesis. Invalidated theses never resume.",
        "",
        "## Common market and portfolio evidence",
        "",
        "All policies must receive the same `market_snapshot_id`. Each event records candidate/thesis IDs, closed-candle IDs, bid/ask, spread, mark/index, funding and next funding time in UTC. The primary comparison applies one common portfolio to every policy: at most 10 positions, 5x leverage, 1% target initial margin, 0.25% base stop-risk and a 1% hard stop-risk ceiling. Native or legacy caps are metadata only and cannot govern paired results. No order is sent.",
        "",
        "## Exact execution costs",
        "",
        "LONG enters from ask and exits TP/SL/time from bid (reverse for SHORT). Spread is therefore embedded exactly once. On each filled side the model adds 5 bps taker fee and 1 bp adverse slippage. The historical 12 bps allowance is explicitly decomposed as 10 bps round-trip fees plus 2 bps round-trip slippage, excluding spread; it is never subtracted again as a generic cost.",
        "",
        "## Identity recorded in every future event",
        "",
        "Every event must contain `policy_freeze_commit`, `runtime_commit`, `runtime_build_hash`, `freeze_hash`, and `policy_config_hash`. A runtime implemented later cannot masquerade as the policy-freeze source.",
        "",
        "## Policy hashes",
        "",
    ]
    for policy in spec["policies"]:
        lines.append(
            f"- `{policy['policy_id']}@{policy['policy_version']}`: `{policy['config_hash']}`"
        )
    lines.extend([
        "",
        "## Exact machine authority",
        "",
        "The JSON artifact is authoritative for all numeric thresholds and transitions. This document explains it but does not override it.",
        "",
        "## Comparison interpretation",
        "",
        "- A − Control measures candidate/direction selection plus the frozen Active Altcoin path.",
        "- B − A isolates B's immediate entry timing and causal geometry repricing on paired candidate IDs under the same portfolio cap.",
        "- C − B isolates the top-down decision/location timing; all mechanics after each policy's ENTRY_READY are identical.",
        "",
        "No policy may pool rows across a different config hash or version.",
    ])
    return "\n".join(lines) + "\n"


def acceptance_markdown() -> str:
    return """# Prospective Shadow Experiment v1 freeze r3 — Acceptance Tests

Shadow runtime must not start until every item passes.

1. The machine specification validates and every policy `config_hash` is reproducible.
2. Any mutation of a frozen config without changing version fails validation.
3. All four policies declare `execution_authority=NONE`.
4. The runtime engine contains no import or callable route to exchange order methods.
5. One shared market snapshot is fanned out to all policies with the same ID and UTC timestamp.
6. A and B receive identical paired candidate IDs and LONG direction before entry modeling.
7. B never reuses old entry/SL/TP and records the causal level IDs used to rebuild geometry.
8. B skips when no causal target/stop exists or actual RR is below 1.5.
9. B and C have identical shared-execution-mechanics hashes after policy-specific ENTRY_READY.
10. C uses only fully closed candles and obeys the frozen state transitions.
11. C cannot use 5m as a hard gate in v1.
12. Missing or stale bid/ask produces `DATA_UNAVAILABLE_NO_FILL` rather than an invented fill.
13. Same-bar TP/SL is retained as causal ambiguity and also reported under the conservative policy.
14. Strategy-normalized and capital-constrained portfolio metrics are produced separately.
15. Portfolio simulation enforces 10 positions, 5x leverage, 1% target margin, 0.25% base stop-risk, 1% hard ceiling, rounding and min-notional.
16. Every event separates policy freeze commit, runtime commit/build hash, freeze hash and policy config hash.
17. Restart recovery cannot duplicate a candidate, thesis, fill or terminal outcome.
18. Checkpoint counters use independent thesis IDs, not raw repeated events.
19. A/B/C results cannot be pooled after any policy hash changes.
20. A static no-execution proof and a runtime fake-exchange test pass before launch.
21. B records `candidate_emitted_at == entry_ready_at` and never waits for A's pullback/reclaim.
22. C records `entry_ready_at` only at its own `WAIT_FOR_TRIGGER -> ENTRY_READY` transition and cannot enter at candidate emission.
23. A, B, C and Control use the same ten-position experimental capacity; A's five-position legacy cap is metadata only.
24. Ask-entry/bid-exit embeds spread once; 5 bps fee and 1 bp slippage apply per filled side, with no second generic 12 bps subtraction.
25. C tests TOO_EXTENDED recovery, bias loss, separate breakout/retest, four-bar expiry, null levels and invalidation restart.
26. ENTRY_READY, SIMULATED_FILL and TERMINAL_OUTCOME fail schema validation when their stage-specific evidence is absent.
27. No Shadow process starts until the future runtime commit/build passes all static and fake-exchange checks.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-root", type=Path,
        default=ROOT / "research_data" / "prospective_shadow_v1_r3",
    )
    args = parser.parse_args()
    spec = build_freeze_spec(ROOT)
    event_schema = build_event_schema(ROOT)
    errors = validate_freeze_spec(spec)
    proof = no_execution_proof(ROOT)
    verification = {
        "schema_version": 3,
        "freeze_hash": spec["freeze_hash"],
        "spec_validation_errors": errors,
        "no_execution_proof_passed": proof["passed"],
        "passed": not errors and proof["passed"],
    }
    args.output_root.mkdir(parents=True, exist_ok=True)
    _atomic_json(args.output_root / "prospective_shadow_policies_v1_r4.json", spec)
    _atomic_json(args.output_root / "prospective_shadow_event_schema_v1_r4.json", event_schema)
    _atomic_json(
        args.output_root / "resolved_runtime_config_evidence_r4.json",
        spec["resolved_runtime_config"],
    )
    _atomic_json(args.output_root / "shadow_no_execution_proof_v1_r4.json", proof)
    _atomic_json(args.output_root / "freeze_verification_v1_r4.json", verification)
    (args.output_root / "PROSPECTIVE_SHADOW_EXPERIMENT_V1_FREEZE_R4.md").write_text(
        render_markdown(spec), encoding="utf-8"
    )
    (args.output_root / "PROSPECTIVE_SHADOW_ACCEPTANCE_TESTS_V1_R4.md").write_text(
        acceptance_markdown(), encoding="utf-8"
    )
    package = package_review(ROOT, args.output_root)
    print(json.dumps({"verification": verification, "review_package": package}, indent=2))
    return 0 if verification["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
