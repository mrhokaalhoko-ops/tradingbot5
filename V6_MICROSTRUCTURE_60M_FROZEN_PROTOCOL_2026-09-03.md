# V6 Microstructure 60m — Frozen Research Protocol

Status: **FROZEN BEFORE OUTCOMES**
Authority: **RESEARCH ONLY — NO DEMO/LIVE ORDERS**

## Hypothesis

V5 may have failed because a 15-minute label is dominated by spread, noise and
turnover.  V6 asks one different question only: can the same causal aggregate-
trade features rank the next 60-minute directional return when decisions are
made once per hour?

## Fixed specification

- Source: the already validated May--July 2026 V5 one-minute/5-minute feature
  dataset.
- Target: `future_return_60m_pct` from the next-minute entry reference.
- Features and model: exactly V5's allow-list and frozen
  `HistGradientBoostingRegressor` parameters; symbol identity is excluded.
- June prediction: train on May 1--30 UTC; May 31 is embargoed.
- July prediction: train on June 1--29 UTC; June 30 is embargoed.
- Candidate decisions: only timestamps on the UTC hour.
- At most the strongest predicted LONG and strongest predicted SHORT.
- Minimum absolute predicted move: `0.25%`.
- Holding period: 60 minutes; five concurrent positions; no symbol overlap.
- Cost: 12 bps round trip per position.
- No stop/target replay unless this ranking test passes.

## Acceptance gate

Both June (VALIDATION) and July (TEST) must independently have:

- at least 100 accepted positions;
- positive mean after costs;
- profit factor above 1.10;
- positive prediction/realized-return correlation;
- non-negative LONG and SHORT mean expectancy;
- no winner contributing 10% or more of positive PnL.

Any failed item rejects V6.  Thresholds, model and horizon will not be adjusted
after the results are viewed.  A pass grants shadow eligibility only.
