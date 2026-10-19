# Trading V2 research kernel

This package is intentionally isolated from `main.py`, CCXT order methods and
Telegram execution callbacks. It cannot place a trade.

Its purpose is to replace guess-driven live patches with a causal workflow:

1. Download public Binance USD-M candles without credentials.
2. Build snapshots from candles that were closed at the historical decision time.
3. Produce immutable entry, invalidation, target and expiry plans.
4. Replay on lower-timeframe OHLC without treating ambiguous same-bar outcomes as wins.
5. Charge entry/exit fees and slippage.
6. Report chronological train, validation and test results.

Run an experiment with:

```powershell
python scripts/run_v2_research.py `
  --symbols BTCUSDT ETHUSDT SOLUSDT `
  --start 2026-01-01T00:00:00Z `
  --end 2026-04-01T00:00:00Z
```

Downloaded data and result files live under `research_data/`, which is ignored
by Git. A strategy must show positive, stable out-of-sample expectancy across
multiple market regimes before any separate shadow integration is considered.

The first two candidate strategies are deliberately **not approved for live or
Demo execution**. The initial 30-day experiment rejected them as a portfolio:
their test expectancy was not stable across BTC, ETH, SOL, BNB and DOGE. This
negative result is evidence to improve or discard the hypotheses, not a reason
to loosen filters in production.

A later pre-registered "recent 24h strength then EMA pullback" hypothesis was
also discarded rather than integrated.  On nine hot contracts its fixed-2R
variant lost `-5.79R` in the calendar TEST segment; on twelve liquid contracts
the unchanged rule lost `-23.07R` in TEST.  Its structural-target variant lost
`-16.06R` in the same liquid-contract TEST.  A positive middle segment did not
survive the next period, so neither variant is present in the strategy module.

Timestamped human calls are audited only as case studies.  Telegram message
time and displayed entry often differed by hundreds of basis points, so a call
cannot be credited as a filled trade unless the post-message 1m path actually
touches its reference entry.  These samples may generate hypotheses but are
not valid training labels or proof of win rate.

The cross-sectional event study likewise did **not** justify a new strategy.
Outer-20% 24h continuation produced 203 independent-ish four-hour events on
twelve liquid contracts, but calendar TEST averaged `-19.14 bps` after costs.
A high-volume subset looked positive there, then failed the unchanged
cross-symbol check on nine hot contracts at `-211.9 bps` in TEST.  The tool is
kept for discovery; the failed signal and feature are not execution rules.

Extending that study to 2,679 events from June--August initially made absolute
24h strength above 10% look stable.  The rule was frozen and checked on the
older March--May period; it then averaged `-19.4 bps` in TRAIN and `-96.6 bps`
in VALIDATION, with negative medians in every segment.  This temporal failure
also blocks promotion and shows why a single high-momentum regime is not a
valid basis for live execution.

## Frozen regime hypothesis — validation protocol

The March--August discovery data left exactly one simple continuation
hypothesis for a fresh check.  Before loading the untouched September 2025 to
February 2026 window, the rule is frozen as follows:

- hourly evaluation on closed Binance USD-M bars;
- contract is in the top 20% of the available cross-section by 24h return;
- 24h return is at least `+10%`;
- side is `BUY` only;
- the causal BTC/ETH regime is exactly `MILD_RISK_ON`;
- entry is the next 5m open, exit is four hours later;
- twelve basis points of round-trip costs and a four-hour per-symbol episode
  cooldown are charged.

This is a directional event hypothesis, not an executable trade system.  It
survives the untouched window only if it has at least 30 events, positive
full-sample net mean, at least two of three equal calendar thirds have positive
net means, and no single event contributes 25% or more of total positive PnL.
The median and positive rate are reported even though a positively skewed
momentum distribution need not win more than half the time.  Failure of any
criterion rejects the hypothesis; thresholds are not changed after seeing the
new results.

The directional hypothesis passed those minimal criteria on the untouched
window, but only by `+2.52 bps` after assumed costs and with one negative
calendar third.  It is therefore not promoted.  Three execution geometries
were compared on discovery data only.  `1.5 ATR` stop plus a four-hour time
exit was the only one with positive expectancy in both discovery windows
(`+0.165R` and `+0.281R`); both 2R-target variants failed at least one window.
The time-exit variant is frozen for a single execution-level check on the
untouched window.  It passes only with positive expectancy, profit factor above
one, at least 30 realized positions, maximum drawdown below `20R`, and no
single winner contributing 25% or more of gross positive R.  Even a pass means
shadow eligibility only, never automatic Demo/live integration.

The frozen execution check then failed.  On 136 untouched events the selected
variant produced `-0.151R` expectancy, `0.827` profit factor, `-20.58R` total
and `33.70R` maximum drawdown; 98 positions hit the stop.  The hypothesis is
rejected for both shadow and execution use.  No post-OOS ATR, target or regime
threshold is tuned, because doing so would overfit the validation window.

## Causal 12-month USD-M dataset

The larger research dataset is built directly from Binance Vision's official
monthly USD-M archives, without private API keys.  For every target month from
August 2025 through July 2026, the universe is the top 50 USDT contracts by the
**previous complete month's** 1h quote volume with at least 75% candle coverage.
This avoids selecting historical contracts using volume observed in their
future.  The union contains 205 symbols; 600 month/symbol Parquet partitions
contain 5,256,000 5m observations, of which 5,246,160 pass the causal warm-up
and forward-label checks.  The derived 15m and 1h features are attached only
after their source candle has closed.

The reproducible artifacts under `research_data/causal_futures_dataset/` are:

- `universe.json`: prior-month liquidity selection and rank;
- `klines_manifest.json`: SHA-256-verified archive download status;
- `features_manifest.json`: feature partition row counts;
- `validation.json`: timestamp, future-label and coverage integrity results;
- `universe_contract_audit.json`: timestamped ExchangeInfo market-type tags;
- `exact_rule_replay.json`: fee/slippage-aware 5m execution replay.

Binance USD-M now contains TradFi contracts as well as crypto.  The current
metadata snapshot identifies equity and commodity contracts explicitly.  That
snapshot is used only to stratify research; it never changes which instruments
were historically available, because doing so would introduce survivorship
bias.

Two deliberately simple regime rules were replayed exactly with next-5m-open
entry, 1 ATR stop, 2 ATR target, four-hour timeout, costs, cooldown and a
five-position portfolio cap.  `STRONG_RISK_ON_BUY_10` was positive only in the
aggregate (`+6.10R`) but lost in both validation (`-4.52R`) and test (`-2.36R`),
with only 6/12 profitable months.  `STRONG_RISK_OFF_SELL_10` lost `-8.76R` and
had only 3/12 profitable months.  Neither rule is eligible for shadow, Demo or
live use.  Their failure is retained to prevent later threshold recycling.

## Frozen aligned-breakout hypothesis

Before inspecting this setup's outcomes, one structural continuation hypothesis
is frozen.  It is evaluated only on contracts tagged `COIN` by the timestamped
ExchangeInfo audit:

- signal is evaluated after a closed 5m candle;
- close breaks the preceding 24 completed 5m-bar high for BUY, or low for SELL;
- 5m, last-closed 15m and last-closed 1h EMA20/EMA50 alignment all agree;
- 5m quote-volume ratio is at least `1.5`, the candle body is at least half an
  ATR, and the close is in the directional outer 30% of the candle;
- price is no more than 2 ATR beyond its 5m EMA20;
- BUY is forbidden in `STRONG_RISK_OFF` and SELL in `STRONG_RISK_ON`; both are
  forbidden during `HIGH_VOLATILITY_EVENT`;
- entry is the next 5m open with fees and slippage;
- structural stop is 0.1 ATR beyond the breakout candle/prior boundary, accepted
  only when planned risk is between 0.5 and 2 ATR;
- target is fixed at 2R, timeout is four hours, per-symbol cooldown is four
  hours, and portfolio capacity is five concurrent positions.

There is no parameter search.  The combined setup is rejected unless it has at
least 200 realized trades, positive expectancy and profit factor above 1.10 in
both calendar VALIDATION and TEST, at least 7/12 profitable months, and TEST
drawdown below 20R.  Directional legs are reported but are not rescued by
post-result threshold changes.  A pass permits only a later shadow experiment.

The frozen hypothesis failed decisively and is rejected.  After cooldown,
structural-risk validation and portfolio capacity, 7,193 positions were
realized.  Expectancy was `-0.480R`, profit factor `0.501`, total was
`-3454.16R`, and **0/12** months were profitable.  TRAIN, VALIDATION and TEST
were all negative; BUY and SELL legs both failed.  This is direct evidence that
entering the next open after a 5m breakout candle is adverse selection even
when volume, multi-timeframe trend and global regime agree.  Those filters must
not be loosened to rescue it.  Any subsequent breakout research must test a
separate retest/reclaim entry state and treat this full 12-month window as
development evidence rather than a fresh holdout.

## Frozen breakout retest/reclaim hypothesis

The immediate-entry failure motivates one architectural timing test, frozen
before its outcomes are inspected.  The signal definition remains exactly the
failed aligned breakout above; only the entry state changes:

- after the breakout, wait at most 90 minutes for price to intersect a zone of
  0.25 ATR around the broken 5m boundary;
- a wick through the boundary is not invalidation;
- a 5m **close** at least 0.5 ATR beyond the boundary against the thesis is hard
  invalidation;
- after a touch, require a directional 5m candle to close back beyond the
  boundary with its close in the directional outer 40% of its range;
- enter at the next 5m open; place the stop 0.1 ATR beyond both the reclaim wick
  and the hard-invalidation boundary; require 0.5--2 ATR planned risk;
- target, costs, four-hour holding timeout, cooldown and portfolio capacity are
  unchanged from the failed immediate-entry experiment.

This is one fixed state machine, not a tolerance search.  It uses the 12-month
window as development evidence because the parent breakout was already viewed
there.  It must still meet the same numerical stability criteria before a
separate untouched August 2026 check is even allowed.  Thresholds will not be
changed after viewing its result.

The retest/reclaim state also failed and is rejected.  It confirmed 4,782 of
8,475 independent-ish breakout episodes and realized 3,903 portfolio positions,
so the failure was not caused by a lack of touches.  Net expectancy was
`-0.465R`, gross expectancy before modeled costs was already `-0.235R`, profit
factor was `0.515`, and 0/12 months were profitable.  Modeled cost drag averaged
`0.230R`, but removing all costs would not create an edge.  Both directions and
all calendar splits lost.  Therefore neither a wider tolerance nor another
timeout adjustment is justified for this parent setup.

## Frozen causal ranking baseline

Before fitting any statistical model, a non-executable ranking baseline is
frozen to answer one question: do the causal features contain enough directional
information to rank contracts better than chance?  Observations are taken only
once every four hours, after the 5m candle closes, on contracts tagged `COIN`.
Inputs are past returns, EMA alignment/distance, ATR, relative volume, candle
structure, BTC/ETH regime and the prior-month liquidity rank; no forward column
is an input.  The label is the four-hour return from the next 5m open.

A fixed shallow `HistGradientBoostingRegressor` is trained on August 2025 through
March 2026.  April--May are VALIDATION and June--July are TEST.  At each decision
timestamp the policy selects at most the three contracts with the largest
absolute predicted move, requires at least 0.20% predicted magnitude, follows
the predicted sign and charges 12 bps round trip.  There is no model/threshold
search.  It passes only if VALIDATION and TEST each have at least 300 selections,
positive net mean and profit factor above 1.10, with at least three of the four
individual holdout months profitable.  A pass demonstrates ranking information
only; it still requires a separately frozen stop/target execution replay and an
untouched August 2026 check before shadow eligibility.

The OHLCV-only baseline failed.  TRAIN correlation was `0.448`, but it collapsed
to `-0.002` in VALIDATION and `0.031` in TEST.  VALIDATION lost `-2.43 bps` per
selection with profit factor `0.989`; TEST gained only `+11.46 bps` with profit
factor `1.042`, and April/June were negative.  This is classic temporal
overfitting, not a production signal.

One incremental derivatives experiment is frozen before its results are viewed.
It keeps the same model, sample times, policy, costs, splits and pass criteria,
and adds only causally joined Binance metrics: one/four-hour OI changes, OI
24h z-score, account/top-trader/taker long-short ratios, and current/24h funding
rates.  Current snapshots are joined backward with a ten-minute staleness cap;
funding is joined backward with a three-day cap.  If this unchanged comparison
does not pass both VALIDATION and TEST, OI/Funding are not granted execution
authority merely because they sound sophisticated.

The incremental derivatives comparison also failed its frozen criteria.  Data
coverage was 99.4--99.8%, so the result is not explained by missing OI.  Adding
the features improved TEST to `+37.03 bps` mean and `1.140` profit factor, and
three of four holdout months were positive, but VALIDATION remained negative at
`-4.56 bps` with `0.977` profit factor; May lost while April gained.  Predictive
correlation was only `0.014` in VALIDATION and `0.069` in TEST.  OI/Funding may
contain weak incremental information, but this experiment does not justify an
execution gate, hard filter or live API dependency.  No threshold is retuned
after this result.

## Launch-event dataset and 72h study

The separate launch track uses the first available intramonth 1h Vision candle
after July 2025 as a listing observation, then downloads the launch month and
following month at both 5m and 1m.  It currently contains 148 contracts and 592
SHA-256 verified archives.  The primary study uses the first 30 closed 1m bars
as its only classification information; labels begin at the next 1m open and
cover 1h/4h/12h/24h/72h.
The archive alignment delay has median 30 minutes and p95 45 minutes, with no
missing 5m bars in the measured 72h windows.

The result is deliberately not a launch strategy.  A frozen, conspicuous gate
(absolute first-30m return at least 5%, directional close location and taker-buy
ratio confirmation) produced only one LONG and zero SHORT events.  That one
event lost 3.77% at 1h and 13.33% at 4h before recovering later; this is not a
sample from which to infer an edge.  The raw buckets are similarly unstable:
first-30m expansion is not a sufficient reason to buy or short a new contract.
The 1m/5m one-hour return measurements have zero median absolute difference,
but p95 differs by 0.269 percentage points and one thin launch differs by 14.93
points.  The 1m study therefore supersedes 5m for any later launch replay.
The event artifacts are `research_data/launch_event_dataset/launch_universe.json`,
`klines_1m_manifest.json`, `launch_event_study_1m.json` and
`launch_event_study_1m.md`.  Current ExchangeInfo is used for contract-type tags,
so delisted contracts remain a documented survivorship limitation.

One launch reversal hypothesis is frozen before its exact 1m order replay.  It
is a symmetric test, not a parameter search: after a first-30m move of at least
10% with the close in the directional outer 35% of the range, wait up to 60
minutes for a directional 1m close through the first-range midpoint and trade
the reversal.  A wick beyond the extreme is soft; a close 0.25 initial ranges
beyond it is hard invalidation.  Entry is the next 1m open, stop is 0.10 range
beyond the structural extreme, target is 2R, and the position times out after
four hours.  Normal replay fees and slippage apply, and structural risk must be
between 0.5% and 15% of entry.

Because the launch cohort has already been viewed descriptively, even a pass is
only a candidate for an unchanged future cohort.  Development requires at
least ten realized positions in both calendar VALIDATION and TEST, positive
expectancy and profit factor above 1.10 in both, and at least four profitable
calendar months.  Failure rejects the hypothesis without threshold retuning.

The exact replay rejected it.  Of 148 launches, 72 never formed the required
extreme, 14 were hard-invalidated, 44 never reclaimed the midpoint, and 17 had
unsafe structural risk.  Only one plan remained; it filled and hit its stop for
`-1.011R` net, despite reaching `0.796R` MFE.  The sample is radically below the
predeclared evidence floor and the only realized outcome lost, so launch
reversal is not eligible for shadow, Demo or live execution.  Its thresholds
are not loosened after this result.

## Current SMC parity and final hand-written counterfactuals

The active SMC architecture was replayed on the causal 12-month dataset before
any further live relaxation.  It produced 4,888 independent structural theses.
The production-risk midpoint variant admitted 882 portfolio plans but realized
only 204 positions.  Aggregate expectancy was effectively zero (`+0.0001R`),
VALIDATION was `-0.180R` with `0.686` profit factor, and TEST contained only 32
trades.  Moving the entry to the proximal order-block edge did increase fills,
but both frozen distances lost (`-0.206R` at `0.1 ATR` and `-0.220R` at
`0.2 ATR`).  Entry tolerance is therefore not a safe repair.

The actual closed-5m production confirmation was then replayed over those 882
plans.  Three hundred touched the zone, 119 passed confirmation, and only 29
still had at least `1.5R` at the trigger.  Those 29 averaged `+0.030R`, but the
chronological VALIDATION result was `-0.842R` and the one TEST trade lost
`-1.128R`.  More importantly, all 118 confirmation passes with valid geometry
averaged `-0.092R`; TRAIN, VALIDATION and TEST were all negative.  Confirmation
and RR are not merely suppressing a proven upstream edge.

Three last, pre-registered hand-written alternatives were tested and rejected:

- `FROZEN_SWEEP_REJECTION_V1`: 6,270 realized positions, `-0.436R`
  expectancy, `0.582` profit factor, and 0/12 profitable months.  A wick sweep
  plus same-bar rejection is not sufficient.
- `FROZEN_HOURLY_TREND_TRANSITION_V1`: 1,594 positions, `-0.048R`
  expectancy and `0.931` profit factor.  Costs mattered, but TEST was already
  negative before costs and finished at `-0.200R`; using a slower timeframe did
  not create a durable edge.
- `FROZEN_FAILED_BREAKOUT_REVERSAL_V1`: a three-bar close-back-inside state
  confirmed 1,609 range-regime breakouts, but geometry and net-RR checks left
  175 realized positions.  They averaged `-0.150R`, with `0.831` profit factor,
  only 5/12 profitable months and `-0.326R` in TEST.  The positive middle split
  is not rescued after the later failure.

These failures close the current hand-written rule search.  None is eligible
for Demo or live authority.  The already isolated prospective shadow monitor
may continue collecting immutable evidence, but production settings must not be
loosened merely to increase trade count.

## V3 rolling linear ranking

A final architecture test replaced frozen hand-written thresholds with a
simple monthly walk-forward `Ridge(alpha=10)` model.  It uses a trailing 180-day
window, a seven-day embargo before each prediction month, hourly closed-candle
observations, same-timestamp cross-sectional ranks, at most one long and one
short per hour, a four-hour hold, five-position capacity and twelve basis
points of round-trip costs.  No model coefficient, threshold or training window
is tuned after observing the results.

The OHLCV/context model was positive in the February--March adaptation
diagnostic, then failed both holdouts.  VALIDATION selected 1,117 positions at
`-14.26 bps` mean and `0.924` profit factor; TEST selected 1,130 at `-7.87 bps`
and `0.972` profit factor.  April, June and July were negative.  The result is
not eligible for an execution-level stop/target replay.

One pre-declared incremental comparison added the causal OI, long/short and
funding inputs without changing the model or selection policy.  It made
VALIDATION worse (`-31.68 bps`, `0.830` profit factor).  TEST became positive
in aggregate (`+18.84 bps`) but profit factor was only `1.072`, June lost and
July produced the gain.  This temporal instability rejects the derivative
variant too.  OI/funding remain observational features, not hard gates or
execution authority.

## V4 cross-sectional dollar-neutral relative value

V4 deliberately changes the target rather than retuning V3.  It removes the
same-timestamp cross-sectional future median from each training label and adds
only causal BTC-relative returns, rolling 168-hour BTC beta, market breadth and
dispersion.  Each accepted observation is an atomic highest-score LONG versus
lowest-score SHORT pair held for four hours.  The predicted spread must be at
least `0.35%`; capacity is four legs and each leg pays 12 basis points of
round-trip costs.  The monthly 180-day training window, seven-day embargo,
model and holdout months remain frozen.

The model produced enough activity but failed stability.  VALIDATION contained
532 pairs and averaged `+16.20` pair basis points, yet profit factor was only
`1.060` and May lost.  TEST contained 505 pairs and averaged `-25.71` pair basis
points with `0.945` profit factor.  June lost `-131.75` basis points per pair
while July gained `+79.91`.  Prediction correlation was negative in all four
April--July prequential evaluation months.
This rules out lack of candidates as the explanation.  The relative-value
target and features do not have durable ranking power, so the experiment is
rejected before stop/target execution replay and is not integrated into the
bot.

An independent audit reproduced that rejection but correctly found weaknesses
in the original evidence construction: forward-label availability affected
same-time membership before ranks, current ExchangeInfo tags filtered the
historical subset, and beta used 168 observations rather than 168 clock hours.
The same frozen Ridge model, features, threshold, costs, capacity and holding
period were rerun after repairing only those issues.  The primary cohort uses
all 205 contracts selected causally by prior-month liquidity; the original 188
current-COIN subset is retained only as a sensitivity result.  All 205 symbols
were found in the snapshot, so there was no observed UNKNOWN/delisted survivor
exclusion, although the original type classification was still post-period.

The evidence-clean full-universe result was weaker: 543 VALIDATION pairs at
`+7.80 bps` and `1.029` profit factor, followed by 520 TEST pairs at `-29.33`
bps and `0.934` profit factor.  TEST gross expectancy was already `-5.33 bps`
before costs.  The current-COIN sensitivity TEST averaged `-40.63 bps` with
`0.917` profit factor and `-16.63 bps` gross.  Code, manifests, package versions
and the 600-file feature tree are SHA-256 fingerprinted in
`v4_evidence_clean_rerun.json`.  This corrected rerun strengthens rather than
weakens the decision not to promote V4.
