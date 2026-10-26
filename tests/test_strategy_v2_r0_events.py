"""R0 decision, event, manifest, and purity invariants."""

import ast
import builtins
import socket
import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from strategy_v2.causality import CausalTimes
from strategy_v2.decisions import StrategyDecision, no_trade
from strategy_v2.enums import (DecisionStatus, NoTradeReason, ReadinessGate, Side)
from strategy_v2.events import (EventEnvelope, InputEvidenceRef, append_unique,
                                canonical_json, canonical_sha256)
from strategy_v2.manifest import GateAssessment, RunManifest
from strategy_v2.models import (ExitPlanContract, ExitTarget, IdentityRefs,
                                RewardReference, ThesisId)


T0 = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)
T3 = T0 + timedelta(minutes=3)
T4 = T0 + timedelta(minutes=4)


def event(**overrides):
    times = CausalTimes(T0, T1, T2)
    values = dict(
        schema_version="1.0", event_type="V2_PROPOSAL_REJECTED",
        event_time=T3, recorded_at=T4, decision_at=T3,
        source_commit="a" * 40, config_hash="b" * 64,
        rule_version="spec-1.1", variant_id="r0", symbol="ABCUSDT", side=Side.LONG,
        identities=IdentityRefs(thesis_id=ThesisId("thesis-1")),
        parent_event_ids=("parent-1",), input_evidence_ids=("level-1",),
        input_evidence=(InputEvidenceRef("level-1", "4h close", times, "c" * 64),),
        payload={"b": [Decimal("2.50"), {"x": "value"}], "a": 1},
        reason_code=NoTradeReason.FUTURE_EVIDENCE,
        failed_invariant="available_at <= decision_at",
    )
    values.update(overrides)
    return EventEnvelope(**values)


def manifest(**overrides):
    values = dict(
        source_base_commit="a" * 40, source_base_parent="b" * 40,
        source_base_tree_hash="c" * 40, source_commit="a" * 40,
        remote_url="https://example.invalid/repo.git", branch_name="codex/research",
        worktree_clean=True, source_file_hashes=(("main.py", "d" * 64),),
        config_hash="e" * 64, rule_version="spec-1.1", variant_id="r0",
        data_snapshot_hash="f" * 64, cost_model_id="cost-v0",
        cost_model_hash="0" * 64, created_at=T3,
    )
    values.update(overrides)
    return RunManifest(**values)


@pytest.mark.parametrize("reason", list(NoTradeReason))
def test_every_reason_is_typed_no_trade(reason):
    first = no_trade(symbol="ABCUSDT", side=Side.LONG, decision_at=T3,
                     stage="R0_VALIDATION", reason_code=reason,
                     failed_invariant="test invariant", input_evidence_ids=("level-1",))
    second = no_trade(symbol="ABCUSDT", side=Side.LONG, decision_at=T3,
                      stage="R0_VALIDATION", reason_code=reason,
                      failed_invariant="test invariant", input_evidence_ids=("level-1",))
    assert first == second
    assert first.status is DecisionStatus.NO_TRADE
    assert first.execution_authority is False
    assert not any(name in first.__dataclass_fields__ for name in
                   ("order_intent_id", "order_id", "fill_id", "proposal_id"))


def test_no_trade_requires_reason_stage_and_failed_invariant():
    with pytest.raises(ValueError):
        no_trade(symbol="ABCUSDT", side=None, decision_at=T3, stage="",
                 reason_code=NoTradeReason.UNKNOWN_REGIME, failed_invariant="x")
    with pytest.raises(ValueError):
        no_trade(symbol="ABCUSDT", side=None, decision_at=T3, stage="regime",
                 reason_code=NoTradeReason.UNKNOWN_REGIME, failed_invariant="")
    with pytest.raises(ValueError):
        StrategyDecision(DecisionStatus.NO_TRADE, "ABCUSDT", None, T3, "regime",
                         "bad", (), reason_code=None)


