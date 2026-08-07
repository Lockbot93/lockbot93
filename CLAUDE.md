# LOCKBOT

An autonomous algorithmic day-trading bot for US equities, trading an Alpaca
**paper** account. Python, Windows, no git repository.

Run it with `run_lockbot.bat`, which launches `lockbot_controller.py` under
`.venv\Scripts\python.exe`. Always use that interpreter — the project has no
other environment.

## Architecture

`lockbot_controller.py` is the supervisor and the only long-running process.
It holds a Windows named mutex so a second instance cannot start. Every
`SCAN_INTERVAL_SECONDS` (300) it checks the market clock, and when the market
is open it runs four scripts in sequence **as subprocesses**:

| Order | Module | Role |
|---|---|---|
| 1 | `market_scanner.py` | Two-stage scan, ranks setups, **submits equity bracket orders** |
| 2 | `trade_manager.py` | Reconciles orders against Alpaca, journals completed trades |
| 3 | `position_monitor.py` | Evaluates equity positions and alerts — never exits |
| 4 | `options_manager.py` | **Sole options exit authority** — the software stop loss |
| 5 | `options_scanner.py` | Regime → strategy, selects contracts, submits options orders |
| 6 | `health_monitor.py` | Diagnostics |

Options run exits (4) before entries (5) — there is no broker-side stop for
options, so open positions get their stop checked before new premium is
committed. Both are skipped entirely when `OPTIONS_ENABLED` is `False`.

Each gets 3 attempts, then `self_repair.py`, then a Pushover alert. The
controller stays up regardless. `watchdog.py` runs **outside** the controller
on its own schedule so it can catch the controller process itself dying.

Supporting modules: `universe.py` → `universe_volatility.py` build the daily
symbol list (in that order — `universe.py` rewrites `universe.csv` from
scratch). `adaptive_brackets.py` sizes stop/target/shares per stock.
`risk_manager.py` gates entries. `shadow_trades.py` replays setups that were
*not* taken to test whether the ranking is any good.

`backtest.py` answers the question `shadow_trades.py` cannot: what a
**different** entry rule would have done. It imports
`market_scanner.detect_signal` and `indicators.add_indicators` rather than
reimplementing them, so it always tests the bot that actually trades. Every
result carries its day count and single-session concentration, because
ninety trades from one afternoon is one observation. `signal_research.py`
does the same for the live rule's own logged setups.

The layout is flat. Twelve empty scaffolding directories (`ai/`, `broker/`,
`config/`, `data/`, `execution/`, `indicators/`, `journal/`, `logs/`, `risk/`,
`scanner/`, `strategy/`, `tests/`) were removed on 2026-08-06 — nothing
referenced any of them, and `indicators/` sitting beside `indicators.py` was
an import ambiguity waiting to happen.

## Invariants — do not break these casually

These are load-bearing and were each written in response to a specific bug.

1. **`lockbot_config.py` is the single source of truth.** Never define a local
   copy of a shared setting in a module. Mismatched risk limits and stop
   percentages across modules were the root cause of several v1.0 bugs, and a
   journal-filename mismatch silently zeroed all performance reporting.
2. **`market_scanner.py` is the only module that submits orders.** Nearly every
   other module carries a "does not submit, modify, replace, or cancel orders"
   line in its docstring. Keep that true.
3. **One exit mechanism per instrument, one owner each.** For equities the
   broker-side bracket order is the sole exit; `position_monitor.py` once
   submitted its own exits at tighter thresholds — two mechanisms racing on
   one position — and was deliberately reduced to monitoring/alerting.
   `ENABLE_PAPER_EXITS` stays `False`. For options the owner is
   `options_manager.py`, and `options_scanner.py` never closes anything.
6. **Never mix equity and option positions.** `get_all_positions()` returns
   both in one list. Always filter through `position_filters.py`
   (`equity_positions` / `option_positions`). Four modules called it
   unfiltered before options existed; each was a latent bug the moment
   LOCKBOT could hold a contract.
4. **`PAPER_TRADING = True`, `LIVE_TRADING_ENABLED = False`** until paper
   testing is complete.
5. Every `*_PERCENT` constant is a **fraction** — `0.02` means 2%.

`validate_configuration()` enforces much of this and catches unit-confusion
typos. Run `python lockbot_config.py` after any config change.

## `ACCOUNT_PROFILE` is the switch that changes everything

Currently `"small"` — a ~$250 account. Three real-world rules bite at this
size and none are optional: whole shares only, no shorting under $2,000 of
equity, and PDT (3 same-day round trips per 5 business days under $25,000).
`"standard"` is the ~$100K profile: 8 positions, shorts allowed, 300-name
universe.

## Data files

Equity: `position_state.json`, `lockbot_pending_trades.csv`,
`completed_trades.csv`, `risk_state.json`, `signals.csv`, `universe.csv`,
`trade_journal.jsonl`. Shared: `lockbot_heartbeat.json`.

Options keep their own, deliberately separate: `options_position_state.json`,
`options_risk_state.json`, `options_completed_trades.csv`,
`options_shadow_log.csv`. Options P&L is kept out of the equity performance
files so the two strategies can be judged independently.

`daily_report.py` reports all three books separately: equity from the
journal, options from their own files, and the buy-and-hold portfolio read
live from the broker. That last one has no journal by design — it produces no
completed trades — and `position_filters` hides it from the trading engine, so
a report built from trades alone would never have mentioned the sleeve now
holding most of the account.

Paths live in `lockbot_config.py` — read them from there, never hardcode.

The `*_OLD`, `*_backup` and `*_v0_N` dead copies this file used to warn about
are gone — git holds the history now, which is what they were standing in for.
Verified absent 2026-08-06.

## Known constraints

- **`daytrade_count` is gone.** Alpaca removed it from account responses on
  2026-07-06 (FINRA intraday-margin migration); it now returns `None`, as does
  `pattern_day_trader`. `day_trade_tracker.py` counts round trips locally from
  filled order history instead, and both scanners enforce it. It deliberately
  over-counts (7-day lookback, `min(buys, sells)` per symbol-day) and treats a
  counting *failure* as "block", because the old code's mistake was reading a
  missing count as permission to trade.
- **Options do not support brackets.** Alpaca's order classes are
  security-type dependent: equities get `simple`/`oco`/`oto`/`bracket`, but
  options get only `simple` and `mleg` (2–4 legs). Options time-in-force is
  `day` only — no GTC. This is why `options_manager.py` exists and why it is
  the most safety-critical file in the project: **if it stops running, open
  option positions have no stop loss of any kind.** A stale
  `OPTIONS_MANAGER` heartbeat is not a reporting problem, it is unprotected
  capital.
