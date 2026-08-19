"""
options_shadow.py — what would the options LOCKBOT did not buy have done?

WHY THIS EXISTS

options_scanner.py writes a row to options_shadow_log.csv for every
decision it makes: orders submitted, orders the broker refused, contracts
ruled unaffordable, and shadow-mode entries. Nothing ever read those rows
back. The equity side had the same hole -- shadow_trades.py existed but
had no scheduler, so 40 resolvable setups sat unscored from 2026-07-28
until someone ran it by hand on 2026-08-02.

That gap matters more on the options side, because the decisions being
discarded are the interesting ones. On 2026-07-30 four BP spreads were
refused for insufficient buying power and two PBR calls never filled.
Six decisions, no record of whether skipping them was luck or judgement.
This module answers that.

HOW IT RESOLVES

Alpaca serves historical bars for individual option contracts, so a
decision can be replayed directly against the contract's own price rather
than inferred from the underlying. Each row is replayed from its
timestamp forward using options_manager's own exit rules -- +50% take
profit, -35% stop -- so the counterfactual is scored the way a real
position would have been.

WHAT IT DELIBERATELY DOES NOT DO

It places no orders and touches no position state. It also does not
pretend to precision it lacks: fills are assumed at the exact target or
stop, spread is ignored on exit, and a bar whose high and low span both
levels is AMBIGUOUS and counted as a loss. Every one of those choices is
pessimistic on purpose. A measurement that flatters the strategy is worse
than no measurement, because it gets believed.
"""

from __future__ import annotations

import csv

# One owner for CSV header migration across every journal.
import csv_schema
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import lockbot_config as config

MODULE_NAME = "OPTIONS_SHADOW"
CONTRACT_MULTIPLIER = 100

SHADOW_FILE = getattr(
    config, "OPTIONS_SHADOW_FILE",
    Path(__file__).resolve().parent / "options_shadow_log.csv",
)
RESOLVED_FILE = getattr(
    config, "OPTIONS_SHADOW_RESOLVED_FILE",
    Path(__file__).resolve().parent / "options_shadow_resolved.csv",
)

# How far behind real time the free options feed must be queried. The
# published delay is 15 minutes; the extra minute is slack so a request
# built a few seconds before it is sent does not land inside the window.
DELAYED_FEED_MINUTES = timedelta(minutes=16)

OUTCOME_TARGET = "TARGET"
OUTCOME_STOP = "STOP"
OUTCOME_AMBIGUOUS = "AMBIGUOUS"
OUTCOME_EXPIRED = "EXPIRED"
OUTCOME_UNRESOLVED = "UNRESOLVED"
OUTCOME_NO_DATA = "NO_DATA"

# Verticals are not replayed at all. See can_replay() for why -- this is a
# refusal to measure, not an oversight.
OUTCOME_SPREAD_UNSUPPORTED = "SPREAD_UNSUPPORTED"

RESOLVED_COLUMNS = [
    "timestamp",
    "underlying",
    "long_symbol",
    "short_symbol",
    "strategy",
    "action",
    "debit",
    "quality",
    "spread_percent",
    # high_low for single legs, close_only for verticals. Recorded because
    # the two are not equally sensitive: close_only cannot see a level
    # touched and given back inside a bar, so it undercounts both targets
    # and stops. A reader comparing the two must know which is which.
    "method",
    "target_price",
    "stop_price",
    "outcome",
    "profit_loss",
    "return_percent",
    "bars_checked",
    "resolved_at",
]


@dataclass
class Replay:
    """The result of replaying one logged decision."""

    outcome: str
    bars_checked: int
    profit_loss: float | None
    return_percent: float | None

    # The last per-contract value seen inside the hold window, so a
    # position whose window ran out can be marked to market instead of
    # being dropped from the scorecard. None when nothing priced --
    # absent, not zero.
    last_value: float | None = None


def mark_to_market(debit: float, last_value: float | None) -> tuple[
        float | None, float | None]:
    """What an expiring position was worth when its window closed.

    Returns (profit_loss, return_percent), both None when the mark cannot
    be computed. Never 0.0 as a stand-in: `simulate_symbol` once left
    timed-out trades at an r_multiple of 0.0 and thereby recorded them as
    having broken even, and the equity shadow book was censoring its own
    aged-out rows the same way until 2026-08-10. A default value is a
    claim.
    """

    if last_value is None or not debit or debit <= 0:
        return None, None

    profit = last_value - debit

    return round(profit, 2), round(profit / debit * 100.0, 4)


