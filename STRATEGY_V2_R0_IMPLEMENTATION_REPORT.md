# Strategy V2 R0 — pure research foundation

## A. Source identity and authority boundary

- Research worktree: `E:\Level 3 Term 2\Ai\Tradingbot\worktrees\strategy-v2-research-r0\repo`.
- Branch: `codex/strategy-v2-research-r0`.
- Source HEAD: `cac0b1bef50ebff40f011b0371dd2354dc7170ef`.
- Parent: `44cf6c6d459689a67f2d7c9a5c75ce6dc58bf789`.
- Tree: `7e6aa5407a58b3f478e7c7ef383f5de669b81e8d`.
- Preflight `git status --porcelain` was empty. This delivery is **uncommitted**; HEAD remains the approved base.
- The Revision 1.1 ZIP supplied only the design text and traceability CSV. It was not used as a code base or copied into Git. Every source file used for implementation came from the canonical Git worktree.
- `execution_authority=false` is an `init=False` frozen field on decision, event, run manifest and gate records. This package has no Exchange, Telegram, persistence or runtime adapter.

## B. R0 scope

Implemented immutable domain contracts only: separate zone/trigger/quote/order-plan/fill price roles; typed IDs; causal timestamps; orthogonal regime axes; invalidation and reward/exit contracts; typed `NO_TRADE`; append-only event envelope/canonical JSON/SHA-256; reproducible run manifest and independent readiness-gate record. Identifier tuples and source-file hash pairs validate their members so callers cannot hide mutable lists inside frozen records. `FillLedger` keeps each partial fill and derives, rather than stores over it, quantity-weighted average price.

Not implemented: scanner ranking, classifier or 4×4 policy decisions, market structure/pivot/BOS/CHoCH, thesis lifecycle, setup/trigger strategy, chase/quote/spread/Net-RR policy, stop or target selector, sizing/portfolio, management/re-entry, replay, storage, Shadow runtime integration, proposal bridge or execution gateway. No trading parameter was chosen or defaulted.

## C. New files and role

| File | Role |
| --- | --- |
| `strategy_v2/__init__.py` | Package boundary; exports no runtime authority. |
| `strategy_v2/enums.py` | Typed sides, regime axes, invalidation scopes, decision/reason and readiness-gate names. |
| `strategy_v2/causality.py` | UTC-aware provenance and explicit availability check. |
| `strategy_v2/models.py` | Immutable identities, price lineage, fill ledger, regime, invalidation, reward and exit contracts. |
| `strategy_v2/decisions.py` | Non-authoritative `StrategyDecision` and typed `no_trade()` constructor. |
| `strategy_v2/events.py` | Frozen event envelope, input provenance references, deterministic serialization/hash and pure idempotent append model. |
| `strategy_v2/manifest.py` | Immutable source/run identity and separate gate-assessment models. |
| `tests/test_strategy_v2_r0_models.py` | Price, timestamp, ID, regime and reward/target contract tests. |
| `tests/test_strategy_v2_r0_events.py` | NO_TRADE, event, manifest, purity and no-I/O tests. |
| `STRATEGY_V2_R0_IMPLEMENTATION_REPORT.md` | This review artifact. |

## D. Existing-file diff

No tracked file was modified or staged: `git diff --name-status` and `git diff --cached --name-status` returned empty; `git diff --check` passed. Test-generated temporary folders inside this initially clean research worktree were verified as non-reparse directories and removed. Final `git status --short` contains only the new R0 package, two tests and this report. The separate `shadow-v2-isolated` worktree and its untracked artifacts were untouched.

## E. Revision 1.1 traceability — coverage is deliberately limited

