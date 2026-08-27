"""
LOCKBOT Central Configuration v1.4

This module is the single source of truth for shared LOCKBOT settings.
Every module below imports its shared settings from here instead of
defining its own local copy — that was the root cause of several bugs
found during the v1.0 audit (mismatched risk limits, mismatched stop
percentages, and a journal-filename mismatch that silently broke
performance reporting).

Unit convention: every *_PERCENT constant below is a FRACTION
(0.02 means 2%), matching how these values are used directly in the
trading math throughout the codebase, e.g.:
    risk_dollars = account_equity * MAX_RISK_PER_TRADE_PERCENT

Important:
- PAPER_TRADING should remain True during validation.
- ENABLE_PAPER_EXITS should remain False. Bracket orders (submitted by
  market_scanner.py) are LOCKBOT's sole exit mechanism. position_monitor.py
  is monitoring/alerting only and must never submit its own exit order
  while this remains the design — see position_monitor.py's docstring.
- LIVE_TRADING_ENABLED should remain False until paper testing is complete.

v1.2 changes (universe scanning + multiple concurrent positions):
- MAX_OPEN_POSITIONS raised from 1 to 5.
- MAX_TRADES_PER_DAY raised from 4 to 10.
- MAX_TOTAL_EXPOSURE_PERCENT raised from 0.20 to 0.50. This one mattered
  most: at 0.20, with each position allowed 0.10, the account could only
  ever hold two full-size positions no matter what MAX_OPEN_POSITIONS said.
- New "Universe scanning" section. market_scanner.py now takes its symbol
  list from universe.csv (built each morning by universe.py) instead of
  the SYMBOLS list, which is kept as a fallback.
- New MAX_SAME_DIRECTION_POSITIONS cap. Several positions all facing the
  same way are one bet, not diversification.
- validate_configuration() now catches a position cap that the exposure
  ceiling makes unreachable, and a same-direction cap set above the
  position cap.

v1.4 changes (movement filter + adaptive brackets):
- UNIVERSE_MIN_ATR_PERCENT / UNIVERSE_MAX_ATR_PERCENT, used by
  universe_volatility.py to drop symbols that cannot reach the target
  (bond ETFs) or whose normal daily wiggle is wider than the stop.
- USE_ADAPTIVE_BRACKETS plus four ATR_* constants, used by
  adaptive_brackets.py to size the stop, the target, and the share count
  per stock instead of applying one fixed 2%/4% bracket to everything.
  Defaults to False — nothing changes until it is switched on.
- validate_configuration() now range-checks all six of those, so a typo
  like 0.30 where 0.030 was meant fails loudly instead of silently
  filtering the entire universe away.

Risk note: each position is capped at 10% of equity with a 2% stop, so a
single trade risks roughly 0.2% of the account. Five at once is about 1%.

To revert to the old two-symbol, one-position behaviour, set
USE_UNIVERSE_FILE = False and MAX_OPEN_POSITIONS = 1.
"""

from __future__ import annotations

from pathlib import Path


# ============================================================
# Project identity
# ============================================================

LOCKBOT_PROJECT_VERSION = "0.9"
LOCKBOT_CONFIG_VERSION = "1.4"

PROJECT_FOLDER = Path(__file__).resolve().parent


# ============================================================
# Trading environment
# ============================================================

PAPER_TRADING = True
LIVE_TRADING_ENABLED = False

# Equity ENTRIES. False means market_scanner.py still scans, still ranks,
# and still writes shadow trades — it simply submits no share orders.
#
# Set False on 2026-07-30 so the account's capital goes to options. The
# scanning deliberately continues: the shadow log is the only measurement
# of whether the signal engine works, and it costs nothing to keep
# collecting while the money is deployed elsewhere. Turning the scanner
# off entirely would stop the clock on the one experiment that matters.
#
# Existing positions are unaffected — this gates new entries only.
#
# Back to True on 2026-08-07 at the owner's instruction, relayed by
# LOCKBOT as agent_channel bd26ffca and confirmed with him directly. He
# wants the account trading end to end and visible.
#
# Deliberately WITHOUT the other half of that directive: OPTIONS_SHADOW_MODE
# stays True. That was LOCKBOT's own recommendation and the owner took it —
# equity shadow reads 16.7% wins over 162 resolved against a 33.3%
# breakeven, options 20.2% against a 41.2% breakeven, so options lose
# faster per trade. Equity first cuts the expected bleed to roughly a
# fifth while still showing the whole machine working.
#
# Every entry from here carries a horizon tag (trade_horizon.py), so this
# is also the first data that can be grouped by holding period.
#
# ---------------------------------------------------------------------
# OFF again 2026-08-14, owner's instruction: options only.
#
# Note what this reverses. The 08-07 note above records the owner taking
# LOCKBOT's recommendation to run EQUITY first and keep options in shadow,
# on the reasoning that options lose faster per trade -- 20.2% against a
# 41.2% breakeven versus 16.7% against 33.3%. The account is now doing
# the opposite of that on both halves: options live, equity entries off.
# The owner was shown the current figures and chose it; recorded here so
# the reversal is legible rather than looking like drift.
#
# WHAT THIS DOES NOT STOP, and the distinction matters:
#   market_scanner still SCANS and still SHADOW-LOGS every setup, so the
#   equity shadow book -- 497 rows, 192 decided, the measurement engine
#   every open question in this project depends on -- keeps accumulating
#   at the new 150-symbol rate. Only order submission stops.
#   Existing positions are untouched. T and IEMG hold working broker-side
#   bracket legs; trade_manager and position_monitor keep reconciling and
#   journalling them.
#   The buy-and-hold sleeve is a SEPARATE switch (ETF_PORTFOLIO_ENABLED)
#   and is deliberately left alone -- it is not the trading engine, it is
#   52% of the account, and it is the only thing here that has
#   outperformed. Turning it off is its own decision.
#
# ROLLBACK: set back to True.
# ---------------------------------------------------------------------
EQUITY_ENTRIES_ENABLED = False

# Bracket orders are LOCKBOT's sole exit mechanism. Keep this False —
# position_monitor.py must stay monitoring/alerting only. See its
# module docstring for the full rationale.
ENABLE_PAPER_EXITS = False


# Whether LOCKBOT may place orders from a REMOTE (Telegram) session.
#
# Set True on 2026-08-07 at the owner's explicit and repeated
# instruction, after the trade-off was put to him directly.
#
# WHAT THIS GIVES UP. lockbot_telegram.py hardcoded READ_ONLY = True with
# the reasoning that a leaked bot token must not be able to move money,
# regardless of what is asked or how convincingly. That reasoning is
# sound and has not been refuted — it has been overruled for a paper
# account, which is a different judgement. The remaining access control
# is TELEGRAM_ALLOWED_USER_IDS, an allowlist of one.
#
# WHY IT IS A FLAG RATHER THAN A DELETED LINE. The wall is worth being
# able to put back in one word, and worth being visible in
# `python lockbot_config.py` rather than buried in a handler.
TELEGRAM_TRADING_ENABLED = True

# The part that is NOT the owner's to turn off, and no longer a switch.
#
# Remote order authority is permitted ONLY while PAPER_TRADING is True.
# The decision above was made about fake money; it must not silently
# become a decision about real money the day LIVE_TRADING_ENABLED flips.
#
# This WAS a flag, and LOCKBOT filed the hole in it (agent_channel
# b16e2f2a) rather than vetoing: the guard's own off-switch sat in the
# very file somebody edits on the day they go live, so "going live
# re-raises the wall automatically" was true only if a second flag had
# never been touched — precisely the silent persistence the guard exists
# to prevent.
#
# The check is unconditional in lockbot_telegram.remote_trading_allowed()
# and this setting is no longer read. It is kept only so that anyone who
# set it True and expected it to matter finds this note instead of a
# silent no-op.
TELEGRAM_TRADING_REQUIRES_PAPER = True  # not consulted; see the note above


# ------------------------------------------------------------
# Holding horizons
# ------------------------------------------------------------
#
# Owner directive 2026-08-07: LOCKBOT must be able to take a mix of day
# trades, swings and overnight holds. Before this it had no concept of
# one — every trade rode its bracket for whatever ~23–25 hours it took,
# which is an accidental overnight hold and was recorded as nothing.
#
# See trade_horizon.py for what each horizon changes. In short: the stop
# width, the maximum hold, and whether the position must be flat by the
# close. Entry selection is untouched.
#
# THIS IS PLUMBING, NOT A STRATEGY. Every swing entry rule tested in the
# lab is negative, and the horizon tag exists so results can finally be
# GROUPED by holding period — not because any period is known to work.

# One of "mixed", "day", "overnight", "swing". "overnight" reproduces the
# behaviour LOCKBOT had before horizons existed.
EQUITY_HORIZON_POLICY = "mixed"

# The rotation used when the policy is "mixed". A deliberately
# uninformative sampling scheme, not a judgement about which horizon is
# better — choosing per setup would be a new entry-side model, and after
# seventeen failed entry families that is not something to invent here.
#
# Day appears once in five because PDT allows three round trips per five
# business days under $25,000, so that is roughly the real day-horizon
# capacity. A rotation producing more would simply be blocked, and a
# blocked entry teaches nothing.
EQUITY_HORIZON_MIX = ("overnight", "swing", "day", "overnight", "swing")

# The software time stop for day-horizon equity positions, owned by
# equity_time_stop.py.
#
# Options have no broker-side stop, which is why options_manager.py
# exists. Day-horizon equities have the opposite problem: they have a
# bracket, and the time stop must not race it. The module cancels the
# bracket, confirms the cancel, and only then closes — never both at once.
EQUITY_TIME_STOP_ENABLED = True

# How long before the close a day-horizon position is flattened. Wide
# enough that a cancel-then-close round trip completes comfortably; the
# controller only wakes every SCAN_INTERVAL_SECONDS (300), so anything
# under about 10 minutes could be missed entirely.
DAY_HORIZON_FLATTEN_MINUTES_BEFORE_CLOSE = 15

ALPACA_API_KEY_ENV = "ALPACA_API_KEY"
ALPACA_SECRET_KEY_ENV = "ALPACA_SECRET_KEY"


# ============================================================
# Market universe
# SYMBOLS is now the FALLBACK list. It is used when
# USE_UNIVERSE_FILE is False, or when universe.csv is missing or
# unreadable. These symbols are also always appended to the scan
# list so SPY and QQQ stay covered.
# ============================================================

SYMBOLS = ["SPY", "QQQ"]


# ============================================================
# Universe scanning
# The real symbol list is built each morning by universe.py and
# saved to universe.csv. Schedule 'python universe.py' to run on
# weekday mornings before the market opens.
# ============================================================

USE_UNIVERSE_FILE = True

# Symbols per bar request. Do not add a `limit` to those requests —
# Alpaca applies limit as a TOTAL across all symbols, which starves
# every symbol when many are requested at once.
SCAN_BATCH_SIZE = 100

# Bar history pulled per cycle. The 5-minute lookback covers every
# scanned symbol, so keep it short; the higher timeframes are only
# fetched for the few candidates that pass stage one.
SCAN_LOOKBACK_DAYS_5M = 3
SCAN_LOOKBACK_DAYS_HIGHER = 15

# Warn when universe.csv has not been rebuilt recently. The scanner
# still uses a stale file rather than refusing to trade.
UNIVERSE_STALE_HOURS = 30

# Console detail. Full per-symbol printouts across hundreds of
# symbols make the log unreadable, so detail is reserved for
# candidates that reach stage two.
VERBOSE_SYMBOL_LOGGING = False


# ============================================================
# Universe builder settings (used by universe.py)
# ============================================================

# UNIVERSE_MIN_PRICE, UNIVERSE_MAX_PRICE, UNIVERSE_TOP_N and
# MAX_SCAN_SYMBOLS are set by ACCOUNT_PROFILE below.
UNIVERSE_LOOKBACK_DAYS = 20
UNIVERSE_MIN_BARS = 15

# Minimum and maximum average daily movement (used by
# universe_volatility.py). A stock that moves less than 1.25% a day
# can't reach the 4% target; one that moves more than 3% a day has a
# normal wiggle bigger than the 2% stop.
#
# IMPORTANT: universe.py rewrites universe.csv from scratch, so the
# daily order is universe.py FIRST, then universe_volatility.py.
# BROAD MARKET, 2026-08-13 (agent_channel c6812f3a). Was 1.25%-3.00%/day.
#
# This band, not UNIVERSE_TOP_N, was what actually sized the pool: TOP_N
# has been 150 all along and the volatility filter cut it to about 42. So
# widening here is the change that opens the universe, and it does so by
# roughly 3.5x without touching any rate limit.
#
# Set wide rather than removed, so the filter still runs, still writes
# universe_volatility_report.csv, and can be re-narrowed by editing two
# numbers. They cannot go to zero -- validate_configuration requires
# 0 < min < 0.5 and 0 < max < 1.0 -- so these are the practical
# equivalent of off.
UNIVERSE_MIN_ATR_PERCENT = 0.001
UNIVERSE_MAX_ATR_PERCENT = 0.500

