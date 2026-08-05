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
EQUITY_ENTRIES_ENABLED = False

# Bracket orders are LOCKBOT's sole exit mechanism. Keep this False —
# position_monitor.py must stay monitoring/alerting only. See its
# module docstring for the full rationale.
ENABLE_PAPER_EXITS = False

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
UNIVERSE_MIN_ATR_PERCENT = 0.0125
UNIVERSE_MAX_ATR_PERCENT = 0.030

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
ALPACA_DATA_FEED = "iex"


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
POSITION_MONITOR_INTERVAL_SECONDS = 300
TRADE_MANAGER_INTERVAL_SECONDS = 300
HEALTH_MONITOR_INTERVAL_SECONDS = 300
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
    MAX_DAILY_LOSS_PERCENT = 0.02
    MAX_SAME_DIRECTION_POSITIONS = 2
    MAX_NEW_ENTRIES_PER_CYCLE = 1

    # Long only. Shorting is not permitted below $2,000 of equity.
    ALLOW_SHORT_ENTRIES = False

    # Stop opening new positions once the broker's rolling 5-day
    # day-trade count reaches this. Read straight from Alpaca's own
    # daytrade_count field, so it matches what the broker enforces.
    # Set to 0 to disable the check.
    MAX_DAY_TRADES_PER_5_DAYS = 3

    # Cheap stocks only, or nothing is affordable. A $50 ceiling
    # means every position buys at least two shares.
    UNIVERSE_MIN_PRICE = 5.00
    UNIVERSE_MAX_PRICE = 50.00
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

CONTINUOUS_MODULES = {"CONTROLLER"}

SCHEDULED_MODULES = {
    "MARKET_SCANNER",
    "POSITION_MONITOR",
    "TRADE_MANAGER",
    # OPTIONS_MANAGER is the software stop loss for every open option
    # position. A stale heartbeat here means those positions are
    # currently unprotected — treat it as urgently as a broker outage.
    "OPTIONS_MANAGER",
    "OPTIONS_SCANNER",
}

ON_DEMAND_MODULES = {"HEALTH_MONITOR"}


# ============================================================
# Notifications
# ============================================================

NOTIFY_ON_STARTUP = True
NOTIFY_ON_SHUTDOWN = True
NOTIFY_ON_TRADE_SIGNAL = True
NOTIFY_ON_ORDER_SUBMISSION = True
NOTIFY_ON_EXIT_SIGNAL = True
NOTIFY_ON_CRITICAL_ERROR = True
NOTIFY_ON_HEARTBEAT_DEGRADED = True


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
OPTIONS_SHADOW_MODE = True

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
OPTIONS_ENTRY_FILL_TIMEOUT_MINUTES = 15

# How far above the ask an entry limit is placed. A limit at exactly the
# ask has no cushion: two PBR calls on 2026-07-30 never filled, the second
# stranded within 45 seconds when the ask moved 0.48 -> 0.52. This is the
# price of getting filled at all, and it is charged against
# OPTIONS_MAX_RISK_PER_TRADE_PERCENT rather than sneaking past it. Set to
# 0.0 to restore at-the-touch pricing.
OPTIONS_ENTRY_LIMIT_BUFFER_PERCENT = 0.03

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
OPTIONS_TARGET_DELTA_MIN = 0.35
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
OPTIONS_MAX_SPREAD_PERCENT = 0.05
OPTIONS_MIN_OPEN_INTEREST = 100
OPTIONS_MIN_CONTRACT_VOLUME = 10

# A zero bid means there is no buyer at any price — the position
# cannot be exited. Never enter one of these.
OPTIONS_REQUIRE_NONZERO_BID = True

# Exits, all measured against the premium paid, not the underlying.
OPTIONS_TAKE_PROFIT_PERCENT = 0.50
OPTIONS_STOP_LOSS_PERCENT = 0.35

# Time-based exits. Both are theta protection, not signal logic.
OPTIONS_MAX_HOLD_DAYS = 10
OPTIONS_MIN_DTE_EXIT = 14

# Strategy per regime. LONG_CALL / LONG_PUT buy premium outright
# and want a strong directional move. The debit spreads cost less
# and decay less, at the cost of a capped payoff — the right trade
# when the trend is real but weak, or when volatility is high and
# outright premium is expensive.
OPTIONS_ALLOW_SPREADS = True
OPTIONS_SPREAD_WIDTH_STRIKES = 1

OPTIONS_REGIME_STRATEGY = {
    "STRONG_UPTREND": "LONG_CALL",
    "STRONG_DOWNTREND": "LONG_PUT",
    "WEAK_UPTREND": "BULL_CALL_SPREAD",
    "WEAK_DOWNTREND": "BEAR_PUT_SPREAD",
    "HIGH_VOLATILITY": "BULL_CALL_SPREAD",
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
# account. What does need checking is whether the reserved-symbol
# mechanism holds against a REAL holding — it has only ever been tested
# against synthetic positions, and this week produced four separate bugs
# that passed their tests and failed in reality.
ETF_PORTFOLIO_ENABLED = True

# When False the module reports what it WOULD do and places nothing.
ETF_PORTFOLIO_LIVE = True

# Hard ceiling on capital committed to the portfolio, in dollars.
# Deliberately small: at $253 equity with $164 cash, the options side
# needs room to keep operating while this is evaluated.
ETF_PORTFOLIO_BUDGET = 100.00

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
ETF_MIN_REBALANCE_DOLLARS = 25.00

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
    implied_max_premium = (
        OPTIONS_MAX_RISK_PER_TRADE_PERCENT / OPTIONS_STOP_LOSS_PERCENT
    )

    if implied_max_premium < OPTIONS_MAX_PREMIUM_PERCENT:
        print(
            "NOTE: the options risk ceiling binds before the premium "
            f"ceiling. Contracts above {implied_max_premium * 100:.1f}% of "
            "equity will be rejected as unaffordable, so "
            f"OPTIONS_MAX_PREMIUM_PERCENT ({OPTIONS_MAX_PREMIUM_PERCENT:.2f}) "
            "is not reachable. This is expected on a small account."
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
        ("POSITION_MONITOR_INTERVAL_SECONDS", POSITION_MONITOR_INTERVAL_SECONDS),
        ("TRADE_MANAGER_INTERVAL_SECONDS", TRADE_MANAGER_INTERVAL_SECONDS),
        ("HEALTH_MONITOR_INTERVAL_SECONDS", HEALTH_MONITOR_INTERVAL_SECONDS),
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
        "position_monitor_interval_seconds": POSITION_MONITOR_INTERVAL_SECONDS,
        "trade_manager_interval_seconds": TRADE_MANAGER_INTERVAL_SECONDS,
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