# PRE-REGISTRATION: was the entry rule measured through a keyhole?

**Status: DRAFT, not yet committed. Nothing has been run.**
Written 2026-08-10, before any SIP bar was loaded for this purpose.
If a future session finds itself arguing about what a clause meant, the
answer is no.

---

## Why this exists

`ALPACA_DATA_FEED = "iex"` (lockbot_config.py:280). `backtest.load_history`
passes it through, `rule_search.py:607` and `strategy_lab.py:468` both call
that loader, and `shadow_trades.py:501` resolves the shadow book with it.

So the 864-rule search, the 5,856-rule ADX/DI sweep, r0315 across four years,
and the entire shadow book were all measured on IEX — a feed carrying about
**4% of consolidated volume**, with whole bars absent where nothing traded on
that one venue (AAPL: 83 SIP bars against 38 IEX over the same 8-hour window).

`calendar_timing.py:192` and `news_signal.py:195` hardcode `feed="sip"`. The
two studies that cleared condition (a) of THE BAR were measured on the real
tape. The entry-rule work that underpins "the signal has no edge" was not.

SIP **history** is already included in the current free plan. This test has
been available at zero cost since the project began and was never run.

## What this is NOT

Not a search for a profitable rule. Not a new strategy family. Not an
authorisation to trade anything.

It is a **measurement diagnostic** with one variable: the feed. It asks
whether a conclusion already drawn was drawn on adequate data.

It also cannot produce a deployable result, and that limit is structural
rather than a caveat to be argued away: **live SIP is refused on this plan**,
so any rule validated on SIP would be scored on a tape the live scanner can
never see. A pass is an argument for a $99/month subscription. It is not a
trade.

## The hypothesis

> The entry rule's edge over a seeded random-entry control is materially
> larger when measured on SIP bars than on IEX bars.

Mechanism, stated in advance so it cannot be invented afterwards: three of the
five conditions `detect_signal` requires are price-based and largely
feed-insensitive, but `volume_confirmed` and `volume_ratio` are computed from
a 4% sample, and missing bars cause the indicator series to skip intervals
that really happened. If selection on corrupted volume is actively
misleading, the rule could be worse than random on IEX and not on SIP.

## PHASE 0 — the audit, which is NOT part of the hypothesis

Re-resolve the equity shadow book on SIP bars. This runs **first and
unconditionally**, and it is deliberately outside the pass/fail structure
below.

LOCKBOT's ruling, and it is correct: this is not a hypothesis. There is
nothing to pass or fail — it checks whether recorded outcomes were resolved
against bars that actually existed. Gating it behind a one-attempt test
would mean a likely failure permanently forbids correcting the record that
the 17.6% win rate, the 59 EXPIRED rows, the 2026-08-10 horizon-exit result
and the approaching n=200 regime checkpoint all rest on.

It runs before the comparison for the same reason: it revises the inputs to
how anyone reads that comparison. Reading them in the other order risks
interpreting the test against numbers the audit is about to change.

The audit reports, and changes nothing else: outcomes that moved, in which
direction, and how many rows were resolved on bars IEX never carried.

## Design of the hypothesis test

**The comparison is EDGE against EDGE, never raw performance against raw
performance.** SIP returns more bars, so it will detect more setups and
resolve more of them; the two populations are not identical and cannot be
paired setup-by-setup. Within a single feed, however, the rule arm and its
random control see exactly the same bars, so the edge is internally valid on
each feed.

    fixed          symbols, sessions, bracket geometry, the entry rule
                   (market_scanner.detect_signal), the indicator code
                   (indicators.add_indicators)
    varied         the feed. ONE variable. Nothing else.
    adjustment     Adjustment.ALL on both arms, never RAW
    control        seeded random entry, seed 20260811, >= 500 draws,
                   same symbols and sessions as its own feed's rule arm
    PRIMARY        edge_SIP = mean R (rule) - mean R (control), on SIP
    secondary      delta = edge_SIP - edge_IEX, reported as decomposition
                   and NEVER as a pass condition

**Why edge_SIP is the single primary and delta is not.** edge_IEX <= 0 is
already the established result — it is why this test exists — so a delta
clause is nearly implied by the primary and does no independent work. Worse,
making delta a pass condition would let the test partly succeed by
re-measuring a failure already on record. Delta also has no control
distribution of its own and its variance is the sum of two arms. One
primary, stated in advance.

### The holding window must be WALL-CLOCK, not a bar count

This is the confound that would have invalidated the whole test, found by
LOCKBOT on review.

`strategy_lab.HORIZONS["day"]` sets `max_bars_held: 78`, and the comment
beside it reads "78 five-minute bars is 6.5 hours" — which is true only if
the bars are contiguous. On IEX they are not: the same 8-hour AAPL window
returns 83 SIP bars against 38 IEX. So 78 bars is roughly one session on SIP
and roughly two on IEX, and **the IEX arm silently gets twice the wall-clock
time to reach its target.**