- **One contract cannot be subdivided.** 100 shares of exposure, minimum. On
  a $250 account that collides with the 1% risk budget the equity side uses —
  a $70 contract with a 35% stop risks $24.50, or 10% of the account.
  LOCKBOT refuses such trades rather than quietly accepting the larger risk
  (`OPTIONS_MAX_RISK_PER_TRADE_PERCENT`). At this equity that admits only
  contracts under about $71, which is most of the options market ruled out.
  That is the honest answer at this account size, not a bug.
- **Greeks can be missing** on the indicative feed. When delta is absent the
  delta gate is replaced by a distance-from-the-money check, never skipped —
  skipping it let a deep in-the-money F call through during the first live
  dry run.
- **An option order that is absent from the broker has two meanings.** It
  either filled and then closed, or it never filled at all. `options_manager.py`
  assumed the first, so an unfilled PBR call on 2026-07-30 was journaled as a
  −$56, −100% trade that never happened, while the order stayed live and would
  have produced an untracked position with no software stop. Entries now carry
  `entry_order_id` / `entry_filled`, and `classify_entry_order()` resolves the
  ambiguity from the order rather than guessing. Unfilled entries are cancelled
  after `OPTIONS_ENTRY_FILL_TIMEOUT_MINUTES` so they cannot hold a slot forever.
- **A multi-leg fill price is signed; a single-leg one is not.** Alpaca
  reports an `mleg` order's `filled_avg_price` as the net across legs — a
  spread opened for a debit fills at `+0.24`, the same spread closed for a
  credit at `-0.16`. Reading that sign literally journaled the JD spread of
  2026-07-30 as −$45.00 (−155%) when it really lost $8.00, and −155% is not
  reachable: a debit spread cannot lose more than the debit. Go through
  `entry_debit_from_order()` / `exit_credit_from_order()`, never
  `filled_avg_price` directly.
- **Size the exit bands off the fill, not the quote.** `options_scanner.py`
  writes `entry_debit` from the quote at submission; `options_manager.py`
  re-bases it on the actual fill the first time it sees the position at the
  broker. The JD spread was quoted at $29 and filled at $24, EWZ quoted $67
  and filled $65 — and `OPTIONS_TAKE_PROFIT_PERCENT` /
  `OPTIONS_STOP_LOSS_PERCENT` are percentages *of that number*, so a wrong
  basis moves both bands.
- **A debit spread cannot be worth less than zero.** `long_bid - short_ask`
  on a stale or crossed book can still come out negative, which read as a
  −203% return five minutes into the JD trade and fired the software stop on
  an impossible number. `current_exit_value()` now returns `None` there —
  holding on a broken quote is safer than acting on it.
- The free `iex` data feed reports only a fraction of real volume, which is
  why `UNIVERSE_MIN_AVG_DOLLAR_VOLUME` is 0 and ranking is relative.

## The confidence score is a tautology

`confidence_score()` in `market_scanner.py` rewards twenty points for each
of five conditions — trend direction, price vs EMA, MACD vs signal, the RSI
band, price vs VWAP. `detect_signal()` **requires all five** for a signal.
So every tradable setup scores exactly 100, necessarily. All 208 setups in
`shadow_trades.csv` carry confidence 100.

`MIN_SIGNAL_CONFIDENCE` (80) is therefore an inert gate: the
`CONFIDENCE_TOO_LOW` rejection and `options_scanner.py`'s "confidence below
80" drop can never fire. `market_scanner.py --self-test` proves this by
enumerating all 120 indicator combinations.

It was left in place rather than replaced with an invented formula. The
continuous measure that *can* rank setups is `signal_quality.py`, already
logged per setup as `q_*` columns; `signal_research.py` will report whether
those components carry information once enough resolve. Do not fit a new
score before that evidence exists.

## Options rules added 2026-08-03

Each was written after the behaviour it prevents actually happened.

- **A stop must survive two consecutive cycles** (`OPTIONS_STOP_CONFIRM_CYCLES`).
  An EWZ call exited at −8.1% against a −35% stop: the sell went out as a
  limit at the stop and filled 42% above it, and no trade in that window
  printed near the level. These books are fresh but wide and jittery —
  16–28% spreads with the bid moving 8% between polls seconds apart — so
  one bid print is not evidence. Take-profit and the time rules still fire
  immediately; only the destructive rule waits.
- **One position per underlying.** The same IBIT 36/36.5 spread was
  submitted three times across three cycles, because an entry whose order
  is still working does not look like a position yet. A signal that
  persists is a reason to keep holding, not to buy again every five
  minutes. The scanner claims the name on submission so a second candidate
  in the same cycle cannot slip through either.
- **Fills are verified by order id, not by symbol.** Both IBIT entries
  shared a `long_symbol`, so the one that filled satisfied the check for
  both. `entry_filled` is sticky, so a reconciliation pass compares tracked
  entries against broker *quantity* and resolves any excess by order.
- **There is no minimum quality gate**, and setting one is still a guess.
  The spread, delta and affordability checks ask whether a setup is
  tradable, never whether it is good. Every ranked candidate is now logged
  with `action=CANDIDATE` so the distribution accumulates — 61 arrived in
  a single afternoon, so this is answerable in weeks, not months. First
  reading: median 28.8, max 63.8, and a floor of 50 would reject 98%.
  Do not set `OPTIONS_MIN_QUALITY` from one session.

## Widening the profit target does not fix the win rate (tested 2026-08-04)

The arithmetic is seductive: at a 20.2% win rate you need a 3.95:1 payout
to break even, and the options bands give 1.43:1. So let winners run.

It was tested and it is wrong. Sweeping the reward ratio over real bars:

    reward   breakeven   trades   win rate   avg R
      1.43      41.2%       20      40.0%    -0.03
      2.00      33.3%       14      14.3%    -0.57
      3.00      25.0%       12       0.0%    -1.00
      4.00      20.0%       12       0.0%    -1.00

Every widening made it worse. At 3:1 and beyond nothing reaches the
target at all — the moves are not there inside the holding window, so a
higher payout multiplies a number that never occurs.

Note the top row: at the tightest target tested the signal wins 40% and
sits at −0.03R, essentially breakeven. That is on SHARES, where there is
no spread paid twice and no theta. It is 20 trades over 5 days and proves
nothing, but it is the one configuration that has ever looked close.

