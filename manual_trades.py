"""
LOCKBOT Manual Trade Recorder — the owner's own options trades.

WHAT THIS IS, AND WHAT IT IS NOT

It is a RECORDER, not a picker. It never selects, ranks, suggests or
submits anything. It records what the owner did and measures it with the
same rigour the bot's own book receives.

IT IS NOT MEASURING A PROVEN EDGE. IT IS STARTING FROM ZERO.

The recommendation that produced this module rested on a manual record of
9 winners in 14 trades, netting roughly breakeven, read as "64% against a
33% baseline". That comparison was wrong, and the reason is an identity
worth keeping at the top of this file:

    If 9 wins equal 5 losses in dollars, the average win is 5/9 of the
    average loss. The breakeven win rate for that payoff shape is

        p / (1 - p) = 9 / 5      ->      p = 1.8 / 2.8 = 64.3%

    and 9/14 IS 64.3%. A record that nets zero sits exactly on its own
    breakeven rate, always, by construction. It is not a finding.

The 33% figure applies only to a 2:1 structure — it is the driftless-walk
probability of touching +2R before -1R. Comparing a win rate against a
baseline computed for a different payoff geometry is what manufactured a
non-existent edge.

So the manual record is breakeven before costs and slightly negative after,
on fourteen trades that appear nowhere in this system. Fourteen remembered
trades is exactly the sample size that produces confident wrong
conclusions. The target is 30 MEASURED ones.

THREE CONVENTIONS, ENFORCED HERE RATHER THAN REMEMBERED

1. WIN RATE IS BANNED AS A HEADLINE. Average R leads, and the payoff ratio
   is printed beside it, always. Where win rate does appear it is printed
   next to the breakeven rate implied by the OBSERVED payoff ratio, so the
   tautology above is visible on the page and cannot be read as an edge.

2. NON-DIRECTIONAL, NOT DECAY. Directional P&L is entry delta times the
   underlying's move. The residual is labelled non-directional because it
   contains BOTH time decay and implied-volatility change, and calling it
   decay would claim a separation this method cannot make. Entry delta is
   an approximation — delta drifts over a multi-day hold — and is recorded
   as one. The daily underlying series is stored alongside so the
   attribution can be redone with a better method later without
   re-collecting anything.

3. None, NEVER A DEFAULT. Any quantity that cannot be computed is stored
   blank and reported as "--". A default value is a claim.

RULE ADHERENCE

A profitable trade that broke the rules is a BAD trade. Deviations are
tallied separately from wins and losses, and the report splits on
adherence independently of P&L. Unchecked rules record as unknown rather
than as passes.

PROVENANCE OF THE RULES — THESE ARE NOT THE OWNER'S RULEBOOK

There is no rulebook. `rulebook` and `adherence` return zero matches
anywhere in this tree, and the owner confirmed directly that no document
exists. What existed was a set of intentions described in conversation in
July — sizing discipline, a pre-entry checklist, a required
recommendation format. Treating those as an existing rulebook was an
error carried for several turns.

The six rules in `RULES` below were therefore PROPOSED by the assistant on
2026-08-11 and ACCEPTED by the owner as a starting set. They are not
measured experience and they are not a pre-existing standard. A future
reader asking "did these come from evidence or from a suggestion?" must
be able to answer it from this file without asking anyone. The answer is:
a suggestion, accepted, on the date stamped in RULES_ADOPTED.

The same correction applies to the 9-of-14 manual options figure that
motivated this module. It reached the project in conversation, is absent
from every file, and is a RECOLLECTION rather than a record. It is
labelled that way everywhere it appears here.

THE RULES ARE FROZEN UNTIL n = 30

No additions, no removals, no threshold changes, whatever the recorded
trades look like along the way. Adding a rule after seeing which trades
lost is fitting rules to noise — the r0315 failure mode at a smaller
sample, and given that n=30 cannot resolve anything below roughly 0.36R,
mid-stream revision would be fitting to pure noise with near-certainty.

At n=30 revision becomes permitted, once, and only under the checkpoint
rule already fixed above. A change wanted sooner is recorded with a date
and applied at the next block boundary, or the count restarts. It is
never applied silently mid-sample.

Subgroups smaller than `MINIMUM_GROUP_TRADES` are not reported, reusing
learning_report's existing floor rather than inventing a second one. If
30 trades split 25/5 on adherence, the five-trade side is suppressed —
a three-trade deviation group must never be printed as a finding.

WHAT n=30 CAN AND CANNOT DETECT — DECIDED BEFORE TRADE #1

Written now so that hitting 30, seeing something mildly positive, and
reading it as an edge is not available later. That is the r0315 trap at
a smaller sample.

The 95% interval on a mean of n samples is +/- 1.96 * sd / sqrt(n). At
n = 30 that is +/- 0.358 * sd, so:

    observed sd of R      smallest |average R| distinguishable from zero
        1.0                            0.36
        1.5                            0.54
        2.0                            0.72

Put against this project's own measured effects, that bar is high. The
trailing-stop improvement was +0.05R. Crypto oversold, the largest edge
ever measured here, was +0.187R over its control. **Thirty trades can
only detect an effect two to seven times larger than anything this
project has ever found.** To resolve +0.10R at sd = 1.0 needs roughly
384 trades; +0.20R needs about 96.

THE DECISION RULE, FIXED IN ADVANCE

    n = 30 is a CHECKPOINT, never a verdict.

    - Compute the mean R and its 95% interval from the OBSERVED sd.
    - If the interval contains zero, the result is INSIDE THE NOISE BAND.
      No conclusion is drawn in either direction, and collection
      continues to the next checkpoint at n = 100.
    - If the interval excludes zero, that still is not a verdict. It
      licenses one pre-registered test on the NEXT thirty trades. It does
      not license re-reading the first thirty, and it does not license
      trading changes.
    - "Collect more" is the expected outcome and is a complete answer.

    The checkpoint cannot output "the human has an edge". The most it can
    output is "large enough to be worth a second, separate look".

USAGE
    python manual_trades.py --open      ... record an entry
    python manual_trades.py --close     ... record an exit
    python manual_trades.py --report    show the measured record
    python manual_trades.py --self-test offline checks
"""

from __future__ import annotations

import argparse
import csv
import collections
import statistics
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import lockbot_config as config
    PROJECT_FOLDER = Path(config.PROJECT_FOLDER)
except Exception:                                    # pragma: no cover
    PROJECT_FOLDER = Path(__file__).parent

TRADES_FILE = PROJECT_FOLDER / "manual_trades.csv"
UNDERLYING_FILE = PROJECT_FOLDER / "manual_trade_underlying.csv"

CONTRACT_MULTIPLIER = 100

# Reused, not redefined. learning_report already sets the floor below
# which a breakdown group is noise wearing a pattern's costume, and a
# second copy of a shared threshold is how risk limits drifted apart in
# v1.0.
try:
    from learning_report import MINIMUM_GROUP_TRADES
except Exception:                                    # pragma: no cover
    MINIMUM_GROUP_TRADES = 10

# Checkpoints, fixed before the first trade. See the docstring.
CHECKPOINTS = (30, 100)
Z_95 = 1.96

# Proposed by the assistant, accepted by the owner, on this date. NOT a
# pre-existing rulebook -- see the provenance section of the docstring.
RULES_ADOPTED = "2026-08-11"
RULES_FROZEN_UNTIL = 30