def exit_levels(debit: float) -> tuple[float, float]:
    """Target and stop, per contract in dollars, from the entry debit.

    Uses options_manager's live thresholds rather than its own copy, so
    the counterfactual is always scored on the rules actually in force.
    """

    take_profit = getattr(config, "OPTIONS_TAKE_PROFIT_PERCENT", 0.50)
    stop_loss = getattr(config, "OPTIONS_STOP_LOSS_PERCENT", 0.35)

    return debit * (1.0 + take_profit), debit * (1.0 - stop_loss)


def replay_bars(
    bars: Any,
    *,
    debit: float,
    max_hold_days: int | None = None,
    start: datetime | None = None,
) -> Replay:
    """Walk a contract's bars and decide what the position would have done.

    Bars are quoted per share; the debit is per contract. Both are
    converted to per-contract dollars before comparison so a 0.65 bar and
    a $65.00 debit are on the same scale.
    """

    if debit <= 0:
        return Replay(OUTCOME_NO_DATA, 0, None, None)

    target, stop = exit_levels(debit)
    hold_days = (
        max_hold_days
        if max_hold_days is not None
        else getattr(config, "OPTIONS_MAX_HOLD_DAYS", 10)
    )

    checked = 0
    last_value = None

    for bar in bars:
        moment = getattr(bar, "timestamp", None)

        if start is not None and moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)

            if moment < start:
                continue

            if hold_days and moment > start + timedelta(days=hold_days):
                profit, percent = mark_to_market(debit, last_value)
                return Replay(OUTCOME_EXPIRED, checked, profit, percent,
                              last_value=last_value)

        checked += 1

        high = float(bar.high) * CONTRACT_MULTIPLIER
        low = float(bar.low) * CONTRACT_MULTIPLIER

        bar_close = getattr(bar, "close", None)

        if bar_close is not None:
            try:
                last_value = float(bar_close) * CONTRACT_MULTIPLIER
            except (TypeError, ValueError):
                pass

        hit_target = high >= target
        hit_stop = low <= stop

        if hit_target and hit_stop:
            # One bar spanned both. There is no way to know which came
            # first, so it counts as the loss.
            loss = stop - debit
            return Replay(
                OUTCOME_AMBIGUOUS, checked, loss, loss / debit * 100.0
            )

        if hit_target:
            gain = target - debit
            return Replay(
                OUTCOME_TARGET, checked, gain, gain / debit * 100.0
            )

        if hit_stop:
            loss = stop - debit
            return Replay(
                OUTCOME_STOP, checked, loss, loss / debit * 100.0
            )

    if checked == 0:
        return Replay(OUTCOME_NO_DATA, 0, None, None)

    return Replay(OUTCOME_UNRESOLVED, checked, None, None,
                  last_value=last_value)


def align_closes(long_bars: Any, short_bars: Any) -> list[tuple[Any, float, float]]:
    """Pair the two legs' bars by exact timestamp.

    Only bars present on BOTH legs are returned. A spread's value is the
    difference between two prices at the same instant; a long-leg bar with
    no matching short-leg bar cannot be priced at all, and filling the gap
    with the nearest neighbour would reintroduce exactly the fabrication
    this module refuses to make.
    """

    shorts = {}

    for bar in short_bars:
        stamp = getattr(bar, "timestamp", None)

        if stamp is not None:
            shorts[stamp] = float(bar.close)

    paired = []

    for bar in long_bars:
        stamp = getattr(bar, "timestamp", None)

        if stamp is None or stamp not in shorts:
            continue

        paired.append((stamp, float(bar.close), shorts[stamp]))

    return paired