That is a second variable inside a test whose entire claim to validity is
"varied: the feed. ONE variable." It is the same defect class as the
bar-count-as-clock bug fixed in `shadow_trades.resolve_pending` on
2026-08-10, in a different file — and here it does not merely censor rows,
it biases the comparison itself.

**The holding window is converted to wall-clock before anything runs.** If
that conversion cannot be made cleanly, the test is void.

## Preconditions — check BEFORE loading data for the test

1. SIP history must return bars for the required symbols and window. If it
   does not, the test is **void**, not re-scoped to whatever SIP returns.
2. **Symbol lists are intersected on the coverage floor in BOTH feeds.**
   `backtest.load_history` silently skips any symbol with fewer than 60 bars
   (backtest.py:778). Run the loader on both feeds and keep only symbols
   clearing the floor on both, so the two arms cannot quietly run on
   different universes.
   This is itself a selection effect and it is accepted deliberately,
   because it cuts **against** the hypothesis: the excluded symbols are
   exactly where IEX starvation is worst, so intersecting biases the
   measured feed effect toward zero. A pass surviving a conservative filter
   is stronger. Report the excluded count and names; do not test them.
3. Bar counts per feed are recorded and reported. If SIP does not return
   materially more bars than IEX, the premise is absent and the test is void.
4. Horizons and brackets are **NOT swept**. The day-horizon configuration is
   used as it stands, converted to wall-clock. A second horizon is a separate
   pre-registration with a multiplicity correction stated up front.

## PASS requires ALL of

    1.  edge_SIP >= +0.10R over its seeded random control
    2.  edge_SIP above the 95th percentile of the DAY-CLUSTERED control
        distribution
    3.  same sign for edge_SIP in BOTH halves of the session range,
        split chronologically
    4.  >= 300 rule trades on the SIP arm AND >= 100 distinct sessions
    5.  day-clustered significance: collapse to daily mean R, bootstrap
        resample DAYS (>= 1,000 draws) for the confidence interval.
        Forty symbols sharing a calendar is nearer forty observations
        per day than a thousand independent trades.

A single clause missed is a failure. A positive delta with any other clause
missed counts for NOTHING.

## On failure

**The "bad data" explanation for the entry-rule family closes permanently.**
No re-cut on a different symbol set, no retry at a different horizon, no
"it was underpowered" rescue, no ex-volume-condition variant. The negative
entry-rule result becomes the strongest version of itself: measured on the
full consolidated tape and still losing to random entry.

That is a genuinely valuable outcome and is the expected one.

## On a pass

A pass authorises exactly one thing: **a costed proposal for the $99/month
Algo Trader Plus plan, with this evidence attached.**

It does **not** authorise trading the rule, because the live feed remains
IEX. It does not reopen any closed strategy family. A second, out-of-sample
pre-registration is required before any capital decision, per the r0315
lesson that replication across symbols within one period is not independent
evidence.

(The shadow-book re-resolution is no longer listed here. It is Phase 0 and
runs regardless of outcome.)

## One attempt

One attempt, ever, on this comparison. The feed question is binary and does
not get a second cut on a different universe.

## Stated priors

    drafter   0.20
    LOCKBOT   0.05 - 0.08

Both are recorded because they disagree and the disagreement is informative.

The drafter's reasoning: **the random control ate the same corrupted bars as
the rule.** Both arms were handicapped identically on IEX and random still
won, so for the feed to be the explanation the corruption must actively
mislead selection rather than merely add noise.

LOCKBOT's correction, which lowers the prior rather than raising it:
corruption in a *selection variable* — `volume_confirmed`, `volume_ratio` —
attenuates a real edge toward the control even without actively misleading
it, so the mechanism needs **less** than the drafter claimed. But a pass
still requires a real >= +0.10R edge to exist in order to be revealed, and
the independent evidence says it does not: r0315 failed across separate
YEARS, and calendar and news both failed on clean SIP data. Neither is
explicable by sampling.

So: the mechanism is more plausible than the drafter thought, and the thing
it would reveal is probably not there.

## Sequencing — this test does not go first

LOCKBOT's ruling, adopted:

    1. Phase 0 shadow-book SIP audit    now, unconditional, cheap
    2. broad-market expansion c6812f3a  owner directive, top of the agenda
    3. O6.3 / O6.4 options measurement  the one path that has made money
    4. this hypothesis test             whenever there is slack

The hypothesis test is not a distraction, but at roughly 6% odds it buys an
argument for a subscription and cannot produce a deployable rule, because
live SIP is refused on this plan. It must not displace the three items above
it.

---

## Note on what this does not touch

Every input on both arms is derived from the same OHLCV bars. Even a pass
would not clear condition (a) of THE BAR — it would mean the existing
measurement was distorted, not that a new source of information exists. The
binding constraint remains a purchasing decision.