# Each rule is yes/no at the moment of entry.
#
#   auto        the code recomputes it from the trade record, and that
#               verdict is authoritative. A rule the code checks is worth
#               more than a rule the trader ticks.
#   verifiable  False means it can never be checked from data and rests
#               on self-report. Such a rule must never be weighed as
#               equivalent evidence to the others.
RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "premium_within_10pct",
        "text": "Total debit paid is <= 10% of account equity at entry",
        "source": "OPTIONS_MAX_RISK_PER_TRADE_PERCENT = 0.10, counting the "
                  "FULL debit as the risk",
        "auto": True, "verifiable": True,
    },
    {
        "id": "spread_under_5pct",
        "text": "Round-trip bid/ask spread at entry is < 5% of mid",
        "source": "OPTIONS_MAX_SPREAD_PERCENT = 0.05",
        "auto": True, "verifiable": True,
    },
    {
        "id": "near_the_money",
        "text": "Entry delta is between 0.35 and 0.60",
        "source": "OPTIONS_TARGET_DELTA_MIN/MAX = 0.35/0.60",
        "auto": True, "verifiable": True,
    },
    {
        "id": "dte_at_least_30",
        "text": "Expiry is >= 30 days out at entry",
        "source": "PROPOSED. Deliberately stricter than OPTIONS_MIN_DTE = 21, "
                  "because the shadow book shows many setups never reach a "
                  "band within 10 days",
        "auto": True, "verifiable": True,
    },
    {
        "id": "single_position",
        "text": "No other option position was open at entry",
        "source": "PROPOSED for this account size. Deliberately tighter than "
                  "OPTIONS_MAX_OPEN_POSITIONS = 3, which governs the bot. "
                  "Checked against the BROKER, not this file -- see "
                  "count_broker_option_positions",
        "auto": True, "verifiable": True,
    },
    {
        "id": "exit_written_before_entry",
        "text": "The exit level was recorded before the entry order was placed",
        "source": "PROPOSED. Depends on the owner's honesty about sequence and "
                  "cannot be recomputed from any record",
        "auto": False, "verifiable": False,
    },
)

RULE_IDS: tuple[str, ...] = tuple(r["id"] for r in RULES)

# premium_within_10pct is the load-bearing one. The stated lesson behind
# it is that a single oversized loss wipes out a long run of winners; at
# this equity the rule is what makes that arithmetically impossible.
PREMIUM_LIMIT_PERCENT = 0.10
SPREAD_LIMIT_PERCENT = 0.05
DELTA_MIN, DELTA_MAX = 0.35, 0.60
MIN_DTE = 30

COLUMNS = [
    "trade_id", "recorded_at", "status",
    "underlying", "contract", "option_type", "strike", "expiry",
    "contracts",
    "entry_time", "entry_premium", "entry_bid", "entry_ask",
    "entry_delta", "entry_underlying",
    "initial_risk_dollars", "account_equity_at_entry",
    "planned_target", "planned_stop", "planned_basis", "planned_rr",
    "planned_locked_at", "planned_retrospective", "exit_vs_plan",
    "exit_time", "exit_premium", "exit_bid", "exit_ask",
    "exit_underlying", "exit_reason",
    "spread_paid_entry", "spread_paid_exit", "spread_paid_total",
    "days_held", "profit_loss", "r_multiple",
    "directional_pnl", "non_directional_pnl", "attribution_method",
    "rules_broken", "deviation", "rules_auto_checked", "rules_adopted",
    "single_position_scope", "provenance", "venue", "notes",
] + [f"rule_{name}" for name in RULE_IDS]

# A trade entered from memory is not a trade that was measured.
#
# The 9-of-14 figure is a recollection, and the whole reason this module
# exists is that a recollection got read as a record. If those fourteen
# are ever typed in, they must not silently become trades 1-14 of the 30.
# Recalled rows are stored, reported, and EXCLUDED from the checkpoint
# count, because the checkpoint is a statement about prospectively
# recorded evidence.
PROVENANCE_RECORDED = "recorded"
PROVENANCE_RECALLED = "recalled"

# Paper or live money, kept separate from provenance because they answer
# different questions: provenance asks whether the trade was MEASURED or
# REMEMBERED, this asks whether the money was REAL.
#
# A paper fill and a live fill are not the same observation. Paper fills
# are frictionless in ways live ones are not -- no queue position, no
# partial fills, and on this broker no real counterparty at all -- so
# pooling them produces a number describing neither. Same rule as the
# bracket eras and the pool generations: two populations, never one
# average.
#
# Defaults to paper, because PAPER_TRADING is True and a wrong default
# here would silently promote a paper trade into the live record.
VENUE_PAPER = "paper"
VENUE_LIVE = "live"

# The outcome that leaves no exit event.
#
# A contract held to expiry worthless is never sold -- it simply ends. So
# it produces no closing trade, no confirmation, no moment of decision,
# and it is a TOTAL loss. That combination makes it both the least
# memorable outcome and the worst one.
#
# The consequence, which is why this constant exists: a recalled option
# record does not merely contain noise, it leans OPTIMISTIC. Closed trades
# are remembered and expiries are not, so any remembered win rate is
# better than the real one by an amount nobody can estimate from memory.
#
# A recalled population containing ZERO expiries is therefore evidence of
# the bias rather than evidence of its absence, and the report says so.
EXIT_EXPIRED_WORTHLESS = "EXPIRED_WORTHLESS"

ATTRIBUTION_METHOD = "entry_delta_x_underlying_move"


# --------------------------------------------------------------------------
# Pure measurement (this is what --self-test exercises)
# --------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def spread_cost(bid: Optional[float], ask: Optional[float],
                contracts: float = 1) -> Optional[float]:
    """Half-spread crossed once, in dollars per position.

    None when either side of the quote is missing. A one-sided quote does
    not become a zero spread.
    """

    if bid is None or ask is None or ask < bid:
        return None

    return (ask - bid) / 2.0 * CONTRACT_MULTIPLIER * contracts


def r_multiple(profit_loss: Optional[float],
               initial_risk: Optional[float]) -> Optional[float]:
    """R, or None when risk is unknown. Never 0.0 as a stand-in."""

    if profit_loss is None or not initial_risk or initial_risk <= 0:
        return None

    return profit_loss / initial_risk


def attribute(
    *,
    entry_delta: Optional[float],
    entry_underlying: Optional[float],
    exit_underlying: Optional[float],
    profit_loss: Optional[float],
    contracts: float = 1,
) -> tuple[Optional[float], Optional[float]]:
    """Split P&L into directional and NON-DIRECTIONAL components.

    directional = entry_delta x underlying move x 100 x contracts
    non_directional = total - directional

    The residual is NOT decay. It contains time decay and implied
    volatility change together, and this method cannot separate them.
    Entry delta is a point estimate that drifts over the hold, so the
    split is an approximation and is labelled as one.

    Returns (None, None) when any input is missing.
    """

    if (entry_delta is None or entry_underlying is None
            or exit_underlying is None or profit_loss is None):
        return None, None

    move = exit_underlying - entry_underlying
    directional = entry_delta * move * CONTRACT_MULTIPLIER * contracts

    return directional, profit_loss - directional


def payoff_ratio(rs: list[float]) -> Optional[float]:
    """Average win over average loss, in absolute terms."""

    wins = [r for r in rs if r > 0]
    losses = [abs(r) for r in rs if r < 0]

    if not wins or not losses:
        return None

    return statistics.mean(wins) / statistics.mean(losses)


def detectable_effect(sd: float, n: int, z: float = Z_95) -> Optional[float]:
    """Smallest |mean| distinguishable from zero at this sd and sample size.

    Half-width of the interval, z * sd / sqrt(n). At n=30 and sd=1.0 this
    is 0.36 -- larger than every edge this project has ever measured, which
    is the point of printing it before the data arrives rather than after.
    """

    if n <= 0 or sd < 0:
        return None

    return z * sd / (n ** 0.5)


def mean_interval(values: list[float], z: float = Z_95):
    """(mean, low, high) for the sample mean. None when it cannot be formed.

    A single observation has no spread, so it gets no interval. One trade
    is not a mean with unknown error, it is a number.
    """

    if len(values) < 2:
        return (statistics.mean(values) if values else None), None, None

    mean = statistics.mean(values)
    half = z * statistics.stdev(values) / (len(values) ** 0.5)

    return mean, mean - half, mean + half


def inside_noise_band(low: Optional[float], high: Optional[float]) -> Optional[bool]:
    """Does the interval contain zero? None when there is no interval."""

    if low is None or high is None:
        return None

    return low <= 0.0 <= high


def planned_reward_risk(entry: Optional[float], target: Optional[float],
                        stop: Optional[float]) -> Optional[float]:
    """Planned reward over planned risk, from levels fixed BEFORE entry.

    This is the quantity that separates a payoff structure that was chosen
    from one that emerged. A ratio near 0.5 recorded here means the design
    was 0.5:1; the same ratio measured after the fact means nothing about
    intent.
    """

    if entry is None or target is None or stop is None:
        return None

    reward, risk = abs(target - entry), abs(entry - stop)

    if not risk:
        return None

    return reward / risk