# 0 means rank by liquidity rather than apply an absolute floor.
# Keep it at 0 while using the free IEX feed, which reports only a
# fraction of each stock's real volume.
UNIVERSE_MIN_AVG_DOLLAR_VOLUME = 0

UNIVERSE_ALLOWED_EXCHANGES = ["NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"]

# Symbols to keep out of the universe regardless of liquidity.
# Leveraged, inverse, and volatility products are meant to be excluded
# automatically by fund name, but that filter has a known gap — RWM, an
# inverse Russell 2000 fund, passed straight through it. Add anything
# else here, e.g. "BITO".
UNIVERSE_EXCLUDE_SYMBOLS = []

UNIVERSE_FILE = PROJECT_FOLDER / "universe.csv"
UNIVERSE_BATCH_SIZE = 200
UNIVERSE_BATCH_PAUSE_SECONDS = 0.35

# Data feed used for both the universe build and the scan.
# "iex" is the free feed. Switch to "sip" only with a paid data plan.
#
# This one must STAY "iex". It is what the live scanner can actually see:
# live SIP is refused on this plan, so a universe or a scan built on SIP
# would describe a bot that cannot be deployed.
ALPACA_DATA_FEED = "iex"

# NOT a setting: a SHADOW_DATA_FEED constant was added here on 2026-08-10
# and removed the same night on LOCKBOT's ruling. An unwired constant is
# the b16e2f2a defect class — a switch that reads as configuration while
# controlling nothing. Reinstate it only together with the code that
# consults it, and only after the owner has decided on the feed.


# ============================================================
# Adaptive brackets (used by adaptive_brackets.py)
#
# OFF BY DEFAULT. While USE_ADAPTIVE_BRACKETS is False, every trade
# uses BRACKET_STOP_LOSS_PERCENT and BRACKET_TAKE_PROFIT_PERCENT
# below, exactly as LOCKBOT has always done. Setting it back to
# False is the complete rollback — no other change is needed.
#
# When True, each trade's stop is sized to the stock's own average
# daily movement, and the share count comes DOWN as the stop widens
# so that the dollars at risk per trade stay where
# MAX_RISK_PER_TRADE_PERCENT put them. Widening the stop without
# shrinking the position would quietly multiply risk per trade.
# ============================================================

USE_ADAPTIVE_BRACKETS = True

# Stop = this many times the stock's average daily movement.
# At 1.0 the stop sits exactly at a typical day's range, so ordinary
# noise reaches it about half the time.
ATR_STOP_MULTIPLIER = 1.5

# Target = this many times the stop. 2.0 keeps the existing 2%/4%
# shape, so the win-rate arithmetic is unchanged.
ATR_REWARD_RATIO = 2.0

# Floor and ceiling on the stop, whatever the measurement says. The
# floor keeps the stop outside spread and jitter on very quiet names;
# the ceiling stops one wild name from eating the account.
ATR_MIN_STOP_PERCENT = 0.015
ATR_MAX_STOP_PERCENT = 0.060


# ============================================================
# Scheduling
# ============================================================

SCAN_INTERVAL_SECONDS = 300
# POSITION_MONITOR_INTERVAL_SECONDS, TRADE_MANAGER_INTERVAL_SECONDS and
# HEALTH_MONITOR_INTERVAL_SECONDS stood here at 300 each until 2026-08-19.
# Deleted on LOCKBOT's ruling (item 1c2b28b6).
#
# They said each module has its own cadence. None of them ever did. The
# controller runs all four scripts in sequence inside ONE cycle governed by
# SCAN_INTERVAL_SECONDS -- see lockbot_controller.py, which calls
# SCANNER_FILE, TRADE_MANAGER_FILE, POSITION_MONITOR_FILE and the two
# options scripts one after another.
#
# The lie was harmless only because all three read 300, the same as
# SCAN_INTERVAL_SECONDS. It would have become real the moment somebody
# edited one expecting an effect. Worse, two of them were published into
# the state snapshot below, so LOCKBOT was being told a schedule that does
# not exist -- which is how a wrong belief reaches the thing doing the
# reasoning.
PREMARKET_CHECK_SECONDS = 60


# ============================================================
# Signal / entry thresholds
# Unchanged. These stay untouched until real completed-trade data
# exists to evaluate them against.
# ============================================================

MIN_SIGNAL_CONFIDENCE = 80
MIN_VOLUME_RATIO = 1.10


# ============================================================
# Setup ranking (used by signal_quality.py)
#
# When more setups are approved than there are position slots,
# something has to choose. That choice was previously:
#
#   sort by (confidence, volume_ratio), highest first
#
# and it was broken in a way that is worth recording. The
# confidence score awards 20 points each for the same five
# conditions that define an entry, so every approved setup scores
# exactly 100 — all 157 in the shadow log. A constant primary key
# means the TIEBREAKER was the real ranking, and the tiebreaker was
# volume_ratio, which the shadow data shows degrading as it rises:
#
#   1.10-1.25   37.5% wins   +0.125 R    (8 trades)
#   1.25-1.75   26.7% wins   -0.20 R     (15 trades)
#   1.75+       25.0% wins   -0.25 R     (32 trades)
#
# So LOCKBOT was picking trades by the one measure pointing the
# wrong way. USE_QUALITY_RANKING replaces that with a continuous
# score built from measures the entry rules do NOT already use.
#
# Setting it False restores the old (confidence, volume_ratio)
# sort exactly — that is the complete rollback.
# ============================================================

USE_QUALITY_RANKING = True

# Weights are PRIORS, not fitted values. Fitting them to 55 resolved
# trades would describe one week and nothing else. They are equal
# across the four components so that no unproven belief is baked in.
#
# volume_ratio sits at zero deliberately: it is still computed and
# written to the shadow log on every setup so it stays measurable,
# but a factor whose only evidence is negative should not steer live
# decisions while it is being re-measured. Raise it once the data
# says something different.
SIGNAL_QUALITY_WEIGHTS = {
    "trend_strength": 1.0,   # ADX — entry rules never look at it
    "momentum": 1.0,         # MACD histogram width, in ATR units
    "conviction": 1.0,       # +DI / -DI spread
    "restraint": 1.0,        # distance past VWAP — high means chasing
    "volume_ratio": 0.0,     # measured, not trusted
}


# ============================================================
# Bracket order exit legs
# This is LOCKBOT's authoritative, sole exit mechanism.
# When USE_ADAPTIVE_BRACKETS is True these become the FALLBACK,
# used for any symbol with no movement data on file.
# ============================================================

BRACKET_STOP_LOSS_PERCENT = 0.02
BRACKET_TAKE_PROFIT_PERCENT = 0.04


# ============================================================
# Position-monitor informational thresholds
# position_monitor.py uses these ONLY to decide when to alert —
# it never submits its own exit order while bracket orders remain
# the sole exit mechanism (see ENABLE_PAPER_EXITS above).
# ============================================================

BREAK_EVEN_TRIGGER_PERCENT = 0.005
TRAILING_STOP_TRIGGER_PERCENT = 0.01
TRAILING_STOP_DISTANCE_PERCENT = 0.005
MONITOR_STOP_LOSS_PERCENT = -0.005


# ============================================================
# ACCOUNT PROFILE  <<< the one switch that changes everything
#
#   "standard" — the ~$100K paper account. Five positions, shorts
#                allowed, the full 300-name universe.
#
#   "small"    — a $250 account. Three real-world rules bite at
#                this size, and none of them are optional:
#                  1. Whole shares only. $250 cannot buy a share
#                     of a $400 stock, so the universe has to be
#                     cheap stocks and each position takes a much
#                     bigger slice of the account.
#                  2. No shorting under $2,000 of equity. That is
#                     a brokerage rule, not a setting.
#                  3. Pattern day trader rule: under $25,000 you
#                     get 3 same-day round trips per 5 business
#                     days. LOCKBOT opened 5 positions in 15
#                     minutes on 7/27, so this is the tightest
#                     limit of the three.
# ============================================================

ACCOUNT_PROFILE = "small"


if ACCOUNT_PROFILE == "small":

    # Sizing. A $250 account with a 40% cap gives $100 per position,
    # enough for whole shares of a cheap stock. Risk per trade is
    # 40% x 2% = 0.8% of the account, about $2.
    MAX_RISK_PER_TRADE_PERCENT = 0.01
    MAX_POSITION_VALUE_PERCENT = 0.40

    # Two positions, no margin assumed.
    MAX_OPEN_POSITIONS = 2
    # Raised to 10 at the user's request. The broker's day-trade limit
    # (MAX_DAY_TRADES_PER_5_DAYS below) will usually bite first, since an
    # account under $25,000 only gets 3 same-day round trips per 5 business
    # days. Trades held overnight are not day trades and don't count.
    MAX_TRADES_PER_DAY = 10
    MAX_TOTAL_EXPOSURE_PERCENT = 0.80

    # Raised from 0.02 to 0.10 on 2026-08-20, on the owner's direct
    # instruction: "I don't want Lockbot to lock itself out of trades for
    # the rest of the day. Take the blows and move forward."
    #
    # It had been a runtime override since that morning. Made permanent
    # here because an override is a file, and a file can be deleted,
    # emptied or fail to parse -- at which point the limit silently
    # reverts to 0.02 and the lockout the owner overrode comes back
    # without anyone touching a setting. A decision that matters should
    # not depend on a JSON file surviving.
    #
    # WHAT THIS GIVES UP, stated plainly rather than buried: the daily
    # circuit breaker is now five times looser, and at this account size
    # the per-trade caps are the only real brakes -- the full-debit
    # ceiling at 10% of equity, and the -35% software stop. The owner
    # chose throughput over the breaker knowing that.
    #
    # The "standard" profile below keeps 0.02 deliberately. Ten percent of
    # a ~$100K account is a different quantity of money and this reasoning
    # does not carry across.
    MAX_DAILY_LOSS_PERCENT = 0.10

    MAX_SAME_DIRECTION_POSITIONS = 2
    MAX_NEW_ENTRIES_PER_CYCLE = 1

    # Long only. Shorting is not permitted below $2,000 of equity.
    ALLOW_SHORT_ENTRIES = False

    # Stop opening new positions once the broker's rolling 5-day
    # day-trade count reaches this. Read straight from Alpaca's own
    # daytrade_count field, so it matches what the broker enforces.
    # Set to 0 to disable the check.
    MAX_DAY_TRADES_PER_5_DAYS = 3

    # BROAD MARKET, 2026-08-13 (agent_channel c6812f3a).
    #
    # Was $5-$50, on the reasoning "cheap stocks only, or nothing is
    # affordable; a $50 ceiling means every position buys at least two
    # shares." That was written at ~$250 of equity and is now stale.
    #
    # The owner's directive is to stop excluding by this band and by the
    # 1.25-3.00%/day volatility band. Both are widened rather than deleted,
    # so the mechanism survives and the pool can be re-narrowed later
    # without restoring code.
    #
    # The ceiling is not arbitrary: at MAX_POSITION_VALUE_PERCENT of 0.40
    # a $650 account can put $260 into one position, so above roughly that
    # price a name cannot be bought at even one whole share. $250 keeps
    # nearly everything in the universe theoretically tradable instead of
    # spending scan slots on names that can never fill. REVISIT THIS IF
    # EQUITY MOVES MATERIALLY -- it is a function of account size, not a
    # property of the market.
    #
    # Expect POSITION_SIZE_CALCULATION_FAILED rejections to rise anyway.
    # That is the sizing gate doing its job on a wider pool, not a fault.
    UNIVERSE_MIN_PRICE = 1.00
    UNIVERSE_MAX_PRICE = 250.00
    UNIVERSE_TOP_N = 150
    MAX_SCAN_SYMBOLS = 150

else:  # "standard"

    MAX_RISK_PER_TRADE_PERCENT = 0.01
    MAX_POSITION_VALUE_PERCENT = 0.10

    # Raised 7/27. On the first live day all five slots filled within 15
    # minutes while only 5 of 10 permitted trades were used — the position
    # count was the real limit, not the daily count, so both moved up.
    # Eight positions at 10% each is 80% exposure and about 1.6% total risk.
    MAX_OPEN_POSITIONS = 8
    MAX_TRADES_PER_DAY = 16
    MAX_TOTAL_EXPOSURE_PERCENT = 0.80
    MAX_DAILY_LOSS_PERCENT = 0.02
    MAX_SAME_DIRECTION_POSITIONS = 5
    MAX_NEW_ENTRIES_PER_CYCLE = 3

    ALLOW_SHORT_ENTRIES = True
    MAX_DAY_TRADES_PER_5_DAYS = 0

    UNIVERSE_MIN_PRICE = 5.00
    UNIVERSE_MAX_PRICE = 2000.00
    UNIVERSE_TOP_N = 300
    MAX_SCAN_SYMBOLS = 300


# ============================================================
# Heartbeat thresholds
# ============================================================

