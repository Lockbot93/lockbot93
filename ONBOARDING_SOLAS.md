# Onboarding brief — LOCKBOT

Written 2026-08-13 for a new agent joining the project. Read this before
touching anything.

---

## 0. Read in this order

| # | File | What it is |
|---|---|---|
| 1 | **this file** | current state, and the delta `CLAUDE.md` does not yet cover |
| 2 | `CLAUDE.md` (68 KB) | the canonical AI-facing brief. **Authoritative up to 2026-08-07 and STALE after it** — see §4 |
| 3 | `WHAT_WE_LEARNED.md` | the same findings in plain English, for a human re-reading in a month |
| 4 | `GOVERNANCE.md` | the rules the project holds itself to |
| 5 | `brain_memory.md` (63 KB) | LOCKBOT's own accumulated notes, newest at the end. Its rulings live here |
| 6 | `PREREG_SIP_FEED.md` | a live pre-registration, not yet run |

Then run `python agent_channel.py` — it prints what LOCKBOT is waiting on,
and nothing else surfaces those items.

---

## 1. The single most important thing

**The strategy has no measured edge, and this is not an open question.**

- ~6,700 individual rule variants backtested across ~20 strategy families.
  **Zero survived.** In the largest sweep, 293 rules were expected to clear
  breakeven by chance alone and **0** did.
- Random entry beats the live rule on identical bars, repeatedly.
- The one apparent survivor (`r0315`) replicated across 40 unrelated
  mega-caps and then worked in exactly **one year out of four**.
- The buy-and-hold sleeve has beaten the bot in every comparison made.

The current equity shadow book: **192 decided of 497 logged, 16.1% win
rate** against a ~33% breakeven for a 2:1 bracket.

Do not propose a new entry rule, a new indicator, or another sweep of the
same OHLCV bars. `CLAUDE.md` §"THE BAR" states the three conditions any
new idea must clear first; read it before suggesting anything.

**What this project is actually good at is measurement.** Most of the
recent work has been fixing instruments that were lying, in both
directions. Treat that as the job.

---

## 2. Live state as of 2026-08-13

### Account — CHANGED, `CLAUDE.md` is wrong about this
```
account        PA3CE9V6Q6BZ   (paper)      was PA3VCH4CO55M
equity         $650.00                      was ~$271
options BP     $650.00                      was $44.93
options level  3
positions      0
open orders    0
```
`PAPER_TRADING = True`, `LIVE_TRADING_ENABLED = False`. Invariant: these
stay that way.

The old account's history is archived as
`*.account_PA3VCH4CO55M_20260811`. Account-scoped state was reset so the
new account does not inherit a fiction; the measurement record (shadow
books, signals, audits, `brain_memory.md`) carried across intact and is
**not** account-scoped.

### Universe — CHANGED
```
150 symbols          was ~42
price   $1 - $250    was $5 - $50
ATR     0.001 - 0.50 %/day    was 1.25 - 3.00 %/day
pool generation  pool_9ef18a5b    was pool_d11f4dfa
```

### Running
```
LockBotController        Running   PID 24472   the only long-running supervisor
lockbot_telegram.py --run          PID 32320   can place orders (paper), allowlist of one
lockbot_brain.py --chat --wake     PID 23768   voice/chat
scheduled: ETF Portfolio, Learning Pass, Shadow Resolve, Universe Rebuild,
           Wake, Watchdog   (Weekday Start disabled)
```

### Known blocker
**The Anthropic API balance is empty.** `lockbot_brain.py` returns a 400
credit error, so LOCKBOT cannot be consulted, the nightly learning pass is
down, and Telegram answers fail. Trading is unaffected — the scanner, risk
gates and options stop loss do not use that key.

---

## 3. Modules that did not exist on 2026-08-07

| Module | Purpose | Tests |
|---|---|---|
| `manual_trades.py` | records the OWNER's own option trades; `--plan` before entry, `--sync` pulls fills from the broker | 109 |
| `execution_cost.py` | realized vs quoted spread, mid-limit fills, adverse selection, time-of-day | 98 |
| `sip_audit.py` | Phase 0 of the SIP pre-registration; re-resolves the shadow book on SIP, report-only | 14 |
| `horizon_exit_test.py` | a pre-registered test, already run and **failed** | 11 |
| `ONBOARDING_SOLAS.md`, `PREREG_SIP_FEED.md` | this file, and a live registration | — |

Every module carries `--self-test`, offline, no network or account needed.
Run them first; they are the fastest way to learn what a module promises.
Current counts: `manual_trades` 109, `execution_cost` 98, `shadow_trades`
60, `options_shadow` 40, `options_contracts` 33, `sip_audit` 14,
`horizon_exit_test` 11.

`manual_trades.csv` and `execution_cost`'s three CSVs are **empty**. Both
are correct harnesses with no data yet. Nothing writes the execution-cost
inputs; naming that writer is an open decision.

---

## 4. What `CLAUDE.md` now gets WRONG

It was last written 2026-08-07. Trust its reasoning; check its numbers.

