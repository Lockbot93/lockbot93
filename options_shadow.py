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
    "strategy",
    "action",
    "debit",
    "quality",
    "spread_percent",
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

    for bar in bars:
        moment = getattr(bar, "timestamp", None)

        if start is not None and moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)

            if moment < start:
                continue

            if hold_days and moment > start + timedelta(days=hold_days):
                return Replay(OUTCOME_EXPIRED, checked, None, None)

        checked += 1

        high = float(bar.high) * CONTRACT_MULTIPLIER
        low = float(bar.low) * CONTRACT_MULTIPLIER

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

    return Replay(OUTCOME_UNRESOLVED, checked, None, None)


def can_replay(decision: dict[str, Any]) -> tuple[bool, str]:
    """Whether this decision can be scored honestly. Returns (ok, reason).

    Single-leg positions can: the contract's own bars are exactly what the
    position was worth. Verticals cannot, and the first version of this
    module got that badly wrong.

    A bull call spread is worth long minus short. Replaying the long leg
    alone values a position at several times its real price, so every
    spread cleared its +50% target on the first bar it saw. That produced
    a 100% win rate across nine decisions, including ASHR and JD -- both
    of which had actually stopped out for real losses of $11 and $8. The
    replay contradicted the broker's own record.

    Combining two legs' OHLC bars does not fix it either. The long leg's
    high and the short leg's low did not occur at the same instant, so
    max(long) - min(short) is an upper bound on a price that never
    existed. It would inflate the result the same way, just less visibly.

    Scoring these correctly needs synchronised quotes for both legs, which
    the free feed does not provide. So they are marked unsupported and
    excluded from every statistic. An unmeasured decision is honest; a
    confidently wrong one is not.
    """

    if (decision.get("short_symbol") or "").strip():
        return False, "vertical spread: both legs cannot be priced together"

    return True, ""


def load_decisions(path: Path | None = None) -> list[dict[str, Any]]:
    """Read the options decision log, tolerating a missing file."""

    source = Path(path or SHADOW_FILE)

    if not source.exists():
        return []

    try:
        with source.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
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

        try:
            response = client.get_option_bars(
                OptionBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                    start=entered,
                    end=window_end,
                )
            )
            bars = list(response[symbol]) if symbol in response.data else []

        except Exception as error:
            if verbose:
                print(f"  {symbol}: bar fetch failed "
                      f"({type(error).__name__}: {error})")
            bars = []

        replay = replay_bars(bars, debit=debit, start=entered)
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
        with Path(RESOLVED_FILE).open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(
                handle, fieldnames=RESOLVED_COLUMNS, extrasaction="ignore"
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

    print()
    print(f"  decided            : {len(decided)}")
    print(f"  win rate           : {wins / len(decided):.1%} "
          "(needs 41.2% at +50%/-35%)")
    print(f"  net if all taken   : ${net:+,.2f}")

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
        def __init__(self, high, low, minutes=0):
            self.high = high
            self.low = low
            self.timestamp = datetime(
                2026, 7, 30, 14, 0, tzinfo=timezone.utc
            ) + timedelta(minutes=minutes)

    start = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)

    print("Exit levels")

    target, stop = exit_levels(100.0)
    check("target is +50%", abs(target - 150.0) < 1e-9, str(target))
    check("stop is -35%", abs(stop - 65.0) < 1e-9, str(stop))

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
    print("Spreads are refused, not guessed")

    single = {"long_symbol": "EWZ260821C00036500", "short_symbol": ""}
    ok, _ = can_replay(single)
    check("a single-leg call is replayable", ok is True)

    # The real ASHR spread, which the first version of this module scored
    # as a TARGET on bar one while the broker recorded a $11 stop loss.
    ashr = {
        "long_symbol": "ASHR260821C00034500",
        "short_symbol": "ASHR260821C00035500",
    }
    ok, reason = can_replay(ashr)
    check("the ASHR spread is refused", ok is False, reason)
    check("and says why", "spread" in reason.lower(), reason)

    jd = {
        "long_symbol": "JD260821C00032500",
        "short_symbol": "JD260821C00033000",
    }
    check("the JD spread is refused", can_replay(jd)[0] is False)

    check(
        "a whitespace-only short leg still counts as single-leg",
        can_replay({"long_symbol": "X", "short_symbol": "   "})[0] is True,
    )
    check(
        "a missing short_symbol key is single-leg",
        can_replay({"long_symbol": "X"})[0] is True,
    )

    print()
    print("Loading")

    check("a missing log returns no rows",
          load_decisions(Path("does_not_exist.csv")) == [])
    check("float parsing survives junk", _float("n/a") is None)
    check("float parsing works", _float("48.0") == 48.0)

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
