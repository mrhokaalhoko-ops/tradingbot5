# Strategy V2 Shadow-only collection plan

Base: `44cf6c6d459689a67f2d7c9a5c75ce6dc58bf789`.

This candidate adds observational evidence only. It does not authorize, veto,
resize, open, protect, trail, or close a trade. The single runtime flag is
`STRATEGY_V2_SHADOW_EVIDENCE_ENABLED`, default `false`.

## Isolation contract

- V1 remains authoritative for BUY/SELL, entry, SL, TP, sizing, leverage,
  re-entry, trailing, close, and lifecycle state.
- Collector return values are ignored by V1.
- Every collector hook catches failures and records
  `SHADOW_COLLECTION_ERROR_V1_UNCHANGED`.
- The evidence modules import no exchange client and expose no order method.
- Runtime research helpers are pure functions in
  `strategy/strategy_v2_research.py`.

## Storage

Evidence is stored below `/app/data/strategy_v2_shadow/` (or `DATA_DIR` on
other hosts) in `strategy_v2_shadow.sqlite3`. It never enters
`bot_state.json` and has no `.bak` copy.

Global defaults:

| Record class | Hard count |
|---|---:|
| decisions | 500 |
| management | 20,000 |
| outcomes | 500 |
| theses | 1,000 |
| resets | 2,000 |

The aggregate primary + WAL + shared-memory footprint is capped at 128 MiB.
Old management/reset records are pruned first if the byte ceiling is reached.
This leaves more than 70% of a 500 MiB volume unused even before applying the
normal count bounds. SQLite is part of Python's standard library; no dependency
was added.

Each management event performs one small SQLite transaction. It does not
serialize, fsync, or copy the authoritative V1 state. WAL uses
`synchronous=NORMAL` and automatic checkpoints. There is no backup copy, so
backup overhead is zero; transient WAL/SHM bytes are included in the hard cap.

## Causality

Decision validity is the union of market-level, TA-context, thesis, and reset
integrity. Missing or future source timestamps make the dependent research
component invalid. Management evidence requires
`structure_confirmed_at <= observed_at`; invalid evidence can never produce a
`MEANINGFUL_MOVE` label.

## Hooks

- `main.py`: decision snapshot after the authoritative user-visible proposal.
- `main.py`: closed outcome and post-exit checkpoints.
- `main.py`: timestamp-only 15m source metadata and reset evidence translation.
- `strategy/trailing_stop.py`: copied-input management counterfactual before
  V1's activation return; it cannot change `trade`, SL, or activation state.

All hooks are behind the same default-OFF flag.