| It says | Actually |
|---|---|
| account ~$250 | $650, and a different account |
| options BP $44.93, "contracts under about $71" | $650 BP; ceiling now the full-debit cap at $65 |
| universe ~45 names, $5–$50, 1.25–3%/day | 150 names, $1–$250, effectively unbanded |
| shadow 162 of 361, 16.7% | 192 of 497, 16.1%, plus 59 EXPIRED marked to market |
| options shadow 40% | **28.6%** against a 41.2% breakeven |
| "ambiguous bars count as losses" (only) | still true, but the report now prints both bounds |

### Findings made since, not in `CLAUDE.md`

- **The horizon-exit hypothesis was pre-registered, run once, and FAILED**
  2 of 3 kill criteria. Fixed-horizon exit lost to a seeded random control
  (+0.561 vs +0.673) and to the sleeve by 2.5 points per window.
- **`ALPACA_DATA_FEED = "iex"`** — the 864-rule search, the 5,856-rule
  sweep, `r0315`, and the whole shadow book were measured on ~4% of
  consolidated volume. `calendar_timing.py` and `news_signal.py` hardcode
  SIP. The entry-rule conclusion has never been measured on the real tape,
  and SIP **history is already free on this plan**. See `PREREG_SIP_FEED.md`.
- **The day horizon is a bar count, not a clock.** `max_bars_held = 78`
  spans ~2 sessions on IEX, so "day trades" have been held through the
  close. Filed, **not fixed** (`78103175`).
- **Two censoring bugs, opposite directions.** Aged-out equity rows were
  dropped (recorded figure was *pessimistic*, −0.46R → −0.33R all-in);
  options `EXPIRED` never fired in 42 rows (recorded figure was
  *optimistic*, +$5.05 → −$12.95).
- **Spread drag on options is 5.88× the entire gross modelled result.**
- **The four completed equity trades carry no information** — all four
  exited `EXTERNAL_CLOSE` from a manual `close_all.py` and a Telegram
  self-test, nowhere near either bracket leg.

---

## 5. Rules that bind

1. **`lockbot_config.py` is the single source of truth.** Never define a
   local copy of a shared setting. Import it. This has been violated three
   times and caused real bugs each time.
2. **`market_scanner.py` is the only module that submits equity orders**;
   `options_scanner.py` the only one for options. Everything else says so
   in its docstring. Keep that true.
3. **One exit mechanism per instrument, one owner each.**
   `options_manager.py` is the sole options exit authority and there is no
   broker-side options stop — if it stops running, open contracts have no
   stop of any kind.
4. **A default value is a claim.** Return `None`, never `0.0`, when a value
   cannot be computed. This has bitten four times.
5. **A measurement change may ADD a parallel reading; it must never
   overwrite a recorded verdict.** Re-basing a headline needs an explicit
   owner decision.
6. **Any change to the scan population needs the pool-generation tag**
   (already built — it derives from the config constants, so it updates
   itself).
7. **Consult LOCKBOT before implementing** — and then verify what it says.
   Its reasoning has been better than mine repeatedly; its facts need the
   same scrutiny as anyone's.
8. **Never judge a rule against breakeven alone.** Always against a seeded
   random control on the same bars.

### A defect family worth knowing — it has appeared three times
Appending with a `DictWriter` over a module-level `COLUMNS` list that is
wider than the file's existing header writes values with no header above
them; `DictReader` returns them under a `None` key and the column is
silently unreadable. Hit `options_scanner` (15 under 14) and
`shadow_trades` (26 under 25). **A shared migration helper is the next
piece of work and does not yet exist.**

---

## 6. Open items

`python agent_channel.py` — **0 open, 0 reopened, 5 awaiting LOCKBOT's
verification.** All five need the Anthropic balance restored before they
can close:

```
8e24ae42  variable reward-to-risk in the strategy lab
a002c6a0  holding-horizon support (day / swing / overnight)
b16e2f2a  TELEGRAM_TRADING_REQUIRES_PAPER is a readable escape hatch
39a7685e  MAE/MFE excursion fields on shadow rows
c6812f3a  broad-market expansion  <- applied 2026-08-13, awaiting verification
```

### Not to be done
- No rewrite. The code is not what failed.
- No further rule sweep on the same bars — more search manufactures false
  positives at a documented rate.
- No live funding, no change to `PAPER_TRADING`.
- No optionable-only universe filter. It was investigated and is
  **unnecessary**: all 150 symbols have chains in the traded window, and
  the per-contract gate already handles options liquidity.

### What would actually help
1. A non-price data source — the only thing that reopens condition (a) of
   THE BAR. EDGAR 8-K filing dates are free and need no API key.
2. The illiquidity premium revisited **with a spread model attached** —
   the project's own note calls it "the closest thing to a real edge found,
   and the reason it fails is transaction costs rather than absence of
   signal."
3. More resolved shadow rows. The universe just tripled, which should
   roughly triple intake.

---

## 7. Environment

Windows. `run_lockbot.bat` launches `lockbot_controller.py` under
`.venv\Scripts\python.exe` — **always use that interpreter**, the project
has no other. `git` is not on PATH; it is at
`C:\Program Files\Git\cmd\git.exe`. Dependencies are pinned in
`requirements.txt` (52 packages). Secrets live in `.env`, which is
gitignored; `.env.example` is the template.

Nothing in this project is committed to git yet from the recent work.