HEARTBEAT_WARNING_MINUTES = 10
HEARTBEAT_CRITICAL_MINUTES = 20

# CONTINUOUS_MODULES, SCHEDULED_MODULES and ON_DEMAND_MODULES stood here
# until 2026-08-19. Deleted on LOCKBOT's ruling (item 1c2b28b6).
#
# Nothing read them. They described a scheduling model the controller does
# not use.
#
# WHERE THE REAL SCHEDULE LIVES: lockbot_controller.py. It holds the script
# paths (SCANNER_FILE, TRADE_MANAGER_FILE, POSITION_MONITOR_FILE,
# HEALTH_MONITOR_FILE, OPTIONS_MANAGER_FILE, OPTIONS_SCANNER_FILE) and runs
# them in a fixed sequence inside one cycle. There is no per-module
# schedule and no module-group dispatch anywhere.
#
# This pointer exists because these sets had already misled a reader who
# should know better: LOCKBOT's own 2026-08-08 watchdog ruling said
# equity_time_stop would be "added to SCHEDULED_MODULES", as though that
# set did something. It never did. Look in the controller.



# ============================================================
# Notifications
# ============================================================

# Seven NOTIFY_ON_* flags stood here, all True, until 2026-08-19.
# Deleted on LOCKBOT's ruling (item 1c2b28b6), which called them the most
# dangerous of the unwired set -- and the reason is worth keeping.
#
# Nothing read any of them. They read as owner-usable switches, so somebody
# quietening a noisy channel by setting NOTIFY_ON_HEARTBEAT_DEGRADED=False
# would believe they had done something, and would keep being paged. A
# constant that looks like a control and is not is worse than no constant,
# because it costs the reader their next hour rather than their next minute.
#
# If notification routing is worth having it returns as ONE item shipping
# the flags AND the dispatch code together, with a self-test proving a
# False flag actually suppresses the send. Never the flags first.
#
# Notification behaviour today lives in notifications.send_smart_notification,
# which de-duplicates by symbol and event type on a cooldown. That is the
# real control surface.


# ============================================================
# Data files
# These paths match what trade_journal.py and trade_grader.py
# actually read and write. (v1.0 pointed COMPLETED_TRADES_FILE at
# "lockbot_trade_journal.csv" — a file nothing ever wrote to. Fixed.)
# ============================================================

POSITION_STATE_FILE = PROJECT_FOLDER / "position_state.json"
PENDING_TRADES_FILE = PROJECT_FOLDER / "lockbot_pending_trades.csv"
COMPLETED_TRADES_FILE = PROJECT_FOLDER / "completed_trades.csv"
HEARTBEAT_FILE = PROJECT_FOLDER / "lockbot_heartbeat.json"
RISK_STATE_FILE = PROJECT_FOLDER / "risk_state.json"
NOTIFICATION_STATE_FILE = PROJECT_FOLDER / "notification_state.json"
SIGNALS_FILE = PROJECT_FOLDER / "signals.csv"

# What was said, so the next answer knows it. Added 2026-08-04 -- every
# Telegram message was answered from a standing start until then.
#
# Credentials are stripped before anything is written here, but this is
# still a plaintext record of everything typed at a system holding live
# broker keys. It is not in the same class as the trading data files:
# deleting it loses conversation history and nothing else.
CONVERSATION_LOG_FILE = PROJECT_FOLDER / "conversation_log.jsonl"

# Messages between LOCKBOT and whoever edits its code. Added 2026-08-05
# after LOCKBOT diagnosed two real bugs over Telegram, wrote a patch for
# each into a sandbox nothing else can read, and had no way to hand them
# over or to be told when they were applied.
AGENT_CHANNEL_FILE = PROJECT_FOLDER / "agent_channel.jsonl"

# Who decided what, between LOCKBOT and whoever edits its code. Added
# 2026-08-06 when LOCKBOT was given binding veto and halt authority over
# changes to the project. Vetoes, the engineer's overrides of them, halts,
# the agenda LOCKBOT keeps, and departures from it. See governance.py and
# GOVERNANCE.md.
#
# Nothing in the trading path reads this file. It cannot affect a cycle.
GOVERNANCE_FILE = PROJECT_FOLDER / "governance.jsonl"

# Push to the phone when LOCKBOT files something needing a code change.
#
# LOCKBOT can reach its engineer but cannot summon one: items sit in the
# channel until somebody starts a session and reads it. The SELL_SHORT
# gap sat open overnight for exactly that reason. This is the nudge.
#
# It stays quiet while you are mid-conversation with LOCKBOT, since you
# will read the reply that filed the item, and it deduplicates per item
# so a second filing alerts and a re-read never does.
NOTIFY_AGENT_CHANNEL = True

# Measure the shorts LOCKBOT is not allowed to trade.
#
# Shorting is off under $2,000 of equity, so SELL_SHORT signals were
# rejected in stage one of the scan and discarded. They never reached
# the shadow logger, which iterates approved setups only, so all 119
# resolved shadow trades are LONG and roughly 350 short signals a
# session went unmeasured. The strategy has been judged on half its
# own output while the other half was invisible.
#
# These are advanced through the full stage-two pipeline -- alignment,
# regime, adaptive bracket -- so what is measured is a short LOCKBOT
# would actually have taken. They never receive trade_approved, and the
# submission loop selects on that alone, so this measures without ever
# being able to trade. Filed by LOCKBOT as agent_channel item 46169c86.
SHADOW_LOG_BLOCKED_SHORTS = True

# ============================================================
# The research lab's own symbol pool
#
# Filed by LOCKBOT as agent_channel item 7425d2f7, and the diagnosis is
# correct. strategy_lab backtests whatever frames it is handed, and
# propose_strategy handed it universe.csv -- names filtered to
# 1.25-3.00%/day movement. The swing horizon scores a 2:1 reward on a 5%
# stop, so it needs roughly a 10% favourable move inside a trading week.
# Names SELECTED for low movement cannot deliver that, so every swing
# backtest was capped by the universe rather than by the rule.
#
# Measured: at the swing configuration 83% of entries never touched
# either band. The test was not measuring entry logic at all.
#
# The live universe is deliberately untouched. Its 1.25-3.00% band is
# right for what it does -- feeding the shadow log, whose population must
# stay stable while the regime split accumulates toward n~200. Changing
# it would invalidate 269 logged setups to fix a different problem.
#
# The lab pool is built from names the live filter REJECTS as too wild:
# 95 of the 150-name pre-filter pool, 78 of them inside this band.
# ============================================================

LAB_UNIVERSE_FILE = PROJECT_FOLDER / "lab_universe.csv"

# What a backtest charges per side, as a fraction of price.
#
# First item on LOCKBOT's agenda 2026-08-07: the lab filled at the exact
# stop or target and charged nothing, so every number this project has
# produced is optimistic by an amount nobody had measured.
#
# This one IS measured rather than assumed, which matters because the
# obvious source is unusable: the live feed is iex, and a resting iex
# quote on AAPL has read 293.64 / 324.64. A cost model built on those
# quotes would be wilder than the thing it models.
#
# So trading_costs.corwin_schultz estimates the spread from high-low
# ranges, which needs only OHLC. Measured 2026-08-07 across 40 lab
# symbols on 30 days of 5-minute bars:
#
#     median round-trip spread   0.058%   -> 0.029% per side
#     mean                       0.063%
#     range                      0.027% - 0.132%
#
# Set to 0.05% per side: the measured 0.029% spread, plus roughly 0.02%
# for slippage. Slippage is an ALLOWANCE, not a measurement -- a stop
# order becomes a market order and fills at the next available price,
# which in a fast move is past the stop, and nothing in this data can
# tell us how far. It is the honest half of this number to argue about.
#
# The unit that bites is R, not percent. See trading_costs.round_trip_r:
# at a 2% stop this is 0.050R a round trip, at a 0.5% stop it is 0.200R.
# Any sweep that varies the stop is silently varying the cost in R.
BACKTEST_COST_PER_SIDE_PERCENT = 0.0005

# Daily ATR band for the lab, as fractions. A rule needing a 10% move in
# a week is plausible at 3%/day and impossible at 1.5%.
LAB_MIN_ATR_PERCENT = 0.030
LAB_MAX_ATR_PERCENT = 0.080

# Cap the pool so a backtest stays affordable. There is no point holding
# 300 names when a decile is what gets traded.
LAB_TOP_N = 80


# ============================================================
# OPTIONS TRADING
#
# Read this section before changing anything in it. Options do
# not behave like the equity side, and three differences are
# structural rather than cosmetic:
#
# 1. THERE IS NO BRACKET ORDER. Alpaca's order classes are
#    security-type dependent: equities support bracket/oco/oto,
#    options support only 'simple' and 'mleg'. Options are also
#    day-only — there is no GTC. So the exit cannot be parked at
#    the broker the way the equity side parks it. options_manager.py
#    holds the stop and the target in software and is the sole
#    exit authority for options positions. That makes it the most
#    safety-critical file in the project: if it stops running,
#    open option positions have NO stop loss of any kind.
#
# 2. AN OPTION BLEEDS. A stock position that goes sideways costs
#    nothing to hold. An option loses value every day. That is why
#    OPTIONS_MAX_HOLD_DAYS and OPTIONS_MIN_DTE_EXIT exist and why
#    they are not optional.
#
# 3. ONE CONTRACT IS THE SMALLEST TRADE. 100 shares of exposure,
#    indivisible. On a $250 account this collides with the 1%
#    risk-per-trade budget the equity side uses: a $70 contract
#    with a 35% stop risks $24.50, which is 10% of the account,
#    not 1%. The account cannot subdivide below one contract, so
#    the risk budget CANNOT be met by buying fewer.
#
#    LOCKBOT resolves this by refusing the trade rather than by
#    quietly accepting the larger risk: OPTIONS_MAX_RISK_PER_TRADE_PERCENT
#    is a hard ceiling, and any contract whose premium x stop
#    exceeds it is rejected as unaffordable. At $250 that admits
#    only cheap contracts, which is the honest answer — most of
#    the options market is genuinely out of reach at this size.
# ============================================================

OPTIONS_ENABLED = True

# When True, every decision is written to options_shadow_log.csv and NO
# order is sent to the broker.
#
# Set to False at the user's explicit request — LOCKBOT places real
# (paper) options orders. Worth recording what that means: the options
# entry path had never placed a live order before this, so the mleg
# spread request, the limit pricing and the fill handling all meet a
# real market for the first time in production rather than in a shadow
# session. Every decision is still written to options_shadow_log.csv,
# so the log is available either way.
#
# Flip back to True for a no-risk observation session at any time; it is
# the complete off switch for options order submission and needs no
# other change.
# Paused 2026-08-04. Trading stops; measurement does not.
#
# The shadow log is written by the SCANNER, not by fills, so every
# decision is still ranked and recorded — the candidate quality
# distribution, the resolvers at 3:15 and the learning pass at 3:30 all
# carry on exactly as before. Nothing about the experiment slows down.
#
# What stops is paying for it. Over 119 resolved setups the signal wins
# 20.2% against a 41.2% breakeven on the options bands, and at 10% risk
# per trade that is roughly -5.55% of the account per trade: half of it
# gone in about twelve trades. The information learned per trade is
# identical at any size, and identical again at zero size.
#
# Open positions are unaffected. options_manager.py continues to run its
# exits — pausing entries must never mean abandoning what is already held.
#
# Set back to False when the evidence supports it, which is what
# signal_research.py and backtest.py exist to establish.
#
# ---------------------------------------------------------------------
# RESUMED 2026-08-14, by owner decision, with the evidence AGAINST it.
#
# Recorded honestly because the comment above says "when the evidence
# supports it", and it does not. At the time of the flip:
#
#   options shadow book   8 of 28 decided = 28.6%
#   breakeven needed      41.2% at the +50%/-35% bands
#   direction of travel   it was 40.0% before the EXPIRED censoring fix;
#                         every one of the eight rows that resolved since
#                         has been a stop
#   resolved net          +$5.05 -> -$12.95 once formerly-censored windows
#                         are booked at mark to market
#   spread drag           5.88x the entire gross modelled result
#   the underlying signal 16.1% against a 33.3% breakeven on equities
#
# The owner was shown these figures and chose to proceed. That is their
# call to make; this note exists so no future reader mistakes the flip
# for a conclusion the measurements supported.
#
# WHAT NOW PROTECTS THE ACCOUNT, since this is the only stop that exists:
#   full-debit cap        $65.18 per position (10% of equity)
#   concurrent positions  3, so $195.54 = 30.0% of equity is undefended
#                         if options_manager stops running
#   PDT                   option round trips count; 3 per 5 days
#
# Alpaca offers no options bracket, no stop order type and no GTC, so
# options_manager.py IS the stop loss. A stale OPTIONS_MANAGER heartbeat
# is unprotected capital, not a reporting problem.
#
# ROLLBACK: set this back to True. It is the complete off switch for
# options order submission and needs no other change. Open positions are
# unaffected either way -- options_manager keeps running its exits.
# ---------------------------------------------------------------------
OPTIONS_SHADOW_MODE = False