The lesson is not about targets. It is that "many small losses, few huge
wins" requires the huge wins to be reachable, and on this universe and
holding period they are not.

## PRE-REGISTRATION: crypto oversold. One attempt, ever. (2026-08-06)

Written by LOCKBOT, not by me, because it has caught me twice and has no
stake in the answer being yes. Committed BEFORE any forward data exists.
Do not reinterpret this. If a future session finds itself arguing about
what a clause meant, the answer is no.

    rule            rsi < 35, swing long, crypto
    pool            the FULL pool including BTC and ETH
    holdout         FORWARD ONLY -- data that does not yet exist
    sample          n >= 300 AND >= 180 days elapsed
    control         week-matched seeded random, seed 20260807, >= 500 draws
    discriminator   a mirrored rsi > 65 SHORT arm, logged alongside

    PASS requires ALL of:
      edge >= +0.10R over the control
      above the control distribution's 95th percentile
      net > 0 after costs -- beating the control is not the same as
        making money, and the control here is positive
      non-negative edge in BOTH calendar halves

    ANY failure kills the entire rsi-oversold-long family on crypto
    PERMANENTLY. No variants, no re-cut thresholds, no ex-BTC/ETH
    rescue. ONE ATTEMPT EVER.

    If it passes WITHOUT the short arm also behaving, the result is
    regime luck: shadow only, no capital.

    LOCKBOT's stated prior that it passes: 0.15

Why forward-only rather than a held-out slice of history: the sample it
was found in has been looked at, and a held-out cut of the same four
years shares the same cycle. Forward data cannot be re-cut. LOCKBOT's
note: "the clock starts when the forward shadow logging actually starts
running, not today's date on the document -- the registration binds from
the first logged bar."

So nothing counts until the rule and BOTH control arms are wired into a
shadow path and logging. That is the next build, and until it runs this
registration is a piece of paper.

The reason for the severity: this is the closest result the project has
produced (+0.187R over control, 4x anything on equities, and the only
one measured with costs charged) and it already FAILED the earlier sign
test at 2022 and 2026. r0315 had exactly this shape -- beat its controls,
replicated across symbols, died across years. One attempt is what stops
a fourteenth family becoming a fourteenth rationalisation.

## Crypto: half the direction died on costs, the rest on one year (2026-08-06)

The fresh start. Crypto removes four structural constraints at once --
PDT does not apply, sizing is fractional, it trades 24/7, and the book
is fully visible rather than the 4% equities give.

LOCKBOT reframed the question and saved a week: on equities the tenth
question turned out to be "does entry beat random". Here the ZEROTH
question is "does ANYTHING beat random after costs", because Alpaca
charges ~25bps per side. Every one of the 17 failed equity families was
measured at zero cost.

    round trip = 2 x (25bp fee + 0.092% observed half-spread) = 0.683%

    horizon   stop      n     gross R   cost R    net R    gate
    day        2%    2,994    +0.083    0.342    -0.259   CLOSED
    swing      5%    2,993    +0.032    0.137    -0.105   open

**The day horizon is closed by the pre-registered gate** (drag 0.342R
against a 0.25R limit). And note the net column: random long entries
lose money at BOTH horizons after costs, across four years containing a
major bull run. In crypto the friction exceeds the drift.

A flaw in our own criterion, found before it could flatter anything: the
bar was "+0.10R over the random control", but the control is NEGATIVE
after costs, so a rule clearing it exactly would still lose money. The
honest bar is edge > +0.105R -- it must cover friction, not just beat
the coin.

### Two candidates cleared the bar and failed the year test

    rule                        net R   vs control   ex-2023
    oversold (rsi<35)          +0.233      +0.187     +0.081
    pullback (close<ema21)     +0.184      +0.137     -0.045

Pullback is entirely 2023 -- remove one year and it loses money.

Oversold is the closest thing this project has produced. It survives
removing its best year at +0.081R, and +0.101R with BTC and ETH
excluded. It still FAILS the pre-registered sign test: 2022 at -0.01 and
2026 at -0.09.

The honest nuance, recorded and NOT acted on: the two failing years are
the thinnest in the sample (2022 is 9% of trades, 2026 is partial). That
observation is exactly the rationalisation the pre-registration exists to
block. If oversold is ever revisited it needs fresh criteria and a
held-out year decided in advance, not this sample re-cut.

Other limits LOCKBOT named that bound any crypto work: Alpaca crypto is
long-only spot, so no short arm can ever run; and 73 pairs is misleading
because nearly everything is beta to BTC -- effective breadth is closer
to one and a half assets than seventy-three.

## The EXIT was never varied, and it matters (2026-08-06)

Every backtest in this project used one exit: fixed stop, fixed target,
fixed time, set at entry and never moved. Its dials were swept
thoroughly and its SHAPE never was.

LOCKBOT had argued exits cannot matter -- "sizing and exits scale an
expectation, they don't change its sign". True of symmetric exits. A
trailing stop is asymmetric: losses capped at the initial distance,
winners left open, which manufactures positive skew from the exit alone.

**Its claim is refuted.** 5,307 seeded random entries, 60 symbols,
3.3 years, identical bars, daily:

    exit structure     entry     mean R    by year
    fixed bracket      random    +0.093    +0.08 +0.09 +0.15 +0.02
    trailing stop      random    +0.143    +0.08 +0.13 +0.25 +0.04
    breakeven+trail    random    +0.143    +0.06 +0.15 +0.24 +0.04

A trailing stop improves random entries by +0.05R, a 54% lift, positive
in every year. The exit alone does work the entry does not.

**But it is a better EXIT, not an edge**, and it makes the entry matter
less rather than more:

    exit structure      rule R   random R     edge
    fixed bracket       +0.101     +0.093   +0.008
    trailing stop       +0.096     +0.143   -0.047
    breakeven+trail     +0.106     +0.143   -0.037

The rule goes BACKWARDS relative to random under better exits, and the
reason is mechanical rather than statistical: the rule buys pullbacks
(close < ema_21), a mean-reversion setup, while a trailing stop wants
trends. The entry selects against the exit.

**The caveat that bounds all of it:** these are long-only entries in a
rising market, so both columns are positive because the market rose. The
finding is the DIFFERENCE (+0.05R), not the level. A trailing stop is
not a strategy; it is a better way to hold whatever you already hold.

Do not read the trailing-stop number as an edge. Read it as: if anything
is ever held with a stop, trail it rather than fixing it.