def test_candidate_does_not_arise_from_missing_reward_or_exit_contract():
    with pytest.raises(ValueError, match="reward/exit"):
        StrategyDecision(DecisionStatus.PROPOSAL_CANDIDATE, "ABCUSDT", Side.LONG,
                         T3, "research", None, ())
    reward = RewardReference("reward-1", Decimal("12"), Side.LONG,
                             "level-1", CausalTimes(T0, T1, T2))
    plan = ExitPlanContract("runner-v0", reward, None, False, ("STRUCTURE_EXIT",))
    candidate = StrategyDecision(DecisionStatus.PROPOSAL_CANDIDATE, "ABCUSDT",
                                 Side.LONG, T3, "research", None, ("level-1",), exit_plan=plan)
    assert candidate.execution_authority is False


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("future_part", ["reward_reference", "exit_target"])
def test_candidate_rejects_future_reward_or_exit_target(side, future_part):
    available = CausalTimes(T0, T1, T2)
    future = CausalTimes(T0, T1, T4)
    reference = RewardReference("reward-1", Decimal("12"), side,
                                "level-1", future if future_part == "reward_reference" else available)
    target = ExitTarget("target-1", Decimal("13"), side, "level-2",
                        future if future_part == "exit_target" else available)
    plan = ExitPlanContract("target-v0", reference, target, True, ("FINAL_TP",))
    with pytest.raises(ValueError, match="unavailable at decision"):
        StrategyDecision(DecisionStatus.PROPOSAL_CANDIDATE, "ABCUSDT", side,
                         T3, "research", None, ("level-1",), exit_plan=plan)


@pytest.mark.parametrize("decision_side,plan_side", [
    (Side.LONG, Side.SHORT), (Side.SHORT, Side.LONG),
])
@pytest.mark.parametrize("with_target", [False, True])
def test_candidate_rejects_opposite_reward_or_target_side(decision_side, plan_side, with_target):
    provenance = CausalTimes(T0, T1, T2)
    reward = RewardReference("reward-1", Decimal("12"), plan_side, "level-1", provenance)
    target = ExitTarget("target-1", Decimal("13"), plan_side, "level-2", provenance) if with_target else None
    plan = ExitPlanContract("policy-v0", reward, target, with_target,
                            ("FINAL_TP",) if with_target else ("STRUCTURE_EXIT",))
    with pytest.raises(ValueError, match="candidate side"):
        StrategyDecision(DecisionStatus.PROPOSAL_CANDIDATE, "ABCUSDT",
                         decision_side, T3, "research", None, ("level-1",), exit_plan=plan)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("with_target", [False, True])
def test_candidate_accepts_matching_reward_and_optional_target_side(side, with_target):
    provenance = CausalTimes(T0, T1, T2)
    reward = RewardReference("reward-1", Decimal("12"), side, "level-1", provenance)
    target = ExitTarget("target-1", Decimal("13"), side, "level-2", provenance) if with_target else None
    plan = ExitPlanContract("policy-v0", reward, target, with_target,
                            ("FINAL_TP",) if with_target else ("STRUCTURE_EXIT",))
    candidate = StrategyDecision(DecisionStatus.PROPOSAL_CANDIDATE, "ABCUSDT",
                                 side, T3, "research", None, ("level-1",), exit_plan=plan)
    assert candidate.side is side and candidate.execution_authority is False


def test_candidate_requires_evidence_but_no_trade_can_have_none():
    reward = RewardReference("reward-1", Decimal("12"), Side.LONG,
                             "level-1", CausalTimes(T0, T1, T2))
    plan = ExitPlanContract("runner-v0", reward, None, False, ("STRUCTURE_EXIT",))
    with pytest.raises(ValueError, match="input evidence"):
        StrategyDecision(DecisionStatus.PROPOSAL_CANDIDATE, "ABCUSDT",
                         Side.LONG, T3, "research", None, (), exit_plan=plan)
    accepted = StrategyDecision(DecisionStatus.PROPOSAL_CANDIDATE, "ABCUSDT",
                                Side.LONG, T3, "research", None, ("level-1",), exit_plan=plan)
    assert accepted.input_evidence_ids == ("level-1",)
    rejected = no_trade(symbol="ABCUSDT", side=Side.LONG, decision_at=T3,
                        stage="data", reason_code=NoTradeReason.MISSING_CAUSAL_TIMESTAMP,
                        failed_invariant="required evidence missing")
    assert rejected.input_evidence_ids == ()