| Requirement | R0 evidence | R0 completion boundary |
| --- | --- | --- |
| `V2-001`, `V2-061` | Exact approved Git base/parent/tree and clean research preflight; prior Source Identity Gate recorded remote and source hashes. | Source bootstrap satisfied; no runtime promotion implied. |
| `V2-003`, `V2-052` | Frozen `execution_authority=false`; package import AST audit and fake open/socket/SQLite-deny test. | Pure contract package only, not a full future engine call-graph proof. |
| `V2-007`, `V2-059` | `DirectionState` and `VolatilityState` separate; all 16 combinations constructed in tests. | No classifier, conflict algorithm, threshold or trading-policy matrix implemented. |
| `V2-011` | `InvalidationLevel` includes ID/scope/price/relation/source/timeframe/causal times/thesis or setup relation; zone includes source level/event IDs. | No level selector or event-source adapter. |
| `V2-015`, `V2-051` | Distinct immutable zone, trigger, quote, order-plan, fill and `EntryPriceLineage`; partial fills retain IDs and prices. | No live freshness/chase/retest/rounding policy or exchange fill reconciliation. |
| `V2-030`, `V2-060` | Mandatory causal `RewardReference`, separately optional `ExitTarget`; Runner contract can omit target only with explicit exit event types. | No Net-RR calculation, target selection, replay or profitable-policy claim. |
| `V2-053` | Frozen versioned event schema, source/config/rule/variant identity, parent/input IDs, provenance, deterministic payload hash and stable semantic event ID; superseding event model. | No durable append store, restart reconstruction or schema migration. |
| `V2-054` | Typed first-class `NO_TRADE`, stage, failed invariant, evidence IDs and decision time; no order/proposal/fill IDs on decision. | No production proposal suppression integration or full rejection matrix evaluation. |
| `V2-055` | Separate `ReadinessGate` and frozen `GateAssessment`; one gate does not set another. | No approval workflow or gate-transition engine. |
| `V2-056` | `RunManifest` requires base/parent/tree/source commit, remote/branch/clean state/file hashes, config/rule/variant/data/cost identity and external timestamp. | No Git/Railway/filesystem adapter; runtime identity remains explicitly optional/unknown offline. |

The matrix was read directly from `TRADINGBOT_STRATEGY_V2_TRACEABILITY_REVISED.csv` inside `TRADINGBOT_STRATEGY_V2_REVISION_1_1.zip`; code remained sourced only from Git.

## F. Tests and warnings

| Command | Final result |
| --- | --- |
| `python -m pytest tests/test_strategy_v2_r0_models.py tests/test_strategy_v2_r0_events.py -q` | **109 passed**, 0 failed after the final candidate-contract patch (100 passed after the first review; 85 before correction). |
| `python -m pytest tests/test_strategy_v2_research.py tests/test_strategy_v2_evidence.py tests/test_strategy_v2_shadow_isolation.py -q` | **42 passed**, 0 failed when `TEMP`/`TMP` pointed inside the research worktree. |
| `python -m pytest -q` | **1028 passed**, 0 failed, 1 pre-existing pandas `FutureWarning` in `scripts/replay_current_smc_historical.py:211`; `TEMP`/`TMP` pointed inside the research worktree. |

The initial targeted run had two test-only argument-name errors (`quote`/`plan` instead of constructor fields); corrected, then expanded immutability coverage and reran to 85 pass. The first Shadow regression run under the sandbox's default Windows Temp path had two `PermissionError [WinError 5]` failures while tests attempted to rename closed SQLite files. The exact two tests, then all 42, passed using a Temp path under the writable research worktree. No Shadow production/storage code was changed. This is a test-environment limitation, not a claim that the default Temp route is fixed.

## G. Purity and side-effect audit

- `strategy_v2/` imports only standard library and its own relative modules. Static AST test rejects prohibited imports (Exchange, core state, network/Telegram/Binance/Railway/SQLite) and hidden `now`/`utcnow` or write calls.
- A no-I/O test replaces `open`, socket creation and SQLite connection with failures while creating a decision, event and manifest; it passes.
- Timestamps enter constructors explicitly. No `datetime.now()` or wall-clock fallback exists in the package.
- Models are frozen and payload mappings are deep-frozen; canonical serialization and hashes are deterministic for the tested inputs.
- No filesystem persistence, database write, network call or exchange client is present in the R0 package.
- R0 tests were local. No Railway/Binance tools or order endpoints were called as part of this implementation.

## H. Protected source-file SHA-256