### The drift objection, and the test that settled it

LOCKBOT accepted the refutation in form and contested it in substance,
with the strongest objection available: a trailing stop harvests DRIFT
without any autocorrelation at all. It holds winners and cuts losers, so
time-in-market is loaded onto positions already rising, and in four
up-years "+0.05R, positive every year" is exactly what pure drift plus
differential exposure would print.

It proposed the discriminator: run the same arms on random SHORTS. If
the lift is drift exposure it vanishes or flips; if shorts gain too, it
is real. 5,987 seeded entries each, 10bp/side charged:

    direction  exit    mean R   net of cost   bars held    R/bar
    long       fixed   +0.089      +0.049        10.7    +0.0083
    long       trail   +0.142      +0.102         8.7    +0.0163
    short      fixed   -0.072      -0.112        10.7    -0.0068
    short      trail   -0.021      -0.061         7.7    -0.0027

    trail lift on LONGS   +0.054R
    trail lift on SHORTS  +0.051R

**The objection is refuted.** The lift is the same size on shorts, so it
is not drift exposure. And its own second check cuts harder still: the
mechanism required holding winners LONGER, but the trail holds FEWER
bars (8.7 vs 10.7) while producing more R -- R per bar nearly doubles.
Exposure cannot explain that.

Net of costs the long trail makes +0.102R against the +0.10R bar
LOCKBOT pre-registered. It clears, barely, and the margin is thin enough
that a worse cost assumption would sink it.

WHAT THIS IS AND IS NOT. It is a real exit improvement of about +0.05R,
independent of direction, surviving costs. It is NOT a tradeable edge:
random long entries net +0.102R because the market rose, and shorts stay
negative (-0.061R) even with the better exit. The trail improves how you
hold; it does not tell you what to hold.

The part of LOCKBOT's original claim that survives is the part that
mattered: every entry edge measured under every exit is within
+/-0.05R. The trail did not expose a hidden edge, it confirmed there was
never one.

`exit_strategies.py` holds the three structures and 12 self-tests,
including that an intrabar low is not rescued by the same bar's high --
the classic way a trailing-stop backtest flatters itself.

## News was the first input to clear the bar, and it failed (2026-08-06)

Both LOCKBOT and I concluded nothing available cleared conditions (a)
and (b) below. **Neither of us had checked.** The Alpaca surface carries:

    news API               5 years of history, 100% universe coverage,
                           median 24 articles per symbol per quarter
    historical option bars 227 daily bars per contract
    corporate actions      cash dividends, spin-offs

News is not derived from OHLCV bars and is testable across years, so it
cleared (a) and (b) — the first thing all week to do so. The verdict we
had both signed off on was premature, and the lesson is to check the
data surface rather than reason about it.

Tested with acceptance criteria pre-registered by LOCKBOT BEFORE any
result was seen: 500+ resolved events, 3+ years, no year over 40%, same
sign every year, and >= +0.10R over a PRICE-MATCHED control.

The price-matched control was LOCKBOT's idea and it is the important
part. News follows price, so "buy the news spike" can beat random entry
purely by rediscovering momentum through a more expensive data source.
Matching on day-T return with no news isolates what the headline adds.

    series           n      win     mean R
    news spike     448    41.1%    -0.073
    random entry   453    49.2%    +0.088
    price-matched  450    48.9%    +0.081

    vs random          -0.161R
    vs price-matched   -0.154R   (needed +0.10)

Negative in all four years: -0.061, -0.223, -0.015, -0.340. Buying
attention spikes is measurably WORSE than entering at random.

Note the price-matched control landed at +0.081R against random's
+0.088R — indistinguishable. So no momentum effect was hiding here
either; the news itself is what hurts.

**One observation, and the discipline that goes with it.** The edge is
consistently negative, which raises the obvious question of whether the
inverse is a signal. Testing that now would be post-hoc fishing of
exactly the kind the pre-registration exists to prevent. If it is ever
tested it needs its own pre-registration and its own held-out years
BEFORE anyone looks at a result.

Lookahead rules used, also pre-registered: counting window closes at day
T's market close, entry at the T+1 open, articles after the T+1 open
excluded outright, articles within 30 minutes of T's close excluded
because created_at can lag the wire.

`news_signal.py` holds the harness and its 11 self-tests.

## THE BAR: what a new strategy idea must clear before it is worth testing

Set by LOCKBOT on 2026-08-06, when asked directly whether it had any
remaining ideas and answered no. It proposed a standard instead of a
hypothesis, and the standard is better than a hypothesis would have been.

Everything below this section is a record of ten strategy families that
were tested and lost. Read the bar first; it is what stops an eleventh
from being started on the same footing.

**An idea is only worth testing if all three hold.**

### (a) It uses an input not derived from these OHLCV bars

Every indicator available — EMA, RSI, MACD, VWAP, ATR — is a
transformation of the same price and volume series. Smoothings and
ratios of numbers already in hand.

864 combinations of them cleared nothing. That is not bad luck: you
cannot extract more information than the input contains. "Try a
different indicator" fails here immediately. Earnings dates, order
flow, fundamentals, short interest would pass.

### (b) It is testable across separate YEARS, not just separate symbols

Different stocks in the same period is not independent evidence. Same
regime, same macro, same year.

r0315 is why. It beat the controls by 6.4 points on held-out data, then
replicated almost exactly on 40 mega-caps that share none of the
original universe — and scored +0.1%, -0.3%, -0.3% across 2022, 2023
and 2024. It worked only in the year it was discovered, and the
cross-symbol replication was convincing enough to have justified
trading it.

### (c) It beats RANDOM ENTRY, not breakeven

For a 2:1 bracket, breakeven is 33.3% — which is also what a driftless
random walk scores. In a rising year every long-biased rule clears it
while contributing nothing.

The lab universe change on 2026-08-06 is the cleanest illustration:
win rates went from 9.4% to 25.1%, a 167% improvement and a very
persuasive number. Random entry on identical bars went 11.6% to 27.9%.
The gap WIDENED. Without the control that reads as a breakthrough.

### Why this is a gate and not a mood

It is falsifiable and it names what would change the answer. It does
not say nothing can work; it says nothing available on this
subscription clears (a) and (b) simultaneously. If a data source is
ever added that is not derived from price bars, (a) opens — and the
illiquidity result is the one thing worth reopening, with a real spread
model attached.

Do not propose work that fails this bar, and do not let a persuasive
in-sample number substitute for it.

## What the evidence currently says about the strategy