def replay_spread(
    paired: list[tuple[Any, float, float]],
    *,
    debit: float,
    max_hold_days: int | None = None,
    start: datetime | None = None,
) -> Replay:
    """Score a vertical from synchronised CLOSING prices only.

    WHY CLOSES AND NOT HIGH/LOW

    A single-leg position can be replayed from its own high and low --
    those are prices that genuinely traded. A spread cannot. Its value is
    long minus short, and the long leg's high did not occur at the same
    moment as the short leg's low, so `max(long) - min(short)` is a number
    that never existed. Scoring the first version of this module that way
    marked the ASHR and JD spreads as TARGET on their opening bar, when
    the broker had recorded both as stop losses.

    Closing prices at a shared timestamp are simultaneous and therefore
    real. The cost is sensitivity: a target touched and given back inside
    a five-minute bar is invisible here, so this UNDERCOUNTS both targets
    and stops. That bias is accepted deliberately -- it errs toward
    "unresolved" rather than toward inventing a result, which is the same
    standard the rest of this module holds to.
    """

    if debit <= 0:
        return Replay(OUTCOME_NO_DATA, 0, None, None)

    target, stop = exit_levels(debit)
    hold_days = (
        max_hold_days
        if max_hold_days is not None
        else getattr(config, "OPTIONS_MAX_HOLD_DAYS", 10)
    )

    checked = 0
    last_value = None

    for stamp, long_close, short_close in paired:
        if start is not None and stamp is not None:
            moment = stamp

            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)

            if moment < start:
                continue

            if hold_days and moment > start + timedelta(days=hold_days):
                profit, percent = mark_to_market(debit, last_value)
                return Replay(OUTCOME_EXPIRED, checked, profit, percent,
                              last_value=last_value)

        checked += 1

        value = (long_close - short_close) * CONTRACT_MULTIPLIER

        # A debit spread cannot be worth less than zero. A negative here
        # is a stale or crossed quote on one leg, not a loss.
        if value < 0:
            continue

        last_value = value

        if value >= target:
            gain = target - debit
            return Replay(OUTCOME_TARGET, checked, gain, gain / debit * 100.0)

        if value <= stop:
            loss = stop - debit
            return Replay(OUTCOME_STOP, checked, loss, loss / debit * 100.0)

    if checked == 0:
        return Replay(OUTCOME_NO_DATA, 0, None, None)

    return Replay(OUTCOME_UNRESOLVED, checked, None, None,
                  last_value=last_value)


def can_replay(decision: dict[str, Any]) -> tuple[bool, str]:
    """Whether this decision can be scored honestly. Returns (ok, reason).

    Both structures are now scorable, but by different methods.

    A single leg is replayed from its own high and low -- prices that
    genuinely traded. A vertical is replayed from synchronised CLOSING
    prices via replay_spread, because its value is long minus short and
    the long leg's high never coincided with the short leg's low.

    The first version of this module ignored that and priced spreads from
    the long leg alone. Every spread then cleared its +50% target on its
    opening bar, producing a 100% win rate across nine decisions --
    including ASHR and JD, both of which the broker had recorded as stop
    losses. The replay contradicted the account statement.

    This function is kept as the single place that decides scorability,
    so a future structure that genuinely cannot be priced has somewhere
    to be refused rather than guessed at.
    """

    return True, ""


def load_decisions(path: Path | None = None) -> list[dict[str, Any]]:
    """Read the options decision log, tolerating a missing file.

    Reads through csv_schema so a row carrying MORE values than the header
    names is reported rather than consumed. DictReader hands those back
    under a None key, and treating that as surplus to ignore is how the
    2026-08-02 misalignment survived long enough to be read back as real
    data -- a quality score of 31.84 parsed as a contract symbol.

    A missing file is still nothing to worry about; a corrupt one is not
    the same thing and no longer looks like one.
    """

    source = Path(path or SHADOW_FILE)

    if not source.exists():
        return []

    try:
        return csv_schema.read_rows(source)
    except csv_schema.SchemaRefused as refusal:
        print(f"options_shadow: refusing to replay a damaged log -- {refusal}")
        return []
    except OSError:
        return []


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    return result


def _unresolved_row(
    decision: dict[str, Any],
    symbol: str,
    debit: float,
    stamp: str,
) -> dict[str, Any]:
    """A placeholder row for a decision too recent to have history yet."""

    target, stop = exit_levels(debit)

    return {
        "timestamp": stamp,
        "underlying": decision.get("underlying", ""),
        "long_symbol": symbol,
        "strategy": decision.get("strategy", ""),
        "action": decision.get("action", ""),
        "debit": round(debit, 2),
        "quality": decision.get("quality", ""),
        "spread_percent": decision.get("spread_percent", ""),
        "target_price": round(target, 2),
        "stop_price": round(stop, 2),
        "outcome": OUTCOME_UNRESOLVED,
        "profit_loss": "",
        "return_percent": "",
        "bars_checked": 0,
        "resolved_at": "",
    }