The first digest was recorded in Source Identity Gate from the canonical Git blob. The second audit recomputed each digest from the **same approved commit**, checked equality, and verified that the current worktree's Git-cleaned blob equals `HEAD:path`. Raw Windows checkout line endings may differ from Git blob LF bytes; the comparison uses Git's canonical blob representation. All 11 matched.

| Protected file | SHA-256 of Git blob at `cac0b1b...` | Match |
| --- | --- | --- |
| `main.py` | `8DC3A91C7EE1FF2BE17E59F2F1F455A10C9D014B078ADFDF15295E1FD560A1E8` | YES |
| `core/config.py` | `08AF5CBAE6B0ABEA0BCE4A49F25EDF96CBD42F5242E8505322F79D529457CCC1` | YES |
| `core/exchange.py` | `1D5A036D57BB87BB83065DA17A07560A1CBE8EABBD26A8BD177D28122F7BA27F` | YES |
| `utils/state_manager.py` | `DD8BB74D66D3ACD920DD6BCD5908D82E9E10B4CB1470CFD6FFC1F13D3632D8FC` | YES |
| `strategy/risk_manager.py` | `8D7F706AD7A68A20EB421F2898FCC1B10061508B87F422D3FB9E2E677687C229` | YES |
| `strategy/trailing_stop.py` | `2809A9EAC1568B5E52AC7BAB022EF62C1B977C16FE8762E26ED3DE1FEC07162A` | YES |
| `strategy/hybrid_logic.py` | `E2B08C17044CA102BF68E7C5942C18304462088A2A408E564DD9E8BB98B9D141` | YES |
| `engines/ta_engine.py` | `82A8A822CA5E68A37FCB783E9E8F53D35060480222D8F2DB9D7347BAC909C403` | YES |
| `strategy/strategy_v2_evidence.py` | `CE7C19B4BD45EB3A0D020441CBBD88E7454710B288B19BD21D8F8C47C6E474B8` | YES |
| `strategy/strategy_v2_research.py` | `2BD1038CD400BFF767A13A414AB51012ABEE0371DF6BADA67BAA82D9AB3234E1` | YES |
| `strategy/strategy_v2_shadow_storage.py` | `A285C252E98076F323C0B88B855294B6800E0A669A273B7EF4CF85E57BC4F13F` | YES |

## I. Open items / next gates

- Review R0's new-file diff and decide whether contract names/validation are accepted. No Commit/Push is authorized yet.
- R1/R2 must define and freeze unresolved strategy parameters; R0 invents none.
- Actual market data adapters, classifier/structure/thesis/entry/risk/reward/exit-policy algorithms, runner evaluation, append-only persistence/restart, replay and holdout remain future separately approved work.
- Phase C operational plan remains undefined and independent. This R0 result is not `READY_FOR_REPLAY`, `READY_FOR_FORWARD_SHADOW`, or `READY_FOR_DEMO`.

## J. Independent Code Review Corrections

An independent reviewer reproduced the original 85 R0 tests and identified five contract gaps. All corrections remain in the new R0 files; no V1/Production file was edited, no R1 algorithm or adapter was started, and approval to commit is still pending.