Read this before building anything intended to improve returns.

As of 2026-07-29, `shadow_trades.py` resolved 55 of 157 logged setups:

- **15 hit target, 40 hit stop — a 27.8% win rate, average −0.17R.**
- The brackets run at 2:1 reward:risk, so **breakeven needs 33.3%**. The
  measured edge is negative, not merely unproven.
- **Volume ratio, the tiebreaker used to rank which setups get taken, is
  inverted.** Higher-volume half: 21.4% win rate, −0.36R. Lower-volume half:
  33.3%, 0.00R. LOCKBOT's ranking is currently selecting the worse setups.

Caveats that matter: the sample is small and narrow (mostly 7/28–7/29, one
regime), fills are simulated at exact stop/target, and ambiguous bars count
as losses. It is directional evidence, not a verdict.

The implication for options is direct: options pay the spread on entry *and*
exit and lose value to theta daily, so a setup that loses money in shares
loses money faster in contracts. `OPTIONS_SHADOW_MODE` is `True` for this
reason. Resolve more shadow trades before spending effort on new features —
measurement is worth more than code here.

## The entry signal has no directional edge (searched 2026-08-04)

The shadow finding above is thin — 55 trades, one regime. `rule_search.py`
answered the same question on 384,700 bars across 123 sessions and 40
symbols, and the answer is not thin.

**864 candidate rules were generated** from combinations of trend, MACD,
price against VWAP and EMA, five RSI bands and three volume conditions.
621 produced enough trades to rank. Sessions were split chronologically at
2026-06-11; the search never saw the holdout.

**Zero of the 621 cleared breakeven — in training.** Not "the winner failed
out of sample": nothing led even on the data it was fitted to. The best was
32.3% against a 33.3% requirement. That is a stronger result than
overfitting, because overfitting needs noise to exploit. When 621 rules
cannot be made to look good on their own training data, the signal is not
there to find.

The mechanism, measured on the live rule:

    reward  bars  entries  timeout  targets  stops    win     b/e    avgR
      1.00    24     8664      89%      438    479  47.8%   50.0%  -0.04
      1.43    24     8590      92%      174    474  26.9%   41.2%  -0.35
      2.00    24     8568      94%       55    473  10.4%   33.3%  -0.69
      3.00    24     8561      94%        7    472   1.5%   25.0%  -0.94

Two things are visible and they are different.

**The target is unreachable at the bracket LOCKBOT actually uses.** At 2:1,
94% of entries never touch either band inside the holding window, and 55
targets print against 473 stops. Widening the target does not widen the
payout, it removes the payout — which is the same conclusion the 2026-08-04
reward sweep reached, now with the mechanism attached.

**Underneath that, there is no edge at all.** At 1:1, where a 2% move is
genuinely reachable, the rule wins 47.8% against the 50% it needs. That is
a coin flip, slightly worse — and this simulation charges no spread and
fills at exact stop and target, so live is worse than the coin.

So the negative results at 2:1 and 3:1 are the geometry, and the coin flip
at 1:1 is the signal. Fixing the geometry would move LOCKBOT from losing
badly to losing slowly. Neither is a strategy.

What this does NOT say: that these indicators can never work. It says they
do not work on *this* universe at *this* holding period with *these* bands,
and that searching the same space harder is answered. A different search
space — different holding period, different universe, or data these
indicators cannot see — is the only thing that changes the answer.

Do not build entry-side features on the current signal. Cost and hazard
controls (spread, IV, theta, event risk) are still worth having, because
they reduce what a bad trade costs and do not depend on the signal working.

## ADX/DI: 5,856 rules, and the number that came back is the bracket's

`add_indicators` has computed `adx`, `plus_di` and `minus_di` on every bar
since the project began, and `strategy_lab.FIELDS` never listed them — so
no rule could reference them. The entire 864-rule space was blind to trend
STRENGTH and directional pressure. Every condition it contained asks where
price sits relative to an average or whether momentum is rising; none asked
how hard the market was moving. Unlocked, the space went 864 → 5,856.

    rules tested                       5,856
    dropped for too few trades         5,073   (87%)
    ranked                               783
    CLEARED BREAKEVEN IN TRAINING          0
    expected to clear on chance alone    293

    controls, same holdout bars
      always_long      633 trades    9.3%   -0.72R
      random_entry     427 trades   10.8%   -0.68R

**Zero against an expectation of 293 is not "nothing found" — it is worse
than a random pile, and that is the tell.** The controls failed the same
holdout catastrophically. Breakeven was unreachable for ANY entry over that
window, so the zero measures the exit structure rather than the indicators.
This is the 2:1-over-78-bars geometry already measured: ~94% of entries
never touch either band, which caps the win rate far below 33.3% no matter
what the entry does.

LOCKBOT's verdict, filed as `b1e9157b` and worth quoting because it refuses
to overclaim in the direction the result invites:

> Establishes: no ADX/DI conjunction rescues the fixed 2:1 day bracket.
> Does NOT establish ADX/DI carries no information — the family was never
> scored rule-minus-control, and finalists' holdout win rates ran roughly
> double random's on too few trades to read.

So this is the fifteenth family only in the narrow sense. **Do not re-propose
ADX/DI at this exit structure.** A retest is legitimate ONLY under the
variable-ratio machinery (`8e24ae42`), scored against a seeded control on the
same bars, counting against a multiplicity budget of ~783 rather than 5,856.

**The process lesson, which binds the next sweep.** 87% of rules were dropped
for too few trades: the ADX/DI conditions are conjunctive on top of the
existing four, so most of the 4,992 new rules are too narrow for the
available history to measure. The search grammar over-conjuncts. Fix that
before the time-of-day sweep, or that run spends its compute measuring the
sample floor instead of the hypothesis.

And the standing verdict does not move. LOCKBOT: *"The exhaustion verdict
predicted this outcome; a prediction coming true doesn't weaken it."*

## VWAP never reset, so backtests tested a different bot (fixed 2026-08-06)

`add_indicators` computed VWAP as a plain `cumsum()` over the whole
DataFrame, with no session boundary. The indicator therefore meant
whatever the CALLER's frame length made it mean:

    market_scanner (live)   SCAN_LOOKBACK_DAYS_5M = 3   -> 3-day average
    backtest.load_history   days=365                    -> 1-year average

So `close > vwap` — one of the five conditions `detect_signal` requires
— meant "above the 3-day average" in the live scanner and "above the
YEARLY average" in every backtest. A 120x difference in the same named
condition.

