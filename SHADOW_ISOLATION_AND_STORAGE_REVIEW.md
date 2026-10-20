# Shadow Isolation and Storage Review

Status: **LOCAL VERIFIED CANDIDATE — NOT COMMITTED / NOT PUSHED / NOT DEPLOYED**

Base: `44cf6c6d459689a67f2d7c9a5c75ce6dc58bf789`

Verification date: `2026-10-21` (Asia/Aden)

## Scope and safety boundary

The mixed Strategy V2 worktree was not cleaned in place. A detached worktree
was created from the exact base and only observation code, causal validation,
isolated persistence, tests, scripts, and documentation were reintroduced.
No Railway variable, service, deployment, Binance endpoint, account state,
order, or position was touched.

The candidate adds no authoritative Target V2, Stop V2, Thesis Re-entry V2, or
Trade Management V2 branch. Pure counterfactual calculations live in
`strategy/strategy_v2_research.py`; their return values are stored as evidence
and are never read to control V1.

## Runtime-source isolation

Tracked runtime files changed relative to the base:

- `core/config.py` — default-OFF observation flag and storage configuration.
- `main.py` — additive, best-effort decision/outcome/checkpoint hooks.
- `strategy/trailing_stop.py` — copied-input management observation hook.

New Shadow-only modules:

- `strategy/strategy_v2_research.py`
- `strategy/strategy_v2_evidence.py`
- `strategy/strategy_v2_shadow_storage.py`

Explicit must-match-base checks cover:

- `strategy/risk_manager.py`
- `core/exchange.py`
- `utils/state_manager.py`
- `strategy/user_visible_trade_proposal.py`

For every must-match file:

- `git diff <base> -- <file>` is empty;
- `git diff --numstat <base> -- <file>` is empty;
- the working canonical Git blob equals the base Git blob;
- LF-normalized SHA-256 values match exactly;
- only raw checkout bytes differ because the Windows checkout contains CRLF
  while the base Git blob contains LF.

`MUST_MATCH_BASE_CANONICALLY_IDENTICAL = YES`

## Trailing isolation

V1's exact control flow remains:

```python
if best_r < activation_r:
    return None
```

The Shadow hook runs separately on a deep copy before that return. It does not
write `trade['trailing_active']`, `current_sl`, activation state, replacement
state, or any lifecycle field. Its failure is caught as
`SHADOW_COLLECTION_ERROR_V1_UNCHANGED`.

## SQLite lifecycle root cause and fix

`SQLITE_LOCK_ROOT_CAUSE = BOTH`

Two conditions caused `WinError 32`:

1. The restart/outcome test retained a direct connection after a
   `with sqlite3.Connection` block.
2. `ShadowEvidenceStore` used that block as if it closed the connection.
   Python's SQLite connection context manager commits or rolls back but does
   not close the file handle.

The store now provides deterministic lifecycle behavior:

- a transaction context that always closes its connection;
- idempotent `close()`;
- rollback of active transactions during forced close;
- context-manager support;
- tracked direct connections closed by `store.close()`;
- safe creation of a new store against the same database after close;
- no dependency on `__del__` or garbage collection.

Verified on Windows:

- the database can be renamed immediately after close;
- a tracked direct connection raises `sqlite3.ProgrammingError` after close;
- close twice is safe;
- restart/reopen succeeds;
- SQLite database/WAL/SHM paths can be moved and the TemporaryDirectory can be
  deleted immediately after close.

`SQLITE_CONNECTION_LIFECYCLE_SAFE = YES`

`WINDOWS_FILE_RELEASE_VERIFIED = YES`

## Storage design and stress result

The store is independent from `bot_state.json` and uses stdlib SQLite at:

`/app/data/strategy_v2_shadow/strategy_v2_shadow.sqlite3`

Default global limits:

- decisions: `500`
- management: `20,000`
- outcomes: `500`
- theses: `1,000`
- resets: `2,000`
- hard database + WAL + SHM ceiling: `128 MiB`

Exact stress command:

```text
python scripts/stress_strategy_v2_shadow_storage.py --output-dir .shadow_stress --result-json SHADOW_STORAGE_STRESS.json
```

Result:

