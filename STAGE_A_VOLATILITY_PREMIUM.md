# Stage A: the volatility premium is real, and the retail vehicle destroys it

Run 2026-08-26 on the approved ungated audit. 2,616 daily bars per symbol,
2016-04-01 to 2026-08-26, split-adjusted.

Alpaca carries no VIX index, so the premium was measured through the
tradable instrument instead — which is arguably the better test, since the
question was never "does the premium exist in theory" but "can this
account collect it".

## The premium exists, and it is enormous

    VIXY    -48.6% CAGR    -100% total    max drawdown -100%

VIXY holds short-term VIX futures. Its decay IS the premium changing
hands: hedgers pay it for protection, and somebody on the other side
collects. Roughly half its value, every year, for a decade.

Negative in 9 of the 11 calendar years. The two exceptions are the two
crises:

    2016 -62.4%   2017 -72.8%   2018 +67.2%   2019 -67.8%
    2020 +10.7%   2021 -72.5%   2022 -25.0%   2023 -72.7%
    2024 -27.4%   2025 -43.0%   2026 -30.5%

LOCKBOT's stated prior that the premium exists was 0.9. Confirmed.

## And the buyable side captured almost none of it

    SVXY    +1.7% CAGR    worst single day -83.0%    max drawdown -95%
    SPY    +15.2% CAGR    worst single day -10.8%    max drawdown -34%

SVXY is the inverse — the side that collects rather than pays, and the
only side this account could take, since it cannot short.

Ten years of a 48.6%/yr premium bleeding out of the instrument next to it,
and SVXY returned 1.7% a year. **Buy-and-hold on SPY beat it by nine
times, with a third of the drawdown and an eighth of the worst day.**

Daily rebalancing, volatility drag and the February 2018 event ate the
entire premium and then some. The -83% day is not a tail scenario; it
happened, and it is in this sample.

## The verdict

The premium is real. It is large. It is not collectible here.

This matches LOCKBOT's 2026-08-26 ruling exactly -- premium exists ~0.9,
capturable at this account 2-5% -- and it closes the question with data
rather than with reasoning.

**It also makes the sharpest case in this project's archive for the
sleeve.** The most documented risk premium in finance, in its most direct
tradable form, lost to simply holding the index by a factor of nine.

Do not revisit via inverse-volatility ETFs. If the family is ever
reopened it is through defined-risk short premium at an account size that
can absorb the tail, and it needs its own pre-registration.