This directly defeated the design goal stated above: backtest.py imports
`detect_signal` and `add_indicators` rather than reimplementing them,
specifically so it tests the bot that trades. It did import them, and
was still testing a different signal, because frame shape changed what
the function meant. **Importing the same function is not sufficient for
fidelity if the function's output depends on how much data you pass it.**

Fixed by grouping the cumulative sums by session date, which is the
conventional definition and makes the indicator independent of caller
history. US regular hours never straddle a UTC date change, so the UTC
date is a correct session key.

Nothing caught this because `indicators.py` had no self-test at all —
the module every measurement in the project is computed from. It has 21
checks now, including that VWAP is independent of frame length.

The headline finding was re-measured on the corrected signal and holds:

    live rule      32.9%   -0.01R   p=0.7055   (was 33.8% on broken VWAP)
    random entry   36.7%   +0.10R   p=0.0002

Marginally worse, still losing to blind entry by 3.8 points, and now
measured on the signal LOCKBOT actually uses rather than a proxy.

## r0315: the one that looked real, and how it was killed (2026-08-05)

The search was re-run at POSITION horizon (1560 bars, ~20 days) with the
random-entry controls attached. Unlike the day-horizon run, which
produced zero rules clearing breakeven in training, this produced 412 of
816 against 43 expected by chance — and two finalists beat the controls
on the holdout.

    BUY_LONG when macd > macd_signal, close < vwap, close < ema_9,
                   rsi < 35, volume > volume_avg_20

Buy capitulation on a momentum turn with volume confirmation. Coherent,
well-known, and the exact opposite of LOCKBOT's live rule, which buys
strength. It also matched a pattern seen repeatedly and dismissed:
inverse momentum beat momentum, bottom decile beat top decile.

It then passed the check that should have killed it. universe.csv is
survivor-heavy and "buy the dip" is precisely what that bias flatters,
so it was re-run on 40 mega-caps that essentially never delist:

    universe.csv (survivors)   42.0%   vs best control 36.7%   +5.3%
    large caps (no bias)       41.9%   vs best control 36.4%   +5.5%

Near-identical on a completely different symbol set, p=0.0000. That is
not what an overfit rule does.

**And it was still fake.** Both tests covered the SAME 365 days. Run
across separate years:

    2022 bear market   r0315 32.3%   control 32.3%   +0.1%
    2023 recovery      r0315 38.7%   control 39.0%   -0.3%
    2024               r0315 39.1%   control 39.4%   -0.3%
    2025-26 (found on) r0315 42.0%   control 36.7%   +5.3%

The edge exists only in the window the rule was found in. Three other
years, thousands of trades each, indistinguishable from blind entry.

### The methodological lesson, which is worth more than the rule

**Replication on different SYMBOLS in the same PERIOD is not
independent evidence.** It feels like out-of-sample and is not: every
symbol shares the same regime, the same macro, the same year. It was
convincing enough here to nearly justify trading a rule that has no
edge in three of the four years available.

Time is the binding dimension. Any future candidate must clear separate
YEARS before anything else, and a rule that only works in the period it
was discovered in is a description of that period, not a strategy.

Do not re-propose r0315. It is in the search space and will be found
again; it has been tested across four years and works in one.

## Why every strategy found online has already stopped working

Read this before researching "what strategies do profitable bots use".
It is the general result that explains all the specific ones below.

McLean & Pontiff studied what happens to documented anomalies after
publication: returns are **26% lower out of sample and 58% lower after
publication**, with roughly half the alpha disappearing as investors
learn about it and trade it away. Other work puts post-publication decay
around 35% and finds it is real rather than statistical artefact.

**Being findable is what destroys an edge.** A strategy that still
worked would not be in a blog post; it would be inside a fund that does
not take outside money. This is not cynicism, it is the measured
half-life of published alpha.

It also explains this project's results exactly. The strategies the
trading-content industry lists are mean reversion, momentum, arbitrage,
market making and scalping. Momentum and mean reversion were tested here
and lost to their controls. The other three need colocation, exchange
membership or microsecond latency — measured below as 54,640x out of
reach. There was no gap in the search.

Beware the numbers those sites publish. One claimed "around 60% of
retail algorithmic traders show positive annual returns". The
peer-reviewed evidence: Barber & Odean found **under 1%** of active
Taiwanese day traders profitable after fees over 1992-2006; Chague &
De-Losso tracked 19,646 Brazilian futures traders and found **97% of
those persisting past 300 days lost money**, with the profitable 3%
earning a median of about $10 a day. Long-run profitability sits at
1-3%. The marketing figure is wrong by a factor of about thirty.

### The one edge with a reason to persist — tested 2026-08-05

Capacity-constrained anomalies escape publication decay, because decay
requires money to be able to arrive. A $5bn fund needs a $100m position
to matter, which in a small company means owning an uncomfortable share
of it, so institutions structurally cannot participate. That is the only
structural advantage a $253 account holds over a fund.

Tested as an illiquidity tilt across 1,245 names, 901 sessions, sip
feed, split-adjusted, liquidity floor dropped to $200k/day so thin names
could actually enter:

    thinnest 249 (least liquid)   +25.3% CAGR   Sharpe 1.33
    all names (control)           +34.2% CAGR   Sharpe 1.50
    thickest 249 (most liquid)    +20.8% CAGR   Sharpe 1.26

    illiquidity premium (thin - thick)   +4.52%/yr
    versus holding everything            -8.95%/yr

The premium is REAL — thin beat thick by 4.5 points, the sign the theory
predicts — and the capacity argument holds: a $50 position is 0.0119% of
a day's volume in that bucket, so a small account genuinely can go where
funds cannot.

It is still not a strategy. Holding everything beat both extremes, and
three things eat the 4.5% before it reaches an account: monthly
rebalancing across 249 thin names is enormous turnover, thin names carry
the widest spreads (a 0.5% round trip twelve times a year is 6%), and
survivorship bias is worst exactly here, since small illiquid companies
are the ones that go to zero and vanish from the sample.

Recorded because it is the closest thing to a real edge found, and
because the reason it fails is transaction costs rather than absence of
signal. If anything is ever revisited, revisit this — with a spread
model attached.

## The entry rule is worse than buying at random (2026-08-05)

This supersedes everything above about whether the strategy has an edge.
It is not "the edge is small" or "the sample is thin". It is that the
signal carries no information and mildly negative selection.

