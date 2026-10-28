# Strategy V2 R1 — causal market-structure foundation

## Source and authority boundary

- Source base: `c74fbd8e310ceaae9ed27f47837466eca9fece2b` (approved and pushed R0).
- Branch: `codex/strategy-v2-research-r1`.
- Isolated worktree: `E:\Level 3 Term 2\Ai\Tradingbot\worktrees\strategy-v2-research-r1\repo`.
- The worktree was clean at the approved R0 HEAD before R1 additions. R0's worktree was not used for R1 edits.
- R1 is offline/pure research evidence only. It has no Exchange, Telegram, Railway, network, DB, filesystem-persistence, wall-clock, order, proposal, or execution adapter. It emits no trade side or trading decision. No commit or push was made.

## Scope and file inventory

| New file | Purpose |
| --- | --- |
| `strategy_v2/market_data.py` | Immutable final closed OHLCV bar and strict point-in-time series validation. |
| `strategy_v2/structure_models.py` | Explicit research parameters and immutable pivot/point/generic-break evidence contracts; protected-level schema and typed unresolved result. |
| `strategy_v2/structure.py` | Pure pivot detection, confirmed-pivot classification, generic close-cross observations, unresolved protection. |
| `tests/test_strategy_v2_r1_market_data.py` | Finality, UTC, OHLC, ordering, duplicates, future data, and tiny-input adversarial tests. |
| `tests/test_strategy_v2_r1_structure.py` | Pivot/label/break, equality, symmetry, prefix causality, event-envelope, and purity tests. |
| `STRATEGY_V2_R1_IMPLEMENTATION_REPORT.md` | This review artifact. |

Existing R0, V1, production, and protected files modified: **0**. All six files are additive and uncommitted.

## Implemented algorithms and causal contracts

`ClosedBar` requires an explicit final status, bar ID, symbol, timeframe, UTC-aware `open_time < close_time <= available_at`, finite `Decimal` OHLC and nonnegative finite `Decimal` volume. OHLC geometry is checked. A bar series must have unique IDs, one symbol/timeframe, nonoverlapping strictly ordered time windows, and strictly increasing availability timestamps. A future-unavailable bar is rejected, not silently filtered. This intentionally fails closed on out-of-order delayed feeds; an adapter/reorder policy is outside R1.

`StructureSpec` requires **caller-supplied** `pivot_radius_left`, `pivot_radius_right`, `equal_price_tolerance`, `equal_tie_policy`, `close_break_buffer`, and `rule_version`. No production or optimized values are chosen. The numerical values in tests are synthetic fixtures only. Both radii must be positive integers; tolerance and close-break buffer must be nonnegative finite Decimals. The explicit supported tie policy is `EARLIEST` within a candidate's comparison window: a prior near-equal extreme disqualifies a later candidate; a right-side near-equal extreme does not disqualify the earlier candidate. Separate pivot windows may still yield separately confirmed equal structural pivots. This is identity handling, not a liquidity interpretation.

For center bar `i`, pivot high requires its high to exceed each of `left` prior highs beyond tolerance, and to be at least each of `right` following highs within tolerance. Pivot low is symmetric. The pivot becomes confirmed only at the close of bar `i + right`; it becomes available only at that bar's supplied feed `available_at`. Its `source_at` remains the center bar close. A candidate invalidated by a higher/lower right bar emits nothing. The evidence retains source/confirmation bar IDs, every bar ID in its comparison window, a canonical SHA-256 `source_window_hash` of the complete immutable `ClosedBar` records in that window, symbol/timeframe, price, times, `spec_id` (hash of the full supplied spec), and explicit rule version. The window hash participates in `pivot_id`, so an otherwise unchanged ID/time/price cannot conceal altered supporting OHLCV content.

`classify_structure` compares each pivot only with the **previous causally available pivot of the same type**: high→HH/LH/EQUAL_HIGH, low→HL/LL/EQUAL_LOW, first of each type→UNCLASSIFIED. The point retains both pivot IDs where applicable. Outputs sort by availability, source time, type, and stable ID; IDs derive deterministically from immutable inputs and are checked by the record contracts. All classification inputs must be available by the supplied `as_of`.

