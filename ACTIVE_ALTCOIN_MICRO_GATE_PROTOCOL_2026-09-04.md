# Active Altcoin Microstructure Gate — Frozen Protocol

Status: **RESEARCH ONLY**. This protocol does not authorize live or testnet orders.

## Purpose

Test whether information already present inside the completed five-minute event
can distinguish tradable pullback/reclaim setups from late, exhausted impulses.
No future bar is used to construct a feature.

## Cohort

- Strategy variant: `PULLBACK_RECLAIM_1M` only.
- Parent event and execution rules remain frozen by
  `ACTIVE_ALTCOIN_SCALPER_FROZEN_PROTOCOL_2026-09-03.md`.
- Only filled records with a finite `net_r` participate.
- Train: 2025-08 through 2026-03.
- Validation: 2026-04 through 2026-05.
- Confirmation only: 2026-06 through 2026-07. This block was already viewed for
  the ungated parent strategy, so it is not described as pristine OOS data.

## Candidate features

Only the following causal event-close features may be tested:

1. directional five-minute return;
2. fraction of one-minute bodies agreeing with the setup;
3. mean and final directional close location;
4. volume concentration and final-minute volume ratio;
5. event range divided by pre-event ATR;
6. signed return dispersion;
7. mean and final direction-adjusted taker imbalance;
8. flow/price agreement;
9. directional absorption proxy.

## Frozen search family

For each feature independently, thresholds are the Train-only 33rd, 50th and
67th percentiles. The only allowed screens are:

- value <= Train P33;
- value <= Train P50;
- value >= Train P50;
- value >= Train P67.

No feature pairs, hand-tuned constants or symbol exclusions may be introduced
after seeing outcomes.

## Selection gate

A screen is eligible only when all conditions hold:

- at least 120 Train and 40 Validation positions;
- positive expectancy and profit factor above 1.05 in both Train and Validation;
- positive expectancy in at least six of eight Train months;
- positive expectancy in both Validation months, with at least 10 positions in
  each month.

If multiple screens qualify, select the screen maximizing the smaller of Train
and Validation expectancy. Ties prefer the larger Validation sample, then the
feature name in lexical order. If none qualifies, the result is **NO GATE**.

## Confirmation and authority

The selected screen, if any, is then reported on June-July without further
changes. Passing this experiment permits a prospective shadow monitor only.
It never permits automatic order execution.
