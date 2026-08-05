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

The empty `ai/`, `broker/`, `config/`, `execution/`, `strategy/`, `tests/`
directories are unused scaffolding. The real layout is flat.

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
files so the two strategies can be judged independently — which does mean
`daily_report.py` does not yet include options.

Paths live in `lockbot_config.py` — read them from there, never hardcode.

Note the `*_OLD`, `*_backup`, `*_v0_N` files scattered through the root. They
are dead copies, not imports. `market_scanner.py` is live;
`market_scanner_OLD.py`, `market_scanner_refracted,py` (sic) and the rest are
not.

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

## Conventions

Module docstrings explain **why**, not just what, and record the bug that
motivated the design. Keep that up — it is the most valuable documentation in
the project. Config comments do the same. Functions are small, typed, and use
keyword-only arguments for anything order-related.