`detect_structure_breaks` emits **generic close-cross evidence only**. The pivot must have been confirmed before the crossing bar's close and available no later than that bar's supplied decision availability; availability at bar open is not required. It then observes previous close on one side of `pivot.price ± explicit close_break_buffer` and current final close on the other. A wick alone is insufficient. It retains broken pivot ID, previous and break bar IDs, close price, threshold, break direction, final bar time and availability, plus canonical SHA-256 `break_support_hash` of the full pivot and both participating `ClosedBar` records. The support hash participates in `break_id`. This is deliberately **not** named BOS or CHoCH: Revision 1.1's BOS buffer needs tick-floor/ATR semantics, and CHoCH needs a protected swing and prior structure event; those policy inputs are not present in R1. Generic repeated close-cross observations are possible and are not a trade signal or level lifecycle state.

`ProtectedLevel` is a typed evidence schema requiring originating pivot, R0 `ThesisId`, continuation-event identity, and an explicit protection reason. `derive_protected_levels` returns `UNRESOLVED` with no levels because R1 has no thesis/continuation engine. A nearby swing is never silently promoted to a protected level.

## No-repaint and evidence compatibility

The incremental test runs the engine on every prefix of uptrend, downtrend, and range fixtures, plus a delayed-feed fixture in which a pivot becomes available inside the crossing bar. At each prefix time, pivots, points, and generic breaks exactly equal the final run restricted to evidence with `available_at <= prefix_time`. Repeated identical inputs produce identical IDs and records. Up/down price inversion has symmetric pivot and structural-label results. Each emitted record carries parent evidence IDs and R0-compatible `CausalTimes`. Tests construct actual R0 `EventEnvelope` records with pivot and break `InputEvidenceRef` objects whose `data_hash` fields carry the real source-window and break-support hashes. No persistence is created.

## Independent R1 Code Review Corrections

- **Break decision availability:** The first R1 version required `pivot.available_at <= break_bar.open_time`, incorrectly omitting a valid close-cross when a previously confirmed pivot arrived during the break candle. Eligibility now requires `pivot.confirmed_at < break_bar.close_time` and `pivot.available_at <= break_bar.available_at`. The strict confirmation inequality prevents same-bar self-confirmation. Tests cover availability before open, inside the candle, after the decision, same-bar circularity, wick-only rejection, both break directions, and delayed-feed prefix equivalence.
- **Content-bound provenance:** The first `pivot_id` used bar IDs/times/price/spec but not all supporting bar contents. `source_window_hash = canonical_sha256(tuple(window))` now hashes the complete immutable closed bars and participates in the pivot ID. `break_support_hash = canonical_sha256((pivot, previous_bar, break_bar))` participates in the generic break ID. Adversarial tests change supporting OHLC while retaining IDs/times and verify changed hashes/IDs; repeated unchanged inputs remain deterministic. R0 EventEnvelope tests carry these actual hashes in `InputEvidenceRef.data_hash`.
- **Typed thesis identity:** `ProtectedLevel.thesis_id` now requires R0 `ThesisId`, and a raw string is rejected. Promotion remains unresolved; no thesis or BOS policy was added.
- **Test environment:** The first related regression run hit two `WinError 5` file-rename failures because Python's sandbox-package temporary root denied rename. Both tests passed when `TMP` and `TEMP` pointed to the writable project directory; the complete related group and full suite then passed with the same setting. No Shadow source file was changed. The full-suite warning remains the pre-existing pandas `FutureWarning` at `scripts/replay_current_smc_historical.py:211`.

## Final R1 identity-integrity correction

An independent review demonstrated that `dataclasses.replace()` could change a pivot, structure point, or generic break while retaining its former ID. Factories produced deterministic IDs, but the immutable contracts did not verify them on construction or reload.

- Added shared pure `make_pivot_id`, `make_structure_point_id`, and `make_break_id` functions in `strategy_v2/structure_models.py`. Factories and each corresponding contract now call the same canonical R0 SHA-256 function. The `pvt_`/`str_`/`brk_` prefix format is preserved. The canonical payload binds all stored evidence fields (including point price, symbol/timeframe, parent IDs, rule version, and the previously introduced content hashes), so the digests of formerly emitted records change; this is necessary for self-validation, not a change in structure classification.
- `ConfirmedPivot`, `StructurePoint`, and `StructureBreak` now reject mismatched IDs with explicit `ValueError`. The contract binds each stored hash to the record ID; rebuilding the source-bar hash from the original bars remains the source/replay validator's responsibility.
- Added adversarial tests for mutation of pivot price/times/window hash, point label/price/times, and break price/times/support hash, plus unchanged-record acceptance. The two deliberate delayed/circular timing probes regenerate a canonical pivot ID when constructing alternate evidence, rather than using an internally inconsistent `replace()` object.
- All prior pivot, tie, classification, delayed-break, prefix/no-repaint, close-only, unresolved-protection, and no-execution rules remain unchanged. No R0, V1, Production, Shadow source, Railway, or Binance file/state was changed.

