# PRE-REGISTRATION: the 733 unscored shadow rows

**Committed BEFORE the scoring run. Nothing below may be edited after the
first score exists.**

The protection is structural, not a promise: this file is committed to git
on its own, and the scoring tool prints the commit hash it was frozen at.
A prediction changed after the fact would change the hash, and the run's
own output would no longer match the file it cites.

## The holdout

733 rows in `shadow_trades.csv` carrying no outcome. Nobody has seen a
per-row result, including me. I have looked at file-level totals only —
611 STOP, 521 EXPIRED, 112 TARGET, 733 UNRESOLVED — while verifying an
independent analysis. LOCKBOT ruled that uncontaminating: those rows have
no outcomes yet, so there was nothing in them to see.

Composition as reported by that analysis: ~450 early non-crypto, ~252 late
non-crypto, ~31 correlation-grouped.

## The three predictions

Correlation-grouped names (BITCOIN, IG_CREDIT, and every group in
`exposure_groups.py`) are EXCLUDED from all three. Every number is also
reported with them included, but the predictions are judged on the
excluded set.

**P1.** Setups entered before 3:00pm ET average **at least 0.15R better**
than setups entered from 3:00pm ET onward.

**P2.** Setups whose stop was pinned to the 1.5% floor average **worse
than −0.30R**, and their target rate stays **under 5%**.

**P3.** The book overall stays **negative**, before and after the time
split.

## LOCKBOT's binding amendment

This is a **time-ordered split**, so it is confounded with whatever regime
prevailed during the resolution window. A result that holds only because
one period dominated is not a finding.

**Every prediction must hold with the SAME SIGN across sub-periods.** A
split that passes in aggregate but flips sign between halves FAILS.

## Rules, fixed now

1. **Score all 733 at once.** No partial reads. The maturity gate in
   `shadow_trades.cohort_maturity` enforces this — a slice containing any
   unresolved or too-young row prints NOT READABLE rather than a number.
2. **Report every number twice**, with and without the correlation groups.
   The 08-29 retraction happened because a slice was reported once.
3. **If P1 fails, the time-of-day idea is dropped and not revisited.** No
   re-cut hour, no "it works if you exclude X".
4. **Nothing becomes a trading change** until this test passes AND one
   further forward month agrees. A pass here is evidence, not permission.
5. The 30-trade settings freeze is unaffected by this test either way.

## What a pass would and would not establish

It would establish that entry time carries information about outcome in
this book, on non-correlated names, surviving a sub-period sign check.

It would NOT establish an edge. Prediction 3 explicitly expects the book
to stay negative in both halves — so the best available outcome is
"one part of the book loses less than the other", which is a cost finding,
not a profit one.

## Stated prior

Engineer's prior that all three pass, including the sign amendment:
**0.25**. P2 and P3 are near-certain from what is already visible; P1 is
the whole uncertainty, and it is the one carrying a real mechanism —
a 3:50pm entry gets almost no price path before an overnight gap that can
jump the stop.