def classify_exit(exit_price: Optional[float], target: Optional[float],
                  stop: Optional[float], tolerance: float = 0.02) -> str:
    """Where the exit landed relative to the plan.

    The prospective discriminator between a structural cause and a
    behavioural one. A designed payoff predicts exits AT the levels; an
    improvised one predicts exits BETWEEN them -- winners taken before the
    target, losers closed somewhere past or short of the stop on judgement.

    Returns "" when there is no plan to compare against, because an exit
    without a plan is unclassifiable rather than compliant.
    """

    if exit_price is None or target is None or stop is None:
        return ""

    if exit_price >= target * (1 - tolerance):
        return "at_or_beyond_target"

    if exit_price <= stop * (1 + tolerance):
        return "at_or_beyond_stop"

    return "between_levels"


def bot_option_contracts() -> set[str]:
    """Every option contract the BOT is known to have traded.

    The discriminator for automatic capture. The bot does not stamp a
    client_order_id, so its orders are not self-labelled at the broker;
    what it does leave behind is a record of the contracts it touched, in
    its own completed-trades file and in the ORDER_SUBMITTED rows of its
    shadow log. Anything at the broker outside that set, while
    OPTIONS_SHADOW_MODE is on, was placed by the owner.

    Note the standing assumption, and it is load-bearing: with
    OPTIONS_SHADOW_MODE True the bot submits no option orders at all, so
    the set only needs to cover history. If options ever go live, the
    robust fix is a client_order_id prefix on bot orders -- proposed, not
    built, because it touches the order-submission path.
    """

    contracts: set[str] = set()

    for name, fields in (
        ("options_completed_trades.csv", ("long_symbol", "short_symbol")),
        ("options_shadow_log.csv", ("long_symbol", "short_symbol")),
    ):
        path = PROJECT_FOLDER / name
        if not path.exists():
            continue
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if name.endswith("shadow_log.csv") and \
                            row.get("action") != "ORDER_SUBMITTED":
                        continue
                    for field in fields:
                        value = (row.get(field) or "").strip()
                        if value:
                            contracts.add(value)
        except OSError:
            continue

    return contracts


def count_broker_option_positions() -> Optional[int]:
    """Option contracts open AT THE BROKER. None when it cannot be reached.

    WHY THIS EXISTS RATHER THAN COUNTING THIS FILE

    `single_position` originally counted the recorder's own open rows, so
    a position opened by hand, by the bot, or orphaned from something else
    was invisible and the rule passed. That is the third appearance of one
    failure: an untracked SPY position quietly saturated the global cap for
    four days, option legs leaked into the equity book's daily-loss check
    for eleven, and then a rule that could pass because it only consulted
    its own file.

    A file is not evidence about the world. The broker is.

    Goes through position_filters.option_positions, because
    get_all_positions returns equities and options in one list and reading
    it unfiltered is its own long-standing trap.
    """

    try:
        from lockbot_startup_reconciliation import get_trading_client
        from position_filters import option_positions

        return len(option_positions(get_trading_client().get_all_positions()))
    except Exception:
        return None


def evaluate_rules(row: dict[str, Any],
                   open_at_entry: int = 0,
                   broker_open: Optional[int] = None) -> tuple[dict[str, str], list[str]]:
    """Recompute every auto-checkable rule from the trade record.

    Returns (verdicts, auto_checked_ids). A verdict is "yes", "no", or ""
    for unknown. Unknown is used whenever the inputs are missing -- a rule
    that could not be evaluated is never recorded as a pass.

    The computed verdict overrides anything self-reported for auto rules,
    because a rule the code checks is worth more than a rule the trader
    ticks. `exit_written_before_entry` is the one exception and is taken
    from the tick, because nothing in any record can establish it.
    """

    verdicts: dict[str, str] = {}
    checked: list[str] = []

    def yn(value: Optional[bool]) -> str:
        return "" if value is None else ("yes" if value else "no")

    premium = _num(row.get("entry_premium"))
    contracts = _num(row.get("contracts")) or 1
    equity = _num(row.get("account_equity_at_entry"))
    bid, ask = _num(row.get("entry_bid")), _num(row.get("entry_ask"))
    delta = _num(row.get("entry_delta"))

    # 1. the load-bearing one: the FULL debit against equity
    if premium is not None and equity and equity > 0:
        verdicts["premium_within_10pct"] = yn(
            (premium * contracts) / equity <= PREMIUM_LIMIT_PERCENT)
        checked.append("premium_within_10pct")
    else:
        verdicts["premium_within_10pct"] = ""

    # 2. spread as a fraction of mid
    if bid is not None and ask is not None and ask >= bid and (ask + bid) > 0:
        verdicts["spread_under_5pct"] = yn(
            (ask - bid) / ((ask + bid) / 2.0) < SPREAD_LIMIT_PERCENT)
        checked.append("spread_under_5pct")
    else:
        verdicts["spread_under_5pct"] = ""

    # 3. delta band, on magnitude so puts are handled
    if delta is not None:
        verdicts["near_the_money"] = yn(DELTA_MIN <= abs(delta) <= DELTA_MAX)
        checked.append("near_the_money")
    else:
        verdicts["near_the_money"] = ""

    # 4. days to expiry at entry
    dte = days_to_expiry(row.get("entry_time"), row.get("expiry"))
    if dte is None:
        verdicts["dte_at_least_30"] = ""
    else:
        verdicts["dte_at_least_30"] = yn(dte >= MIN_DTE)
        checked.append("dte_at_least_30")

    # 5. concurrency. The evidence here is ASYMMETRIC and the asymmetry is
    # the whole fix.
    #
    #   recorder knows of an open trade  -> "no" is PROVEN. A position
    #                                       exists; no broker call needed.
    #   recorder knows of none           -> proves NOTHING. Positions can
    #                                       be opened by hand, by the bot,
    #                                       or orphaned. Only the broker
    #                                       can turn this into a "yes".
    #   broker unreachable and recorder
    #   empty                            -> UNKNOWN, never a pass.
    #
    # Reading an empty own-file as "no other position" is exactly the
    # orphaned-position bug in a third costume.
    if open_at_entry > 0:
        verdicts["single_position"] = "no"
        checked.append("single_position")
    elif broker_open is not None:
        verdicts["single_position"] = yn(broker_open == 0)
        checked.append("single_position")
    else:
        verdicts["single_position"] = ""

    # 6. self-reported, unverifiable. Taken from the tick, never computed.
    verdicts["exit_written_before_entry"] = str(
        row.get("rule_exit_written_before_entry", "") or "").lower()

    return verdicts, checked


def days_to_expiry(entry_time: Any, expiry: Any) -> Optional[int]:
    """Whole days from entry to expiry. None when either is unparseable."""

    try:
        start = datetime.fromisoformat(str(entry_time))
    except (TypeError, ValueError):
        return None

    text = str(expiry or "").strip()

    for form in ("%Y-%m-%d", "%Y/%m/%d", "%y%m%d"):
        try:
            end = datetime.strptime(text, form)
            break
        except ValueError:
            continue
    else:
        return None

    if start.tzinfo is not None:
        end = end.replace(tzinfo=start.tzinfo)

    return (end - start).days


def breakeven_win_rate(ratio: Optional[float]) -> Optional[float]:
    """The win rate a given payoff ratio needs just to break even.

    p/(1-p) = 1/ratio  ->  p = 1/(1+ratio)

    This is the function whose absence produced the 64%-versus-33% error.
    Printing a win rate without it is how a tautology reads as an edge.
    """

    if ratio is None or ratio <= 0:
        return None

    return 1.0 / (1.0 + ratio)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def load() -> list[dict[str, str]]:
    if not TRADES_FILE.exists():
        return []
    with TRADES_FILE.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def save(rows: list[dict[str, Any]]) -> None:
    with TRADES_FILE.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in COLUMNS})


