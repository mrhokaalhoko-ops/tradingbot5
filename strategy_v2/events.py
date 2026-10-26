"""Immutable append-only event *contracts*, without a storage adapter."""

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .causality import CausalTimes, require_utc
from .enums import NoTradeReason, Side
from .models import IdentityRefs


def _freeze(value):
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("payload keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, bool, type(None), Decimal, datetime, Enum)):
        return value
    if isinstance(value, float):
        raise TypeError("float payloads are forbidden; use Decimal")
    raise TypeError(f"unsupported payload value: {type(value).__name__}")


def _normal(value):
    if isinstance(value, Mapping):
        return {key: _normal(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normal(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite Decimal cannot be serialized")
        return str(value)
    if isinstance(value, datetime):
        require_utc(value, "serialized timestamp")
        return value.isoformat().replace("+00:00", "Z")
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _normal(getattr(value, item.name)) for item in fields(value)}
    return value


def canonical_json(value) -> str:
    return json.dumps(_normal(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def canonical_sha256(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest().upper()


@dataclass(frozen=True, slots=True)
class InputEvidenceRef:
    evidence_id: str
    source: str
    times: CausalTimes
    data_hash: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (
            self.evidence_id, self.source,
        )):
            raise ValueError("evidence ID/source required")
        if not isinstance(self.data_hash, str) or len(self.data_hash) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in self.data_hash
        ):
            raise ValueError("data_hash must be SHA-256 hex")
        if not isinstance(self.times, CausalTimes):
            raise TypeError("causal times are required")


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    schema_version: str
    event_type: str
    event_time: datetime
    recorded_at: datetime
    decision_at: datetime
    source_commit: str
    config_hash: str
    rule_version: str
    variant_id: str
    symbol: str
    side: Side | None
    identities: IdentityRefs
    parent_event_ids: tuple[str, ...]
    input_evidence_ids: tuple[str, ...]
    payload: Mapping[str, object]
    input_evidence: tuple[InputEvidenceRef, ...] = ()
    source_bundle_hash: str | None = None
    runtime_build_hash: str | None = None
    reason_code: NoTradeReason | None = None
    failed_invariant: str | None = None
    supersedes_event_id: str | None = None
    execution_authority: bool = field(default=False, init=False)
    payload_hash: str = field(init=False)
    event_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("schema_version", "event_type", "source_commit", "config_hash",
                     "rule_version", "variant_id", "symbol"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} required")
        if len(self.source_commit) != 40 or any(
            character not in "0123456789abcdefABCDEF" for character in self.source_commit
        ):
            raise ValueError("source_commit must be full Git SHA-1")
        for name in ("config_hash", "source_bundle_hash", "runtime_build_hash"):
            value = getattr(self, name)
            if value is not None and (
                len(value) != 64 or any(
                    character not in "0123456789abcdefABCDEF" for character in value
                )
            ):
                raise ValueError(f"{name} must be a SHA-256 hex digest")
        for name in ("event_time", "recorded_at", "decision_at"):
            require_utc(getattr(self, name), name)
        if self.recorded_at < max(self.event_time, self.decision_at):
            raise ValueError("recorded_at cannot precede event_time or decision_at")
        if self.side is not None and not isinstance(self.side, Side):
            raise TypeError("side must be Side or None")
        if self.reason_code is not None and not isinstance(self.reason_code, NoTradeReason):
            raise TypeError("reason_code must be typed")
        for name in ("failed_invariant", "supersedes_event_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be nonempty when present")
        if not isinstance(self.identities, IdentityRefs):
            raise TypeError("identities must be IdentityRefs")
        if any(not isinstance(ids, tuple) or any(
            not isinstance(value, str) or not value.strip() for value in ids
        ) for ids in (self.parent_event_ids, self.input_evidence_ids)):
            raise TypeError("event/evidence IDs must be tuples of nonempty strings")
        if not isinstance(self.input_evidence, tuple) or any(
            type(item) is not InputEvidenceRef for item in self.input_evidence
        ):
            raise TypeError("input_evidence must be a tuple of InputEvidenceRef")
        if self.input_evidence and tuple(ref.evidence_id for ref in self.input_evidence) != self.input_evidence_ids:
            raise ValueError("input_evidence_ids must match input evidence")
        if any(ref.times.available_at > self.decision_at for ref in self.input_evidence):
            raise ValueError("future input evidence is forbidden")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        frozen = _freeze(self.payload)
        object.__setattr__(self, "payload", frozen)
        payload_hash = canonical_sha256(frozen)
        object.__setattr__(self, "payload_hash", payload_hash)
        semantic = {
            "schema_version": self.schema_version, "event_type": self.event_type,
            "event_time": self.event_time, "decision_at": self.decision_at,
            "source_commit": self.source_commit, "config_hash": self.config_hash,
            "rule_version": self.rule_version, "variant_id": self.variant_id,
            "symbol": self.symbol, "side": self.side, "identities": self.identities,
            "parent_event_ids": self.parent_event_ids,
            "input_evidence_ids": self.input_evidence_ids,
            "input_evidence": self.input_evidence,
            "source_bundle_hash": self.source_bundle_hash,
            "runtime_build_hash": self.runtime_build_hash,
            "payload_hash": payload_hash, "reason_code": self.reason_code,
            "failed_invariant": self.failed_invariant,
            "supersedes_event_id": self.supersedes_event_id,
        }
        object.__setattr__(self, "event_id", "evt_" + canonical_sha256(semantic).lower())

    def canonical_record(self) -> str:
        return canonical_json(self)


def append_unique(existing: tuple[EventEnvelope, ...], event: EventEnvelope) -> tuple[EventEnvelope, ...]:
    """Model idempotent append semantics without writing a database."""
    if not isinstance(existing, tuple) or not isinstance(event, EventEnvelope):
        raise TypeError("event stream must be a tuple of EventEnvelope")
    for prior in existing:
        if prior.event_id == event.event_id:
            return existing
    return existing + (event,)