Holding longer looked like it fixed everything. At a 2% stop, over 251
sessions and 783,508 bars, the live rule improves monotonically as the
window widens — because 80% of entries were timing out before either
band was reached, so the bracket was deciding outcomes, not the signal:

    hold      trades  timeout  win rate   avg R
    1 day       8319      80%     14.5%   -0.57
    5 days      3295      27%     29.3%   -0.12
    20 days     2565       3%     34.4%   +0.03   <- clears breakeven

34.4% against a 33.3% breakeven. The first thing all week to clear it.

**It is drift, and the rule is a liability.** 33.3% is not merely the
breakeven for a 2:1 bracket — it is also the probability a driftless
random walk touches +4% before −2%. So beating it by a point means
nothing without a control. Run over identical data and bracket:

    rule             trades   win rate   avg R        p
    live rule          2497      33.8%   +0.01   0.3330
    live inverted      2570      33.5%   +0.01   0.4363
    always long        3027      36.0%   +0.08   0.0010
    scattered long     2460      36.7%   +0.10   0.0002

(Re-run 2026-08-05 on split-adjusted prices — see the split bug below.
The first run used raw bars and was slightly kinder to the rule. The
rule and its exact inverse now land within 0.3 points of each other,
which is what zero information looks like.)

**Buying on every bar beats the rule. Entering at random beats it by
more.** The rule's own p-value is 0.12 — indistinguishable from chance —
while blind entry clears at p=0.003. Inverting the rule lands near the
mirror, which is what a signal with no information looks like.

So the positive avg R at 20 days is a rising year, available to anyone
who bought anything. Selecting entries with these indicators produces a
result 1.5 points WORSE than not selecting at all.

### What this rules out

Searching for a better rule in this space. It is not that the right
combination has not been found — 864 were tried and none cleared
breakeven in training — it is that entry timing on these indicators has
no information to extract at any holding period tested. A different
threshold, band or combination cannot fix an input that does not
predict.

### What it does not rule out

Different inputs entirely (order flow, fundamentals, cross-sectional
ranking, anything not derived from the same OHLCV bars), or a different
question than "when to enter" — position sizing, exits, and instrument
selection are untouched by this. Note also that the thing which DID win
is buy-and-hold, which is what `etf_portfolio.py` already does and
which requires no edge at all.

### The benchmark trap this exposed

`rule_search.py` ranked against breakeven, and breakeven is the wrong
bar in a drifting market. Any long-biased rule clears it in a rising
year while adding nothing. **Never judge a rule against breakeven
alone — always against random entry over the same bars.** The controls
are cheap and they are the difference between a finding and an artifact.

### What the free feed actually shows you (measured 2026-08-05)

The note further down says the iex feed "reports only a fraction of real
volume". Here is the fraction, against the full consolidated tape:

    symbol      iex avg daily        sip (full tape)     iex share
    AAPL            1,956,112             55,457,342          3.5%
    SPY             1,557,218             49,318,317          3.2%
    F               2,187,425             53,884,617          4.1%
    PCG             1,383,135             21,809,406          6.3%
    IBIT            1,605,483             35,781,648          4.5%

**LOCKBOT sees about 4% of what trades.** Every volume comparison it
makes — `volume_ratio`, `volume_confirmed`, the volume gate that decides
which shorts get measured — is computed from that sample.

It is worse than a 4% sample of every bar. Over one 8-hour window AAPL
returned 83 bars on sip and 38 on iex: less than half. The missing ones
are intervals in which nothing traded on IEX at all, so the bar simply
does not exist and the indicators skip a beat that really happened.

And the quote quality: a resting iex quote on AAPL read bid 293.64 / ask
324.64. A $31 spread on the most liquid stock in the world, because so
little rests on that one venue.

**The trap: sip is available for history but refused live.**

    end - 60m   sip   ok
    end - 20m   sip   ok
    end -  5m   sip   "subscription does not permit querying recent SIP data"
    live quote  sip   refused

So a backtest CAN be run on the real tape — and doing so would score a
bot that cannot exist, because the live scanner will only ever see iex.
Backtest on iex and you are measuring a distorted world; backtest on sip
and you are measuring a bot you cannot deploy. There is no honest
configuration available on this subscription.

That is the concrete reason "better rules" cannot rescue this. The
inputs are a 4% volume sample with missing bars and unusable quotes.

### Alpaca bars are NOT split-adjusted by default (found 2026-08-05)

Every backtest run before this date over a window containing a split
was scoring a price series that never existed.

`StockBarsRequest` defaults to `Adjustment.RAW`. A 3-for-1 split then
appears as a 67% single-day collapse, which trips every stop beneath it
and invents a catastrophic loss for any open long. It is silent: the
data arrives, the code runs, the numbers look plausible.

Scale of it, measured:

    365-day window, 40 universe names   3 corrupted (XLU, BN, HDB)
    5-year window, 12 index ETFs        7 corrupted
      XLE   +17.4% raw   vs  +181.7% adjusted
      XLK   +20.4% raw   vs  +150.0% adjusted
      SCHG  -76.7% raw   vs   +91.0% adjusted

`backtest.load_history` now passes `adjustment=Adjustment.ALL`. Any new
code fetching bars must do the same — the default is wrong for every
purpose this project has.

It surfaced because a test said SCHG had lost 43% over five years, which
is simply false. **When a number is impossible, check the data before
believing the finding.** A 200-day-average test had already produced a
confident, entirely fictional conclusion from it.

### Cross-sectional momentum was tested too (2026-08-05)

The strongest remaining candidate, and the opposite of everything else
here: no entry timing, no intraday data, daily bars, monthly rebalance.
Rank nine sector ETFs by 6-month return skipping the recent month, hold
the top three. Sector ETFs deliberately, not universe.csv — that file is
screened today, so testing it over five years is survivorship bias.

Five years, 1,253 sessions, split-adjusted, spanning the 2022 bear
market:

    momentum (top 3)          +10.6% CAGR   Sharpe 0.68   maxDD -20.9%
    equal weight (control)    +12.9%        Sharpe 0.90   maxDD -16.9%
    inverse momentum          +15.1%        Sharpe 0.88   maxDD -23.2%
    SPY buy and hold          +15.9%        Sharpe 0.95   maxDD -22.1%

Ranking lost to not ranking by 2.3 points a year, with a worse Sharpe
and a deeper drawdown. Holding the index beat every variant.