# Hard ceiling on what one option trade may lose, as a fraction of
# equity. Premium x stop-loss percent must land under this or the
# contract is rejected. At $250 and a 35% stop this admits
# contracts up to roughly $71.
OPTIONS_MAX_RISK_PER_TRADE_PERCENT = 0.10

# Ceiling on premium paid for one position, as a fraction of
# equity. The risk ceiling above usually binds first; this is the
# backstop for the case where the stop is unusually tight.
OPTIONS_MAX_PREMIUM_PERCENT = 0.30

# Raised from 2 on 2026-08-03.
#
# This does NOT add exposure. OPTIONS_MAX_TOTAL_PREMIUM_PERCENT caps the
# total dollars in options at 60% of equity whatever the slot count is, so
# raising concurrency splits the same money across more, smaller positions
# rather than committing more of it. That is diversification, not leverage.
#
# The arithmetic at $270 of equity:
#     2 slots -> $77.20 each   (the per-trade risk cap binds)
#     3 slots -> $54.04 each   (the premium ceiling binds)
#     4 slots -> $40.53 each   (below the cheapest contract seen, $43)
#
# Three is where it stops being useful: a fourth slot would shrink the
# per-position budget below what any contract in the universe costs, so
# it would exist without ever being fillable.
OPTIONS_MAX_OPEN_POSITIONS = 3
OPTIONS_MAX_TOTAL_PREMIUM_PERCENT = 0.60

# Per-SIDE premium cap: calls and puts each capped separately, from the
# owner's playbook (2026-08-25, Rule 8 -- 15% calls, 15% puts, 30% both).
#
# WHY ONLY THE PER-SIDE HALF WAS ADOPTED. The playbook's 30% COMBINED cap
# is already enforced, structurally and invisibly:
#
#     OPTIONS_MAX_OPEN_POSITIONS (3) x OPTIONS_MAX_RISK_PER_TRADE_PERCENT
#     (0.10) = 0.30
#
# Three positions, each capped at a tenth of equity, cannot exceed three
# tenths of it. Both terms are percentages of live equity, so the
# relationship holds at any account size. Adding a 30% combined setting
# would be a second expression of a limit that already exists -- the
# "one quantity computed in two places" defect that produced the debit
# ceiling, entry limit and exit valuation bugs this month. It was NOT
# added, deliberately. OPTIONS_MAX_TOTAL_PREMIUM_PERCENT at 0.60 is
# already unreachable for the same arithmetic; it is left alone rather
# than tuned, because changing an unreachable number looks like action
# and is not.
#
# The per-side split IS new: nothing has ever stopped LOCKBOT holding
# every position on the same side of the market. On 2026-08-24 it held
# $53.00 of calls and no puts, 14.3% of a $371.26 account -- so this cap
# binds on the very next call entry, which is what makes it worth having
# rather than decoration.
#
# It is a CONCENTRATION control, not a directional view. Three long calls
# are one bet on the market rising, sized three times.
OPTIONS_MAX_SIDE_PREMIUM_PERCENT = 0.15
OPTIONS_MAX_CONTRACTS_PER_POSITION = 1
OPTIONS_MAX_NEW_ENTRIES_PER_CYCLE = 1
OPTIONS_MAX_TRADES_PER_DAY = 4

# Backfill: refill a position slot in the same cycle it is freed.
#
# options_manager.py runs before options_scanner.py, but it only submits
# exit orders — the position stays tracked, and keeps holding its slot,
# until the next cycle reconciles it against the broker. That made a slot
# freed at 10:00 unusable until 10:05. With backfill on, the manager waits
# briefly for its own exits to fill and releases the slot immediately, so
# the scanner running seconds later sees the real count.
#
# The wait is bounded because it delays the whole controller cycle. If an
# exit has not filled within the window the slot simply frees next cycle,
# which is the old behaviour and is not an error.
OPTIONS_BACKFILL_ENABLED = True
OPTIONS_EXIT_SETTLE_SECONDS = 20

# How long an unfilled entry order may hold a slot before LOCKBOT cancels
# it. On 2026-07-30 a PBR call order sat at status "new" and never filled;
# the position was journaled as a total loss while the order stayed live,
# so a later fill would have created an option position with no software
# stop. Cancelling bounds both problems.
# How many times one underlying may be SUBMITTED for entry in a day,
# filled or not. LOCKBOT's 08-25 fill ruling permits a re-attempt on a
# fresh quote -- a stale displayed ask is the likeliest reason an order
# priced AT that ask never fills -- but bounds it.
#
# WHY A BOUND WAS NEEDED AT ALL. When an entry is written off as
# ENTRY_NOT_FILLED the position is deleted, which releases the name, and
# the daily trade slot is refunded in the same pass. So the retry path was
# already open and completely unlimited. On 2026-08-26 three of five
# entries did not fill; one stubborn name could have spent the whole
# session retrying itself while genuinely fillable setups went untaken.
#
# Counted on SUBMISSION rather than on failure, so a filled order also
# spends an attempt. That keeps it a cap on TRIES rather than a cap on
# disappointments -- the second would let a name that fills, stops out and
# re-qualifies cycle round again indefinitely.
OPTIONS_MAX_ENTRY_ATTEMPTS = 2

OPTIONS_ENTRY_FILL_TIMEOUT_MINUTES = 15

# How far above the ask an entry limit is placed. A limit at exactly the
# ask has no cushion: two PBR calls on 2026-07-30 never filled, the second
# stranded within 45 seconds when the ask moved 0.48 -> 0.52. This is the
# price of getting filled at all, and it is charged against
# OPTIONS_MAX_RISK_PER_TRADE_PERCENT rather than sneaking past it. Set to
# 0.0 to restore at-the-touch pricing.
OPTIONS_ENTRY_LIMIT_BUFFER_PERCENT = 0.03

# How far across the spread an entry limit sits, 0.0 = mid, 1.0 = the touch.
#
# LOCKBOT ruled 0.5 on 2026-08-19 (channel 80b8a35f) and the reasoning is
# worth more than the number.
#
# The old design priced entries at ask x 1.03 -- ABOVE the offer -- to buy
# fill certainty, after two PBR calls at the exact ask went unfilled on
# 2026-07-30. That premise turned out to be false: the same design has
# produced 4 of 10 ENTRY_NOT_FILLED since, so the 3% buffer bought roughly a
# 60% fill rate, not certainty. It was paying over the offer for something
# it was not receiving.
#
# 0.5 rather than 0.75 because the asymmetry runs the other way from the
# exit side. A missed entry is free: there is no position to protect and no
# proven edge forgone -- the options ledger on this account is 0-for-3. So
# start at the cheap end and walk UP on measured fill rates, never down on
# faith. The PBR evidence is n=2 on one illiquid underlying in one session;
# it can speak to 1.0 with no cushion, not to 0.5 against 0.75.
#
# The buffer above still applies, but ONLY at 1.0 -- it exists to stop a
# limit resting exactly at a moving touch, which is not a risk anywhere
# inside the spread.
#
# DO NOT MOVE THIS ON A FEELING. LOCKBOT's conditions: log the fraction, the
# quote at submit, fill or no fill, and time-to-fill on every entry, and run
# an adverse-selection check before believing any saving -- mid-fills happen
# preferentially when the market comes TOWARD you, so the filled and
# unfilled cohorts must be compared on what the underlying did next. Decide
# from about two weeks of that, not sooner.
OPTIONS_ENTRY_LIMIT_FRACTION = 0.50

# How many consecutive cycles the stop condition must hold before an
# option is sold.
#
# Added 2026-08-03 after an EWZ call exited at -8.1% against a -35% stop.
# The sell was priced at the stop and filled 42% above it, and no trade in
# that window printed anywhere near the level — the quote feed had shown a
# bid far below what the contract was worth.
#
# The books here are fresh but wide and jittery: 16-28% spreads with the
# bid moving 8% between polls seconds apart. One reading is not evidence.
# Two cycles costs at most five minutes on a real stop and removes an
# entire class of exit that should never have fired. Set to 1 to restore
# the old immediate behaviour.
OPTIONS_STOP_CONFIRM_CYCLES = 2

# Sessions an underlying is benched after a realised options STOP_LOSS.
#
# Owner directive 2026-08-20: "Make it to where the scanner has memory
# losing on a name so it is aware." The trigger was options_scanner
# re-entering the SOFI Sep-18 19/20 spread -- the identical strikes that
# stopped out at -36.8% two days earlier -- because nothing on the entry
# path had ever read the completed-trades ledger.
#
# A COOLDOWN, NOT A SCORE. LOCKBOT's design ruling (channel 152a38bd): a
# mechanical bench, keyed on the UNDERLYING rather than the contract, read
# from the journal rather than memory so it survives a restart. Fitting a
# predictive rule to four closed trades would be a curve fit; benching a
# name for a fixed count is a rule that can be measured and removed.
#
# IT SHIPS ON A DIRECTIVE, NOT ON EVIDENCE, and that is recorded rather
# than glossed: four realised option losses is far below any sample floor.
# Every entry this blocks is shadow-logged as cooldown_blocked, so in
# roughly thirty resolutions the data says whether the blocked trades
# would have won. If they would have, the cooldown is destroying value and
# the log will prove it.
# Cut from 5 to 1 on 2026-08-21, one day after it shipped, on the owner's
# instruction: "I don't want to miss out on opportunities just because of
# one bad trade."
#
# The reversal is his, but the data had already made the case. At 5
# sessions the bench held SEVEN names -- BAC, GDX, SOFI, XLF, NFLX, NVDA,
# INTC -- and LOCKBOT is 0 for 8, so it benched every name it touched. At
# roughly one trade a day it was removing names faster than they returned,
# and only a handful ever clear the 5% spread gate in the first place.
# NVDA, INTC and XLF are among the tightest books it can reach and all
# three were locked out at once.
#
# Left alone that is not discipline, it is a slow walk to having nothing
# left to trade. The rule was sized for a book that sometimes wins.
#
# At 1 it still fixes the thing that prompted it: options_scanner
# re-entered the SOFI Sep-18 19/20 spread two days after those exact
# strikes stopped out. A name is not re-bought on the next cycle or the
# same session; it returns the following day.
#
# The measurement is unchanged -- blocked entries are still shadow-logged
# as COOLDOWN_BLOCKED and still resolve against the registry floor, so
# this remains a rule that can be convicted rather than a preference.
OPTIONS_LOSS_COOLDOWN_SESSIONS = 1

# What a contract may cost to OWN, as opposed to to trade.
#
# Added 2026-08-04. Every other gate asks whether a contract is tradable;
# these ask whether it is expensive. LOCKBOT had been reading implied
# volatility off the feed and discarding it, so it could not tell an
# overpriced option from a fair one.
#
# OPTIONS_MAX_IV_PREMIUM is implied volatility divided by the underlying's
# REALISED volatility. 1.0 is fairly priced. The PCG put currently held
# is 1.83x — 48% implied against 26% of actual movement — and the IBIT
# call is 0.96x. The same gates passed both.
#
# OPTIONS_MAX_DAILY_THETA is time decay as a fraction of premium per day.
# That PCG position loses 3.6% daily to theta alone, 61% across the
# remaining hold. Over a 21-45 day window this is the largest single cost
# in the trade and nothing looked at it before.
#
# These are COSTS, not predictions, which is why they gate without
# waiting for shadow evidence — the same argument that justified the
# spread gate. Neither claims low IV predicts direction.
OPTIONS_MAX_IV_PREMIUM = 1.60
OPTIONS_MAX_DAILY_THETA = 0.030

# Whether an event is scheduled inside the holding period.
#
# Added 2026-08-04, and the last of the blind spots in contract
# selection. LOCKBOT could not tell whether a company reported earnings
# tomorrow, which is the classic way to be right about direction and
# lose money regardless: implied volatility inflates before the report
# and collapses the moment it lands.
#
# There is no earnings calendar behind this. Alpaca does not sell one,
# and the free alternatives are scrapers that break silently. Instead
# event_risk.py reads it out of the option prices already flowing —
# implied volatility normally RISES with time to expiry, so when the
# near month costs more than the far month, the market is pricing
# something it knows about and LOCKBOT does not. That also catches FDA
# decisions, court rulings and index rebalances, which an earnings
# calendar would miss entirely.
#
# The number is near-dated IV divided by far-dated, at a matched strike.
# Under 1.0 is the normal ordering.
#
# THIS THRESHOLD IS THE LEAST-EVIDENCED NUMBER IN THE GATE. Sixteen live
# symbols on 2026-08-04 read as:
#
#   0.87 0.88 0.88 0.89 0.96 0.97 | 1.00 1.07 1.09 1.09 | 1.11 1.13
#   1.20 1.20 1.36 3.81
#
# There is a real gap between the 0.87-0.97 cluster and everything above
# 1.07, which is the split this is meant to catch. Where to cut inside
# the 1.00-1.20 band is a judgement, not a measurement: 1.10 refuses six
# of sixteen, 1.15 refuses four, 1.25 refuses two.
#
# 1.10 is the conservative choice and is chosen deliberately. Refusing a
# trade costs an opportunity in a system whose measured edge is negative
# anyway; taking one into a volatility crush costs money. The slope is
# now written to options_shadow_log.csv for every candidate, passed or
# refused, so this can be re-derived from outcomes instead of judgement
# once there are enough of them.
#
# NOT YET VERIFIED: that a flagged name resolves after its event. A
# stock whose term structure is permanently inverted -- distressed
# names, heavy short interest -- would be refused forever by a check
# that cannot tell the difference. That needs several days of logged
# slopes per symbol to answer and cannot be settled from one session.
#
# Like the cost gates above this is a mechanism, not a prediction, so it
# does not wait on shadow evidence. Unlike them it can be switched off
# outright, because it costs an extra chain fetch per candidate.
#
# NOTE: this assumes LOCKBOT is BUYING premium, which it is. A premium
# seller wants exactly the inversion this refuses. If that ever changes
# the gate must be inverted, not disabled.
OPTIONS_EVENT_RISK_ENABLED = True
OPTIONS_MAX_TERM_INVERSION = 1.10

