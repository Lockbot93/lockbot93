# PRE-REGISTRATION: clustered Form 4 insider buying

**Written BEFORE the coverage gate runs and before any event is priced.**
Committed alone so the freeze is verifiable from git history.

## Why this one is allowed to exist

Condition (a) of THE BAR requires an input not derived from these OHLCV
bars. Roughly twenty families died breaking it. An SEC Form 4 is a
**filing**, not a price: it records that a named officer or director
bought stock with their own money, at a disclosed size, on a disclosed
date. EDGAR publishes it free, in structured XML, with decades of history.

That is the first input since news and the calendar to clear (a), and both
of those were tested and failed. This is not a promising idea. It is a
qualifying one.

## The rule

- **Open-market purchases only.** Transaction code **P**. Sales, awards,
  option exercises and gifts are excluded.
- **10b5-1 plan trades excluded.** A pre-scheduled purchase carries no
  information about today.
- **Notional under $25,000 excluded.** Token buys are noise and are
  sometimes optics.
- **Clustering required: at least 3 DISTINCT insiders within 5 calendar
  days** on the same issuer. Solitary buys are the weaker documented
  effect; clusters are the stronger one.
- **Entry at the next open after the ACCEPTANCE TIMESTAMP of the third
  filing** — not the transaction date. A Form 4 may be filed days after
  the trade, and entering on the transaction date buys information nobody
  could have had. This is the lookahead that quietly flatters most
  published insider studies.
- **Liquidity floor:** price ≥ $5, ADV ≥ $1M.
- **Long only.** No shorting is available under $2,000 of equity.
- **Holds: 21 and 60 trading days**, reported separately.

## The coverage gate, which runs FIRST

**Nothing is priced until this passes.** It is the lesson from the
illiquidity closure, and it applies to **coverage, not returns**.

    PASS   fewer than 10% of 2016-2026 EDGAR event symbols are
           unpriceable on Alpaca
    FAIL   10% or more -- the family closes as UNMEASURABLE HERE,
           exactly as illiquidity did

**A live symbol missing from Alpaca fails the probe.** That is a data-gap
in the present and it cannot be worked around.

**A historical delist must STAY IN THE RETURN SAMPLE.** It is booked at
its last print, or at a Shumway proxy of −30% (NYSE/AMEX) / −55% (Nasdaq)
where no final price exists. **Dropping delisted names is precisely how a
4.5% illiquidity premium got manufactured**, and repeating it here would
reproduce the same error in a new family. Written down before anyone runs
it, so it cannot be decided afterwards by whoever is holding the result.

## The control

**Matched non-event names**, seeded, drawn from the same investable
universe on the same dates, matched on price bucket and ADV bucket. Never
breakeven, never SPY. In a rising market every long-biased rule clears
breakeven while contributing nothing — the benchmark trap that nearly
cost this project the r0315 result.

## Splits and kill criteria

Reported in three windows: **2016–19, 2020–22, 2023–26.**

| outcome | consequence |
|---|---|
| 2023–26 net of 2× quoted spread ≤ 0 | **capital kill** at any n — no money is committed |
| the above AND n ≥ 100 in that window | **permanent family kill** |
| the above with n < 100 | closed as **unmeasurable-recent**, not refuted |

The split is LOCKBOT's amendment. A recent-window failure always stops
capital, but killing a family permanently on a thin window is how a
sample-size problem gets recorded as a fact — the mistake the crypto
registration nearly made in the other direction.

## Infrastructure boundary

Collection is an **offline, frozen-snapshot** pass, following the 2026-08-06
news pattern. The trading path reaches Alpaca, Telegram and Pushover and
nothing else, and that stays true: a bad web response must never be able
to affect a trade.

## Prior

**0.10** that this clears its own bar, set by LOCKBOT and not revised
upward here.

If it misses, LOCKBOT's probability that this project ever finds a
repeatable active edge at this account size drops to **3–5%**. It has
declined to sign a closure declaration on that basis, and the reason is
worth keeping: *the last closure declaration aged badly.*