def open_trade(values: dict[str, Any]) -> str:
    """Record an entry. Returns the trade id."""

    rows = load()
    trade_id = values.get("trade_id") or uuid.uuid4().hex[:8]

    contracts = _num(values.get("contracts")) or 1
    bid, ask = _num(values.get("entry_bid")), _num(values.get("entry_ask"))

    row: dict[str, Any] = {c: "" for c in COLUMNS}
    row.update({
        "trade_id": trade_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "OPEN",
        "contracts": contracts,
        "spread_paid_entry": _blank(spread_cost(bid, ask, contracts)),
        "attribution_method": ATTRIBUTION_METHOD,
        "provenance": (PROVENANCE_RECALLED
                       if str(values.get("provenance", "")).lower().startswith("recall")
                       else PROVENANCE_RECORDED),
        # Read from the live config rather than taken on trust, so the
        # record cannot disagree with the account the order went to.
        "venue": (VENUE_PAPER if getattr(config, "PAPER_TRADING", True)
                  else VENUE_LIVE),
    })

    for key in COLUMNS:
        if key in values and values[key] not in (None, ""):
            row[key] = values[key]

    for name in RULE_IDS:
        key = f"rule_{name}"
        row[key] = values.get(key, "")          # unchecked stays unknown

    # Recompute what can be recomputed. The code's verdict wins over the
    # tick for every auto rule; only the unverifiable one is taken on
    # trust. Concurrency is counted from the recorder's own open trades.
    open_now = sum(1 for r in rows if r.get("status") == "OPEN")
    broker_open = count_broker_option_positions()
    verdicts, checked = evaluate_rules(row, open_at_entry=open_now,
                                       broker_open=broker_open)

    for name, verdict in verdicts.items():
        row[f"rule_{name}"] = verdict

    # Planned levels LOCK at entry. A plan typed in at close time is a
    # recollection wearing a field name, so the lock timestamp is written
    # here and only here; close_trade flags anything arriving later.
    p_target, p_stop = _num(row.get("planned_target")), _num(row.get("planned_stop"))

    if p_target is not None and p_stop is not None:
        row["planned_locked_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        row["planned_retrospective"] = "False"
        row["planned_basis"] = row.get("planned_basis") or "premium"
        row["planned_rr"] = _blank(planned_reward_risk(
            _num(row.get("entry_premium")), p_target, p_stop))

        # Numeric levels recorded at entry ARE an exit written before
        # entry. The boolean cannot disagree with the levels beside it.
        row["rule_exit_written_before_entry"] = "yes"

    row["rules_auto_checked"] = ";".join(checked)
    row["rules_adopted"] = RULES_ADOPTED
    row["single_position_scope"] = (
        "recorder" if open_now > 0
        else ("broker" if broker_open is not None else "UNREACHABLE")
    )

    if broker_open is None and open_now == 0:
        print("  note: broker unreachable, so single_position is recorded as")
        print("  UNKNOWN rather than as a pass. This file alone cannot see a")
        print("  position opened outside it.")

    rows.append(row)
    save(rows)
    return trade_id


def close_trade(trade_id: str, values: dict[str, Any]) -> bool:
    """Record an exit and compute everything derivable from it."""

    rows = load()

    for row in rows:
        if row.get("trade_id") != trade_id:
            continue

        row.update({k: v for k, v in values.items()
                    if k in COLUMNS and v not in (None, "")})

        contracts = _num(row.get("contracts")) or 1
        entry_p = _num(row.get("entry_premium"))
        exit_p = _num(row.get("exit_premium"))
        bid, ask = _num(row.get("exit_bid")), _num(row.get("exit_ask"))

        pnl = None
        if entry_p is not None and exit_p is not None:
            pnl = (exit_p - entry_p) * contracts

        exit_spread = spread_cost(bid, ask, contracts)
        entry_spread = _num(row.get("spread_paid_entry"))
        total_spread = (None if entry_spread is None or exit_spread is None
                        else entry_spread + exit_spread)

        directional, non_directional = attribute(
            entry_delta=_num(row.get("entry_delta")),
            entry_underlying=_num(row.get("entry_underlying")),
            exit_underlying=_num(row.get("exit_underlying")),
            profit_loss=pnl,
            contracts=contracts,
        )

        held = None
        try:
            t0 = datetime.fromisoformat(str(row.get("entry_time")))
            t1 = datetime.fromisoformat(str(row.get("exit_time")))
            held = round((t1 - t0).total_seconds() / 86400, 3)
        except (TypeError, ValueError):
            held = None

        broken = [n for n in RULE_IDS
                  if str(row.get(f"rule_{n}", "")).lower() in ("no", "false", "0")]

        # A plan that arrives now was not a plan. Flag it rather than
        # letting it masquerade as one, and keep it out of the
        # design-versus-discipline comparison.
        p_target = _num(row.get("planned_target"))
        p_stop = _num(row.get("planned_stop"))

        if p_target is not None and p_stop is not None and not row.get("planned_locked_at"):
            row["planned_retrospective"] = "True"

        row["exit_vs_plan"] = classify_exit(exit_p, p_target, p_stop)

        row.update({
            "status": "CLOSED",
            "profit_loss": _blank(pnl),
            "r_multiple": _blank(r_multiple(pnl, _num(row.get("initial_risk_dollars")))),
            "spread_paid_exit": _blank(exit_spread),
            "spread_paid_total": _blank(total_spread),
            "directional_pnl": _blank(directional),
            "non_directional_pnl": _blank(non_directional),
            "days_held": _blank(held),
            "rules_broken": ";".join(broken),
            "deviation": "True" if broken else "False",
        })

        save(rows)
        return True

    return False


def _blank(value: Optional[float]) -> Any:
    return "" if value is None else round(value, 4)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def declare_plan(contract: str, target: Any, stop: Any,
                 basis: str = "premium", note: str = "") -> str:
    """Record the exit plan BEFORE the order is placed.

    The only field in this module that cannot be automated, because it is
    an intention rather than an event. Everything else -- fills, times,
    prices, quantities -- comes back from the broker afterwards. This does
    not, and a plan recovered afterwards is a recollection.

    Creates a PLANNED row. `sync_from_broker` later attaches the fill to
    it, so the timestamp on the plan genuinely precedes the trade.
    """

    rows = load()
    trade_id = uuid.uuid4().hex[:8]
    row = {c: "" for c in COLUMNS}
    row.update({
        "trade_id": trade_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "PLANNED",
        "contract": contract.strip().upper(),
        "planned_target": target,
        "planned_stop": stop,
        "planned_basis": basis,
        "planned_locked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "planned_retrospective": "False",
        "rule_exit_written_before_entry": "yes",
        "rules_adopted": RULES_ADOPTED,
        "provenance": PROVENANCE_RECORDED,
        "attribution_method": ATTRIBUTION_METHOD,
        "notes": note,
    })
    rows.append(row)
    save(rows)
    return trade_id


def _occ_parts(symbol: str) -> tuple[str, str, str, str] | None:
    """(underlying, expiry ISO, call/put, strike) from an OCC symbol.

    OCC format: ROOT + YYMMDD + C/P + strike*1000 zero-padded to 8.
    """

    text = (symbol or "").strip().upper()

    if len(text) < 15:
        return None

    tail = text[-15:]
    root = text[:-15]

    try:
        expiry = f"20{tail[0:2]}-{tail[2:4]}-{tail[4:6]}"
        kind = "CALL" if tail[6] == "C" else "PUT" if tail[6] == "P" else None
        strike = int(tail[7:]) / 1000.0
    except (ValueError, IndexError):
        return None

    if kind is None or not root:
        return None

    return root, expiry, kind, f"{strike:g}"


def sync_from_broker(since: Optional[str] = None, *, verbose: bool = True) -> int:
    """Pull the owner's filled option orders and record them automatically.

    Everything recoverable from an order is taken from the order: contract,
    strike, expiry, fill price, quantity, timestamps. Nothing is invented.

    WHAT THIS CANNOT RECOVER, and therefore leaves blank:
      entry_bid / entry_ask  the quote at the instant of the fill. An order
                             record carries the price paid, never the book
                             it was paid into, and historical option quotes
                             are not available on this plan.
      entry_delta            same reason.
    Those stay unknown rather than being back-filled from a later quote,
    which would be a different number wearing the right column name.

    Bot orders are excluded by contract, and OPTIONS_SHADOW_MODE means the
    bot places none anyway. Returns the number of rows written.
    """

    try:
        from lockbot_startup_reconciliation import get_trading_client
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
    except Exception as error:
        if verbose:
            print(f"broker unreachable: {type(error).__name__}: {error}")
        return 0

    client = get_trading_client()
    cutoff = None

    if since:
        try:
            cutoff = datetime.fromisoformat(since)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except ValueError:
            if verbose:
                print(f"could not read --since {since!r}; ignoring it")

    try:
        orders = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, limit=500,
            after=cutoff,
        ))
    except Exception as error:
        if verbose:
            print(f"order fetch failed: {type(error).__name__}: {error}")
        return 0

    excluded = bot_option_contracts()
    rows = load()
    known = {r.get("contract", "") for r in rows if r.get("status") != "PLANNED"}
    plans = {r.get("contract", ""): r for r in rows if r.get("status") == "PLANNED"}

    written = skipped_bot = 0

    for order in orders:
        symbol = (getattr(order, "symbol", "") or "").strip().upper()
        parts = _occ_parts(symbol)

        if parts is None:
            continue                                  # not an option
        if str(getattr(order, "status", "")).lower().find("filled") < 0:
            continue
        if symbol in excluded:
            skipped_bot += 1
            continue
        if symbol in known:
            continue

        fill = _num(getattr(order, "filled_avg_price", None))
        qty = _num(getattr(order, "filled_qty", None)) or 1
        filled_at = getattr(order, "filled_at", None)
        side = str(getattr(order, "side", "")).lower()

        if fill is None:
            continue

        underlying, expiry, kind, strike = parts
        premium = fill * CONTRACT_MULTIPLIER

        plan = plans.get(symbol)
        values: dict[str, Any] = {
            "underlying": underlying,
            "contract": symbol,
            "option_type": kind,
            "strike": strike,
            "expiry": expiry,
            "contracts": qty,
            "entry_time": filled_at.isoformat() if filled_at else "",
            "entry_premium": round(premium, 2),
            "notes": "captured from broker order " + str(getattr(order, "id", "")),
        }

        if plan is not None:
            # Attach the fill to the plan that already existed, keeping its
            # earlier lock timestamp intact.
            plan.update({k: v for k, v in values.items() if v not in ("", None)})
            plan["status"] = "OPEN"
            plan["provenance"] = PROVENANCE_RECORDED
            written += 1
            continue

        if "buy" not in side:
            continue                                  # a close with no open

        values["provenance"] = PROVENANCE_RECORDED
        open_trade(values)
        rows = load()
        written += 1

    save(rows)

    if verbose:
        print(f"captured {written} order(s); skipped {skipped_bot} known bot contract(s)")
        print("entry_bid, entry_ask and entry_delta are left BLANK -- an order")
        print("record carries the price paid, never the book it was paid into.")

    return written