# Contract selection. Delta near 0.50 is at-the-money; lower delta
# is cheaper but needs a bigger move to pay. The DTE window keeps
# LOCKBOT away from the last two weeks of an option's life, where
# theta decay accelerates sharply.
# Lowered from 0.35 to 0.20 on 2026-08-23, and the reason is AFFORDABILITY,
# not signal. Say so plainly rather than dressing it as a view.
#
# It moves with OPTIONS_ALLOW_SPREADS going False -- the two are one
# decision. With verticals off and a $38.34 ceiling, the 0.35 floor left
# exactly THREE affordable single contracts across 31,304 sampled quotes
# (BITO $25, SCHD $28, NOK $35). That is not cheap-option trading, it is
# no trading. At 0.20 the pool is 13 contracts across 7 symbols -- still
# thin, roughly one entry a day.
#
# 0.10 was rejected by LOCKBOT as a lottery ticket (channel 5acfe857).
# The theta objection to low delta -- that a cheap contract is nearly all
# extrinsic and bleeds fastest as a fraction of premium -- applies to
# HOLDING INTO EXPIRY, which the 10-day and 14-DTE exits already forbid.
OPTIONS_TARGET_DELTA_MIN = 0.20
OPTIONS_TARGET_DELTA_MAX = 0.60
OPTIONS_MIN_DTE = 21
OPTIONS_MAX_DTE = 45

# Fallback for contracts whose greeks are missing, which happens on the
# indicative feed. Delta cannot be checked, so the strike must instead
# sit within this fraction of the underlying price. Without it, a
# contract with no greeks bypasses the delta gate entirely — that let a
# deep in-the-money F call through during the first live dry run.
OPTIONS_MAX_MONEYNESS_PERCENT = 0.07

# Liquidity gates. A wide spread is a guaranteed loss taken at the
# moment of entry — you buy at the ask and can only sell at the
# bid. 10% is already expensive; above that the spread eats the
# move the signal predicted.
#
# Lowered from 0.10 on 2026-08-02. The exit bands (+50%/-35%) pay
# 1.43:1, which needs a 41.2% win rate to break even; the same
# signals measure 27.3% over 55 resolved shadow trades. At a 10%
# spread the underlying has to travel 2.34x further up than down to
# reach the target rather than the stop — a 1.43:1 payout on a
# 2.34:1 requirement. At 5% that falls to 1.84x. The spread is the
# one cost here that is certain rather than predicted, so it is the
# only lever worth pulling before the edge itself is measured.
#
# This deliberately trades fewer entries for cheaper ones. Contracts
# like the 6.5%-spread PBR call taken on 2026-07-30 no longer pass.
# 2026-08-24: 0.05 -> 0.08, on LOCKBOT's conditional recommendation and
# the owner's decision. NOT a free change and must not be described as one.
#
# WHAT 0.05 WAS COSTING. Measured over 26,119 two-sided quotes on 08-24,
# the spread distribution of the universe this gate filters:
#
#     p5 1.2%   p10 2.2%   p25 5.7%   MEDIAN 12.7%   p75 25.5%
#
# The gate sat BELOW the 25th percentile of its own universe. In the
# 13:30-17:00 window it rejected 8,007 of 10,771 contracts and exactly 4
# passed every gate, which is why the morning produced no order at all.
# That silence was read as "no setups" for weeks; it was this.
#
#     ceiling   contracts admitted
#       5%          22.2%
#       8%          34.0%
#      10%          41.3%
#
# WHY THE OLD VALUE WAS DEFENSIBLE WHEN SET. Lowered from 0.10 on
# 2026-08-02 on exit-band geometry, for a universe of $5-$50 names with a
# 0.35 delta floor and verticals enabled. EVERY ONE of those conditions
# has since changed: the delta floor is 0.20, verticals are off, and the
# debit ceiling forces the book into cheap tick-quantized premium where a
# ONE CENT spread on a $0.30 contract is already 3.3%. The number did not
# drift; the universe moved out from under it.
#
# WHY 0.08 AND NOT 0.10. 0.10 was tested and REJECTED on 08-02 because
# exit-band travel asymmetry reached 2.34x. 0.08 lands near 2.1x, short
# of that bar. Going to 0.10 now would be re-cutting a threshold that has
# already failed once.
#
# THE HONEST FRAMING, which is LOCKBOT's and is kept verbatim because the
# temptation to lose it is exactly why it is here: "At the current gate
# the choice being made by default is near-zero trades at ~5% friction;
# at 0.08 it's roughly one-a-day at up to 8% friction, into a book that
# is 0-for-9. Both are defensible. Neither is free."
#
# THE CONDITION THIS SHIPPED UNDER. Registered in rule_registry with a
# cohort split by spread band. If trades entered in the 5-8% band run
# 0.08 of debit worse than those under 5% at n >= 30, the verdict is
# COSTING_MONEY and this reverts to 0.05. entry_spread_percent is
# journalled per trade so that split is possible -- without it the
# condition would be unenforceable and the guarantee empty.
#
# MEASURED ON A FEED THAT IS NOT THE EXECUTABLE BOOK. The owner decided
# on 2026-08-24 to stay on the free IEX feed rather than buy OPRA. IEX
# carries ~4% of real volume, so every percentile above describes the
# displayed book, not the tradable one. Two orders at the displayed ask
# went unfilled on 08-24. This is a known and accepted limit of the
# number, not an oversight.
OPTIONS_MAX_SPREAD_PERCENT = 0.08

# OPTIONS_MIN_OPEN_INTEREST (100) and OPTIONS_MIN_CONTRACT_VOLUME (10)
# stood here until 2026-08-19. Deleted on LOCKBOT's ruling (item 87431cac),
# and the reason is worth more than the constants were.
#
# NOTHING EVER READ THEM. Not a wiring gap either: ContractQuote carries no
# open_interest or volume field, so the data was never fetched. They could
# not have been enforced without a change to the chain fetch.
#
# They sat under the "Liquidity gates" heading beside the spread argument
# above, formatted as settled policy, and were reported to the owner on
# 2026-08-17 as active rules. They had never run. An unwired constant that
# reads as configuration makes this file LIE about what the system does --
# the b16e2f2a class, and the same standing rule that reverted
# SHADOW_DATA_FEED on 08-11: reinstate a constant only together with its
# wiring, never ahead of it.
#
# The idea is not dead, only this version of it. A liquidity gate may well
# be worth having -- 4 of 10 entries since 07-30 went ENTRY_NOT_FILLED and
# measured spread drag runs 5.88x the gross modelled result, both of which
# point at thin books. It returns as its own item: ContractQuote extended
# to carry open interest and volume, the quote-sampler data read to see
# where fill failures actually cluster, and thresholds argued from that
# rather than inherited from two numbers nobody can source.

# A zero bid means there is no buyer at any price — the position
# cannot be exited. Never enter one of these.
OPTIONS_REQUIRE_NONZERO_BID = True

# Exits, all measured against the premium paid, not the underlying.
# Absolute implied-volatility ceiling.
#
# IT IS NOT A SECOND COPY OF OPTIONS_MAX_IV_PREMIUM, and the distinction
# has to stay visible or one of them will be deleted as redundant by a
# future reader. They measure different quantities:
#
#   OPTIONS_MAX_IV_PREMIUM     IV divided by REALISED volatility. Asks
#                              "is this contract overpriced relative to
#                              how much the stock actually moves?"
#   OPTIONS_MAX_IMPLIED_VOL.   raw IV. Asks "how much does this stock
#                              move at all?"
#
# A name that genuinely swings 100% a year priced at 110% IV passes the
# first gate (fairly priced) and fails this one. That is intended, and
# the reason is NOT overpricing -- it is the stop.
#
# WHY THE STOP IS THE REASON. Every one of the nine option trades closed
# to date died on the stop; not one timed out and not one reached target.
# A fixed -35% band is a fixed PREMIUM distance, and the probability of
# touching it before +50% rises with the volatility of the underlying,
# regardless of direction. On a high-IV contract the band is closer in
# time even when it is identical in percent. So this gate reduces how
# often noise alone closes a position.
#
# That claim is falsifiable and is registered in rule_registry rather
# than assumed.
#
# WHY 1.00 AND NOT KEN'S 40%. The owner's playbook (2026-08-25) sets a 40%
# IV gate. Measured against the real chain on 2026-08-24 -- 350 contracts,
# seven names, 21-45 DTE:
#
#     p5 18.6%   p25 26.1%   MEDIAN 50.2%   p75 68.0%   p95 94.8%
#
#     a 40% ceiling keeps  41% of contracts
#     a 60% ceiling keeps  60%
#     a 80% ceiling keeps  85%
#     a 100% ceiling keeps 96%
#
# Ken's 40% sits below the median of this universe and would reject 59% of
# an already thin pool -- 13 contracts across 7 symbols at the current
# delta floor. His gate was written for his universe, not this one.
#
# So this starts as a TAIL CUT, removing only the top 4%: IV above 100%
# means the market prices a more-than-doubling as ordinary, which is event
# or meme territory. Tightening it toward 40% is a decision for evidence,
# not for one afternoon's distribution -- the same discipline that governs
# OPTIONS_MIN_QUALITY, which has been left unset for the same reason.
#
# Registered in rule_registry so it can be judged rather than believed.
# --- Option-implied skew, the first entry signal not derived from the
# --- price bars. See PREREG_OPTION_SKEW.md, committed before any outcome.
#
# WHY: options_scanner has always taken direction from
# market_scanner.detect_signal, measured 2026-08-05 at 32.9% / -0.01R
# against random entry's 36.7% / +0.10R. The options book is a leveraged,
# double-spread, theta-paying expression of a signal with no information,
# which explains 0-for-9 without appealing to luck.
#
# WHAT REPLACES IT: OTM-put IV minus ATM-call IV, delta matched. Published
# to predict returns cross-sectionally (Xing/Zhang/Zhao 2010), and largely
# explained away as a stock-borrow-fee artifact by Muravyev/Pearson/Pollet
# (JFE 2025) -- two thirds of it vanishes once borrow fees are charged or
# high-fee names excluded. This account cannot short at all, so it takes
# the LONG half only and requires easy_to_borrow, which is the same
# exclusion that paper says removes the artifact.
#
# SHADOW UNTIL IT EARNS CAPITAL. Nothing enters on skew while this is
# False; it computes, ranks and logs. The owner can set it True -- his
# account, his call -- and the pre-registered bar does not move if he does.
# The capital generation this account is in. Every registry row and
# cohort split is tagged with it, so measurements taken at a $344 ceiling
# are never silently pooled with measurements taken at $750.
#
# The ceiling is 10% of equity, so it moved from about $34 to $75 without
# any setting changing. That un-censors the top of the quality sort --
# on 2026-08-26 the scanner refused WDAY (q 76.6), EMB (50.3) and TEAM
# (50.8) on price and bought TLT (48.6), CMCSA (21.3) and PFE (23.7)
# instead. It buys MEASUREMENT, not edge: the signal is still measured
# worse than random and larger positions lose faster in dollars.
#
# gen1_344 ended 2026-08-26 at $344.31, 10 closed trades, 0 wins, -$266.
CAPITAL_GENERATION = "gen2_750"

OPTIONS_SKEW_ENABLED = True
# LIVE on the owner's instruction, 2026-08-26: "I want it to live trade.
# I like to see the progress." Nothing about the pre-registered bar in
# PREREG_OPTION_SKEW.md moves because of this -- the criteria were written
# before any skew-sourced entry existed and are judged the same whether
# the signal spends money or not. Running it live buys the owner
# visibility, not a lower standard.
#
# WHAT LIVE ACTUALLY CHANGES. Candidates are reordered lowest-skew first,
# and anything skew cannot vouch for is DROPPED rather than ranked last:
# under the registration an unstable or not-easy-to-borrow name is a
# refusal, not a weak candidate.
#
# EXPECT A QUIET START, AND IT IS NOT A FAULT. The stability history was
# cleared with the account reset, and a name needs
# OPTIONS_SKEW_MIN_READINGS consecutive same-signed readings before it is
# tradable. Until that fills, every cycle logs "none tradable this cycle"
# and enters nothing. It resolves itself as names recur across cycles.
# The alternative -- trading on one reading of a 16-28% wide book -- is
# the exact failure that produced OPTIONS_STOP_CONFIRM_CYCLES.
OPTIONS_SKEW_LIVE = True

