"""Local-only source/diff audit for the Shadow evidence candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "44cf6c6d459689a67f2d7c9a5c75ce6dc58bf789"

CLASSIFICATION = {
    "core/config.py": "SHADOW_ONLY",
    "main.py": "SHADOW_ONLY",
    "strategy/trailing_stop.py": "SHADOW_ONLY",
    "strategy/strategy_v2_evidence.py": "SHADOW_ONLY",
    "strategy/strategy_v2_research.py": "SHADOW_ONLY",
    "strategy/strategy_v2_shadow_storage.py": "SHADOW_ONLY",
    "scripts/stress_strategy_v2_shadow_storage.py": "TEST",
    "scripts/audit_strategy_v2_shadow_candidate.py": "TEST",
    "tests/test_strategy_v2_research.py": "TEST",
    "tests/test_strategy_v2_evidence.py": "TEST",
    "tests/test_strategy_v2_shadow_isolation.py": "TEST",
    "tests/test_strategy_v2_shadow_audit.py": "TEST",
    "STRATEGY_V2_SHADOW_COLLECTION_PLAN.md": "DOCUMENTATION",
    "SHADOW_ISOLATION_AND_STORAGE_REVIEW.md": "DOCUMENTATION",
}

GENERATED_EXACT = {
    "SHADOW_DIFF_AUDIT.json",
    "SHADOW_STORAGE_STRESS.json",
}
GENERATED_PREFIXES = (
    ".shadow_stress/",
    "strategy_v2_shadow/",
)

MUST_MATCH_BASE = (
    "strategy/risk_manager.py",
    "core/exchange.py",
    "utils/state_manager.py",
    "strategy/user_visible_trade_proposal.py",
)


def _git(root: str, *args: str) -> str:
    # Preserve leading porcelain-status spaces; stripping them corrupts the
    # first path (for example core/config.py became ore/config.py).
    return subprocess.check_output(
        ["git", "-C", root, *args], text=True, encoding="utf-8"
    ).rstrip("\r\n")


def _git_bytes(root: str, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", root, *args])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _normalize_lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _classify_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized in GENERATED_EXACT or normalized.startswith(GENERATED_PREFIXES):
        return "GENERATED_ARTIFACT"
    return CLASSIFICATION.get(normalized, "UNEXPECTED")


def _parse_porcelain_z(payload: bytes) -> list[str]:
    """Return paths from ``git status --porcelain=v1 -z`` without trimming."""
    records = payload.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise RuntimeError(f"unexpected porcelain status record: {record!r}")
        status = record[:2]
        path = record[3:].decode("utf-8", errors="surrogateescape")
        # In porcelain v1 -z, rename/copy records are followed by the source
        # path. The first path is the destination and is the one to classify.
        if b"R" in status or b"C" in status:
            if index >= len(records) or not records[index]:
                raise RuntimeError(f"missing rename source for: {record!r}")
            index += 1
        paths.append(path.replace("\\", "/"))
    return paths


def _status_paths(root: str) -> list[str]:
    payload = _git_bytes(root, "status", "--porcelain=v1", "-z")
    return _parse_porcelain_z(payload)


def _compare_must_match(root: str, base: str, path: str) -> dict:
    with open(os.path.join(root, path), "rb") as handle:
        working = handle.read()
    base_blob = _git_bytes(root, "show", f"{base}:{path}")
    working_normalized = _normalize_lf(working)
    base_normalized = _normalize_lf(base_blob)
    base_git_blob = _git(root, "rev-parse", f"{base}:{path}")
    working_git_blob = _git(root, "hash-object", f"--path={path}", path)
    diff_result = subprocess.run(
        ["git", "-C", root, "diff", "--quiet", base, "--", path],
        check=False,
    )
    if diff_result.returncode not in (0, 1):
        raise RuntimeError(f"git diff failed for {path}: {diff_result.returncode}")
    canonical_identical = (
        diff_result.returncode == 0 and working_git_blob == base_git_blob
    )
    return {
        "working_raw_sha256": _sha(working),
        "base_blob_sha256": _sha(base_blob),
        "raw_worktree_bytes_differ": working != base_blob,
        "working_crlf_count": working.count(b"\r\n"),
        "base_crlf_count": base_blob.count(b"\r\n"),
        "working_lf_normalized_sha256": _sha(working_normalized),
        "base_lf_normalized_sha256": _sha(base_normalized),
        "normalized_source_differs": working_normalized != base_normalized,
        "working_canonical_git_blob": working_git_blob,
        "base_git_blob": base_git_blob,
        "canonical_git_source_differs": not canonical_identical,
        "canonical_git_source_identical": canonical_identical,
        "difference_classification": (
            "IDENTICAL"
            if working == base_blob
            else "EOL_NORMALIZATION_ONLY"
            if working_normalized == base_normalized and canonical_identical
            else "ACTUAL_SOURCE_DIFF"
        ),
    }


def run(*, root: str = ROOT, base: str = BASE) -> dict:
    paths = _status_paths(root)
    changed = {path: _classify_path(path) for path in paths}
    must_match = {
        path: _compare_must_match(root, base, path)
        for path in MUST_MATCH_BASE
    }
    generated = sorted(
        path for path, kind in changed.items() if kind == "GENERATED_ARTIFACT"
    )
    commit_eligible = {
        path: kind for path, kind in changed.items()
        if kind != "GENERATED_ARTIFACT"
    }
    unexpected = sorted(
        path for path, kind in changed.items() if kind == "UNEXPECTED"
    )
    return {
        "base": base,
        "head": _git(root, "rev-parse", "HEAD"),
        "changed_files": changed,
        "generated_artifacts": generated,
        "generated_artifacts_excluded_from_commit": True,
        "commit_eligible_changed_files": commit_eligible,
        "unexpected": unexpected,
        "must_match_base": must_match,
        "must_match_base_canonically_identical": all(
            item["canonical_git_source_identical"]
            for item in must_match.values()
        ),
        "diff_name_status": _git(root, "diff", "--name-status", base),
        "diff_stat": _git(root, "diff", "--stat", base),
        "untracked": _git(
            root, "ls-files", "--others", "--exclude-standard"
        ).splitlines(),
    }


def _audit_passes(result: dict) -> bool:
    return (
        not result["unexpected"]
        and result["must_match_base_canonically_identical"]
        and result["generated_artifacts_excluded_from_commit"]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run()
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    if not _audit_passes(result):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