**Re-run at real breadth, because nine ETFs is not the strategy.** The
published work ranks hundreds of individual stocks and holds a decile;
breadth is the mechanism, not a detail. Enumerated 10,918 tradable US
equities, sampled 2,500 across the alphabet, kept the 740 with full
history and median dollar volume above $3M. Same rules, 910 sessions:

    momentum top 74           +24.0% CAGR   Sharpe 0.88   maxDD -27.5%
    equal weight (control)    +37.0% CAGR   Sharpe 1.23   maxDD -20.6%
    bottom 74 (inverse)       +88.1% CAGR   Sharpe 0.65   maxDD -26.1%

Ranking lost by 13 points a year. At nine ETFs it lost by 2.3; at 740
names it lost by 13. **Breadth made it worse, not better.**

The inverse row is NOT a finding and must not be read as one. Every
symbol is listed today, so stocks that collapsed and recovered are in
the sample while those that went to zero are deleted. "Buy the losers"
is measuring the deletion. Survivorship inflates all three rows, which
is what makes the negative result decisive: momentum cannot beat equal
weight even with the failures removed from history in its favour.

That is four families now — entry rules, holding horizons, variance
premium, cross-sectional momentum — each tested against a control, each
losing to doing nothing. Do not re-propose any of them as untested.

### The obvious structural alternative was also tested (2026-08-05)

Having ruled out entry prediction, the next candidate was a structural
edge needing no forecast: the variance risk premium, where implied
volatility sits reliably above realised. It is among the most durable
documented effects in finance, and LOCKBOT had already flagged 1.83x on
a single PCG contract.

It is not present here. Across 30 universe names, near-the-money, 21-45
DTE:

    median IV / realised   0.97
    above 1.0              13/30 = 43%
    mean                   1.10   (WBD 3.93 and PCG 1.92 carry it)

Roughly fair. The mean is an artefact of two event-driven names, which
is what `event_risk.py` exists to detect and refuse.

Two limits on that reading, both real. It compares implied against
TRAILING realised; the premium properly concerns SUBSEQUENT realised,
and Alpaca provides no historical implied volatility, so the correct
test cannot be run with this data at all. And capturing the premium
would mean SELLING options — tail risk, margin, assignment — which this
account cannot support regardless.

Recorded so the idea is not re-proposed as though it were untested.

## Consult LOCKBOT before implementing anything

Standing instruction from the user, 2026-08-06. Not a courtesy — it has
already changed outcomes.

**Before building, ask it.** Describe the proposal, say plainly that
nothing is built yet, and invite disagreement. Ask specifically what
breaks that you have not thought of, and whether it would rather you
built something else with the same time. Tell it to read the source
rather than reason from memory.

It sees the running system every day; a session sees a snapshot. On the
first consultation under this rule — a proposal to compute the ETF
budget from available cash — it found a defect in the design that would
have shipped:

> `build_plan` places a sell whenever `held > wanted`. If free cash ever
> drops below the reserve, `holdings + cash - reserve < holdings`, every
> sleeve target falls below what is held, and the buy-and-hold book is
> mechanically liquidated to rebuild an options reserve.

The fix — make the computed budget buy-only with a floor at current
holdings — came from the consultation, not from me. It also redirected
priority to a larger risk I had not weighed (the flatten paths ignore
reserved symbols) and set a reserve figure with its reasoning attached.

### But verify what it tells you

In the same consultation it stated the broker held zero shares of SCHD
and SCHG. They were held. It had called `equity_positions()` with the
default, which HIDES reserved symbols by design — the exact trap that
function creates for any reader who is not the trading engine.

Two readers made that error within a day of each other, so treat it as a
property of the API rather than a lapse: **anything that is not the
trading engine must pass `include_reserved=True`.**

So: consult first, act on what survives checking, and check the factual
claims rather than the reasoning. The reasoning has been consistently
better than mine. The facts need the same scrutiny as anyone's.

## Start here: `python agent_channel.py`

**Run it at the start of every session, before anything else.** It prints
what LOCKBOT is waiting on. This file is loaded automatically; the channel
is not, so an item nobody reads is an item nobody fixes.

LOCKBOT diagnoses problems it cannot fix — it has no write access to its own
code, by design. On 2026-08-04 it root-caused two real bugs over Telegram,
wrote a patch for each into a sandbox that does not mount this folder, and
both were lost. The next morning it reported, correctly and uselessly, that
both were diagnosed, fixed and unapplied, with no way for anyone to tell it
otherwise. Meanwhile `56.00000000000001` sat in a status dump that had
already been read past. Diagnosis, fix and reader all existed. There was no
wire.

The loop, and every part of it is load-bearing:

    LOCKBOT files          file_for_engineer, with an acceptance test
    you implement          normally, with judgement — see below
    you record             python agent_channel.py --applied <id> --note "..."
    LOCKBOT verifies       verify_fix — it checks, then confirms or REJECTS
    only then              the item closes

**`--applied` does not close an item.** Applying a patch and fixing a bug are
different things, and this is the mechanism that keeps them apart. LOCKBOT's
own PCG fix was a correct diagnosis carrying an incomplete patch: it named
the two code paths it knew about and missed `options_scanner.py`, which mints
the same dirty value for every new position. Applied and closed, that bug
would have been recorded as fixed while still live. The side that reported
the problem is the side that can see whether it went away, so it gets the
last word — including the word "no", which reopens the item with a reason.

**Do not implement a filed item by pasting its suggested fix.** The diagnosis
is usually excellent and the proposed patch is frequently incomplete, for a
structural reason: LOCKBOT reads source through one tool and cannot grep the
whole tree, so it fixes what it can see. Treat the body as a bug report, find
every affected path yourself, and prefer a chokepoint over patching call
sites — that is what turned the PCG fix from two edits into one constructor.

Write the regression test first and watch it fail. The existing
"takes profit exactly at target" check had passed for months against a clean
`70.0` while a real position could not take profit.

A REOPENED item is the most urgent thing on the board: something is on record
as done while the problem is still live.

## `WHAT_WE_LEARNED.md` is the human-facing version

This file is written for you, an AI, and is loaded every session: dense,
repetitive, restating conclusions so they cannot be missed. That makes it
unreadable as a document for the person who owns the project.

`WHAT_WE_LEARNED.md` covers the same findings in plain English, in order,
for someone re-reading in a month. Keep the two in step — when a finding
here changes, change it there. Do not write a second copy of it.

## Conventions

Module docstrings explain **why**, not just what, and record the bug that
motivated the design. Keep that up — it is the most valuable documentation in
the project. Config comments do the same. Functions are small, typed, and use
keyword-only arguments for anything order-related.