# The stability gate. One reading of a 16-28% wide book that moves 8%
# between polls is not a signal; the same lesson produced
# OPTIONS_STOP_CONFIRM_CYCLES after an EWZ call exited at -8.1% against a
# -35% stop on a single bad print. A name needs this many consecutive
# SAME-SIGNED readings inside the drift band before it can be traded.
OPTIONS_SKEW_MIN_READINGS = 3
OPTIONS_SKEW_MAX_DRIFT = 0.05

OPTIONS_SKEW_STATE_FILE = PROJECT_FOLDER / "options_skew_state.json"

OPTIONS_MAX_IMPLIED_VOLATILITY = 1.00

OPTIONS_TAKE_PROFIT_PERCENT = 0.50
OPTIONS_STOP_LOSS_PERCENT = 0.35

# Time-based exits. Both are theta protection, not signal logic.
# Buffer for the SHADOW underlying stop (owner playbook Rule 7): a call
# is stopped when the underlying trades below strike - this, a put when it
# trades above strike + this.
#
# NOTHING EXITS ON THIS. underlying_stop_shadow only writes a log. It is
# here because the live -35% premium stop is not behaving like a -35%
# stop -- across nine closed trades it realised -47% on average, +12% past
# its own level, worst +49% (GDX, twelve minutes). All nine died on the
# stop; none timed out and none reached target.
#
# A percentage of PREMIUM and a distance on the UNDERLYING are different
# triggers on different series, so this is worth measuring rather than
# assuming. It is logged paired with the live stop at the same instant.
#
# $0.50 is the playbook's figure, adopted unchanged as a starting cohort.
# Every row carries it in rule_param so a later change splits cleanly.
OPTIONS_UNDERLYING_STOP_BUFFER = 0.50

OPTIONS_MAX_HOLD_DAYS = 10
OPTIONS_MIN_DTE_EXIT = 14

# Strategy per regime. LONG_CALL / LONG_PUT buy premium outright
# and want a strong directional move. The debit spreads cost less
# and decay less, at the cost of a capped payoff — the right trade
# when the trend is real but weak, or when volatility is high and
# outright premium is expensive.
# Turned OFF 2026-08-23 on the owner's directive, stated twice: "I do not
# want the spreads to be a thing... buy premium cheap options contracts
# until we can afford the more expensive stuff."
#
# THE ARGUMENT THAT CARRIES IT, and it is his: a vertical crosses the
# bid-ask on TWO legs against a NET debit that is the difference of two
# premiums, so friction as a fraction of committed capital is amplified
# beyond 2x. On a book where measured spread drag is 5.88x the gross
# modelled result, a structure that multiplies the dominant cost is a
# real problem.
#
# THE ARGUMENT THAT DOES NOT, recorded because it is the tempting one and
# LOCKBOT refused it explicitly: the 0-for-8 record on spreads is NOT
# evidence against the structure. All eight died on the -35% software
# stop or on gap-through -- NVDA and INTC on the first cycle after an
# open, GDX at -83.8% inside ten minutes -- and that mechanism is
# indifferent to whether the position was one leg or two. Citing the
# losing streak as support would be attributing to the structure a death
# that had nothing to do with it.
#
# WHAT IT COSTS. Dollar loss per trade falls, $25-38 against $155. But
# expect MORE FREQUENT stop-outs, because premium volatility is higher at
# low delta, and WORSE percentage overshoot, because a one-cent tick is a
# larger fraction of a $25 contract than of a $155 one. Smaller losses,
# more of them, higher variance, still bounded at the debit.
#
# REVERSIBILITY IS PART OF THE DECISION, in the owner's own framing --
# "until we can afford the more expensive stuff". Re-enabling is his call
# at an equity level he names. This is not doctrine and must not harden
# into it.
OPTIONS_ALLOW_SPREADS = False
OPTIONS_SPREAD_WIDTH_STRIKES = 1

# Remapped to single legs on 2026-08-23 when OPTIONS_ALLOW_SPREADS went
# False. validate_configuration refused the half-done version -- three
# regimes still pointed at vertical strategies that could no longer be
# built -- which is the check earning its keep.
#
# The weak-trend and high-volatility regimes used verticals precisely
# BECAUSE they were cheaper, so they are the regimes that suffer most from
# the change. Their contracts must now clear the debit ceiling outright,
# and at $38 many will not. Expect the funnel to narrow, not widen.
OPTIONS_REGIME_STRATEGY = {
    "STRONG_UPTREND": "LONG_CALL",
    "STRONG_DOWNTREND": "LONG_PUT",
    "WEAK_UPTREND": "LONG_CALL",
    "WEAK_DOWNTREND": "LONG_PUT",
    "HIGH_VOLATILITY": "LONG_CALL",
    "RANGING": "NONE",
    "UNKNOWN": "NONE",
}

# ============================================================
# What reaches your phone
# ============================================================
#
# send_smart_notification() suppresses an alert when its exact text
# repeats. That defeats nothing when the text contains a number that
# moves: the watchdog reports "Heartbeat file is 62.3 minutes old", so
# every 15-minute run produced a new signature and a new push. One
# unchanged problem therefore alerted four times an hour, which is the
# "hourly health report" that made real alerts easy to ignore.
#
# NOTIFY_MUTED_EVENT_TYPES never reaches the phone at all.
NOTIFY_MUTED_EVENT_TYPES = (
    "SYSTEM_TEST",
    "PAPER_EXECUTION_TEST",
)

# These are ongoing-condition alerts rather than events. The FIRST one
# always goes out immediately -- only repeats are held back, so a real
# emergency still reaches you at once and simply does not repeat every
# quarter of an hour while it stays broken.
NOTIFY_THROTTLED_EVENT_TYPES = (
    "WATCHDOG_ALERT",
    "SCANNER_ERROR",
)

NOTIFY_REPEAT_COOLDOWN_MINUTES = 120

# Deliberately NOT throttled, because each is a discrete event you would
# want twice if it happened twice: TRADE_COMPLETED, BUY_ORDER_SUBMITTED,
# SHORT_ORDER_SUBMITTED, OPTIONS_ORDER_SUBMITTED, DAILY_REPORT.

# ============================================================
# Buy-and-hold ETF portfolio
# ============================================================
#
# A different animal from everything else in this file. No signals, no
# stops, no exits, no timing — a fixed allocation bought and held,
# rebalanced only when it drifts. The trading engine has a measured
# negative edge; this does not depend on picking anything.
#
# It shares the brokerage account with the trading bot, so two rules keep
# them apart:
#
#   1. Symbols in ETF_TARGET_ALLOCATION are RESERVED. position_filters.py
#      hides them from market_scanner, position_monitor and startup
#      reconciliation, so the trading engine cannot count them toward its
#      position cap or try to exit them.
#   2. ETF_PORTFOLIO_BUDGET is a hard ceiling in dollars. The portfolio
#      never spends beyond it, so it cannot quietly consume the cash the
#      options side needs.
#
# Starts DISABLED and in plan-only mode. Nothing is bought until both are
# changed deliberately.
# Enabled 2026-08-04, for a bounded purpose: verifying the plumbing, not
# testing whether broad ETFs go up. That question does not need a paper
# account.
#
# VERIFIED 2026-08-06 against real holdings, which is what it was
# enabled to establish. With SCHD and SCHG genuinely held at the broker:
#
#   broker returns          5 positions (2 ETF shares, 3 option legs)
#   equity the engine sees  []
#   equity in total         [SCHD, SCHG]
#   options                 [3 legs]
#
# Nothing is lost between the filters, option legs are not counted as
# equity, and the ETF book consumes zero trading slots. That last one
# was load-bearing: MAX_OPEN_POSITIONS is 2, so without the filter these
# two holdings would have occupied every slot and silently blocked all
# equity trading. position_filters.py now has 19 self-test checks
# covering it.
# Turned OFF 2026-08-14 on the owner's instruction to sell everything
# except the option position. This flag must be cleared BEFORE the sleeve
# is liquidated, not after: the module rebuilds toward
# ETF_TARGET_ALLOCATION on the next controller cycle, so selling SCHD and
# SCHG with this left True would simply buy them back within 5 minutes.
# BACK ON 2026-08-24, owner's instruction: "just turn the sleeve on for
# now." Reverses the 08-14 instruction recorded immediately above.
#
# WHY HE ASKED. He questioned why the project keeps running a strategy it
# believes is a losing bet, and said other bots make money. The account
# facts answered it: this sleeve is the one thing here that beat every
# control, it was already built and tested, and it was switched off ten
# days ago on his own instruction. Equity went $650 -> $371.26 in that
# window with $318.26 sitting idle in cash.
#
# WHAT IT IS AND IS NOT, kept because the distinction is the whole reason
# it wins: it beat the controls because it HOLDS BETA, not because it
# selects anything. That was always the argument for it -- momentum
# ranking lost to equal weight by 13 points a year, the entry rule lost
# to random entry, and holding the index beat every variant tested. It
# requires no edge, which is exactly why it is not evidence of one.
#
# LEFT DELIBERATELY UNCHANGED: ETF_PORTFOLIO_BUDGET stays at 160.00. The
# owner said "for now"; raising it to put the whole $318 to work is a
# SECOND decision he has not made, and quietly making it for him would be
# deciding how much of his account to commit.
#
# CLEARED BY LOCKBOT BEFORE FLIPPING (channel a8cd0d57). The failure I
# expected -- build_plan mechanically liquidating the sleeve to rebuild
# an options reserve once free cash falls below it -- requires a
# CASH-DERIVED budget to trigger. This budget is hand-set, so the loop
# cannot arise at this setting. It returns the moment anyone makes the
# ceiling cash-derived; do not do that without re-reading a8cd0d57.
#
# STILL BROKEN, AND NOT FIXED BY THIS: the flatten paths remain blind to
# reserved symbols, so a flatten while the sleeve holds shares would sell
# them. It does not block enabling the sleeve; it blocks flattening.
#
# ETF_PORTFOLIO_LIVE is already True, so this places real orders on the
# next controller cycle rather than reporting what it would do.
ETF_PORTFOLIO_ENABLED = True

# When False the module reports what it WOULD do and places nothing.
ETF_PORTFOLIO_LIVE = True

# Hard ceiling on capital committed to the portfolio, in dollars.
#
# Raised from 100 to 160 on 2026-08-05, on a decision to stop funding
# the trading engine. The reasoning is no longer "leave room for the
# options side to operate" -- the options side has a measured negative
# edge and is opening nothing new. It is now simply how much cash is
# available: $94 idle against $69 already held.
#
# NOT raised to cover the whole account, because ~$90 is still committed
# to two option positions that have not closed. Raise it again as they
# do, rather than setting a ceiling the account cannot currently fund.
#
# Whole shares only, so precision here is theatre: SCHD and SCHG at ~$34
# mean the sleeve moves in ~$69 steps and $20-30 will always sit
# unusable. The ceiling is a safety limit, not an allocation target.
ETF_PORTFOLIO_BUDGET = 160.00

# Target weights. Must sum to 1.0.
#
# Chosen for what FITS, not for a market view. Under the `small` profile
# only whole shares are possible, and at a $100 budget that rules out
# almost everything: SPY $771, VOO $709, QQQ $724, VTI $381, QQQM $298.
#
# SCHG is how growth is reachable at all. A single $35 share holds NVDA,
# MSFT, AAPL, AMZN and META — none of which can be bought individually
# here, since one NVDA share is $212 and one MSFT share is $493. The
# affordable individual growth names are speculative small caps, which is
# a different bet from "growth".
#
# SCHD balances it with dividend and value exposure at a similar price.
#
# International sleeves (VEA $72, VWO $60) are deliberately absent: at a
# $100 budget a 20% target is $20, which buys none of either. They belong
# here once the budget can carry them, and the module will say so rather
# than silently under-allocating.
ETF_TARGET_ALLOCATION = {
    "SCHG": 0.50,   # US large-cap growth — NVDA, MSFT, AAPL, AMZN, META
    "SCHD": 0.50,   # US dividend / value, as ballast
}

# Rebalance when a sleeve drifts this far from target, in percentage
# POINTS. At 10, a 40% sleeve is left alone between 30% and 50%.
# Rebalancing more often than that just pays spread to chase noise.
ETF_REBALANCE_DRIFT_POINTS = 10.0

