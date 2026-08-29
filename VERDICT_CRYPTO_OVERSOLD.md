# VERDICT: crypto rsi-oversold-long. CLOSED, and the record was already there.

Scored 2026-08-29 against the pre-registration committed 2026-08-06,
clause by clause, at the owner's instruction to run it to verdict and on
LOCKBOT's precondition that every clause be scored before any wiring.

**Nothing was re-run.** Re-running the historical test is on LOCKBOT's
refused list, and correctly: the sample has been looked at, repeatedly,
by both of us. This is a scoring of what was recorded on 08-06, not a new
measurement.

## The clauses, as committed

    PASS requires ALL of:
      edge >= +0.10R over the control
      above the control distribution's 95th percentile
      net > 0 after costs
      non-negative edge in BOTH calendar halves

    ANY failure kills the entire rsi-oversold-long family on crypto
    PERMANENTLY. No variants, no re-cut thresholds, no ex-BTC/ETH
    rescue. ONE ATTEMPT EVER.

## Scored

| clause | recorded | verdict |
|---|---|---|
| edge >= +0.10R over control | +0.187R | **PASS** |
| net > 0 after costs | +0.233R | **PASS** |
| above the control's 95th percentile | *nothing recorded* | **UNSCORED** |
| non-negative edge in BOTH calendar halves | 2022 −0.01, 2026 −0.09 | **FAIL** |

**One clause failed. The registration says any failure is fatal. The
family is closed.**

## The unscored clause is its own finding

The 95th-percentile clause has no recorded verdict anywhere -- not in
CLAUDE.md, not in the channel, not in the learning log. LOCKBOT flagged
the clause set as unverified at least eight separate times in its nightly
passes and nobody produced it.

It does not change the outcome: a fatal clause already failed. But a
pre-registration whose clauses are only partly scored is a
pre-registration that was not actually run, and that is worth more as a
lesson than the rule was as a candidate.

## What the original author already said

Recorded on 08-06, in the same paragraph as the failure:

> The honest nuance, recorded and NOT acted on: the two failing years are
> the thinnest in the sample (2022 is 9% of trades, 2026 is partial). That
> observation is exactly the rationalisation the pre-registration exists to
> block. **If oversold is ever revisited it needs fresh criteria and a
> held-out year decided in advance, not this sample re-cut.**

So the author scored the fatal clause, recorded the failure, named the
rescue argument in advance, and forbade it. Everything needed to close
this was written on the day it was measured.

## Why it stayed open for three weeks

Nobody connected the recorded failure to the kill clause. The document
kept describing oversold as "the closest thing this project has produced"
-- true, and irrelevant, because closest-and-dead is dead. On 2026-08-28 a
session proposed wiring this registration as the answer to a direct
request for an edge, and found the failure only while checking the
precondition.

**A recorded failure is not a closure until something acts on it.**

## Consequences

- The rsi-oversold-long family on crypto is **permanently closed**. No
  variants: not an ex-BTC/ETH cut, not a re-cut RSI threshold, not a
  swing-only pool, not the mirrored short arm promoted to a rule.
- The **one attempt is spent**. Any future crypto rule needs a new
  pre-registration with fresh criteria and a held-out period decided
  before any data is examined -- the author's own instruction.
- No shadow path will be built for it. Building one would spend months
  confirming a closure.

## What is NOT closed

Crypto as an asset class. That is a separate question and this verdict
says nothing about it. What the measurements do say, recorded elsewhere:

- LOCKBOT trading crypto is refused on arithmetic -- 0.517-0.538% round
  trip against roughly daily turnover, no drift underneath, a picker
  measured worse than random, and no Alpaca crypto options so the live
  path does not transfer.
- Holding crypto is an allocation decision, not an edge question. One BTC
  line captures it; ETH is 0.84 correlated to BTC at 1.35x the volatility.
- LOCKBOT granted one genuine concession: 24/7 markets eliminate the
  gap-through stop-overshoot class that killed six of the first nine
  options trades.