## Revision 1.1 mapping and intentional deferrals

| Revision 1.1 | R1 coverage | Deferred |
| --- | --- | --- |
| §6 and `V2-004/V2-005` causal evidence and pivot-right confirmation | Explicit source/confirmed/available times; final bars; prefix no-repaint test. | Live data adapter and latency model selection. |
| §7/§30.1 and `V2-010` HH/HL/LH/LL, equal handling, closed break | Confirmed same-type comparisons and generic closed-bar crossing with explicit supplied buffer. | BOS buffer formula `max(tick_floor, b*ATR_at_close)`, CHoCH/MSS, displacement, expiry and calibrated parameters. |
| §10/§30.1 and `V2-011` protected high/low linked to thesis | Protected-level contract and explicit unresolved result. | Promotion rule, thesis/continuation relation, invalidation selector, stop placement. |
| `V2-053` event identity/provenance | Stable IDs and an R0 EventEnvelope compatibility test. | Durable append store/restart and runtime integration. |

Not implemented: regime classifier, 4×4 policy, thesis creation, setup/entry, order plan, stop, target, sizing, re-entry, replay, Shadow integration, Demo or Mainnet. R1 does not assert `READY_FOR_REPLAY`, `READY_FOR_FORWARD_SHADOW`, or trading readiness.

## Tests and source isolation

| Test run | Result |
| --- | --- |
| R1 targeted (`test_strategy_v2_r1_market_data.py`, `test_strategy_v2_r1_structure.py`) | **49 passed / 0 failed**. |
| R0 targeted (`test_strategy_v2_r0_models.py`, `test_strategy_v2_r0_events.py`) | **109 passed / 0 failed**. |
| Related Shadow/Research regression (`test_strategy_v2_research.py`, `test_strategy_v2_evidence.py`, `test_strategy_v2_shadow_isolation.py`) | **42 passed / 0 failed**. |
| Full `python -m pytest -q` with writable project `TMP`/`TEMP` | **1077 passed / 0 failed / 1 warning**, `PYTEST_EXIT_CODE=0`. The warning is the existing pandas `FutureWarning` at `scripts/replay_current_smc_historical.py:211`. |

The existing R0 contracts and all 11 protected production/source files have **no tracked or staged diff** against `c74fbd8`; Git-cleaned hashes of all 11 protected files match that commit (11/11). Test-generated temporary folders and SQLite files were removed after the runs. R1's AST purity test rejects runtime/network/DB imports and hidden clock/write calls in the new package files. No Railway or Binance call was made.

## Final review gate

```text
R1_SOURCE_BASE=c74fbd8e310ceaae9ed27f47837466eca9fece2b
R1_FILES_ADDED=6
R1_EXISTING_FILES_MODIFIED=0
R1_PARAMETERS_HARDCODED=0 (test fixtures use explicit illustrative values)
CAUSAL_PIVOT_TESTS=PASS
STRUCTURE_CLASSIFICATION_TESTS=PASS
NO_REPAINT_PREFIX_TEST=PASS
MALFORMED_DATA_TESTS=PASS
BREAK_DECISION_AVAILABILITY=PASS
PIVOT_SOURCE_CONTENT_HASH=PASS
BREAK_SOURCE_CONTENT_HASH=PASS
PROTECTED_LEVEL_TYPED_THESIS_ID=PASS
DELAYED_AVAILABILITY_NO_REPAINT=PASS
PIVOT_ID_SELF_VALIDATION=PASS
STRUCTURE_POINT_ID_SELF_VALIDATION=PASS
BREAK_ID_SELF_VALIDATION=PASS
R1_TARGETED_TESTS=49 passed / 0 failed
R0_REGRESSION_TESTS=109 passed / 0 failed
RELATED_REGRESSION_TESTS=42 passed / 0 failed
FULL_SUITE=1077 passed / 0 failed / 1 warning
PRODUCTION_FILES_MODIFIED=0
PROTECTED_HASHES_UNCHANGED=11/11
EXECUTION_AUTHORITY=FALSE
RAILWAY_CHANGES=0
BINANCE_WRITES=0
COMMIT_CREATED=NO
PUSH_PERFORMED=NO
R1_STATUS=PASS
READY_FOR_R1_REVIEW=YES
R1_CORRECTION_STATUS=PASS
READY_FOR_R1_REVIEW_ROUND_2=YES
R1_FINAL_CORRECTION_STATUS=PASS
READY_FOR_FINAL_R1_APPROVAL=YES
```
