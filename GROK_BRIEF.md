# Brief for an outside model — attack these conclusions

Paste this whole file. The question at the bottom is the only thing asked
for; everything above it is the evidence needed to answer honestly.

---

## What you are being asked to do

Two agents — an autonomous trading bot with full project history, and an
engineer working alongside it — have closed six strategy families in one
week and concluded that no edge is reachable at this account size.

**They agree with each other on nearly everything. That is the problem.**

Your job is not to generate new strategy ideas. It is to find where they
agreed too easily. Specifically:

1. **Which of these six closures is wrong**, and what specific evidence
   would overturn it?
2. **What would you test that neither of them tested**, given the exact
   constraints listed?
3. **Where is their shared reasoning weakest** — not where they were
   unlucky, where they were sloppy?

If the answer is "the closures look sound", say that. A confirmation is
useful. A fabricated opportunity is not.

---

## The account, exactly

| | |
|---|---|
| Size | $667 paper (Alpaca), reset from $344 on 2026-08-26 |
| Instruments | US equities, US equity options, 73 crypto pairs |
| Options level | 3 — long options and defined-risk spreads permitted |
| Margin | none. Cash account, `shortable: False` |
| Per-trade cap | 10% of equity, about $67 |
| Data | free IEX feed: ~4% of consolidated volume, quotes ~2c off executable |
| Options data | no historical implied volatility available at any price |
| Day-trade limit | 3 same-day round trips per 5 business days (under $25k) |
| Crypto | 72 of 73 pairs fractionable; round trip costs 0.52% |

---

## What has already been measured and closed

Each was killed by data, not by argument. Overturning one means showing
the measurement was wrong, not that the conclusion is unappealing.

**1. The entry signal has no information.** 864 rule variants across
trend, MACD, VWAP, EMA, RSI bands and volume; zero cleared breakeven *in
training*. The live rule measured 32.9% win / −0.01R against random entry
at 36.7% / +0.10R, p=0.0002. Its exact inverse landed within 0.3 points of
it — the signature of zero information.

**2. Gamma exposure (dealer positioning).** A pre-registered 8-year SPY
backtest found GEX/DEX/VEX add little after controlling for VIX and ATM
implied volatility. Not built.

**3. The variance risk premium.** Real and enormous — VIXY decayed −48.6%
a year for a decade, negative in 9 of 11 years. But the only side this
account could take returned +1.7%/yr (SVXY) against SPY's +15.6%, with an
−83% single day and −95% drawdown. Premium confirmed, capture refused.

**4. Covered calls and the wheel.** Distribution-adjusted, ten years:
QYLD +9.8% vs QQQ +21.0%; XYLD +8.6% vs SPY +15.6%; JEPI +11.4% vs SPY
+15.6%. Every version lost to holding the thing it sells calls against, by
4 to 11 points a year. Also unaffordable — the wheel needs 100 shares.

**5. Option-implied skew.** Xing/Zhang/Zhao and successors find OTM-put
minus ATM-call IV predicts returns cross-sectionally. Muravyev, Pearson &
Pollet (JFE 2025) found predictability drops ~two-thirds after adjusting
for stock borrow fees, and similarly if high-fee names are excluded. **Not
closed** — running live now on the long half only, easy-to-borrow names
only, stated prior 0.15.

**6. Social sentiment / following what traders pick.** Published work puts
WSB attention at its highest at −8.5% holding period returns. This project
independently measured news spikes at −0.073R against a price-matched
control's +0.081R — −0.154R, negative in all four years tested.

**7. Crypto RSI-oversold.** The only rule ever to clear its own bar
(+0.187R over control, costs charged). It failed the pre-registered
both-halves clause (2022 −0.01, 2026 −0.09) and was closed permanently on
its own kill criteria.

**8. Diversification.** Adding gold and bonds to the equity sleeve raised
return-per-unit-risk from 0.94 to 1.10 and cost 3 points of CAGR.

---

## Their central claim, which is the thing to attack

> An edge comes from one of three places: **information** others don't
> have, **lower costs** than others, or a **structural position** others
> cannot take. All three are closed at this size. What remains is drift —
> the market rising — which requires no edge and is already collected by a
> buy-and-hold sleeve.

The bot amended this once already: cost is a *multiplier*, not a source,
with one exception — resting limit orders occasionally **earn** spread
rather than pay it (observed twice, filling below the displayed bid).

It also identified the only structural advantage a small account holds:
**capacity-constrained anomalies**, where a large fund cannot participate
without owning an uncomfortable share of a small company. Measured here as
an illiquidity tilt: thinnest names beat thickest by 4.5%/yr. Judged
uncapturable because it is a 249-name basket property, survivorship-heavy,
and the turnover pays the premium away.

**Its stated probability that this project ever finds a repeatable active
edge beating buy-and-hold at this size: 5–10%.**

---

## Constraints on any suggestion you make

A proposal that fails these is not useful here, and saying so is fine.

1. **It must use an input not derived from these OHLCV price bars.** ~20
   families died breaking this rule. Smoothings and ratios of the same
   series cannot contain more information than the series.
2. **It must be testable across separate YEARS, not just separate
   symbols.** One rule replicated across 40 unrelated symbols and still
   worked only in the year it was found. Same regime is not independent
   evidence.
3. **It must beat RANDOM ENTRY, not breakeven.** In a rising market every
   long-biased rule clears breakeven while adding nothing.
4. **It must be affordable at $67 a trade**, and survive a 0.5–12%
   bid-ask spread paid on entry and exit.
5. **It cannot require shorting, margin, or historical implied
   volatility.** None are available.

---

## The question

Given all of the above:

**Which closure did they get wrong, what would you test that they did not,
and where is their shared reasoning weakest?**

Be specific enough to act on. If your answer is that the search space
really is closed at this size, say so plainly — that is worth knowing and
they will not be offended by it.