def test_execution_authority_cannot_be_supplied_or_mutated():
    with pytest.raises(TypeError):
        no_trade(symbol="ABCUSDT", side=None, decision_at=T3, stage="data",
                 reason_code=NoTradeReason.FUTURE_EVIDENCE,
                 failed_invariant="x", execution_authority=True)
    decision = no_trade(symbol="ABCUSDT", side=None, decision_at=T3, stage="data",
                        reason_code=NoTradeReason.FUTURE_EVIDENCE,
                        failed_invariant="x")
    with pytest.raises(FrozenInstanceError):
        decision.execution_authority = True
    with pytest.raises(TypeError):
        EventEnvelope(execution_authority=True, **{
            name: getattr(event(), name) for name in event().__dataclass_fields__
            if name not in ("event_id", "payload_hash", "execution_authority")
        })


def test_canonical_serialization_and_hash_are_deterministic():
    assert canonical_json({"z": Decimal("1.00"), "a": 2}) == canonical_json({"a": 2, "z": Decimal("1.00")})
    assert canonical_sha256({"z": 1, "a": 2}) == canonical_sha256({"a": 2, "z": 1})
    assert event().payload_hash == event(payload={"a": 1, "b": [Decimal("2.50"), {"x": "value"}]}).payload_hash
    assert len(event().payload_hash) == 64


def test_event_payload_is_deeply_immutable():
    original = {"nested": {"values": ["a"]}}
    item = event(payload=original)
    original["nested"]["values"].append("b")
    assert item.payload["nested"]["values"] == ("a",)
    with pytest.raises(TypeError):
        item.payload["nested"]["new"] = 1
    with pytest.raises(TypeError, match="float"):
        event(payload={"unsafe_float": 1.2})


def test_duplicate_semantic_event_is_idempotent():
    first = event()
    later_recording = event(recorded_at=T4 + timedelta(seconds=1))
    assert first.event_id == later_recording.event_id
    assert append_unique((first,), later_recording) == (first,)
    assert len(append_unique((first,), event(payload={"different": True}))) == 2


def test_correction_supersedes_without_rewriting_original():
    original = event()
    correction = event(event_type="V2_CORRECTION", supersedes_event_id=original.event_id)
    assert original.supersedes_event_id is None
    assert correction.supersedes_event_id == original.event_id
    assert correction.event_id != original.event_id
    assert len(append_unique((original,), correction)) == 2


def test_future_input_evidence_rejected_in_event_contract():
    future = InputEvidenceRef("future", "bar", CausalTimes(T1, T2, T4), "d" * 64)
    with pytest.raises(ValueError, match="future"):
        event(input_evidence_ids=("future",), input_evidence=(future,))


def test_event_rejects_mutable_parent_ids_and_invalid_input_hash():
    with pytest.raises(TypeError, match="tuples"):
        event(parent_event_ids=(["mutable"],))
    with pytest.raises(ValueError, match="data_hash"):
        InputEvidenceRef("evidence", "4h bar", CausalTimes(T0, T1, T2), "unknown")


def test_event_rejects_unknown_hash_and_recording_before_event():
    with pytest.raises(ValueError, match="config_hash"):
        event(config_hash="unknown")
    with pytest.raises(ValueError, match="recorded_at"):
        event(recorded_at=T2)


def test_decision_event_recording_cannot_precede_decision():
    with pytest.raises(ValueError, match="recorded_at"):
        event(event_time=T2, decision_at=T3, recorded_at=T2)
    assert event(event_time=T2, decision_at=T3, recorded_at=T3).recorded_at == T3


def test_later_outcome_event_can_follow_decision():
    outcome = event(event_type="V2_OUTCOME", event_time=T4,
                    decision_at=T3, recorded_at=T4)
    assert outcome.event_time > outcome.decision_at
    with pytest.raises(ValueError, match="recorded_at"):
        event(event_type="V2_OUTCOME", event_time=T4,
              decision_at=T3, recorded_at=T3)


def test_manifest_is_reproducible_and_unknown_runtime_is_explicit():
    first, second = manifest(), manifest()
    assert first.manifest_hash == second.manifest_hash
    assert first.runtime_build_hash is None
    assert first.source_bundle_hash is None
    assert first.execution_authority is False
    assert len(first.manifest_hash) == 64


