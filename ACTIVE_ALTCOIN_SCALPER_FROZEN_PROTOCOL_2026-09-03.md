# Active Altcoin Scalper — Frozen Research Protocol

Status: **FROZEN BEFORE OUTCOME REPLAY**  
Authority: **RESEARCH ONLY — NO DEMO/LIVE ORDERS**

## Objective

Test the market segment the operator actually trades: fast USD-M altcoin
events. BTC and ETH are context instruments, not ordinary candidates. The
test is deliberately separate from the production SMC funnel.

## Causal event

After a completed 5m bar, a contract is eligible when all of the following
were observable at that close:

- it is not BTC, ETH, BNB, SOL, XRP, DOGE or ADA;
- absolute last-5m return is at least 1.0%;
- absolute trailing-20m return is at least 2.0% and agrees in direction;
- current 5m volume is at least 1.5 times its trailing baseline;
- current 5m quote volume is at least 250,000 USDT;
- candle body is at least 50% of its range and closes in the directional
  outer 35% of the candle.

At most the two strongest causal event scores are retained at a timestamp.
A symbol has a 60-minute signal cooldown. Global BTC/ETH regime is recorded
but is not an absolute veto.

## Frozen entry variants

1. `IMMEDIATE_CONTINUATION`: enter at the next 5m open. Stop distance is one
   event-time ATR and target is 1.5R.
2. `PULLBACK_RECLAIM`: wait up to 30 minutes for a 0.25 ATR pullback. A 1.5 ATR
   adverse excursion before entry hard-invalidates the event. After touch,
   require a directional 5m close in the outer 35% of its range and a reclaim
   to within 0.10 ATR of the event close. Enter at the next 5m open. Stop is
   0.10 ATR beyond the pullback extreme; planned risk must be 0.5–2.0 ATR and
   target is 1.5R.

Both variants time-exit after 60 minutes. Same-bar TP/SL ambiguity is charged
as a loss. Round-trip costs are 12 bps. At most five positions may overlap.

## Evidence split and gate

- TRAIN: August 2025–March 2026.
- VALIDATION: April–May 2026.
- TEST: June–July 2026.

Neither variant is eligible for prospective shadow unless VALIDATION and TEST
each contain at least 100 realized positions, positive expectancy, profit
factor above 1.10, both LONG and SHORT non-negative, and maximum drawdown below
20R. A pass permits shadow observation only; it does not permit execution.

The existing causal dataset is a prior-month top-50 liquidity universe. This
test therefore covers liquid hot altcoins but not every newly listed or thin
contract. Launch-event evidence must remain a separate cohort.