- input decisions: `500`
- input management events: `50,000`
- input outcomes: `500`
- write transactions: `51,500`
- retained management events: `20,000`
- records pruned: `30,000`
- final/peak observed footprint: `31,793,152 bytes` (~`30.32 MiB`)
- configured ceiling: `134,217,728 bytes` (`128 MiB`)
- quick check: `ok`
- restart quick check: `ok`
- recovery time: `0.1070504s`
- elapsed stress time: `3842.8604s`

This is below both the internal 128 MiB ceiling and the intended 500 MB volume
budget.

## Causal integrity

Decision-level validity aggregates:

- `market_level_integrity`
- `ta_context_integrity`
- `thesis_integrity`
- `reset_integrity`

Any used evidence with a missing timestamp or a timestamp later than the
decision is invalid for research. Reset evidence must also be later than the
prior close. Management evidence requires a valid `observed_at`, a present
`structure_confirmed_at`, and
`structure_confirmed_at <= observed_at`; otherwise the result is
`INVALID_CAUSAL_MANAGEMENT_EVIDENCE` and cannot be `MEANINGFUL_MOVE`.

## Diff-audit root cause and correction

`DIFF_AUDIT_ROOT_CAUSE = MULTIPLE`

Proven components:

1. `PARSER_BUG`: `_git(...).strip()` removed the leading porcelain-status
   space from the first record, so slicing at offset 3 changed
   `core/config.py` to `ore/config.py`.
2. `EOL_NORMALIZATION`: raw Windows CRLF checkout bytes were compared directly
   with canonical LF Git blobs. All four must-match files are canonically
   identical and differ only by EOL representation.
3. `GENERATED_ARTIFACT_CLASSIFICATION`: `.shadow_stress/`,
   `SHADOW_STORAGE_STRESS.json`, `SHADOW_DIFF_AUDIT.json`, and
   `strategy_v2_shadow/` were verification/runtime products, not source.

The audit now:

- parses `git status --porcelain=v1 -z` without trimming status bytes;
- compares raw SHA-256 separately from LF-normalized SHA-256;
- uses Git canonical blob identity and `git diff --quiet` for the deployment-
  relevant source verdict;
- labels generated files `GENERATED_ARTIFACT`;
- excludes those generated artifacts from `commit_eligible_changed_files`.

Audit rerun result:

- exit code: `0`
- unexpected files: `[]`
- must-match canonical identity: `true`
- all four raw differences: `EOL_NORMALIZATION_ONLY`
- generated artifacts excluded from proposed commit: `true`

`MUST_MATCH_BASE_CANONICALLY_IDENTICAL = YES`

`GENERATED_ARTIFACTS_EXCLUDED_FROM_COMMIT = YES`

`AUDIT_TOOL_FIXED_WITHOUT_RUNTIME_SOURCE_CHANGE = YES`

## Tests and verification

SQLite failure reproduction test after fix:

```text
1 passed in 2.49s
```

Targeted Shadow suite:

```text
42 passed in 11.71s
```

Full suite, including audit-tool regression tests:

```text
919 passed, 1 warning in 32.10s
```

The warning is a pandas `FutureWarning` in
`scripts/replay_current_smc_historical.py:211` concerning future concat dtype
behavior. It is unrelated to this candidate and is not a test failure.

Git verification:

- `git diff --check`: exit `0`
- tracked diff: only `core/config.py`, `main.py`, and
  `strategy/trailing_stop.py`
- tracked diff size: `3 files changed, 264 insertions(+)`
- generated artifacts remain local evidence and are explicitly excluded from
  the proposed commit.

## Final gates

| Gate | Verdict |
|---|---|
| `SOURCE_DIFF_SHADOW_ONLY` | YES |
| `V1_BEHAVIOR_SHADOW_OFF_UNCHANGED` | YES |
| `V1_BEHAVIOR_SHADOW_ON_UNCHANGED` | YES |
| `TRAILING_STATE_ISOLATED` | YES |
| `SHADOW_STORAGE_OUTSIDE_V1_STATE` | YES |
| `GLOBAL_RETENTION_BOUNDED` | YES |
| `500MB_VOLUME_SAFE` | YES |
| `V2_EXCHANGE_WRITE_SURFACE` | NONE |
| `READY_TO_COMMIT` | YES |
| `READY_FOR_PHASE_A_REVIEW` | YES |

Readiness means the local candidate is ready for a separate commit/review
decision. It does **not** authorize a commit, push, deployment, feature
enablement, Railway change, or Binance write.

No commit, push, deployment, Railway change, feature enablement, or Binance
write occurred during this verification.
