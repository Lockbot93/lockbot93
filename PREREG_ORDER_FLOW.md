# PRE-REGISTRATION: daily signed-volume imbalance. One attempt, ever.

Written by LOCKBOT on 2026-08-20 (channel items 5017f9b1 → 81dafcb9).
Committed to git **before any data was pulled and before any number was
computed**. That ordering is the whole point of the document.

Every threshold below is LOCKBOT's. I proposed none of them, deliberately,
having asked it to set them all rather than choose any myself.

**Do not reinterpret this.** If a future session finds itself arguing about
what a clause meant, the answer is no.

---

## Why this idea was allowed to start at all

THE BAR requires an input not derived from the OHLCV bars, and testability
across separate years. Eighteen dead families broke the first. News and
calendar cleared both and failed on their merits.

Probing the Alpaca surface on 2026-08-20 — rather than reasoning about it,
which is the 2026-08-06 lesson — found `StockTradesRequest` and
`StockQuotesRequest` serving **tick-level data on the full SIP tape**:

    AAPL, 5 minutes, 2026-08-17
      sip   5,000 trades      price, size, exchange, condition codes
      iex     211 trades      (4% of the tape, as measured 2026-08-05)

    quotes carry bid, bid_size, ask, ask_size
    2022, 2023, 2024 and 2025 all return data

Trade size and quote depth are not transformations of price bars. CLAUDE.md
names order flow explicitly as an input that would pass condition (a).

## The trap, and the construction that escapes it

Recent SIP is refused exactly as it is for bars — 5 minutes ago is blocked,
20 minutes ago is not. So live flow is IEX only, at 4% of the tape, and
**every intraday construction is dead on arrival**.

LOCKBOT ruled the end-of-day construction genuinely escapes it: by the T+1
open, day T's tape is more than 17 hours old, so the backtest and the live
path read *identical data*. Two conditions attached:

- signal computation touches **nothing** from T+1, pre-market included
- the live pipeline uses its own post-close SIP fetch, and **never**
  substitutes the scanner's IEX feed in either direction — a train/serve
  feed mismatch is the Phase-0-audit flattery class

## THE FEATURE — one, pinned, no variants

    classifier   QUOTE RULE ONLY
                   at or above ask  -> buy
                   at or below bid  -> sell
                   midpoint and unquoted trades DROPPED
                   no tick-rule fallback
    aggregate    one daily imbalance per symbol
    imbalance    (buy - sell) / classified volume
    normalise    z-score against a trailing 60-day per-symbol baseline
    event        z >= +2.0

**No other threshold may be tried.** No condition codes, no quote depth, no
size buckets, no intraday timing, no conjunctions.

## THE TEST

    entry        T+1 open
    exit         T+1 close          wall clock, one session
                                    (bar counts caused defect 78103175)
    R unit       day return / trailing 20-day ATR%
    costs        0.05% per side
    pool         the lab pool, ~78 symbols. NOT the 150-name universe.

## THE CONTROLS — both, and the second is not optional

    price-matched   MANDATORY. A buy-imbalance day and an up day are
                    nearly the same object, and the seeded control alone
                    cannot separate them. Nearest-neighbour same-symbol
                    non-event day, same-day return within +/-25%.
                    PASS REQUIRES edge >= +0.10R NET over matched control,
                    pooled.

    seeded random   seed 20260820, >= 500 draws. The rule must beat the
                    control mean by +0.10R AND exceed its 95th percentile.

## FLOORS — all of them

    >= 500 events
    >= 3 calendar years
    no single year > 40% of events
    >= 100 distinct event days
    day-clustered significance
    costs charged at 0.05%/side

## THE HOLDOUT

**2025 is excluded entirely.** It is computed ONCE, and only if 2022–2024
has already passed in full. LOCKBOT's words: *"2025 does not get computed
until 2022–2024 has passed in full."*

Same-sign edge is required in **every in-sample year and in the holdout**.

## THE DISCRIMINATOR

A mirrored `z <= -2.0` short arm, run alongside. A long-side pass **without**
the short arm behaving is regime luck: shadow only, no capital.

## KILL RULE

Any failure kills the daily signed-volume-imbalance family on equities
**permanently**. No re-cuts. No rescue. No variants.

    LOCKBOT's stated prior that it passes: 0.10

## What is already known to be against it

Order flow is among the most heavily published anomaly families in finance,
so McLean & Pontiff decay applies at full strength — returns 26% lower out
of sample and 58% lower after publication. And this data sits on a retail
subscription at no extra cost, which is itself evidence that whatever was in
it has been arbitraged.

News cleared both conditions of THE BAR too, and lost to its price-matched
control by 0.154R.

LOCKBOT's closing line, recorded because it binds the next reader as much as
this one:

> If either clause turns out inconvenient mid-analysis, that is the
> pre-registration working, not a reason to amend it.
>
> One attempt, then the family is dead either way.

---

# OUTCOME: CLOSED ON INFEASIBILITY, 2026-08-20

**Not tested. Not refuted. Unmeasurable at this account's data-access cost.**
That is a different verdict from *failed* and must be relayed in exactly
those terms.

## What the cost probes measured

    FULL TAPE, one session, 2024-03-01, feed=sip
      AAPL   871,027 trades                          37.0s
      XLF     85,437 trades / 475,228 quotes         25.2s
      -> 78 symbols x 756 sessions = 407+ hours, and AAPL is 10x XLF

The quote rule needs the prevailing NBBO at every trade, so quotes must be
pulled too — and they outnumber trades roughly six to one. That is what
made the full-tape scope impossible.

## The one amendment, and why it was admissible

LOCKBOT amended the aggregation to **six fixed 10-minute windows** — 09:30,
10:00, 11:00, 13:00, 14:30, 15:50 ET — identical for every symbol and every
session, chosen before any data was pulled.

Its reasoning is the part worth keeping:

> the amendment can only hurt the hypothesis. Sampling noise inflates the
> baseline sd and attenuates z, so marginal days fall out rather than in. A
> pre-data change that biases toward failure is a correction, not a rescue.

It committed at the same moment that a **second** overrun meant kill:

> One amendment is a correction; two is negotiation.

## The second probe

    WINDOWS ONLY, same session
      AAPL   171,829 trades / 189,788 quotes         14.4s
      XLF     22,439 trades /  61,825 quotes          5.0s
      mean 9.7s per symbol-day
      -> 78 x 756 = 158 HOURS, against a 60-130h estimate

## Why a background run was refused

158 hours is 6.6 days and is *not* beyond the hardware. It was refused
anyway, and the reason is not compute:

> a 6.6-day continuous tick pull would contend for API budget with
> options_manager, the sole stop loss on open option positions, and no
> backtest justifies that exposure

No variants. No off-hours runs. No rate-ceilinged pulls.

## The door that stays open

Revisit **only** if data access changes materially — bulk historical tick
files rather than per-request API pulls. That would be a **new
registration**, not a resurrection of this one.

## What this cost, and what it bought

A few probe requests. Nothing else. The hypothesis was never contaminated
by a peek at a result, because the cost was measured before the analysis
rather than during it.
