# The most popular retail options strategy loses to holding the index

Tested 2026-08-27 after the owner asked for strategies found online to be
implemented. The wheel, covered calls and the poor man's covered call are
the most recommended retail options strategies in print. The claim in the
sources -- "every single covered call ETF dramatically underperforming the
S&P 500 over any multi-year period" -- is checkable, so it was checked
rather than accepted.

Total return, split and DISTRIBUTION adjusted. That adjustment is not a
detail: these funds pay most of their return out as income, and a
price-only series would understate them badly and make the test unfair in
the direction of the answer below.

    sym    from        CAGR   maxDD   what it does
    QYLD   2016-10-19   9.8%   -25%   sells calls on the Nasdaq 100
    XYLD   2016-10-19   8.6%   -33%   sells calls on the S&P 500
    JEPI   2020-05-21  11.4%   -14%   active covered-call income
    SPY    2016-10-19  15.6%   -34%   just holds the S&P 500
    QQQ    2016-10-19  21.0%   -35%   just holds the Nasdaq 100

    selling calls on the Nasdaq cost 11.3% a year
    selling calls on the S&P      cost  7.0% a year
    the actively managed version  cost  4.3% a year

Ten years, professionally run, billions under management, and every
version lost to simply holding the thing it sells calls against.

## The honest nuance

JEPI's drawdown is -14% against SPY's -34%. Covered calls DO reduce
volatility -- that is real and is why the funds exist. It is a risk
transfer, not free income. But the owner's stated goal is to make money,
and on total return the family loses by 4 to 11 points a year.

## And it is unaffordable here anyway

The wheel requires 100 shares of the underlying -- $3,000+ on most names
against a $702 account. The poor man's covered call requires a long-dated
in-the-money call, which is the same deep-ITM tier priced at $120-291 on
2026-08-24 and refused by the debit ceiling.

## Verdict

Not implemented, and closed on evidence rather than on preference. This is
the third strategy family closed by measurement this week, after gamma
exposure (adds nothing after VIX/IV controls) and the volatility risk
premium (real, uncollectible here).

It also lands where the last two did: the sleeve already running in this
account beat the sophisticated alternative, without needing an edge.
