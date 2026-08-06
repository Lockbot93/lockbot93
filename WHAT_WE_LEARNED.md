# What we learned building LOCKBOT

Written 2026-08-06, for you rather than for the machine.

`CLAUDE.md` is written for an AI to load every session — dense, repetitive,
front-loaded with warnings. This is the version in English, in order, so you
can re-read it in a month when the details have faded.

---

## The short version

LOCKBOT works. The strategy doesn't.

Over five days we tested nine different ways of deciding what to trade. Every
single one lost to doing nothing at all. Not "underperformed slightly" — lost
to picking stocks at random, which we measured directly rather than assumed.

Along the way we found six real bugs, four of which would have made the
measurements lie to us. Finding those is the reason you can trust the answer.

The account went from about $250 to $253. That's flat, and it's paper money.

---

## What LOCKBOT actually is

An autonomous trading system: it scans stocks every five minutes, decides
whether to trade, sizes positions against a risk budget, places bracket
orders, manages option positions with a software stop loss, and reports to
your phone. About 80 modules, 37 with their own test suites, running
unattended on a schedule with a watchdog outside it.

That part is genuinely well built. It catches its own bugs, refuses fixes it
can't verify, and has told you uncomfortable truths repeatedly.

---

## The question we were really asking

**Can a small account, using free market data, decide when to trade well
enough to make money?**

Everything below is an attempt to answer that honestly.

---

## The nine things we tested

Each was measured against a **control** — usually "enter at random" or "hold
everything." That comparison is the whole point. A strategy can look
profitable simply because the market went up; the only way to know if it
*added* anything is to check what happens without it.

**1. The entry rule itself.**
We simulated 2,497 trades. The rule won 32.9% of the time. Entering at random
on the same bars won 36.7%. Buying on every single bar won 36.0%.
*The rule performed worse than not choosing at all.*

**2. Eight hundred and sixty-four variations of it.**
Every combination of trend, momentum, price position, RSI band and volume
condition. Zero cleared the break-even bar in training. Not "the best one
failed later" — none led even on the data they were fitted to.

**3. Holding longer.**
Day trades, swing trades, month-long positions. Results improved steadily the
longer positions were held — but that's the cost of trading, not skill. Even
at the best holding period the rule still lost to random entry.

**4. Whether options are mispriced.**
The theory: options are usually sold for more than the movement they deliver.
Measured across 30 of your stocks: median 0.97. They're priced about right.
No free money there.

**5. Momentum — buy what's been going up.**
One of the most documented effects in finance. Tested on sector funds: lost to
holding everything by 2.3 points a year.

**6. Momentum again, properly.**
You were right that the first test was too small. Redone across 740 stocks,
the way the research actually describes it. It lost by **13** points a year.
More breadth made it worse, not better.

**7. Low-volatility stocks.**
Another documented survivor. Lost on both return and risk-adjusted return. It
did deliver half the drawdown, which is real — but that's less pain, not more
money.

**8. Buying illiquid stocks that big funds can't touch.**
The most promising idea we had, because it has a *reason* to still work:
institutions physically can't take positions that small. And the effect showed
up — thin stocks beat thick ones by 4.5% a year. But holding everything still
beat both, and trading costs would eat 4.5% several times over.

**9. Paying for better data.**
You asked to see the whole market. We tested it for free first, using
historical full-market data. **The strategy got worse** — 33.8% down to 31.0%.
The extra data generated more signals, and the extra signals lost money. A
$99/month subscription would have bought a worse strategy.

---

## The one that nearly worked

Late on, a search found this rule:

> Buy when a stock is beaten down (below its averages, RSI under 35) but
> momentum has just turned up, on heavy volume.

Buying capitulation with a confirmation trigger. Coherent, well-known, and the
exact opposite of what LOCKBOT currently does.

It beat the controls by 6.4 points on data the search had never seen. Then it
survived the check most likely to kill it — re-run on 40 huge companies that
never go bankrupt, it scored *identically*.

Then it died. Tested across separate years:

| Year | Edge over random |
|---|---|
| 2022 | +0.1% |
| 2023 | −0.3% |
| 2024 | −0.3% |
| 2025–26 | **+5.3%** ← the year it was found in |

It only worked in the window it was discovered in. Three other years,
thousands of trades each, it was indistinguishable from guessing.

**The lesson worth keeping:** testing on different *stocks* in the same *year*
is not independent proof. It feels like it is. It was convincing enough that I
would have told you it was real.

---

## Why no strategy you find online will work either

There's a study that explains the whole week. Researchers tracked what happens
to market patterns after they're published: returns drop **26% out of sample
and 58% after publication**. About half the profit disappears once people know.

**Being findable is what destroys an edge.** Anything in a blog post has
already decayed. A strategy that still worked wouldn't be published — it'd be
inside a fund that doesn't take outside money.

Also worth knowing: one site claimed "60% of retail algorithmic traders show
positive annual returns." The actual research — Barber & Odean in Taiwan,
Chague & De-Losso tracking 19,646 Brazilian traders — puts it at **1–3%**, with
97% of people who persisted losing money. That industry sells tools, not
results.