# Never place a rebalancing order smaller than this. On a $100 budget a
# "correcting" trade of a few dollars costs more in spread than the drift
# it fixes.
# ETF_MIN_REBALANCE_DOLLARS = 25.00 stood here until 2026-08-19. Deleted on
# LOCKBOT's ruling (item 1c2b28b6): nothing read it, the ETF sleeve is off
# (ETF_PORTFOLIO_ENABLED = False since 2026-08-14), and the rebalance path
# that would have consulted it does not exist.
#
# The sleeve was turned back ON 2026-08-24, so one of those three reasons
# no longer holds. The deletion still stands on the other two: nothing
# reads it and there is still no rebalance path. Noted rather than left
# to read as currently true -- a stale justification is how a deleted
# setting gets restored for a reason that expired.

ETF_PORTFOLIO_STATE_FILE = PROJECT_FOLDER / "etf_portfolio_state.json"

OPTIONS_STATE_FILE = PROJECT_FOLDER / "options_position_state.json"
OPTIONS_PENDING_FILE = PROJECT_FOLDER / "options_pending_trades.csv"
OPTIONS_SHADOW_FILE = PROJECT_FOLDER / "options_shadow_log.csv"
OPTIONS_RISK_STATE_FILE = PROJECT_FOLDER / "options_risk_state.json"

# The options trade journal. This lived only inside options_manager.py
# until 2026-08-02, which is precisely the shape of the bug that once
# zeroed all equity performance reporting: two modules naming the same
# file independently, and nothing to notice when they drift apart. Every
# reader takes it from here.
OPTIONS_COMPLETED_FILE = PROJECT_FOLDER / "options_completed_trades.csv"

# Resolved counterfactuals for options decisions LOCKBOT logged but did
# not take -- the options equivalent of shadow_trades.csv.
OPTIONS_SHADOW_RESOLVED_FILE = (
    PROJECT_FOLDER / "options_shadow_resolved.csv"
)


# ============================================================
# Validation
# ============================================================

def validate_options_configuration() -> None:
    """Raise an error when an options setting is unsafe or unreachable."""

    if not OPTIONS_ENABLED:
        return

    if LIVE_TRADING_ENABLED:
        raise ValueError(
            "Options trading has not been validated against a live "
            "account. Keep LIVE_TRADING_ENABLED False while "
            "OPTIONS_ENABLED is True."
        )

    for name, value in (
        ("OPTIONS_MAX_RISK_PER_TRADE_PERCENT", OPTIONS_MAX_RISK_PER_TRADE_PERCENT),
        ("OPTIONS_MAX_PREMIUM_PERCENT", OPTIONS_MAX_PREMIUM_PERCENT),
        ("OPTIONS_MAX_TOTAL_PREMIUM_PERCENT", OPTIONS_MAX_TOTAL_PREMIUM_PERCENT),
        ("OPTIONS_TAKE_PROFIT_PERCENT", OPTIONS_TAKE_PROFIT_PERCENT),
        ("OPTIONS_STOP_LOSS_PERCENT", OPTIONS_STOP_LOSS_PERCENT),
        ("OPTIONS_MAX_SPREAD_PERCENT", OPTIONS_MAX_SPREAD_PERCENT),
    ):
        if not 0 < value < 1:
            raise ValueError(
                f"{name} is a fraction between 0 and 1 (0.35 means 35%). "
                f"Got {value}."
            )

    if OPTIONS_MAX_PREMIUM_PERCENT > OPTIONS_MAX_TOTAL_PREMIUM_PERCENT:
        raise ValueError(
            "OPTIONS_MAX_PREMIUM_PERCENT cannot exceed "
            "OPTIONS_MAX_TOTAL_PREMIUM_PERCENT."
        )

    reachable_premium = (
        OPTIONS_MAX_OPEN_POSITIONS * OPTIONS_MAX_PREMIUM_PERCENT
    )

    if reachable_premium < OPTIONS_MAX_TOTAL_PREMIUM_PERCENT:
        raise ValueError(
            "OPTIONS_MAX_TOTAL_PREMIUM_PERCENT "
            f"({OPTIONS_MAX_TOTAL_PREMIUM_PERCENT:.2f}) is higher than "
            f"OPTIONS_MAX_OPEN_POSITIONS x OPTIONS_MAX_PREMIUM_PERCENT "
            f"({reachable_premium:.2f}) can ever reach."
        )

    if OPTIONS_MAX_OPEN_POSITIONS <= 0:
        raise ValueError("OPTIONS_MAX_OPEN_POSITIONS must be greater than zero.")

    if OPTIONS_MAX_CONTRACTS_PER_POSITION <= 0:
        raise ValueError(
            "OPTIONS_MAX_CONTRACTS_PER_POSITION must be greater than zero."
        )

    if OPTIONS_MAX_NEW_ENTRIES_PER_CYCLE > OPTIONS_MAX_OPEN_POSITIONS:
        raise ValueError(
            "OPTIONS_MAX_NEW_ENTRIES_PER_CYCLE cannot exceed "
            "OPTIONS_MAX_OPEN_POSITIONS."
        )

    if OPTIONS_MAX_TRADES_PER_DAY < OPTIONS_MAX_OPEN_POSITIONS:
        raise ValueError(
            "OPTIONS_MAX_TRADES_PER_DAY is lower than "
            "OPTIONS_MAX_OPEN_POSITIONS, so the position slots could "
            "never all be filled in one day."
        )

    if not 0 < OPTIONS_MAX_MONEYNESS_PERCENT < 0.5:
        raise ValueError(
            "OPTIONS_MAX_MONEYNESS_PERCENT is a fraction between 0 and 0.5 "
            f"(0.07 means 7%). Got {OPTIONS_MAX_MONEYNESS_PERCENT}."
        )

    if not 0 < OPTIONS_TARGET_DELTA_MIN < OPTIONS_TARGET_DELTA_MAX < 1:
        raise ValueError(
            "Deltas must satisfy 0 < MIN < MAX < 1. Got "
            f"{OPTIONS_TARGET_DELTA_MIN} and {OPTIONS_TARGET_DELTA_MAX}."
        )

    if not 0 < OPTIONS_MIN_DTE < OPTIONS_MAX_DTE:
        raise ValueError(
            "OPTIONS_MIN_DTE must be greater than zero and less than "
            "OPTIONS_MAX_DTE."
        )

    if OPTIONS_MIN_DTE_EXIT >= OPTIONS_MIN_DTE:
        raise ValueError(
            "OPTIONS_MIN_DTE_EXIT must be below OPTIONS_MIN_DTE, or every "
            "position would be closed by the time-exit rule on the same "
            "cycle it was opened."
        )

    if OPTIONS_MAX_HOLD_DAYS <= 0:
        raise ValueError("OPTIONS_MAX_HOLD_DAYS must be greater than zero.")

    if OPTIONS_SPREAD_WIDTH_STRIKES <= 0:
        raise ValueError(
            "OPTIONS_SPREAD_WIDTH_STRIKES must be greater than zero."
        )

    valid_strategies = {
        "LONG_CALL",
        "LONG_PUT",
        "BULL_CALL_SPREAD",
        "BEAR_PUT_SPREAD",
        "NONE",
    }

    for regime, strategy in OPTIONS_REGIME_STRATEGY.items():
        if strategy not in valid_strategies:
            raise ValueError(
                f"OPTIONS_REGIME_STRATEGY[{regime!r}] is {strategy!r}, which "
                f"is not one of {sorted(valid_strategies)}."
            )

        if not OPTIONS_ALLOW_SPREADS and "SPREAD" in strategy:
            raise ValueError(
                f"OPTIONS_REGIME_STRATEGY maps {regime} to {strategy}, but "
                "OPTIONS_ALLOW_SPREADS is False."
            )

    # The collision described at the top of this section. Warn rather
    # than raise: it is a property of the account size, not a mistake.
    #
    # Corrected 2026-08-14. This used to quote risk/stop -- 28.6% at a 10%
    # risk limit and a 35% stop -- which was the ceiling BEFORE the
    # full-debit cap shipped on 2026-08-13. evaluate_contract now caps
    # cost_to_open at the per-trade risk percent outright, because with no
    # broker-side options stop the whole premium is the worst case. The
    # binding ceiling is therefore 10%, not 28.6%, and printing the old
    # number told a reader the account could take positions 2.9x larger
    # than it will actually accept.
    implied_max_premium = min(
        OPTIONS_MAX_RISK_PER_TRADE_PERCENT / OPTIONS_STOP_LOSS_PERCENT,
        OPTIONS_MAX_RISK_PER_TRADE_PERCENT,          # the full-debit cap
    )

    if implied_max_premium < OPTIONS_MAX_PREMIUM_PERCENT:
        print(
            "NOTE: the full-debit cap binds before the premium ceiling. "
            f"Contracts above {implied_max_premium * 100:.1f}% of equity "
            "will be rejected, so OPTIONS_MAX_PREMIUM_PERCENT "
            f"({OPTIONS_MAX_PREMIUM_PERCENT:.2f}) is not reachable. The cap "
            "assumes the software stop can fail, because it is the only "
            "stop there is."
        )


# ============================================================
# Runtime overrides
# ============================================================
#
# Applied AFTER every default above and BEFORE validate_configuration(),
# so a remotely changed setting is still subject to every safety check in
# this file. runtime_settings.py decides what may be changed; this only
# applies what it allows.
#
# Components are spawned fresh each cycle, so an override takes effect on
# the next cycle without a restart.
#
# PAPER_TRADING and LIVE_TRADING_ENABLED are not on that allowlist and
# cannot arrive here. The boundary between fake and real money stays a
# code change made by a person at the keyboard.
try:
    from runtime_settings import load_overrides as _load_overrides

    _OVERRIDES = _load_overrides()

    for _name, _value in _OVERRIDES.items():
        globals()[_name] = _value

    if _OVERRIDES:
        print(
            "Runtime overrides applied: "
            + ", ".join(f"{k}={v}" for k, v in sorted(_OVERRIDES.items()))
        )

except Exception as _override_error:      # pragma: no cover
    # A broken overrides layer must never stop LOCKBOT starting. The
    # defaults above are already loaded and are the safe fallback.
    print(f"Runtime overrides skipped: {type(_override_error).__name__}")
    _OVERRIDES = {}


