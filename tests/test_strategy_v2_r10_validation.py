"""R10 cost-aware, segregated validation cohort tests."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from strategy_v2.causality import CausalTimes
from strategy_v2.decisions import StrategyDecision
from strategy_v2.enums import DecisionStatus, Side
from strategy_v2.events import canonical_sha256
from strategy_v2.models import (
    AttemptId,
    ExitPlanContract,
    ExitTarget,
    RewardReference,
    SetupId,
    ThesisId,
)
from strategy_v2.r3_planning import TargetMode
from strategy_v2.r4_risk import CostModelSpec, RiskPlan, RiskPlanStatus
from strategy_v2.r7_proposal import AtomicResearchProposal
from strategy_v2.r9_replay import (
    ReplayCase,
    ReplayOutcome,
    ReplayOutcomeReason,
    ReplayOutcomeStatus,
)
from strategy_v2.r10_validation import (
    ValidationPolicySpec,
    ValidationReason,
    ValidationSegment,
    ValidationSplitManifest,
    ValidationVerdict,
    build_validation_observation,
    evaluate_validation_cohort,
)


D = Decimal
BASE = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
COMMIT = "fddc10b06bd4e0fea592ecf3369195894e3c0742"
VARIANT = canonical_sha256(("strategy-v2-r10-fixture",)).lower()
H = lambda value: canonical_sha256((value,)).lower()
HOLDOUT_MANIFEST = ValidationSplitManifest(
    COMMIT, "CALENDAR_SPLIT_V1", VARIANT,
    BASE - timedelta(days=30), BASE - timedelta(days=20),
    BASE - timedelta(days=10), BASE + timedelta(days=30),
)
DEVELOPMENT_MANIFEST = ValidationSplitManifest(
    COMMIT, "ALTERNATE_SPLIT_FOR_MIXING_TEST", VARIANT,
    BASE - timedelta(days=1), BASE + timedelta(days=10),
    BASE + timedelta(days=20), BASE + timedelta(days=30),
)


def costs(nonzero=False):
    if nonzero:
        return CostModelSpec(
            "COSTS_V1", D("0.001"), D("0.001"), D("0.001"),
            D("0.01"), D("0.001"), D("0.001"),
        )
    return CostModelSpec(
        "ZERO_COSTS_TEST_ONLY", D("0"), D("0"), D("0"),
        D("0"), D("0"), D("0"),
    )


def artifacts(index=0, status=ReplayOutcomeStatus.TARGET_HIT, *,
              split_manifest=HOLDOUT_MANIFEST,
              nonzero_costs=False, thesis_key=None):
    at = BASE + timedelta(hours=index)
    thesis_id = ThesisId(f"ths-{index if thesis_key is None else thesis_key}")
    setup_id = SetupId(f"set-{index}")
    attempt_id = AttemptId(f"att-{index}")
    cost = costs(nonzero_costs)
    projected_loss = D("34.7702") if nonzero_costs else D("20")
    risk = RiskPlan(
        RiskPlanStatus.READY, None, thesis_id, Side.LONG, "ABCUSDT",
        "ABC-PERP-E1", D("100"), D("98"), D("10000"), D("10"),
        D("1000"), D("50"), D("100"),
        projected_loss / D("10"), projected_loss, D("100"), D("0.5"),
        "filter-1", cost.cost_hash, H("risk-policy"), at,
    )
    times = CausalTimes(
        at - timedelta(minutes=3), at - timedelta(minutes=2),
        at - timedelta(minutes=1),
    )
    reward = RewardReference("reward", D("108"), Side.LONG, "level", times)
    target = ExitTarget("target", D("108"), Side.LONG, "level", times)
    decision = StrategyDecision(
        DecisionStatus.PROPOSAL_CANDIDATE, "ABCUSDT", Side.LONG, at,
        "R7_ATOMIC_RESEARCH_PROPOSAL", None,
        (f"evidence-{index}",), thesis_id=thesis_id, setup_id=setup_id,
        attempt_id=attempt_id,
        exit_plan=ExitPlanContract("target-policy", reward, target, True, ()),
    )
    proposal = AtomicResearchProposal(
        decision, "ABC-PERP-E1", f"entry-{index}", f"stop-{index}",
        f"target-{index}", risk.plan_id, f"portfolio-{index}",
        H("proposal-policy"),
    )
    case = ReplayCase(
        proposal.assembly_id, "ABCUSDT", "ABC-PERP-E1", Side.LONG, at,
        D("100"), D("98"), D("108"), TargetMode.STRUCTURAL_FINAL_TP,
        f"entry-{index}", f"stop-{index}", f"target-{index}", risk.plan_id,
    )
    if status is ReplayOutcomeStatus.TARGET_HIT:
        outcome = ReplayOutcome(
            status, None, case.case_id, at + timedelta(hours=1), "bar-target",
            D("108"), 4, 1, (f"bar-record-{index}",),
        )
    elif status is ReplayOutcomeStatus.STOP_HIT:
        outcome = ReplayOutcome(
            status, None, case.case_id, at + timedelta(hours=1), "bar-stop",
            D("98"), -1, 1, (f"bar-record-{index}",),
        )
    elif status is ReplayOutcomeStatus.CENSORED:
        outcome = ReplayOutcome(
            status, ReplayOutcomeReason.DUAL_TOUCH_AMBIGUOUS, case.case_id,
            at + timedelta(hours=1), "bar-dual", None, None, None,
            (f"bar-record-{index}",),
        )
    elif status is ReplayOutcomeStatus.UNRESOLVED:
        outcome = ReplayOutcome(
            status, ReplayOutcomeReason.DATA_GAP, case.case_id,
            at + timedelta(hours=1), None, None, None, None,
            (f"bar-record-{index}",),
        )
    else:
        outcome = ReplayOutcome(
            status, None, case.case_id, at + timedelta(hours=1),
            None, None, None, None, (f"bar-record-{index}",),
        )
    observation = build_validation_observation(
        proposal=proposal, case=case, outcome=outcome, risk=risk,
        cost_model=cost, split_manifest=split_manifest,
    )
    return observation


def policy(**overrides):
    values = dict(
        source_commit=COMMIT, policy_id="HOLDOUT_GATE_V1", rule_version="r10",
        expected_segment=ValidationSegment.HOLDOUT,
        strategy_variant_hash=VARIANT,
        split_manifest_hash=HOLDOUT_MANIFEST.manifest_hash,
        minimum_total_observations=4,
        minimum_scored_observations=4,
        minimum_distinct_theses=4,
        maximum_censored_fraction=D("0"),
        maximum_unresolved_fraction=D("0"),
        maximum_open_fraction=D("0"),
        minimum_mean_net_r=D("0.5"),
    )
    values.update(overrides)
    return ValidationPolicySpec(**values)


def test_zero_cost_terminal_observations_preserve_exact_net_r():
    win = artifacts(0, ReplayOutcomeStatus.TARGET_HIT)
    loss = artifacts(1, ReplayOutcomeStatus.STOP_HIT)
    assert (win.net_r_numerator, win.net_r_denominator) == (4, 1)
    assert (loss.net_r_numerator, loss.net_r_denominator) == (-1, 1)
    assert (win.net_pnl_numerator, win.net_pnl_denominator) == (80, 1)


def test_nonzero_costs_reduce_target_r_and_normal_stop_is_minus_one_net_r():
    win = artifacts(0, ReplayOutcomeStatus.TARGET_HIT, nonzero_costs=True)
    loss = artifacts(1, ReplayOutcomeStatus.STOP_HIT, nonzero_costs=True)
    assert D(win.net_r_numerator) / D(win.net_r_denominator) < D("4")
    assert (loss.net_r_numerator, loss.net_r_denominator) == (-1, 1)


def test_qualified_holdout_reports_exact_metrics_without_claiming_execution():
    observations = tuple(
        artifacts(index, status)
        for index, status in enumerate((
            ReplayOutcomeStatus.TARGET_HIT,
            ReplayOutcomeStatus.TARGET_HIT,
            ReplayOutcomeStatus.TARGET_HIT,
            ReplayOutcomeStatus.STOP_HIT,
        ))
    )
    result = evaluate_validation_cohort(observations=observations, policy=policy())
    assert result.verdict is ValidationVerdict.QUALIFIED
    assert result.reason is None
    assert (result.metrics.mean_net_r_numerator,
            result.metrics.mean_net_r_denominator) == (11, 4)
    assert (result.metrics.win_rate_numerator,
            result.metrics.win_rate_denominator) == (3, 4)
    assert result.execution_authority is False


def test_small_or_low_diversity_samples_are_insufficient_not_profitable():
    observations = tuple(artifacts(index) for index in range(3))
    result = evaluate_validation_cohort(observations=observations, policy=policy())
    assert result.verdict is ValidationVerdict.INSUFFICIENT_EVIDENCE
    assert result.reason is ValidationReason.SAMPLE_TOO_SMALL
    repeated_thesis = tuple(
        artifacts(index, thesis_key="same") for index in range(4)
    )
    first_id = repeated_thesis[0].proposal.decision.thesis_id
    result = evaluate_validation_cohort(
        observations=repeated_thesis, policy=policy(),
    )
    assert result.reason is ValidationReason.DISTINCT_THESIS_TOO_SMALL
    assert {item.proposal.decision.thesis_id for item in repeated_thesis} == {first_id}


@pytest.mark.parametrize(
    ("status", "maximum_name", "reason"),
    [
        (ReplayOutcomeStatus.CENSORED, "maximum_censored_fraction",
         ValidationReason.CENSORED_RATE_TOO_HIGH),
        (ReplayOutcomeStatus.UNRESOLVED, "maximum_unresolved_fraction",
         ValidationReason.UNRESOLVED_RATE_TOO_HIGH),
        (ReplayOutcomeStatus.OPEN, "maximum_open_fraction",
         ValidationReason.OPEN_RATE_TOO_HIGH),
    ],
)
def test_unscored_coverage_is_reported_and_gated(status, maximum_name, reason):
    observations = tuple(
        artifacts(index, current)
        for index, current in enumerate((
            ReplayOutcomeStatus.TARGET_HIT,
            ReplayOutcomeStatus.TARGET_HIT,
            ReplayOutcomeStatus.STOP_HIT,
            ReplayOutcomeStatus.TARGET_HIT,
            status,
        ))
    )
    limits = dict(
        minimum_total_observations=5,
        minimum_scored_observations=4,
        minimum_distinct_theses=5,
        maximum_censored_fraction=D("1"),
        maximum_unresolved_fraction=D("1"),
        maximum_open_fraction=D("1"),
    )
    limits[maximum_name] = D("0")
    result = evaluate_validation_cohort(observations=observations, policy=policy(**limits))
    assert result.verdict is ValidationVerdict.INSUFFICIENT_EVIDENCE
    assert result.reason is reason
    assert result.metrics.scored == 4


def test_negative_or_subthreshold_expectancy_is_rejected():
    observations = tuple(
        artifacts(index, status)
        for index, status in enumerate((
            ReplayOutcomeStatus.TARGET_HIT,
            ReplayOutcomeStatus.STOP_HIT,
            ReplayOutcomeStatus.STOP_HIT,
            ReplayOutcomeStatus.STOP_HIT,
        ))
    )
    result = evaluate_validation_cohort(observations=observations, policy=policy())
    assert result.verdict is ValidationVerdict.REJECTED
    assert result.reason is ValidationReason.MEAN_NET_R_BELOW_MINIMUM
    assert (result.metrics.mean_net_r_numerator,
            result.metrics.mean_net_r_denominator) == (1, 4)


def test_mixed_segment_or_variant_is_unresolved():
    observations = tuple(artifacts(index) for index in range(3)) + (
        artifacts(3, split_manifest=DEVELOPMENT_MANIFEST),
    )
    result = evaluate_validation_cohort(observations=observations, policy=policy())
    assert result.verdict is ValidationVerdict.UNRESOLVED
    assert result.reason is ValidationReason.MIXED_COHORT
    another_manifest = ValidationSplitManifest(
        COMMIT, "ANOTHER_VARIANT_SPLIT", H("another-variant"),
        BASE - timedelta(days=30), BASE - timedelta(days=20),
        BASE - timedelta(days=10), BASE + timedelta(days=30),
    )
    observations = tuple(artifacts(index) for index in range(3)) + (
        artifacts(3, split_manifest=another_manifest),
    )
    result = evaluate_validation_cohort(observations=observations, policy=policy())
    assert result.reason is ValidationReason.MIXED_COHORT


def test_holdout_label_cannot_be_assigned_manually_after_observing_result():
    holdout = artifacts(0)
    assert holdout.segment is ValidationSegment.HOLDOUT
    with pytest.raises(ValueError, match="preregistered split"):
        replace(holdout, segment=ValidationSegment.DEVELOPMENT)
    outside = replace(
        HOLDOUT_MANIFEST,
        development_start=BASE + timedelta(days=1),
        development_end=BASE + timedelta(days=2),
        tuning_end=BASE + timedelta(days=3),
        holdout_end=BASE + timedelta(days=4),
    )
    with pytest.raises(ValueError, match="outside preregistered"):
        artifacts(0, split_manifest=outside)


def test_impossible_validation_policy_is_rejected_at_construction():
    with pytest.raises(ValueError, match="distinct thesis minimum"):
        policy(minimum_distinct_theses=5)


def test_duplicate_observation_or_attempt_is_unresolved():
    item = artifacts(0)
    result = evaluate_validation_cohort(
        observations=(item, item),
        policy=policy(
            minimum_total_observations=2,
            minimum_scored_observations=2,
            minimum_distinct_theses=1,
        ),
    )
    assert result.verdict is ValidationVerdict.UNRESOLVED
    assert result.reason is ValidationReason.DUPLICATE_OBSERVATION


def test_numeric_results_do_not_depend_on_global_decimal_precision():
    with localcontext() as context:
        context.prec = 8
        low = artifacts(0, nonzero_costs=True)
    with localcontext() as context:
        context.prec = 28
        high = artifacts(0, nonzero_costs=True)
    assert (low.net_pnl_numerator, low.net_pnl_denominator) == (
        high.net_pnl_numerator, high.net_pnl_denominator,
    )
    assert (low.net_r_numerator, low.net_r_denominator) == (
        high.net_r_numerator, high.net_r_denominator,
    )


def test_direct_contract_forgery_of_gross_net_or_risk_values_is_rejected():
    observation = artifacts(0)
    with pytest.raises(ValueError, match="net performance fields"):
        replace(observation, net_r_numerator=999)
    forged_outcome = replace(observation.outcome, gross_r_numerator=999)
    with pytest.raises(ValueError, match="gross R"):
        build_validation_observation(
            proposal=observation.proposal, case=observation.case,
            outcome=forged_outcome, risk=observation.risk,
            cost_model=observation.cost_model,
            split_manifest=HOLDOUT_MANIFEST,
        )
    forged_risk = replace(observation.risk, projected_total_loss=D("19"))
    forged_proposal = replace(
        observation.proposal, risk_plan_id=forged_risk.plan_id,
    )
    forged_case = replace(
        observation.case, risk_plan_id=forged_risk.plan_id,
        proposal_assembly_id=forged_proposal.assembly_id,
    )
    with pytest.raises(ValueError, match="risk plan disagrees"):
        build_validation_observation(
            proposal=forged_proposal, case=forged_case,
            outcome=replace(observation.outcome, case_id=forged_case.case_id),
            risk=forged_risk, cost_model=observation.cost_model,
            split_manifest=HOLDOUT_MANIFEST,
        )


def test_r10_has_no_exchange_order_or_runtime_surface():
    path = Path(__file__).parents[1] / "strategy_v2" / "r10_validation.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = ("binance", "exchange", "order", "main", "state_manager")
    assert not any(any(term in module.lower() for term in forbidden) for module in modules)
    assert "execution_authority: bool = field(default=False, init=False)" in source
