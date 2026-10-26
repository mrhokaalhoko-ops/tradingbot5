"""Run/source identity supplied by an external adapter, never fetched here."""

from dataclasses import dataclass, field
from datetime import datetime

from .causality import require_utc
from .events import canonical_sha256
from .enums import ReadinessGate


def _commit(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 40 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise ValueError(f"{name} must be a full Git SHA-1")


def _hash(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise ValueError(f"{name} must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class RunManifest:
    source_base_commit: str
    source_base_parent: str
    source_base_tree_hash: str
    source_commit: str
    remote_url: str
    branch_name: str
    worktree_clean: bool
    source_file_hashes: tuple[tuple[str, str], ...]
    config_hash: str
    rule_version: str
    variant_id: str
    data_snapshot_hash: str
    cost_model_id: str
    cost_model_hash: str
    created_at: datetime
    source_bundle_hash: str | None = None
    runtime_build_hash: str | None = None
    worktree_diff_hash: str | None = None
    execution_authority: bool = field(default=False, init=False)
    manifest_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("source_base_commit", "source_base_parent", "source_base_tree_hash", "source_commit"):
            _commit(getattr(self, name), name)
        for name in ("config_hash", "data_snapshot_hash", "cost_model_hash"):
            _hash(getattr(self, name), name)
        for name in ("source_bundle_hash", "runtime_build_hash", "worktree_diff_hash"):
            value = getattr(self, name)
            if value is not None:
                _hash(value, name)
        for name in ("remote_url", "branch_name", "rule_version", "variant_id", "cost_model_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} required")
        require_utc(self.created_at, "created_at")
        if not isinstance(self.worktree_clean, bool):
            raise TypeError("worktree_clean must be explicit bool")
        if not isinstance(self.source_file_hashes, tuple):
            raise TypeError("source_file_hashes must be immutable tuple")
        if not self.source_file_hashes:
            raise ValueError("source_file_hashes required")
        if self.worktree_clean and self.worktree_diff_hash is not None:
            raise ValueError("clean worktree cannot have a diff hash")
        if not self.worktree_clean and self.worktree_diff_hash is None:
            raise ValueError("dirty worktree requires explicit diff hash")
        paths = []
        for entry in self.source_file_hashes:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise TypeError("each source file hash entry must be a pair tuple")
            path, digest = entry
            if not isinstance(path, str) or not path.strip():
                raise ValueError("source file path required")
            _hash(digest, f"source_file_hashes[{path}]")
            paths.append(path)
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate source path")
        object.__setattr__(self, "source_file_hashes", tuple(sorted(self.source_file_hashes)))
        identity = {name: getattr(self, name) for name in self.__dataclass_fields__
                    if name != "manifest_hash"}
        object.__setattr__(self, "manifest_hash", canonical_sha256(identity))


@dataclass(frozen=True, slots=True)
class GateAssessment:
    """Independent evidence for one gate; no automatic gate promotion."""

    gate: ReadinessGate
    ready: bool
    evidence_ids: tuple[str, ...]
    assessed_at: datetime
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.gate, ReadinessGate) or not isinstance(self.ready, bool):
            raise TypeError("gate and explicit ready bool required")
        if not isinstance(self.evidence_ids, tuple) or any(
            not isinstance(value, str) or not value.strip() for value in self.evidence_ids
        ):
            raise TypeError("evidence_ids must be a tuple of nonempty strings")
        if self.ready and not self.evidence_ids:
            raise ValueError("ready gate requires evidence_ids")
        require_utc(self.assessed_at, "assessed_at")
