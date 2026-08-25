# PRE-REGISTRATION: option-implied skew as the options entry signal

Committed 2026-08-25, BEFORE any skew-sourced entry exists and before any
outcome has been computed. Written by the engineer, with the five binding
conditions set by LOCKBOT (channel 6f00cce8) before a line was written.

Do not reinterpret this. If a future session finds itself arguing about
what a clause meant, the answer is no.

## Why this one is different from the nineteen dead families

Every dead family broke condition (a) of THE BAR: it was a smoothing or a
ratio of the same OHLCV bars. Option-implied skew is a **different market**
— option prices, set by different participants, with their own supply and
demand. It is not a transformation of the underlying's price history.

It is also the first idea here that arrives with **published evidence and a
published refutation**, which is a better starting position than any of the
nineteen had.

    FOR      Xing, Zhang & Zhao (2010) and successors: the IV of an OTM put
             minus that of an ATM call negatively predicts stock returns
             cross-sectionally, across decades.

    AGAINST  Muravyev, Pearson & Pollet, Journal of Financial Economics
             (2025): predictability "decreases by about two-thirds after
             returns are adjusted for the borrow fees", and unadjusted
             returns fall similarly when high-fee stocks are EXCLUDED.
             Options are largely pricing the stock borrow fee, which was
             already known to predict returns.

## What is actually being tested

Not the published effect. The **residual** after the borrow-fee artifact is
removed, on the long side only, which is the only part this account could
ever trade:

- **Long half only.** Buy calls on the LOWEST-skew names. No shorting is
  possible under $2,000 of equity, so the short half is unreachable. The
  high-skew tail is recorded and never acted on. Reinterpreting it later
  as a put signal is a SECOND rule and is forbidden under this
  registration.
- **Easy-to-borrow required.** `easy_to_borrow` from the Alpaca asset. This
  is the same exclusion the 2025 paper says removes the artifact. An
  unreadable flag is a refusal, not a pass.
- Expected residual: roughly a third of a documented effect. Small.

The comparison that matters is not "does it make money" but "does it beat
the signal it replaces", which measured at 32.9% / −0.01R against random
entry's 36.7% / +0.10R.

## The five binding conditions (LOCKBOT, 2026-08-25)

1. **Stability gate before anything is scored.** These books are wide and
   jittery; a skew computed from two such quotes inherits all of it. A name
   needs `OPTIONS_SKEW_MIN_READINGS` consecutive same-signed readings
   within `OPTIONS_SKEW_MAX_DRIFT` before it is tradable.
2. **Delta-matched, never fixed moneyness.** A 7%-OTM put on a 60%-vol name
   is a different option from one on a 20%-vol name, so fixed moneyness
   ranks by volatility with skew as a rounding error. Measured on the live
   chain 2026-08-25: NOK read −4.8% at fixed 7% OTM and −11.7% delta
   matched. Different numbers, different rank.
3. **`signal_source` on every row before the first entry.** The
   detect_signal cohort and the skew cohort must never be poolable.
4. **The shadow verdict is on UNDERLYING returns, and it gates capital.**
   Option P&L confounds the signal with spread, theta and the exit bands.
   The claim is about direction of the STOCK.
5. **Easy-to-borrow check.** It decides whether a pass is real or the
   borrow-fee artifact wearing a costume.

## The control, which is the part that decides everything

**Price-matched, on the same chains in the same cycles.** Not a
before-and-after against the detect_signal era — that compares two markets
and three other changes shipped the same week.

    PRIMARY    lowest-skew decile vs a seeded random pick from the SAME
               cycle's tradable candidates, seed 20260825, >= 500 draws
    MATCHED    the random control is matched on same-day underlying return,
               because skew moves with the contemporaneous price move and
               an unmatched control would rediscover momentum through a
               more expensive route -- the trap the news test caught
    ARTIFACT   the same test restricted to easy-to-borrow names only,
               reported separately. If the edge lives in the hard-to-borrow
               tail it is the 2025 paper's artifact and it FAILS.

## PASS requires ALL of

    n >= 200 resolved skew-sourced observations AND >= 60 distinct days
    edge >= +0.05R over the price-matched control on UNDERLYING returns
    above the control distribution's 95th percentile
    same sign in both calendar halves of the sample
    day-clustered significance -- every symbol shares a Tuesday, so a
      thousand observations across forty names is closer to sixty
      observations than to a thousand
    the easy-to-borrow-only arm is NO WORSE than the unrestricted arm

## Kill criteria

**ANY failure closes the option-skew family permanently.** No variants, no
re-cut thresholds, no "it works if we drop the stability gate", no
ex-mega-cap rescue. ONE ATTEMPT.

Additionally, and independently:

- If fewer than 200 observations resolve within 120 days, the family closes
  as **UNMEASURABLE at this account's trade rate** rather than as failed.
  That is a real and likely outcome at roughly one entry a day, and saying
  so now prevents it being reported later as "promising but underpowered" —
  the category the pre-registrations exist to eliminate.
- If the stability gate never passes for any name, the family closes as
  unmeasurable on this feed.

## Capital

**SHADOW ONLY until the above resolves.** `OPTIONS_SKEW_LIVE` gates it and
defaults to False. A PASS is a proposal to the owner, not an arming.

The owner may set it live earlier — it is his account and his call — but
that decision does not alter one word of the criteria above, and a live run
started early is still judged by exactly this bar.

## Engineer's stated prior that it passes: 0.15

LOCKBOT's prior on the broader chain-structure family was 0.05–0.10. This
is higher because the live cross-sectional rank sidesteps the three-years-
of-history problem that priced that estimate, and because the borrow-fee
exclusion is a specific, testable mechanism rather than a hope. It is still
below even odds, and it is written down so it cannot be revised upward
after a good week.

## What a PASS would and would not establish

It would establish that option-implied skew carries information about
direction that the price bars do not, on easy-to-borrow names, net of the
known artifact.

It would NOT establish that the options book makes money. Direction is one
input; spread, theta and the exit bands are the others, and all three have
been measured as costs. A signal with genuine information can still lose
money after paying to express it — which is the whole reason the verdict is
on underlying returns rather than on option P&L.