def test_manifest_source_file_order_does_not_change_identity():
    files = (("z.py", "a" * 64), ("a.py", "b" * 64))
    assert manifest(source_file_hashes=files).manifest_hash == manifest(
        source_file_hashes=tuple(reversed(files))
    ).manifest_hash


def test_manifest_rejects_mutable_file_hash_pairs():
    with pytest.raises(TypeError, match="pair tuple"):
        manifest(source_file_hashes=(["main.py", "a" * 64],))


@pytest.mark.parametrize("change", [
    {"source_commit": ""}, {"source_base_commit": None},
    {"config_hash": "unknown"}, {"source_file_hashes": ()},
    {"worktree_clean": False},
    {"created_at": datetime(2026, 10, 1, 10)},
])
def test_manifest_rejects_missing_or_unreproducible_identity(change):
    with pytest.raises((ValueError, TypeError)):
        manifest(**change)


def test_dirty_worktree_requires_explicit_diff_hash():
    item = manifest(worktree_clean=False, worktree_diff_hash="a" * 64)
    assert item.worktree_clean is False
    assert item.worktree_diff_hash == "a" * 64


def test_clean_worktree_forbids_diff_hash():
    with pytest.raises(ValueError, match="clean worktree"):
        manifest(worktree_clean=True, worktree_diff_hash="a" * 64)


def test_readiness_gates_are_independent_non_authoritative_records():
    research = GateAssessment(ReadinessGate.RESEARCH_IMPLEMENTATION, True,
                              ("source-identity",), T3)
    demo = GateAssessment(ReadinessGate.LIMITED_DEMO, False, (), T3)
    assert research.ready and not demo.ready
    assert research.execution_authority is False
    assert demo.execution_authority is False


def test_all_readiness_gates_are_separate_and_do_not_promote_each_other():
    assert [gate.value for gate in ReadinessGate] == [
        "READY_FOR_RESEARCH_IMPLEMENTATION", "READY_FOR_REPLAY",
        "READY_FOR_VALIDATION/HOLDOUT", "READY_FOR_FORWARD_SHADOW",
        "READY_FOR_TRADING_IMPLEMENTATION", "READY_FOR_DEMO_REVIEW",
        "READY_FOR_LIMITED_DEMO", "READY_FOR_EXPANDED_DEMO",
    ]
    assessments = tuple(GateAssessment(gate, gate is ReadinessGate.RESEARCH_IMPLEMENTATION,
                                        ("source-identity",) if gate is ReadinessGate.RESEARCH_IMPLEMENTATION else (),
                                        T3) for gate in ReadinessGate)
    assert len(assessments) == 8
    assert sum(item.ready for item in assessments) == 1
    assert all(item.execution_authority is False for item in assessments)
    assert all(not item.ready for item in assessments[1:])


def test_ready_gate_requires_evidence_but_not_ready_gate_may_be_empty():
    with pytest.raises(ValueError, match="evidence_ids"):
        GateAssessment(ReadinessGate.REPLAY, True, (), T3)
    assert GateAssessment(ReadinessGate.REPLAY, False, (), T3).evidence_ids == ()


def test_pure_package_import_graph_has_no_forbidden_dependencies_or_clock():
    package = Path(__file__).resolve().parents[1] / "strategy_v2"
    forbidden = ("core", "shadow_runtime", "requests", "httpx", "aiohttp",
                 "socket", "sqlite3", "telegram", "binance", "ccxt", "railway")
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not item.name.lower().startswith(forbidden) for item in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert not (node.module or "").lower().startswith(forbidden)
            elif isinstance(node, ast.Call):
                assert not (isinstance(node.func, ast.Attribute) and node.func.attr in
                            ("now", "utcnow", "connect", "create_order", "write_text", "write_bytes"))


def test_research_contract_calls_do_not_use_filesystem_network_or_db(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("I/O forbidden")
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(sqlite3, "connect", forbidden)
        assert manifest().manifest_hash
        assert event().event_id
        assert no_trade(symbol="ABCUSDT", side=None, decision_at=T3, stage="data",
                        reason_code=NoTradeReason.FUTURE_EVIDENCE,
                        failed_invariant="available_at <= decision_at").execution_authority is False