| Finding | Correction | Files and focused tests |
| --- | --- | --- |
| Future reward/exit evidence could enter `PROPOSAL_CANDIDATE`. | Candidate construction rejects a reward reference or optional exit target whose `available_at` exceeds `decision_at`; no inferred timestamp or wall clock. | `strategy_v2/decisions.py`; LONG/SHORT × reward/target tests in `tests/test_strategy_v2_r0_events.py`. |
| Entry price roles lacked internal time ordering. | Enforced `zone.available_at <= trigger.available_at <= quote.available_at <= order_plan.planned_at <= decision_at`, and every fill time is at or after plan creation. A fill after decision remains a valid outcome. | `strategy_v2/models.py`; tests for each impossible edge, pre-plan fill, and post-decision fill in `tests/test_strategy_v2_r0_models.py`. |
| An event could be recorded before its decision. | `recorded_at` must be at or after both `event_time` and `decision_at`; later outcome events remain valid. | `strategy_v2/events.py`; decision/outcome chronology tests in `tests/test_strategy_v2_r0_events.py`. |
| A generic Demo gate obscured Revision 1.1 stages; `ready=True` could lack evidence. | Eight independent named gates now include validation/holdout, Demo review, limited Demo and expanded Demo. A ready assessment requires at least one evidence ID. There is no automatic promotion or approval workflow; all records remain non-authoritative. Mainnet is not represented as an executable gate. | `strategy_v2/enums.py`, `strategy_v2/manifest.py`; gate vocabulary, independence, and empty-evidence tests in `tests/test_strategy_v2_r0_events.py`. |
| `ATTEMPT` and `SETUP` both appeared as invalidation tiers. | The level hierarchy is exactly `MICRO / SETUP / THESIS / MACRO`; `attempt_id` remains an identity, not a tier. Revision 1.1 §10 explicitly assigns the failed entry attempt to `SETUP`; §30.3 uses `ATTEMPT` as a `trade_invalidation_scope` label, but does not define a separate fifth level tier. This R0 level enum follows the explicit hierarchy; any later setup-family policy still requires its own review. | `strategy_v2/enums.py`; exact-vocabulary test in `tests/test_strategy_v2_r0_models.py`. |

Additional consistency correction: `RunManifest(worktree_clean=True)` rejects a non-null `worktree_diff_hash`; a dirty manifest still requires the hash. Implemented in `strategy_v2/manifest.py` and tested in `tests/test_strategy_v2_r0_events.py`.

Final candidate-contract patch: `StrategyDecision(PROPOSAL_CANDIDATE)` now requires its side to match both the reward reference and any exit target, and requires at least one `input_evidence_id`. The latter is not imposed on `NO_TRADE`, which may describe missing evidence. Only `strategy_v2/decisions.py` and `tests/test_strategy_v2_r0_events.py` changed in this patch. Nine additional parametrized test cases cover both directions, target and runner policies, valid matching candidates, and empty/nonempty candidate evidence. No duplicate-evidence policy, order bridge, or R1 behavior was added.

Changed R0 files: `strategy_v2/enums.py`, `strategy_v2/models.py`, `strategy_v2/decisions.py`, `strategy_v2/events.py`, `strategy_v2/manifest.py`, `tests/test_strategy_v2_r0_models.py`, `tests/test_strategy_v2_r0_events.py`, and this report. All are new, untracked R0 deliverables. The 11 protected Git-cleaned worktree blobs and their SHA-256 digests were rechecked against the approved base: **11/11 unchanged**. Test-created temporary directories and SQLite artifacts were removed after the runs; Git status returned to only the R0 additions.

## K. Final gate

```text
R0_SOURCE_HEAD=cac0b1bef50ebff40f011b0371dd2354dc7170ef
R0_FILES_ADDED=10 (7 package + 2 tests + 1 report)
R0_EXISTING_PRODUCTION_FILES_MODIFIED=0
R0_TARGETED_TESTS=109 passed / 0 failed
R0_RELATED_REGRESSION_TESTS=42 passed / 0 failed
R0_FULL_SUITE=1028 passed / 0 failed / 1 warning
PURE_CORE_IMPORT_AUDIT=PASS
PRICE_LINEAGE_TESTS=PASS
CAUSALITY_TESTS=PASS
NO_TRADE_TESTS=PASS
EVENT_CONTRACT_TESTS=PASS
MANIFEST_TESTS=PASS
PROTECTED_FILE_HASHES_UNCHANGED=YES
EXECUTION_AUTHORITY=FALSE
RAILWAY_CHANGES=0
BINANCE_WRITES=0
COMMIT_CREATED=NO
PUSH_PERFORMED=NO
R0_CORRECTION_STATUS=PASS
R0_FINAL_CORRECTION_STATUS=PASS
R0_STATUS=CORRECTED_PENDING_INDEPENDENT_REVIEW
READY_FOR_R0_REVIEW_ROUND_2=YES
READY_FOR_FINAL_R0_APPROVAL=YES
R1_STARTED=NO
```
