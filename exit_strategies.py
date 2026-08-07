"""
exit_strategies.py — is the EXIT the thing that was never varied?

WHY THIS EXISTS

Every backtest in this project used one exit: a fixed stop, a fixed
target, a fixed time limit, all set at entry and never moved. Its dials
were swept thoroughly -- ratios 1.0 to 3.0, stops 2% to 8%, holds 24 to
1560 bars -- and its SHAPE never was.

LOCKBOT argued that exits cannot matter: "sizing and exits scale an
expectation, they don't change its sign -- the entry carries zero
information, so there is nothing downstream to amplify." That is true of
SYMMETRIC exits. A fixed bracket multiplies whatever the entry gives.

It is not obviously true of asymmetric ones. A trailing stop cuts losses
at a fixed distance and leaves winners open, which manufactures positive
skew from the exit alone regardless of entry quality. Trend-following is
precisely that: an exit rule applied to fairly arbitrary entries.

So the claim has a hole, and this measures it.

THE DESIGN, AND THE TRAP IT AVOIDS

Exit structures cross entry sources. Every exit is run on BOTH the rule
and seeded random entries, on identical bars.

Comparing "trailing stop on the rule" against "fixed bracket on random"
would change two things at once and prove nothing -- the same confound
LOCKBOT's price-matched control caught in the news test. The control for
a trailing stop is a trailing stop.

THREE OUTCOMES, AND THEY MEAN DIFFERENT THINGS

  exits change nothing            LOCKBOT's claim holds in general
  exits help rule AND random
    equally                       a better EXIT, not an edge. Worth
                                  having, but it is not entry skill
  exits help the rule MORE        entry and exit interact, which is the
                                  only result that would be an edge

The middle case is the one LOCKBOT called potentially the most
informative thing the lab has run: random entry with a trailing stop is
trend-following, and if that alone is positive it says the asymmetry
does the work.
"""

from __future__ import annotations

import sys
from collections import defaultdict

STOP_PERCENT = 0.05
REWARD_RATIO = 2.0
MAX_HOLD_DAYS = 20


def _r(entry: float, exit_price: float, stop_percent: float) -> float:
    """Result in R, where 1R is the initial stop distance."""

    risk = entry * stop_percent

    return (exit_price - entry) / risk if risk > 0 else 0.0


def exit_fixed(bars, entry, *, stop_percent=STOP_PERCENT,
               reward_ratio=REWARD_RATIO, max_hold=MAX_HOLD_DAYS):
    """The exit every previous test used. Levels set once, never moved."""

    stop = entry * (1 - stop_percent)
    target = entry * (1 + stop_percent * reward_ratio)

    for bar in bars[:max_hold]:
        if bar["high"] >= target and bar["low"] <= stop:
            return -1.0
        if bar["high"] >= target:
            return float(reward_ratio)
        if bar["low"] <= stop:
            return -1.0

    if not bars:
        return None

    return _r(entry, bars[min(len(bars), max_hold) - 1]["close"], stop_percent)


def exit_trailing(bars, entry, *, stop_percent=STOP_PERCENT,
                  max_hold=MAX_HOLD_DAYS, **_):
    """Stop follows the high up. No target -- winners are left open.

    The asymmetric case. Losses are capped at the initial distance while
    gains are uncapped, so positive skew comes from the exit itself.
    """

    stop = entry * (1 - stop_percent)
    peak = entry

    for bar in bars[:max_hold]:
        # Checked BEFORE the stop is raised, so an intrabar low cannot be
        # rescued by the same bar's high. Assuming otherwise is the
        # classic way a trailing-stop backtest flatters itself.
        if bar["low"] <= stop:
            return _r(entry, stop, stop_percent)

        peak = max(peak, bar["high"])
        stop = max(stop, peak * (1 - stop_percent))

    if not bars:
        return None

    return _r(entry, bars[min(len(bars), max_hold) - 1]["close"], stop_percent)


def exit_breakeven_trail(bars, entry, *, stop_percent=STOP_PERCENT,
                         max_hold=MAX_HOLD_DAYS, **_):
    """Stop to entry once 1R is banked, then trail."""

    stop = entry * (1 - stop_percent)
    trigger = entry * (1 + stop_percent)
    armed = False
    peak = entry

    for bar in bars[:max_hold]:
        if bar["low"] <= stop:
            return _r(entry, stop, stop_percent)

        peak = max(peak, bar["high"])

        if not armed and bar["high"] >= trigger:
            armed = True
            stop = max(stop, entry)

        if armed:
            stop = max(stop, peak * (1 - stop_percent))

    if not bars:
        return None

    return _r(entry, bars[min(len(bars), max_hold) - 1]["close"], stop_percent)