def resolve_all(*, verbose: bool = True) -> list[dict[str, Any]]:
    """Replay every logged decision that carries enough data to score."""

    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from dotenv import load_dotenv

    load_dotenv()

    api_key = os.getenv(config.ALPACA_API_KEY_ENV)
    secret_key = os.getenv(config.ALPACA_SECRET_KEY_ENV)

    if not api_key or not secret_key:
        raise RuntimeError("Alpaca API keys were not found in the .env file.")

    client = OptionHistoricalDataClient(api_key, secret_key)

    decisions = load_decisions()
    resolved: list[dict[str, Any]] = []

    if verbose:
        print(f"Replaying {len(decisions)} logged option decision(s)…")

    hold_days = getattr(config, "OPTIONS_MAX_HOLD_DAYS", 10)

    for decision in decisions:
        symbol = (decision.get("long_symbol") or "").strip()
        debit = _float(decision.get("debit"))
        stamp = decision.get("timestamp")

        if not symbol or debit is None or debit <= 0 or not stamp:
            continue

        replayable, refusal = can_replay(decision)

        if not replayable:
            row = _unresolved_row(decision, symbol, debit, stamp)
            row["outcome"] = OUTCOME_SPREAD_UNSUPPORTED
            resolved.append(row)

            if verbose:
                print(f"  {symbol}: skipped -- {refusal}")

            continue

        try:
            entered = datetime.fromisoformat(stamp)
        except ValueError:
            continue

        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=timezone.utc)

        # The free options feed serves delayed data only. Asking for bars
        # inside the delay window -- or past it, which "entry + hold days"
        # always is for a recent decision -- fails the whole request with
        # "OPRA agreement is not signed" rather than returning what it
        # can. Clamping the end to safely behind the delay is the
        # difference between twelve NO_DATA rows and a real answer.
        window_end = min(
            entered + timedelta(days=hold_days + 1),
            datetime.now(timezone.utc) - DELAYED_FEED_MINUTES,
        )

        if window_end <= entered:
            # Logged too recently to have any delayed history yet. It will
            # resolve on a later run rather than being scored as no data.
            resolved.append(_unresolved_row(decision, symbol, debit, stamp))
            continue

        short_symbol = (decision.get("short_symbol") or "").strip()
        wanted = [symbol] + ([short_symbol] if short_symbol else [])

        def fetch(request_symbols):
            return client.get_option_bars(
                OptionBarsRequest(
                    symbol_or_symbols=request_symbols,
                    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                    start=entered,
                    end=window_end,
                )
            )

        try:
            response = fetch(wanted)
            bars = list(response[symbol]) if symbol in response.data else []
            short_bars = (
                list(response[short_symbol])
                if short_symbol and short_symbol in response.data
                else []
            )

        except Exception as error:
            if verbose:
                print(f"  {symbol}: bar fetch failed "
                      f"({type(error).__name__}: {error})")
            bars = []
            short_bars = []

        if short_symbol:
            # Verticals are priced from synchronised closes, never from
            # the long leg alone. See replay_spread for why.
            paired = align_closes(bars, short_bars)
            replay = replay_spread(paired, debit=debit, start=entered)
            method = "close_only"

            if verbose and not paired:
                print(f"  {symbol}: no overlapping bars with "
                      f"{short_symbol}; cannot price the spread")
        else:
            replay = replay_bars(bars, debit=debit, start=entered)
            method = "high_low"

        # The window ran out in WALL-CLOCK terms even though no bar
        # arrived past its end.
        #
        # The in-loop expiry branch needs a bar timestamped beyond
        # entry + hold_days, and option bars are far too sparse for that
        # to be reliable -- the ASHR decision of 2026-07-30 had TWO bars
        # across eleven days. So decisions whose window had long closed
        # sat at UNRESOLVED forever, counted as neither win nor loss and
        # invisible in the scorecard. OUTCOME_EXPIRED had never once
        # fired in 42 replayed rows.
        #
        # This is the same censoring the equity shadow book carried until
        # 2026-08-10, where the dropped rows turned out to be mildly
        # POSITIVE and the recorded figure was pessimistic by 0.13R.
        if (
            replay.outcome == OUTCOME_UNRESOLVED
            and datetime.now(timezone.utc) > entered + timedelta(days=hold_days)
        ):
            profit, percent = mark_to_market(debit, replay.last_value)
            replay = Replay(
                OUTCOME_EXPIRED,
                replay.bars_checked,
                profit,
                percent,
                last_value=replay.last_value,
            )

        target, stop = exit_levels(debit)

        resolved.append({
            "timestamp": stamp,
            "underlying": decision.get("underlying", ""),
            "long_symbol": symbol,
            "strategy": decision.get("strategy", ""),
            "action": decision.get("action", ""),
            "debit": round(debit, 2),
            "quality": decision.get("quality", ""),
            "spread_percent": decision.get("spread_percent", ""),
            "short_symbol": short_symbol,
            "method": method,
            "target_price": round(target, 2),
            "stop_price": round(stop, 2),
            "outcome": replay.outcome,
            "profit_loss": (
                round(replay.profit_loss, 2)
                if replay.profit_loss is not None else ""
            ),
            "return_percent": (
                round(replay.return_percent, 2)
                if replay.return_percent is not None else ""
            ),
            "bars_checked": replay.bars_checked,
            "resolved_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
        })

    if resolved:
        # A FULL REWRITE cannot misalign -- the header is written fresh
        # every time -- which is why LOCKBOT ordered this file last and
        # called it the safe one. But safe from misalignment is not safe
        # from DELETION: rewriting with fieldnames=RESOLVED_COLUMNS
        # against a WIDER header silently drops the surplus columns and
        # every value in them, exactly as shadow_trades.save_rows would
        # have. This file is derived and could be regenerated, so the loss
        # is recoverable rather than permanent -- but silently discarding
        # another version's output is still the thing csv_schema exists to
        # refuse.
        header = csv_schema.ensure_schema(
            RESOLVED_FILE, RESOLVED_COLUMNS, verbose=False
        )

        with Path(RESOLVED_FILE).open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(
                handle, fieldnames=header, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(resolved)

    return resolved


def report(resolved: list[dict[str, Any]]) -> None:
    """Print what the replayed decisions say."""

    print()
    print("=" * 70)
    print("        LOCKBOT OPTIONS SHADOW REPORT")
    print("=" * 70)

    if not resolved:
        print("No option decisions could be resolved yet.")
        return

    counts: dict[str, int] = {}

    for row in resolved:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1

    print(f"  decisions replayed : {len(resolved)}")

    for outcome, count in sorted(counts.items(), key=lambda i: -i[1]):
        print(f"    {outcome:<20} {count}")

    if counts.get(OUTCOME_SPREAD_UNSUPPORTED):
        print()
        print(f"  {counts[OUTCOME_SPREAD_UNSUPPORTED]} vertical spread(s) "
              "excluded: both legs cannot be")
        print("  priced together on a free feed, and pricing the long leg")
        print("  alone scored real losses as wins. See can_replay().")

    decided = [
        row for row in resolved
        if row["outcome"] in (OUTCOME_TARGET, OUTCOME_STOP, OUTCOME_AMBIGUOUS)
    ]

    if not decided:
        print("\nNothing has reached a target or a stop yet.")
        return

    wins = sum(1 for row in decided if row["outcome"] == OUTCOME_TARGET)
    net = sum(float(row["profit_loss"] or 0) for row in decided)
    ambiguous = sum(1 for row in decided if row["outcome"] == OUTCOME_AMBIGUOUS)

    print()
    print(f"  decided            : {len(decided)}")
    print(f"  win rate           : {wins / len(decided):.1%} "
          "(needs 41.2% at +50%/-35%)")

    # Both bounds, never one. An AMBIGUOUS bar spans target and stop and
    # is booked as the loss, which is the pessimistic reading; excluding
    # it instead is the optimistic one. Reporting the pair bounds the
    # true rate rather than quietly picking a side. Matches the equity
    # shadow convention adopted 2026-08-10.
    unambiguous = len(decided) - ambiguous

    if ambiguous:
        print(f"  win rate excl. ambig: {wins / unambiguous:.1%}"
              f"   ({ambiguous} ambiguous excluded)")
    else:
        print("  win rate excl. ambig: same — no ambiguous bars on record")

    print(f"  net if all taken   : ${net:+,.2f}")

    # The decisions whose window ran out. Kept OUT of the win rate -- a
    # target-touch rate must keep meaning a target-touch rate -- but
    # shown, because they are exactly the slow movers the decided sample
    # drops, and their absence is what makes it fast-mover-enriched.
    expired = [row for row in resolved if row["outcome"] == OUTCOME_EXPIRED]

    if expired:
        marked = [float(row["profit_loss"]) for row in expired
                  if str(row.get("profit_loss", "")).strip() != ""]

        print()
        print(f"  expired (no touch) : {len(expired)}"
              "   — excluded from the win rate above")

        if marked:
            print(f"    net at mark      : ${sum(marked):+,.2f}"
                  f"   ({len(marked)} of {len(expired)} markable)")
            print(f"    ALL-IN net       : ${net + sum(marked):+,.2f}"
                  f"   (decided + expired at mark)")
        else:
            print("    none markable — no closing price inside the window")

    # The rows LOCKBOT did NOT act on are the point of the exercise.
    skipped = [row for row in decided if row["action"] != "ORDER_SUBMITTED"]

    if skipped:
        skipped_net = sum(float(row["profit_loss"] or 0) for row in skipped)
        skipped_wins = sum(
            1 for row in skipped if row["outcome"] == OUTCOME_TARGET
        )

        print()
        print("  Decisions LOCKBOT did not act on:")
        print(f"    count            : {len(skipped)}")
        print(f"    would have won   : {skipped_wins}")
        print(f"    net forgone      : ${skipped_net:+,.2f}")
        print("    (positive means skipping them cost money)")

    print()
    print("  Fills are assumed at the exact target or stop, exit spread is")
    print("  ignored, and ambiguous bars count as losses. Real results")
    print("  would be worse. Directional evidence, not a P&L statement.")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    class FakeBar:
        def __init__(self, high, low, minutes=0, close=None):
            self.high = high
            self.low = low
            # Real option bars always carry a close; the older tests
            # predate it being needed, so it stays optional.
            self.close = close
            self.timestamp = datetime(
                2026, 7, 30, 14, 0, tzinfo=timezone.utc
            ) + timedelta(minutes=minutes)

    start = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)

    print("Exit levels")

    target, stop = exit_levels(100.0)
    check("target is +50%", abs(target - 150.0) < 1e-9, str(target))
    check("stop is -35%", abs(stop - 65.0) < 1e-9, str(stop))

    print()
    print("Expiry is marked to market, not dropped")

    # Debit $65, last close 0.60/share = $60/contract -> -$5.00, -7.69%.
    ran_out = replay_bars(
        [FakeBar(0.70, 0.66, 5, close=0.68),
         FakeBar(0.69, 0.62, 10, close=0.60),
         FakeBar(0.69, 0.62, 60 * 24 * 11, close=0.60)],
        debit=65.0, start=start,
    )
    check("a window that ran out is EXPIRED",
          ran_out.outcome == OUTCOME_EXPIRED, ran_out.outcome)
    check("and carries a mark, not a blank",
          ran_out.profit_loss is not None and abs(ran_out.profit_loss + 5.0) < 1e-9,
          str(ran_out.profit_loss))
    check("the mark is a percentage of the debit",
          abs(ran_out.return_percent + 7.6923) < 1e-3,
          str(ran_out.return_percent))

    check("mark_to_market returns None, never 0.0, with no close",
          mark_to_market(65.0, None) == (None, None))
    check("and None on a zero debit",
          mark_to_market(0.0, 60.0) == (None, None))
    check("a profitable mark is positive",
          mark_to_market(65.0, 80.0)[0] == 15.0)

    # An undecided window that is still OPEN must stay UNRESOLVED -- only
    # resolve_all, which knows the wall clock, may convert it.
    still_open = replay_bars(
        [FakeBar(0.70, 0.66, 5, close=0.68)], debit=65.0, start=start,
    )
    check("an undecided but still-open window stays UNRESOLVED",
          still_open.outcome == OUTCOME_UNRESOLVED, still_open.outcome)
    check("but it carries the last value for later marking",
          still_open.last_value is not None
          and abs(still_open.last_value - 68.0) < 1e-9,
          str(still_open.last_value))

    check("a decided outcome is unaffected by the mark",
          replay_bars([FakeBar(1.00, 0.90, 10, close=0.95)],
                      debit=65.0, start=start).outcome == OUTCOME_TARGET)

    print()
    print("Replay")

    # Bars are per share; a $65 debit is 0.65 per share.
    hit = replay_bars(
        [FakeBar(0.70, 0.66, 5), FakeBar(1.00, 0.90, 10)],
        debit=65.0, start=start,
    )
    check("reaches the target", hit.outcome == OUTCOME_TARGET, hit.outcome)
    check(
        "target pays +50% of the debit",
        abs(hit.profit_loss - 32.5) < 1e-9,
        str(hit.profit_loss),
    )

    stopped = replay_bars(
        [FakeBar(0.66, 0.60, 5), FakeBar(0.50, 0.40, 10)],
        debit=65.0, start=start,
    )
    check("reaches the stop", stopped.outcome == OUTCOME_STOP, stopped.outcome)
    check(
        "stop loses 35% of the debit",
        abs(stopped.profit_loss + 22.75) < 1e-9,
        str(stopped.profit_loss),
    )

    both = replay_bars([FakeBar(1.20, 0.30, 5)], debit=65.0, start=start)
    check(
        "a bar spanning both levels is ambiguous",
        both.outcome == OUTCOME_AMBIGUOUS,
        both.outcome,
    )
    check(
        "and is scored as the loss, not the win",
        both.profit_loss < 0,
        str(both.profit_loss),
    )

    flat = replay_bars(
        [FakeBar(0.70, 0.60, 5), FakeBar(0.72, 0.64, 10)],
        debit=65.0, start=start,
    )
    check("never touching a level is unresolved",
          flat.outcome == OUTCOME_UNRESOLVED, flat.outcome)
    check("and reports how many bars it saw", flat.bars_checked == 2)

    check("no bars means no data",
          replay_bars([], debit=65.0, start=start).outcome == OUTCOME_NO_DATA)
    check("a zero debit is rejected",
          replay_bars([FakeBar(1.0, 0.9)], debit=0.0).outcome == OUTCOME_NO_DATA)

    print()
    print("Time limits and ordering")

    stale = replay_bars(
        [FakeBar(2.00, 1.90, 60 * 24 * 30)],
        debit=65.0, start=start, max_hold_days=10,
    )
    check("a bar past the hold limit expires the trade",
          stale.outcome == OUTCOME_EXPIRED, stale.outcome)

    early = replay_bars(
        [FakeBar(2.00, 1.90, -60), FakeBar(0.70, 0.66, 5)],
        debit=65.0, start=start,
    )
    check(
        "bars before the decision are ignored",
        early.outcome != OUTCOME_TARGET or early.bars_checked == 1,
        f"{early.outcome} after {early.bars_checked} bars",
    )

    print()
    print("Spreads price from synchronised closes")

    class Bar:
        def __init__(self, close, minutes=0, high=None, low=None):
            self.close = close
            self.high = high if high is not None else close
            self.low = low if low is not None else close
            self.timestamp = datetime(
                2026, 7, 30, 14, 0, tzinfo=timezone.utc
            ) + timedelta(minutes=minutes)

    # Legs must line up by exact timestamp.
    longs = [Bar(1.15, 0), Bar(1.20, 5), Bar(1.30, 10)]
    shorts = [Bar(0.91, 0), Bar(0.95, 5), Bar(0.99, 10)]
    paired = align_closes(longs, shorts)
    check("aligns bars by timestamp", len(paired) == 3, str(len(paired)))
    check(
        "and pairs the right closes",
        abs(paired[0][1] - 1.15) < 1e-9 and abs(paired[0][2] - 0.91) < 1e-9,
    )

    # A long-leg bar with no matching short bar cannot be priced.
    check(
        "unmatched bars are dropped, not guessed",
        len(align_closes([Bar(1.15, 0), Bar(1.20, 5)], [Bar(0.91, 0)])) == 1,
    )
    check("no overlap yields nothing", align_closes(longs, []) == [])

    start = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)

    # A $24 debit spread: target $36, stop $15.60.
    widening = align_closes(
        [Bar(1.15, 0), Bar(1.50, 5)], [Bar(0.91, 0), Bar(1.00, 5)]
    )
    hit = replay_spread(widening, debit=24.0, start=start)
    check("a widening spread reaches the target",
          hit.outcome == OUTCOME_TARGET, hit.outcome)

    narrowing = align_closes(
        [Bar(1.15, 0), Bar(1.02, 5)], [Bar(0.91, 0), Bar(0.92, 5)]
    )
    stopped = replay_spread(narrowing, debit=24.0, start=start)
    check("a narrowing spread reaches the stop",
          stopped.outcome == OUTCOME_STOP, stopped.outcome)
    check("and the loss is bounded by the debit",
          abs(stopped.profit_loss) <= 24.0, str(stopped.profit_loss))

    # The bug this replaces: the long leg alone would have cleared the
    # target instantly at 1.15 (=$115 against a $36 target).
    long_leg_only = replay_bars(
        [Bar(1.15, 0, high=1.15, low=1.15)], debit=24.0, start=start
    )
    check(
        "the long leg alone WOULD have falsely hit target",
        long_leg_only.outcome == OUTCOME_TARGET,
        long_leg_only.outcome,
    )
    check(
        "but the spread priced properly does not",
        replay_spread(
            align_closes([Bar(1.15, 0)], [Bar(0.91, 0)]),
            debit=24.0, start=start,
        ).outcome != OUTCOME_TARGET,
    )

    # A crossed quote on one leg must not read as a loss.
    crossed = replay_spread(
        align_closes([Bar(0.80, 0)], [Bar(1.20, 0)]), debit=24.0, start=start
    )
    check("a negative spread value is skipped, not stopped on",
          crossed.outcome != OUTCOME_STOP, crossed.outcome)

    check("a zero debit is rejected",
          replay_spread(paired, debit=0.0).outcome == OUTCOME_NO_DATA)
    check("no paired bars means no data",
          replay_spread([], debit=24.0).outcome == OUTCOME_NO_DATA)

    print()
    print("Everything is scorable now, by the right method")

    check("a single-leg call is scorable",
          can_replay({"long_symbol": "EWZ260821C00036500",
                      "short_symbol": ""})[0] is True)
    check("a vertical is now scorable too",
          can_replay({"long_symbol": "ASHR260821C00034500",
                      "short_symbol": "ASHR260821C00035500"})[0] is True)

    print()
    print("Loading")

    check("a missing log returns no rows",
          load_decisions(Path("does_not_exist.csv")) == [])
    check("float parsing survives junk", _float("n/a") is None)
    check("float parsing works", _float("48.0") == 48.0)

    # ---- csv_schema conversion, 2026-08-13
    #
    # LOCKBOT ordered this file LAST and called it the safe one, because
    # resolve_all rewrites the resolved file whole and a fresh header
    # cannot misalign. That is true and it is not the only risk: a full
    # rewrite against RESOLVED_COLUMNS would silently DELETE a wider
    # header's surplus columns, and load_decisions would happily consume
    # a source log whose rows are already askew.
    print()
    print("Schema safety")

    import tempfile as _tempfile

    tmp = Path(_tempfile.gettempdir())

    damaged = tmp / "options_shadow_damaged_selftest.csv"
    damaged.write_text("timestamp,underlying\n2026-01-01,AAA,SURPLUS\n",
                       encoding="utf-8")
    check("a source log with askew rows is refused, not replayed",
          load_decisions(damaged) == [])
    damaged.unlink(missing_ok=True)

    missing = tmp / "options_shadow_absent_selftest.csv"
    missing.unlink(missing_ok=True)
    check("a MISSING log is still just empty, not an error",
          load_decisions(missing) == [])

    clean = tmp / "options_shadow_clean_selftest.csv"
    with clean.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "underlying"])
        writer.writeheader()
        writer.writerow({"timestamp": "2026-01-01", "underlying": "AAA"})
    check("a clean log still reads normally",
          len(load_decisions(clean)) == 1)
    clean.unlink(missing_ok=True)

    wider = tmp / "options_shadow_wider_selftest.csv"
    wider_cols = list(RESOLVED_COLUMNS) + ["written_by_newer_code"]
    with wider.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=wider_cols)
        writer.writeheader()
        writer.writerow({c: "" for c in wider_cols}
                        | {"underlying": "KEEP", "written_by_newer_code": "KEEP ME"})
    before_bytes = wider.read_bytes()

    refused = False
    try:
        csv_schema.ensure_schema(wider, RESOLVED_COLUMNS, verbose=False)
    except csv_schema.SchemaRefused:
        refused = True
    check("a WIDER resolved file is refused rather than overwritten", refused)
    check("and is left byte-identical", wider.read_bytes() == before_bytes)
    check("so the newer column's values survive",
          "KEEP ME" in wider.read_text(encoding="utf-8"))
    wider.unlink(missing_ok=True)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All options-shadow checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    report(resolve_all())