---

## The bugs, and why they matter

You should care about these because they're the reason the answer above can be
trusted.

**The take-profit that couldn't fire.** `0.56 × 100` is `56.00000000000001` in
computer arithmetic. Your PCG option needed $84.01 to hit an $84.00 target. Its
high-water mark shows it *reached* $84.00 and didn't exit. That bug cost a real
trade.

**Stock splits weren't accounted for.** A 3-for-1 split looked like a 67%
one-day crash that never happened. Seven of twelve funds were corrupted — one
showed +17% over five years against a real +182%. Every backtest run before
this was scoring prices that never existed.

**VWAP never reset.** One of the five conditions your rule requires compares
price to its "volume-weighted average." That average was supposed to reset each
day. It didn't — so it meant "above the 3-day average" in live trading and
"above the **yearly** average" in every backtest. The same condition, meaning
two different things.

**Volume rules never ran.** Any rule mentioning volume silently did nothing —
not rejected, not errored, just permanently silent. Indistinguishable in the
results from a rule that had been tested and found worthless.

**The benchmark was wrong.** The search ranked strategies against "break even"
— but break-even is also what a random guess scores. In a rising year, any
strategy clears it while adding nothing.

**Significance testing crashed on large samples.** It broke the first time a
full year of data was analysed — exactly when the numbers started mattering.

---

## What's true about your account

At $253, three rules bite regardless of code quality:

- **Pattern Day Trader** limits you to 3 same-day round trips per 5 days
- **One option contract** is the smallest possible trade — 100 shares of
  exposure
- **Your 1% risk budget** only allows options under about $71, which excludes
  most of the market

And the cost difference between instruments is larger than any edge we
measured:

| | round-trip cost | win rate needed |
|---|---|---|
| Shares | ~0.1% | 36.7% |
| Options | ~10% | **52.9%** |

You achieved 20%.

Your data is also partial. The free feed shows about **4% of what actually
trades** — and more than half of five-minute intervals have no data at all.

---

## What's running now

| | |
|---|---|
| **ETF portfolio** | SCHD + SCHG, buy and hold. Growing to ~$138. |
| Equity trading | **Off** |
| Options entries | **Off** (logging only) |
| Options exits | **On** — the two open positions still need their stop |
| Money | **Paper.** Nothing real is at risk. |

The two option positions expire 21 and 28 August and close themselves.

---

## What I'd tell you if you asked me straight

**The trading engine doesn't work, and I can't see what would make it work
from here.** That's not one failed idea — it's nine, each measured against a
control, on data corrected for four separate bugs.

**The thing that won every single comparison was buying and holding an index
fund.** No prediction, no timing, no edge required. That's slightly deflating
after building a trading bot, and it's what the numbers say.

**What you actually built is a measurement instrument** — one that caught a
silent data bug in its own foundation, rejected a fix I was confident in, and
told you the upgrade you were about to buy would make things worse. That's
rarer than a working strategy and it's why you can believe the negative
results rather than wondering.

**The capital constraint is separate and real.** Even a genuine edge couldn't
compound through PDT limits and whole-share minimums at $253. If this ever
becomes serious, size matters more than code.

---

## The bar for any new idea

On 6 August I asked LOCKBOT directly whether it had any remaining ideas for
making the engine profitable. It said no — and instead of a hypothesis it
proposed a standard, which is more useful.

**An idea is only worth testing if all three hold:**

**(a) It uses information that isn't already in the price bars.**
Every indicator LOCKBOT has — moving averages, RSI, MACD, VWAP — is just
arithmetic on the same price and volume numbers. 864 combinations of them
found nothing, because you can't extract more information than the input
contains. "Try a different indicator" fails immediately. Earnings dates,
order flow, or company fundamentals would pass.

**(b) It works across separate YEARS, not just separate stocks.**
Testing on different companies in the same year isn't independent proof —
same market, same conditions. This is the r0315 lesson: it beat the controls,
replicated perfectly on 40 completely different large companies, and then
turned out to work only in the year it was found.

**(c) It beats picking at random, not just "break even."**
Break-even for these brackets is 33.3% — which is exactly what random
guessing scores. In a rising market anything long clears it while adding
nothing. Today's universe fix took the win rate from 9.4% to 25.1%, which
looks like a triumph, until you see random entry went 11.6% to 27.9% on the
same data. The gap got *worse*.

**Why this matters:** it isn't pessimism, it's a checklist. It says exactly
what would change the answer. Nothing available on your current data
subscription passes (a) and (b) at the same time — that's a specific missing
thing, not a verdict on trading in general.

## If you come back to this

Three things I'd revisit, in order:

1. **The illiquidity effect** — the only one that failed on trading costs
   rather than absence of signal. It would need a realistic spread model
   attached before it means anything.
2. **Different data entirely** — not more price history. Order flow,
   fundamentals, earnings dates. Expensive, low odds, but it's the only
   untested direction left.
3. **Nothing else.** Entry timing on price indicators is closed. Don't let
   anyone — including a future me — reopen it without new inputs.

And if you just want the money to grow: keep buying the index. That's what
won.