EXITS = {
    "fixed bracket": exit_fixed,
    "trailing stop": exit_trailing,
    "breakeven+trail": exit_breakeven_trail,
}


def run(entries, bars_by_symbol, exit_fn, *, stop_percent=STOP_PERCENT):
    """Score a list of (symbol, date) entries under one exit structure."""

    results = []

    for symbol, day in entries:
        series = bars_by_symbol.get(symbol)

        if not series:
            continue

        days = sorted(series)

        try:
            index = days.index(day)
        except ValueError:
            continue

        if index + 1 >= len(days):
            continue

        entry = series[days[index + 1]]["open"]

        if entry <= 0:
            continue

        forward = [series[d] for d in days[index + 2:]]
        r = exit_fn(forward, entry, stop_percent=stop_percent)

        if r is not None:
            results.append((day.year, r))

    return results


def summarise(results):
    """(n, mean R, win rate) overall and per year."""

    if not results:
        return {"n": 0, "mean_r": 0.0, "win_rate": 0.0, "by_year": {}}

    values = [r for _, r in results]
    by_year = defaultdict(list)

    for year, r in results:
        by_year[year].append(r)

    return {
        "n": len(values),
        "mean_r": sum(values) / len(values),
        "win_rate": sum(1 for v in values if v > 0) / len(values),
        "by_year": {y: sum(v) / len(v) for y, v in sorted(by_year.items())},
    }


def _self_test() -> int:
    failures = []

    def check(name, condition, detail=""):
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    def bar(high, low, close):
        return {"high": high, "low": low, "close": close, "open": close}

    print("The fixed bracket behaves as every previous test assumed")

    entry = 100.0
    win = [bar(111.0, 99.0, 110.0)]
    check("a target hit returns the reward ratio",
          exit_fixed(win, entry) == 2.0, str(exit_fixed(win, entry)))

    loss = [bar(101.0, 94.0, 95.0)]
    check("a stop hit returns -1R", exit_fixed(loss, entry) == -1.0)

    both = [bar(111.0, 94.0, 100.0)]
    check("a bar touching both counts against",
          exit_fixed(both, entry) == -1.0)

    print()
    print("The trailing stop is asymmetric")

    # Rises steadily then reverses. A fixed bracket caps at 2R; a trail
    # should bank more, which is the entire premise.
    climb = [bar(100.0 + i * 3, 99.0 + i * 3, 100.0 + i * 3)
             for i in range(1, 8)] + [bar(121.0, 100.0, 101.0)]

    trailed = exit_trailing(climb, entry)
    fixed = exit_fixed(climb, entry)
    check("a trail banks more than a capped target on a long run",
          trailed > fixed, f"trail {trailed:.2f} vs fixed {fixed:.2f}")

    check("a trail still caps the loss at the initial stop",
          abs(exit_trailing([bar(100.5, 90.0, 91.0)], entry) + 1.0) < 1e-9)

    # The classic backtest flatter: a bar whose HIGH would raise the stop
    # and whose LOW would hit it. The low must win.
    whipsaw = [bar(130.0, 94.0, 95.0)]
    check("an intrabar low is not rescued by the same bar's high",
          exit_trailing(whipsaw, entry) == -1.0,
          str(exit_trailing(whipsaw, entry)))

    print()
    print("Breakeven arms only after a gain")

    early = [bar(101.0, 94.0, 95.0)]
    check("a loss before the trigger is a full loss",
          exit_breakeven_trail(early, entry) == -1.0)

    armed = [bar(106.0, 100.0, 105.0), bar(106.0, 99.0, 100.0)]
    result = exit_breakeven_trail(armed, entry)
    check("once armed, the stop is no worse than entry", result >= -0.01,
          str(result))

    print()
    print("Every exit is available to every entry source")

    check("three structures registered", len(EXITS) == 3)
    check("the fixed bracket is one of them", "fixed bracket" in EXITS)
    check("R is measured against the INITIAL stop distance",
          abs(_r(100.0, 105.0, 0.05) - 1.0) < 1e-9)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All exit-strategy checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(__doc__)