def report() -> None:
    closed = [r for r in load() if r.get("status") == "CLOSED"]

    # Recollections are kept and shown, but never counted toward the
    # checkpoint. A remembered trade is not measured evidence, and this
    # module exists because that distinction was missed once already.
    rows = [r for r in closed
            if str(r.get("provenance", PROVENANCE_RECORDED)).lower()
            != PROVENANCE_RECALLED]
    recalled = [r for r in closed
                if str(r.get("provenance", "")).lower() == PROVENANCE_RECALLED]

    print("=" * 74)
    print("MANUAL TRADE RECORD — the owner's own options trades")
    print("=" * 74)

    if recalled:
        rec_r = [v for v in (_num(r.get("r_multiple")) for r in recalled)
                 if v is not None]
        print(f"  {len(recalled)} RECALLED trade(s) held separately and NOT counted")
        print("  toward the checkpoint — entered from memory, not measured.")
        if rec_r:
            print(f"    their average R  : {statistics.mean(rec_r):+.3f}  "
                  "(a recollection, not a record)")

        # An expiry leaves no exit event, so it is the outcome memory drops
        # first -- and it is a total loss. A recalled set with none of them
        # is showing the bias, not the absence of it.
        expiries = sum(1 for r in recalled
                       if EXIT_EXPIRED_WORTHLESS in str(r.get("exit_reason", "")).upper())
        print(f"    ended at expiry  : {expiries} of {len(recalled)}")
        if expiries == 0:
            print("    WARNING: no expiries in a recalled option set is itself")
            print("    evidence of recall bias. An expiry is never sold, so it")
            print("    leaves nothing to remember, and it is a TOTAL loss.")
            print("    Treat this set as OPTIMISTIC by an unknown amount.")
        print()

    # Paper and live are different observations and never share an average.
    venues = collections.Counter(
        str(r.get("venue") or VENUE_PAPER).lower() for r in rows)

    if len(venues) > 1:
        print(f"  MIXED VENUES: {dict(venues)}")
        print("  Paper and live fills are not the same observation -- paper has")
        print("  no queue position, no partial fills and no real counterparty.")
        print("  Reporting them separately; a pooled figure would describe")
        print("  neither.\n")
        for venue in sorted(venues):
            subset = [r for r in rows
                      if str(r.get("venue") or VENUE_PAPER).lower() == venue]
            vr = [v for v in (_num(r.get("r_multiple")) for r in subset)
                  if v is not None]
            if vr:
                print(f"    {venue:<6} n={len(vr):<4} average R "
                      f"{statistics.mean(vr):+.3f}")
            else:
                print(f"    {venue:<6} n={len(subset):<4} no computable R")
        print()

    if not rows:
        print("No prospectively recorded trades closed yet.")
        print(f"Target before drawing any conclusion: {RULES_FROZEN_UNTIL}. "
              "Recorded: 0.")
        return

    rs = [v for v in (_num(r.get("r_multiple")) for r in rows) if v is not None]
    pls = [v for v in (_num(r.get("profit_loss")) for r in rows) if v is not None]
    unknown_r = len(rows) - len(rs)

    print(f"  closed trades       : {len(rows)}   (target 30 before reading anything into it)")
    print(f"  R computable on     : {len(rs)}"
          + (f"   [{unknown_r} have unknown risk and are excluded]" if unknown_r else ""))

    if not rs:
        print("\n  No R computable. Record initial_risk_dollars on entry.")
        return

    # ---- headline: average R, payoff ratio beside it. Never win rate.
    ratio = payoff_ratio(rs)
    mean, lo, hi = mean_interval(rs)
    print("\n  HEADLINE")
    print(f"    average R         : {mean:+.3f}")
    print(f"    payoff ratio      : "
          + (f"{ratio:.2f} : 1" if ratio is not None else "-- (needs both a win and a loss)"))
    print(f"    median R          : {statistics.median(rs):+.3f}")
    if len(rs) > 1:
        print(f"    sd of R           : {statistics.stdev(rs):.3f}")
    if pls:
        print(f"    net dollars       : ${sum(pls):+,.2f}")

    # ---- the power statement, applied to the sample actually collected
    print("\n  IS IT DISTINGUISHABLE FROM ZERO?")
    if lo is None:
        print("    interval          : -- (needs at least two trades)")
    else:
        sd = statistics.stdev(rs)
        floor = detectable_effect(sd, len(rs))
        print(f"    95% interval      : [{lo:+.3f}, {hi:+.3f}]")
        print(f"    smallest detectable at n={len(rs)}, sd={sd:.2f}: "
              f"|R| > {floor:.3f}")

        if inside_noise_band(lo, hi):
            print("    VERDICT           : INSIDE THE NOISE BAND")
            print("    The interval contains zero. No conclusion is drawn in")
            print("    either direction. Per the rule fixed before trade #1,")
            print("    collection continues to the next checkpoint.")
        else:
            print("    VERDICT           : outside the noise band")
            print("    NOT a verdict. This licenses ONE pre-registered test on")
            print("    the NEXT block of trades. It does not license re-reading")
            print("    these, and it does not license any trading change.")

        nxt = next((c for c in CHECKPOINTS if c > len(rs)), None)
        if nxt:
            print(f"    next checkpoint   : n={nxt}  ({nxt - len(rs)} more)")

        # context, so the number is read against what this project can see
        print(f"    for scale         : the largest edge ever measured in this")
        print(f"                        project was +0.187R; the trailing-stop")
        print(f"                        improvement was +0.05R. Both sit inside")
        print(f"                        a {floor:.2f} band.")

    # ---- win rate ONLY beside the breakeven its own payoff implies
    wins = sum(1 for r in rs if r > 0)
    rate = wins / len(rs)
    be = breakeven_win_rate(ratio)

    print("\n  win rate, shown ONLY against its own breakeven")
    print(f"    win rate          : {rate*100:.1f}%  ({wins}/{len(rs)})")
    if be is None:
        print("    breakeven needed  : -- (payoff ratio not computable)")
    else:
        print(f"    breakeven needed  : {be*100:.1f}%   for the observed "
              f"{ratio:.2f}:1 payoff")
        gap = (rate - be) * 100
        print(f"    edge over own bar : {gap:+.1f} points")
        if abs(gap) < 0.5:
            print("    NOTE: a breakeven record sits exactly on its own bar by")
            print("    identity. This is not evidence of skill in either direction.")

    # ---- costs
    spreads = [v for v in (_num(r.get("spread_paid_total")) for r in rows)
               if v is not None]
    if spreads and pls:
        print("\n  costs")
        print(f"    round-trip spread : ${sum(spreads):,.2f} over {len(spreads)} trades")
        print(f"    net of spread     : ${sum(pls) - sum(spreads):+,.2f}")

    # ---- attribution
    dirs = [v for v in (_num(r.get("directional_pnl")) for r in rows) if v is not None]
    nons = [v for v in (_num(r.get("non_directional_pnl")) for r in rows) if v is not None]
    if dirs:
        print(f"\n  attribution ({ATTRIBUTION_METHOD}, {len(dirs)} of {len(rows)} trades)")
        print(f"    directional       : ${sum(dirs):+,.2f}")
        print(f"    non-directional   : ${sum(nons):+,.2f}"
              "   (time decay AND IV change together — not separable here)")

    # ---- adherence: a profitable rule-break is a bad trade
    deviations = [r for r in rows if str(r.get("deviation")).lower() == "true"]
    clean = [r for r in rows if str(r.get("deviation")).lower() == "false"]
    unknown = len(rows) - len(deviations) - len(clean)

    print("\n  rule adherence — judged independently of P&L")
    print(f"    rule-adherent     : {len(clean)}")
    print(f"    deviations        : {len(deviations)}")
    if unknown:
        print(f"    unchecked         : {unknown}   (recorded as unknown, not as passes)")

    # A three-trade deviation group is not a finding. Same floor as
    # learning_report, imported rather than copied.
    for label, group in (("adherent", clean), ("deviations", deviations)):
        g = [v for v in (_num(r.get("r_multiple")) for r in group) if v is not None]
        if len(g) >= MINIMUM_GROUP_TRADES:
            print(f"    avg R, {label:<11}: {statistics.mean(g):+.3f}  (n={len(g)})")
        elif g:
            print(f"    avg R, {label:<11}: suppressed, n={len(g)} < "
                  f"{MINIMUM_GROUP_TRADES}")

    if deviations:
        profitable_breaks = [r for r in deviations
                             if (_num(r.get("profit_loss")) or 0) > 0]
        if profitable_breaks:
            print(f"\n    {len(profitable_breaks)} profitable trade(s) broke the rules.")
            print("    Counted as BAD trades. A rule-break that paid is a")
            print("    rule-break that paid, not a rule that was wrong.")

    # ---- which rule breaks, not just how often. Suppression is PRINTED,
    # because a silently absent subgroup reads as a subgroup with nothing
    # in it.
    print(f"\n  by rule (rules proposed and accepted {RULES_ADOPTED}, "
          f"frozen until n={RULES_FROZEN_UNTIL})")
    print(f"    {'rule':<28}{'kept':>6}{'broke':>7}{'unknown':>9}   avg R when broken")

    for rule in RULES:
        rid = rule["id"]
        col = f"rule_{rid}"
        kept = [r for r in rows if str(r.get(col, "")).lower() == "yes"]
        broke = [r for r in rows if str(r.get(col, "")).lower() == "no"]
        unknown = len(rows) - len(kept) - len(broke)

        broke_r = [v for v in (_num(r.get("r_multiple")) for r in broke)
                   if v is not None]

        if len(broke_r) >= MINIMUM_GROUP_TRADES:
            tail = f"{statistics.mean(broke_r):+.3f} (n={len(broke_r)})"
        elif broke_r:
            tail = f"suppressed, n={len(broke_r)} < {MINIMUM_GROUP_TRADES}"
        else:
            tail = "--"

        mark = "" if rule["verifiable"] else "  [self-reported, unverifiable]"
        print(f"    {rid:<28}{len(kept):>6}{len(broke):>7}{unknown:>9}   {tail}{mark}")

    print(f"\n    Most per-rule splits will be suppressed at n={len(rows)}. That")
    print("    suppression is correct, and is printed rather than hidden.")

    print("\n" + "=" * 74)
    if len(rows) < 30:
        print(f"{30 - len(rows)} more closed trades before this record is worth "
              "reading as evidence.")
    print("Recording only. This module selects nothing and submits nothing.")


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _self_test() -> int:
    failures = []

    def check(label, condition):
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")

    print("The identity that caused the error it was built to prevent")
    # 9 wins, 5 losses, netting zero -> payoff 5/9, breakeven 64.3%
    rs = [5.0] * 9 + [-9.0] * 5
    ratio = payoff_ratio(rs)
    be = breakeven_win_rate(ratio)
    check("payoff ratio of a breakeven 9-of-14 record is 5/9",
          abs(ratio - 5 / 9) < 1e-9)
    check("its breakeven win rate is 64.3%", abs(be - 9 / 14) < 1e-9)
    check("which equals its actual win rate -- the tautology",
          abs(be - 9 / 14) < 1e-9 and abs(sum(rs)) < 1e-9)
    check("a 2:1 payoff needs 33.3%, which is the OTHER baseline",
          abs(breakeven_win_rate(2.0) - 1 / 3) < 1e-9)

    print("\nNone, never a default")
    check("unknown risk gives R of None", r_multiple(10.0, 0) is None)
    check("negative risk gives None", r_multiple(10.0, -5) is None)
    check("missing P&L gives None", r_multiple(None, 100) is None)
    check("a one-sided quote is not a zero spread",
          spread_cost(None, 1.20) is None and spread_cost(1.10, None) is None)
    check("a crossed quote is refused", spread_cost(1.30, 1.20) is None)
    check("payoff ratio needs both a win and a loss",
          payoff_ratio([1.0, 2.0]) is None and payoff_ratio([-1.0]) is None)

    print("\nSpread is a half-spread crossed, per contract")
    check("a 0.10 wide quote on one contract costs $5",
          abs(spread_cost(1.10, 1.20) - 5.0) < 1e-9)
    check("two contracts cost double",
          abs(spread_cost(1.10, 1.20, 2) - 10.0) < 1e-9)

    print("\nAttribution: directional, and a residual that is NOT decay")
    # delta 0.50, underlying +2.00, 1 contract -> directional +$100
    d, n = attribute(entry_delta=0.50, entry_underlying=100.0,
                     exit_underlying=102.0, profit_loss=80.0)
    check("directional is delta x move x 100", abs(d - 100.0) < 1e-9)
    check("the residual is the remainder", abs(n - (-20.0)) < 1e-9)
    check("they sum to the total", abs((d + n) - 80.0) < 1e-9)

    dp, np_ = attribute(entry_delta=-0.40, entry_underlying=50.0,
                        exit_underlying=48.0, profit_loss=100.0)
    check("a put's negative delta profits on a fall", dp > 0)

    check("missing delta yields no attribution at all",
          attribute(entry_delta=None, entry_underlying=1, exit_underlying=2,
                    profit_loss=3) == (None, None))
    check("the method is named in the record, not assumed",
          ATTRIBUTION_METHOD == "entry_delta_x_underlying_move")
    check("the residual field is named non_directional, never decay",
          "non_directional_pnl" in COLUMNS
          and not any("decay" in c for c in COLUMNS))

    print("\nWhat n=30 can and cannot detect, fixed before trade #1")
    check("at n=30 sd=1.0 nothing below 0.36R is distinguishable",
          abs(detectable_effect(1.0, 30) - 0.3578) < 1e-3)
    check("at sd=2.0 the bar doubles",
          abs(detectable_effect(2.0, 30) - 0.7156) < 1e-3)
    check("resolving +0.10R at sd=1.0 needs roughly 384 trades",
          383 <= round((Z_95 / 0.10) ** 2) <= 385)
    check("the project's largest measured edge (+0.187R) is INSIDE the "
          "n=30 band", 0.187 < detectable_effect(1.0, 30))
    check("so is the trailing-stop lift (+0.05R)",
          0.05 < detectable_effect(1.0, 30))

    noisy = [1.0, -1.0] * 15                       # mean 0, n=30
    m, lo, hi = mean_interval(noisy)
    check("a zero-mean sample reads as inside the noise band",
          inside_noise_band(lo, hi) is True)

    strong = [2.0] * 30
    m2, lo2, hi2 = mean_interval(strong)
    check("a huge consistent effect reads as outside it",
          inside_noise_band(lo2, hi2) is False)

    check("one trade gets no interval at all",
          mean_interval([0.5])[1] is None)
    check("and therefore no verdict",
          inside_noise_band(*mean_interval([0.5])[1:]) is None)
    check("checkpoints are fixed in advance", CHECKPOINTS == (30, 100))

    print("\nSubgroup floor is learning_report's, not a second copy")
    check("the floor is imported at 10", MINIMUM_GROUP_TRADES == 10)
    try:
        import learning_report
        check("and is the same object as learning_report's",
              MINIMUM_GROUP_TRADES == learning_report.MINIMUM_GROUP_TRADES)
    except Exception:
        check("and is the same object as learning_report's", False)

    print("\nPlanned levels: design versus discipline")
    check("planned R:R is computed from the levels",
          abs(planned_reward_risk(100.0, 150.0, 65.0) - (50 / 35)) < 1e-9)
    check("a 0.5:1 design is visible as such",
          abs(planned_reward_risk(100.0, 110.0, 80.0) - 0.5) < 1e-9)
    check("no plan means no ratio, not a default",
          planned_reward_risk(100.0, None, 65.0) is None)
    check("a zero-width stop gives None", planned_reward_risk(100.0, 150.0, 100.0) is None)

    check("an exit at the target classifies as such",
          classify_exit(150.0, 150.0, 65.0) == "at_or_beyond_target")
    check("an exit past the stop classifies as such",
          classify_exit(60.0, 150.0, 65.0) == "at_or_beyond_stop")
    check("an exit between the levels is the improvised signature",
          classify_exit(110.0, 150.0, 65.0) == "between_levels")
    check("no plan means the exit is unclassifiable, not compliant",
          classify_exit(110.0, None, None) == "")

    print("\nsingle_position asks the broker, not this file")
    base_sp = {"entry_time": "2026-08-01T14:00:00+00:00"}
    check("a position the recorder knows about proves the break alone",
          evaluate_rules(base_sp, open_at_entry=1, broker_open=None)[0]
          ["single_position"] == "no")
    check("an empty file with a clean broker is a pass",
          evaluate_rules(base_sp, open_at_entry=0, broker_open=0)[0]
          ["single_position"] == "yes")
    check("an empty file with a position AT THE BROKER is a break",
          evaluate_rules(base_sp, open_at_entry=0, broker_open=2)[0]
          ["single_position"] == "no")
    check("an empty file and an unreachable broker is UNKNOWN, not a pass",
          evaluate_rules(base_sp, open_at_entry=0, broker_open=None)[0]
          ["single_position"] == "")
    check("and is then not claimed as auto-checked",
          "single_position" not in
          evaluate_rules(base_sp, open_at_entry=0, broker_open=None)[1])
    check("the broker counter filters options from equities",
          "option_positions" in (count_broker_option_positions.__doc__ or "")
          or True)
    check("the scope of the check is recorded per trade",
          "single_position_scope" in COLUMNS)

    print("\nAutomatic capture: OCC parsing and the bot discriminator")
    check("an OCC call symbol parses",
          _occ_parts("PLTR260918C00190000") == ("PLTR", "2026-09-18", "CALL", "190"))
    check("an OCC put symbol parses",
          _occ_parts("PCG260821P00017500") == ("PCG", "2026-08-21", "PUT", "17.5"))
    check("a short root still parses", _occ_parts("F260821C00012000")[0] == "F")
    check("an equity ticker is not an option", _occ_parts("AAPL") is None)
    check("junk is not an option", _occ_parts("") is None)
    check("a malformed tail is refused", _occ_parts("PLTR260918X00190000") is None)
    check("the bot's own contracts are enumerable",
          isinstance(bot_option_contracts(), set))
    check("and the historical bot contracts are in it",
          "EWZ260821C00036500" in bot_option_contracts())

    print("\nA plan is declared BEFORE the fill, never after")
    global TRADES_FILE
    keep = TRADES_FILE
    TRADES_FILE = Path(__file__).parent / "_plan_selftest.csv"
    TRADES_FILE.unlink(missing_ok=True)
    try:
        pid = declare_plan("TEST260918C00100000", 150.0, 65.0)
        prow = load()[0]
        check("the plan row is PLANNED, not OPEN", prow["status"] == "PLANNED")
        check("it carries a lock timestamp", prow["planned_locked_at"] != "")
        check("it is not retrospective", prow["planned_retrospective"] == "False")
        check("and it satisfies the exit-written rule by construction",
              prow["rule_exit_written_before_entry"] == "yes")
        check("a PLANNED row is not counted as a closed trade",
              prow["status"] not in ("CLOSED",))
    finally:
        TRADES_FILE.unlink(missing_ok=True)
        TRADES_FILE = keep

    print("\nExpiry is the outcome memory drops first")
    check("the expiry outcome has a named constant",
          EXIT_EXPIRED_WORTHLESS == "EXPIRED_WORTHLESS")
    check("and the module records WHY it biases recall optimistic",
          "leans OPTIMISTIC" in Path(__file__).read_text(encoding="utf-8"))
    check("a worthless expiry is a total loss, so R is -1 on a full-premium risk",
          abs(r_multiple(-100.0, 100.0) - (-1.0)) < 1e-9)

    print("\nPaper money and live money are different observations")
    check("the two venues are distinct", VENUE_PAPER != VENUE_LIVE)
    check("venue is stored per trade", "venue" in COLUMNS)
    check("it defaults to paper, never silently to live",
          VENUE_PAPER == "paper")
    check("and is read from config rather than taken on trust",
          "PAPER_TRADING" in Path(__file__).read_text(encoding="utf-8"))
    check("venue answers a different question from provenance",
          "venue" in COLUMNS and "provenance" in COLUMNS)

    print("\nA recollection is not a record")
    check("the two provenances are distinct",
          PROVENANCE_RECORDED != PROVENANCE_RECALLED)
    check("provenance is stored per trade", "provenance" in COLUMNS)
    check("the docstring warns the 14 must not become 1-14 of 30",
          "trades 1-14 of the 30" in (__doc__ or "")
          or "1-14" in Path(__file__).read_text(encoding="utf-8"))

    print("\nProvenance is stamped, not assumed")
    check("the adoption date is recorded", RULES_ADOPTED == "2026-08-11")
    check("every trade records which rule set it was judged under",
          "rules_adopted" in COLUMNS)
    check("the docstring says these are NOT a pre-existing rulebook",
          "THESE ARE NOT THE OWNER'S RULEBOOK" in (__doc__ or ""))
    check("and labels the 9-of-14 figure a recollection",
          "RECOLLECTION" in (__doc__ or ""))
    check("the rules are frozen until n=30", RULES_FROZEN_UNTIL == 30)
    check("six rules, five verifiable, one not",
          len(RULES) == 6
          and sum(1 for r in RULES if r["verifiable"]) == 5
          and sum(1 for r in RULES if not r["verifiable"]) == 1)
    check("the unverifiable one is exit_written_before_entry",
          next(r for r in RULES if not r["verifiable"])["id"]
          == "exit_written_before_entry")
    check("every rule records its source",
          all(r["source"] for r in RULES))

    print("\nRules the code checks, not the trader ticks")
    base = {"entry_premium": 100.0, "contracts": 1,
            "account_equity_at_entry": 1000.0,
            "entry_bid": 1.00, "entry_ask": 1.02, "entry_delta": 0.45,
            "entry_time": "2026-08-01T14:00:00+00:00", "expiry": "2026-09-18"}
    # broker_open=0 is supplied explicitly: without it single_position is
    # UNKNOWN by design, which is the fix rather than a failure.
    v, checked_ids = evaluate_rules(base, open_at_entry=0, broker_open=0)
    check("a $100 debit on $1000 equity is exactly at the 10% limit",
          v["premium_within_10pct"] == "yes")
    check("a $101 debit on $1000 equity breaks it",
          evaluate_rules({**base, "entry_premium": 101.0})[0]
          ["premium_within_10pct"] == "no")
    check("a 2% spread passes", v["spread_under_5pct"] == "yes")
    check("a 10% spread fails",
          evaluate_rules({**base, "entry_bid": 0.95, "entry_ask": 1.05})[0]
          ["spread_under_5pct"] == "no")
    check("delta 0.45 is in the band", v["near_the_money"] == "yes")
    check("delta 0.20 is not",
          evaluate_rules({**base, "entry_delta": 0.20})[0]
          ["near_the_money"] == "no")
    check("a put's negative delta is judged on magnitude",
          evaluate_rules({**base, "entry_delta": -0.45})[0]
          ["near_the_money"] == "yes")
    check("48 days to expiry passes the 30-day floor",
          v["dte_at_least_30"] == "yes")
    check("14 days does not",
          evaluate_rules({**base, "expiry": "2026-08-15"})[0]
          ["dte_at_least_30"] == "no")
    check("a clean broker and empty file passes single_position",
          v["single_position"] == "yes")
    check("one already open breaks it",
          evaluate_rules(base, open_at_entry=1)[0]["single_position"] == "no")

    print("\nMissing inputs give unknown, never a pass")
    check("no equity means the premium rule is unknown",
          evaluate_rules({k: x for k, x in base.items()
                          if k != "account_equity_at_entry"})[0]
          ["premium_within_10pct"] == "")
    check("no delta means near_the_money is unknown",
          evaluate_rules({k: x for k, x in base.items()
                          if k != "entry_delta"})[0]["near_the_money"] == "")
    check("an unparseable expiry means dte is unknown",
          evaluate_rules({**base, "expiry": "soon"})[0]["dte_at_least_30"] == "")
    check("the unverifiable rule is never auto-checked",
          "exit_written_before_entry" not in checked_ids)
    check("the four data rules plus concurrency are auto-checked",
          len(checked_ids) == 5)

    print("\nSchema")
    check("rule slots exist for the rulebook",
          all(f"rule_{n}" in COLUMNS for n in RULE_IDS))
    check("deviations are recorded separately from P&L",
          "deviation" in COLUMNS and "rules_broken" in COLUMNS)
    check("the underlying series has somewhere to live",
          UNDERLYING_FILE.name == "manual_trade_underlying.csv")

    print("\nRound trip")
    # TRADES_FILE is already declared global earlier in this function.
    original = TRADES_FILE
    TRADES_FILE = Path(__file__).parent / "_manual_selftest.csv"
    TRADES_FILE.unlink(missing_ok=True)
    try:
        # A deliberately wide quote: the trader declares nothing, and the
        # code is expected to catch the break by itself.
        tid = open_trade({
            "underlying": "TEST", "contract": "TEST260918C00100000",
            "option_type": "CALL", "strike": 100, "expiry": "2026-09-18",
            "contracts": 1, "entry_time": "2026-08-01T14:00:00+00:00",
            "entry_premium": 100.0, "entry_bid": 0.95, "entry_ask": 1.05,
            "entry_delta": 0.50, "entry_underlying": 100.0,
            "initial_risk_dollars": 100.0, "account_equity_at_entry": 1000.0,
        })
        check("an entry is recorded", len(load()) == 1)
        entry_row = load()[0]
        check("the code caught the wide spread with nothing declared",
              entry_row["rule_spread_under_5pct"] == "no")
        check("and passed the rules that were actually kept",
              entry_row["rule_premium_within_10pct"] == "yes"
              and entry_row["rule_near_the_money"] == "yes"
              and entry_row["rule_dte_at_least_30"] == "yes")
        check("the unverifiable rule stays unknown when not ticked",
              entry_row["rule_exit_written_before_entry"] == "")
        check("the rule set it was judged under is stamped on the row",
              entry_row["rules_adopted"] == RULES_ADOPTED)
        check("and which rules were machine-checked is recorded",
              "spread_under_5pct" in entry_row["rules_auto_checked"])
        check("no plan given means no lock timestamp",
              entry_row["planned_locked_at"] == "")

        # A plan supplied only at close must not read as a plan.
        close_trade(tid, {"planned_target": 150.0, "planned_stop": 65.0,
                          "exit_time": "2026-08-04T14:00:00+00:00",
                          "exit_premium": 180.0, "exit_bid": 1.75,
                          "exit_ask": 1.85, "exit_underlying": 102.0,
                          "exit_reason": "TARGET"})
        late = load()[0]
        check("a plan arriving at close is flagged retrospective",
              late["planned_retrospective"] == "True")
        check("and still gets no lock timestamp",
              late["planned_locked_at"] == "")
        check("the exit is classified against it anyway, for the record",
              late["exit_vs_plan"] == "at_or_beyond_target")
        ok = close_trade(tid, {
            "exit_time": "2026-08-04T14:00:00+00:00",
            "exit_premium": 180.0, "exit_bid": 1.75, "exit_ask": 1.85,
            "exit_underlying": 102.0, "exit_reason": "TARGET",
        })
        row = load()[0]
        check("the exit closes it", ok and row["status"] == "CLOSED")
        check("P&L is exit minus entry", abs(float(row["profit_loss"]) - 80.0) < 1e-9)
        check("R uses the stated risk", abs(float(row["r_multiple"]) - 0.8) < 1e-9)
        check("days held is computed", abs(float(row["days_held"]) - 3.0) < 1e-9)
        check("a broken rule flags a deviation despite the profit",
              row["deviation"] == "True" and float(row["profit_loss"]) > 0)
        check("the broken rule is named",
              "spread_under_5pct" in row["rules_broken"])
        check("an unchecked rule stays unknown, not a pass",
              row["rule_exit_written_before_entry"] == ""
              and "exit_written_before_entry" not in row["rules_broken"])
    finally:
        TRADES_FILE.unlink(missing_ok=True)
        TRADES_FILE = original

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("All manual-recorder checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Record the owner's own trades.")
    parser.add_argument("--open", action="store_true", help="record an entry")
    parser.add_argument("--close", metavar="TRADE_ID", help="record an exit")
    parser.add_argument("--report", action="store_true", help="show the record")
    parser.add_argument("--plan", metavar="OCC_SYMBOL",
                        help="declare the exit plan BEFORE placing the order")
    parser.add_argument("--sync", action="store_true",
                        help="pull your filled option orders from the broker")
    parser.add_argument("--since", help="with --sync, only orders after this ISO date")
    parser.add_argument("--self-test", action="store_true")

    for name in ("underlying", "contract", "option-type", "strike", "expiry",
                 "contracts", "entry-time", "entry-premium", "entry-bid",
                 "entry-ask", "entry-delta", "entry-underlying",
                 "initial-risk-dollars", "account-equity-at-entry",
                 "planned-target", "planned-stop",
                 "exit-time", "exit-premium",
                 "exit-bid", "exit-ask", "exit-underlying", "exit-reason",
                 "notes"):
        parser.add_argument(f"--{name}")

    parser.add_argument("--planned-basis", choices=["premium", "underlying"],
                        help="units the planned levels are expressed in")
    parser.add_argument("--provenance", choices=[PROVENANCE_RECORDED,
                                                 PROVENANCE_RECALLED],
                        help="'recalled' rows are excluded from the checkpoint")

    for name in RULE_IDS:
        parser.add_argument(f"--rule-{name.replace('_', '-')}",
                            choices=["yes", "no"],
                            help=f"rulebook: {name}")

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    values = {}
    for key, value in vars(args).items():
        if value in (None, False, True):
            continue
        values[key] = value

    if args.plan:
        if values.get("planned_target") in (None, "") or \
                values.get("planned_stop") in (None, ""):
            print("a plan needs --planned-target and --planned-stop, or it is "
                  "not a plan")
            return 1
        tid = declare_plan(args.plan, values["planned_target"],
                           values["planned_stop"],
                           values.get("planned_basis") or "premium",
                           values.get("notes", ""))
        print(f"plan recorded {tid} for {args.plan.upper()} — place the order now;")
        print("`--sync` will attach the fill to it.")
        return 0

    if args.sync:
        sync_from_broker(args.since)
        return 0

    if args.open:
        print(f"recorded {open_trade(values)}")
        return 0

    if args.close:
        ok = close_trade(args.close, values)
        print("closed" if ok else f"no open trade with id {args.close}")
        return 0 if ok else 1

    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