def validate_configuration() -> None:
    """Raise an error when a shared configuration value is unsafe."""

    if LIVE_TRADING_ENABLED and PAPER_TRADING:
        raise ValueError(
            "LIVE_TRADING_ENABLED and PAPER_TRADING cannot both be True."
        )

    if LIVE_TRADING_ENABLED and not ENABLE_PAPER_EXITS:
        raise ValueError(
            "Live trading cannot be enabled while exits remain disabled."
        )

    if not SYMBOLS:
        raise ValueError("At least one trading symbol is required.")

    if len(set(SYMBOLS)) != len(SYMBOLS):
        raise ValueError("SYMBOLS contains duplicate entries.")

    for name, value in (
        ("SCAN_INTERVAL_SECONDS", SCAN_INTERVAL_SECONDS),
    ):
        if value < 60:
            raise ValueError(f"{name} must be at least 60 seconds.")

    if HEARTBEAT_WARNING_MINUTES <= 0:
        raise ValueError("HEARTBEAT_WARNING_MINUTES must be greater than zero.")

    if HEARTBEAT_CRITICAL_MINUTES <= HEARTBEAT_WARNING_MINUTES:
        raise ValueError(
            "HEARTBEAT_CRITICAL_MINUTES must be greater than "
            "HEARTBEAT_WARNING_MINUTES."
        )

    if MONITOR_STOP_LOSS_PERCENT >= 0:
        raise ValueError("MONITOR_STOP_LOSS_PERCENT must be negative.")

    if BREAK_EVEN_TRIGGER_PERCENT <= 0:
        raise ValueError("BREAK_EVEN_TRIGGER_PERCENT must be greater than zero.")

    if TRAILING_STOP_TRIGGER_PERCENT <= 0:
        raise ValueError("TRAILING_STOP_TRIGGER_PERCENT must be greater than zero.")

    if TRAILING_STOP_DISTANCE_PERCENT <= 0:
        raise ValueError("TRAILING_STOP_DISTANCE_PERCENT must be greater than zero.")

    if not 0 <= MIN_SIGNAL_CONFIDENCE <= 100:
        raise ValueError("MIN_SIGNAL_CONFIDENCE must be between 0 and 100.")

    if BRACKET_STOP_LOSS_PERCENT <= 0 or BRACKET_TAKE_PROFIT_PERCENT <= 0:
        raise ValueError(
            "BRACKET_STOP_LOSS_PERCENT and BRACKET_TAKE_PROFIT_PERCENT "
            "must both be greater than zero."
        )

    if MAX_OPEN_POSITIONS <= 0:
        raise ValueError("MAX_OPEN_POSITIONS must be greater than zero.")

    if MAX_TRADES_PER_DAY <= 0:
        raise ValueError("MAX_TRADES_PER_DAY must be greater than zero.")

    # --- v1.2 checks -------------------------------------------------

    # The trap the v1.1 settings were already in: a position count the
    # exposure ceiling makes unreachable.
    reachable_exposure = MAX_OPEN_POSITIONS * MAX_POSITION_VALUE_PERCENT

    if reachable_exposure < MAX_TOTAL_EXPOSURE_PERCENT:
        raise ValueError(
            "MAX_TOTAL_EXPOSURE_PERCENT "
            f"({MAX_TOTAL_EXPOSURE_PERCENT:.2f}) is higher than "
            f"MAX_OPEN_POSITIONS x MAX_POSITION_VALUE_PERCENT "
            f"({reachable_exposure:.2f}) can ever reach."
        )

    if MAX_SAME_DIRECTION_POSITIONS <= 0:
        raise ValueError(
            "MAX_SAME_DIRECTION_POSITIONS must be greater than zero."
        )

    if MAX_SAME_DIRECTION_POSITIONS > MAX_OPEN_POSITIONS:
        raise ValueError(
            "MAX_SAME_DIRECTION_POSITIONS cannot exceed MAX_OPEN_POSITIONS."
        )

    if MAX_NEW_ENTRIES_PER_CYCLE <= 0:
        raise ValueError(
            "MAX_NEW_ENTRIES_PER_CYCLE must be greater than zero."
        )

    if MAX_NEW_ENTRIES_PER_CYCLE > MAX_OPEN_POSITIONS:
        raise ValueError(
            "MAX_NEW_ENTRIES_PER_CYCLE cannot exceed MAX_OPEN_POSITIONS."
        )

    if MAX_TRADES_PER_DAY < MAX_OPEN_POSITIONS:
        raise ValueError(
            "MAX_TRADES_PER_DAY is lower than MAX_OPEN_POSITIONS, so the "
            "position slots could never all be filled in one day."
        )

    if ACCOUNT_PROFILE not in {"small", "standard"}:
        raise ValueError('ACCOUNT_PROFILE must be "small" or "standard".')

    if MAX_DAY_TRADES_PER_5_DAYS < 0:
        raise ValueError("MAX_DAY_TRADES_PER_5_DAYS cannot be negative.")

    if ACCOUNT_PROFILE == "small" and ALLOW_SHORT_ENTRIES:
        raise ValueError(
            "Shorting is not permitted below $2,000 of equity, so "
            "ALLOW_SHORT_ENTRIES must be False on the small profile."
        )

    if MAX_SCAN_SYMBOLS <= 0:
        raise ValueError("MAX_SCAN_SYMBOLS must be greater than zero.")

    if not 1 <= SCAN_BATCH_SIZE <= 500:
        raise ValueError("SCAN_BATCH_SIZE must be between 1 and 500.")

    if SCAN_LOOKBACK_DAYS_5M <= 0 or SCAN_LOOKBACK_DAYS_HIGHER <= 0:
        raise ValueError("Scan lookback windows must be greater than zero.")

    if SCAN_LOOKBACK_DAYS_HIGHER < SCAN_LOOKBACK_DAYS_5M:
        raise ValueError(
            "SCAN_LOOKBACK_DAYS_HIGHER should be at least as long as "
            "SCAN_LOOKBACK_DAYS_5M."
        )

    if UNIVERSE_STALE_HOURS <= 0:
        raise ValueError("UNIVERSE_STALE_HOURS must be greater than zero.")

    if UNIVERSE_MIN_PRICE <= 0:
        raise ValueError("UNIVERSE_MIN_PRICE must be greater than zero.")

    if UNIVERSE_MAX_PRICE <= UNIVERSE_MIN_PRICE:
        raise ValueError(
            "UNIVERSE_MAX_PRICE must be greater than UNIVERSE_MIN_PRICE."
        )

    if UNIVERSE_TOP_N <= 0:
        raise ValueError("UNIVERSE_TOP_N must be greater than zero.")

    if UNIVERSE_MIN_BARS <= 0 or UNIVERSE_MIN_BARS > UNIVERSE_LOOKBACK_DAYS:
        raise ValueError(
            "UNIVERSE_MIN_BARS must be between 1 and UNIVERSE_LOOKBACK_DAYS."
        )

    if ALPACA_DATA_FEED not in {"iex", "sip"}:
        raise ValueError('ALPACA_DATA_FEED must be either "iex" or "sip".')

    if USE_UNIVERSE_FILE and MAX_SCAN_SYMBOLS > UNIVERSE_TOP_N:
        raise ValueError(
            "MAX_SCAN_SYMBOLS is larger than the number of symbols "
            "universe.py saves (UNIVERSE_TOP_N)."
        )

    # --- v1.4 checks -------------------------------------------------
    # These are fractions, not percentages. Writing 3 where 0.03 was
    # meant would silently filter the entire universe away, so the
    # bounds below are deliberately tight enough to catch that.

    if not 0 < UNIVERSE_MIN_ATR_PERCENT < 0.5:
        raise ValueError(
            "UNIVERSE_MIN_ATR_PERCENT is a fraction between 0 and 0.5 "
            f"(0.0125 means 1.25%). Got {UNIVERSE_MIN_ATR_PERCENT}."
        )

    if not 0 < UNIVERSE_MAX_ATR_PERCENT < 1.0:
        raise ValueError(
            "UNIVERSE_MAX_ATR_PERCENT is a fraction between 0 and 1.0 "
            f"(0.030 means 3%). Got {UNIVERSE_MAX_ATR_PERCENT}."
        )

    if UNIVERSE_MAX_ATR_PERCENT <= UNIVERSE_MIN_ATR_PERCENT:
        raise ValueError(
            "UNIVERSE_MAX_ATR_PERCENT must be greater than "
            "UNIVERSE_MIN_ATR_PERCENT, or no symbol can ever qualify."
        )

    if not 0 < ATR_STOP_MULTIPLIER <= 5:
        raise ValueError(
            "ATR_STOP_MULTIPLIER must be greater than 0 and no more than 5."
        )

    if ATR_REWARD_RATIO <= 0:
        raise ValueError("ATR_REWARD_RATIO must be greater than zero.")

    if ATR_REWARD_RATIO < 1:
        raise ValueError(
            "ATR_REWARD_RATIO below 1 means a winner pays less than a loser "
            "costs, which needs a win rate above 50% just to break even."
        )

    if not 0 < ATR_MIN_STOP_PERCENT < 0.5:
        raise ValueError(
            "ATR_MIN_STOP_PERCENT is a fraction between 0 and 0.5 "
            f"(0.015 means 1.5%). Got {ATR_MIN_STOP_PERCENT}."
        )

    if not 0 < ATR_MAX_STOP_PERCENT < 1.0:
        raise ValueError(
            "ATR_MAX_STOP_PERCENT is a fraction between 0 and 1.0 "
            f"(0.060 means 6%). Got {ATR_MAX_STOP_PERCENT}."
        )

    if ATR_MAX_STOP_PERCENT <= ATR_MIN_STOP_PERCENT:
        raise ValueError(
            "ATR_MAX_STOP_PERCENT must be greater than ATR_MIN_STOP_PERCENT."
        )

    # A stop wider than the position cap allows would mean the risk
    # budget can never be met by any affordable share count.
    if USE_ADAPTIVE_BRACKETS and ATR_MAX_STOP_PERCENT > MAX_DAILY_LOSS_PERCENT * 5:
        print(
            "NOTE: ATR_MAX_STOP_PERCENT "
            f"({ATR_MAX_STOP_PERCENT:.3f}) is large relative to the daily "
            f"loss limit ({MAX_DAILY_LOSS_PERCENT:.3f}). Two stopped-out "
            "trades could halt trading for the day."
        )


def configuration_summary() -> dict[str, object]:
    """Return a compact configuration summary for diagnostics."""

    return {
        "project_version": LOCKBOT_PROJECT_VERSION,
        "config_version": LOCKBOT_CONFIG_VERSION,
        "paper_trading": PAPER_TRADING,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "paper_exits_enabled": ENABLE_PAPER_EXITS,
        "account_profile": ACCOUNT_PROFILE,
        "allow_short_entries": ALLOW_SHORT_ENTRIES,
        "max_day_trades_per_5_days": MAX_DAY_TRADES_PER_5_DAYS,
        "universe_price_range": f"${UNIVERSE_MIN_PRICE:.0f}-${UNIVERSE_MAX_PRICE:.0f}",
        "universe_movement_range": (
            f"{UNIVERSE_MIN_ATR_PERCENT * 100:.2f}%"
            f"-{UNIVERSE_MAX_ATR_PERCENT * 100:.2f}% per day"
        ),
        "use_universe_file": USE_UNIVERSE_FILE,
        "fallback_symbols": SYMBOLS,
        "max_scan_symbols": MAX_SCAN_SYMBOLS,
        "scan_batch_size": SCAN_BATCH_SIZE,
        "alpaca_data_feed": ALPACA_DATA_FEED,
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        # Both of these used to publish their own constants. They named a
        # per-module cadence that has never existed: everything runs in one
        # controller cycle on SCAN_INTERVAL_SECONDS. Reporting the real
        # number rather than deleting the keys, so any reader that expects
        # them keeps working and now gets the truth.
        "position_monitor_interval_seconds": SCAN_INTERVAL_SECONDS,
        "trade_manager_interval_seconds": SCAN_INTERVAL_SECONDS,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_same_direction_positions": MAX_SAME_DIRECTION_POSITIONS,
        "max_new_entries_per_cycle": MAX_NEW_ENTRIES_PER_CYCLE,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "max_total_exposure_percent": MAX_TOTAL_EXPOSURE_PERCENT,
        "max_position_value_percent": MAX_POSITION_VALUE_PERCENT,
        "max_risk_per_trade_percent": MAX_RISK_PER_TRADE_PERCENT,
        "max_daily_loss_percent": MAX_DAILY_LOSS_PERCENT,
        "use_adaptive_brackets": USE_ADAPTIVE_BRACKETS,
        "bracket_stop_loss_percent": BRACKET_STOP_LOSS_PERCENT,
        "bracket_take_profit_percent": BRACKET_TAKE_PROFIT_PERCENT,
        "atr_stop_multiplier": ATR_STOP_MULTIPLIER,
        "atr_reward_ratio": ATR_REWARD_RATIO,
        "atr_stop_range": (
            f"{ATR_MIN_STOP_PERCENT * 100:.2f}%"
            f"-{ATR_MAX_STOP_PERCENT * 100:.2f}%"
        ),
        "heartbeat_warning_minutes": HEARTBEAT_WARNING_MINUTES,
        "heartbeat_critical_minutes": HEARTBEAT_CRITICAL_MINUTES,
        "options_enabled": OPTIONS_ENABLED,
        "options_shadow_mode": OPTIONS_SHADOW_MODE,
        "options_max_open_positions": OPTIONS_MAX_OPEN_POSITIONS,
        "options_max_risk_per_trade_percent": OPTIONS_MAX_RISK_PER_TRADE_PERCENT,
        "options_dte_window": f"{OPTIONS_MIN_DTE}-{OPTIONS_MAX_DTE} days",
        "options_delta_window": (
            f"{OPTIONS_TARGET_DELTA_MIN:.2f}-{OPTIONS_TARGET_DELTA_MAX:.2f}"
        ),
        "options_max_spread_percent": OPTIONS_MAX_SPREAD_PERCENT,
        "options_exit_rules": (
            f"+{OPTIONS_TAKE_PROFIT_PERCENT * 100:.0f}% / "
            f"-{OPTIONS_STOP_LOSS_PERCENT * 100:.0f}% / "
            f"{OPTIONS_MAX_HOLD_DAYS}d / {OPTIONS_MIN_DTE_EXIT} DTE"
        ),
    }


if __name__ == "__main__":
    validate_configuration()
    validate_options_configuration()

    print("=" * 60)
    print("LOCKBOT CENTRAL CONFIGURATION")
    print("=" * 60)

    for key, value in configuration_summary().items():
        print(f"{key:<34}: {value}")

    print("=" * 60)

    if USE_ADAPTIVE_BRACKETS:
        print("Bracket mode                      : ADAPTIVE (per stock)")
        print(
            f"Risk per trade (budgeted)         : "
            f"{MAX_RISK_PER_TRADE_PERCENT * 100:.2f}% of equity"
        )
        print(
            f"Risk with all positions open      : "
            f"{MAX_RISK_PER_TRADE_PERCENT * MAX_OPEN_POSITIONS * 100:.2f}% "
            f"of equity"
        )
        print(
            "Position size varies by stock: a wider stop buys fewer shares "
            "so the dollars at risk stay fixed."
        )
    else:
        estimated_risk_per_trade = (
            MAX_POSITION_VALUE_PERCENT * BRACKET_STOP_LOSS_PERCENT
        )

        print("Bracket mode                      : FIXED (same for every stock)")
        print(
            f"Risk per trade (position x stop) : "
            f"{estimated_risk_per_trade * 100:.2f}% of equity"
        )
        print(
            f"Risk with all positions open     : "
            f"{estimated_risk_per_trade * MAX_OPEN_POSITIONS * 100:.2f}% of equity"
        )

    print("=" * 60)
    print("Status                            : READY")