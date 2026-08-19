"""
PRE-REGISTERED TEST, run once: does a fixed-horizon exit beat the bracket?

HYPOTHESIS, committed before any figure was produced

    Across the equity shadow book, exiting at a fixed horizon with no price
    stop and no price target produces a higher average R than the current
    adaptive bracket produces on the same setups.

WHY IT WAS WORTH ASKING

    The expired group -- setups where no band ever fired -- averaged +0.07R
    against the decided group's -0.46R. That is the only positive number in
    the book, and it suggests the price exits may be harvesting noise rather
    than protecting capital.

THE CONFOUND, AND WHY THIS SCORES EVERY ROW

    The expired group is selected ON THE OUTCOME of not touching a band,
    which correlates with being a slow mover. "No stop" was therefore never
    applied to the fast movers, and nothing can be concluded from that
    subset. So every row in the book is scored at horizon close with the
    bands ignored -- no filtering on outcome, no exclusions. A row that
    cannot be priced at horizon is reported as unscorable and counted. It is
    never given a default value, because a default value is a claim.

FIXED PARAMETERS -- set before execution, not tuned afterwards

    horizon     exactly SHADOW_MAX_DAYS. ONE value, never swept. Testing ten
                horizons and reporting the best is a ten-way search dressed
                up as one test.
    metric      average R per setup, using the book's own R definition
                (signed move / |entry - stop|) so figures stay comparable.
                Win rate is reported alongside; average R decides.
    population  every row in the equity shadow book, long and short, no
                symbol filter and no date filter.
    seed        RANDOM_SEED below, reported in the output.

CONTROLS -- the test is void without all three

    1. the bracket on the same rows          the direct comparison
    2. a seeded random-entry control         the do-nothing baseline, with
                                             its SPREAD established
                                             empirically rather than assumed
    3. the buy-and-hold sleeve               the bar to clear

KILL CRITERIA -- written before the answer was known

    The hypothesis FAILS if any of these holds:
      - horizon R does not exceed bracket R by more than the spread of the
        random control
      - horizon R does not exceed the random control at all
      - horizon R is below the sleeve over the same window
      - the result depends on excluding rows, on a different horizon, or on
        any filter not named here

    A failed pre-registered test is a completed piece of work. It is not a
    starting point for a variant.

WHAT THIS TEST CANNOT DO

    It cannot establish that the bot is profitable. It tests one exit
    geometry against three baselines on one book covering a few weeks of one
    market. A pass means "worth a second pre-registered test out of sample",
    not "deploy". And every input is derived from the same OHLCV bars, so it
    does not touch the binding constraint.

USAGE
    python horizon_exit_test.py              run the test
    python horizon_exit_test.py --self-test  offline checks, no network
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import lockbot_config as config
import shadow_trades

RANDOM_SEED = 20260810
CONTROL_REPLICATIONS = 200
SLEEVE = ("SCHG", "SCHD")

HORIZON_DAYS = shadow_trades.SHADOW_MAX_DAYS


# --------------------------------------------------------------------------
# Scoring (pure -- this is what --self-test exercises)
# --------------------------------------------------------------------------

def r_at_exit(
    *,
    entry_price: float,
    stop_price: float,
    exit_price: Optional[float],
    side: str,
) -> Optional[float]:
    """The book's own R definition: signed move over the stop distance.

    Returns None when it cannot be computed. Never 0.0 -- `simulate_symbol`
    once left timed-out trades at 0.0 and thereby recorded them as having
    broken even.
    """

    if exit_price is None or not entry_price:
        return None

    risk = abs(float(entry_price) - float(stop_price))

    if not risk:
        return None

    is_long = str(side).upper() in {"LONG", "BUY_LONG", "BUY"}
    move = (exit_price - entry_price) if is_long else (entry_price - exit_price)

    return move / risk


def pct_at_exit(
    *, entry_price: float, exit_price: Optional[float], side: str
) -> Optional[float]:
    """Signed percentage return, for the sleeve comparison."""

    if exit_price is None or not entry_price:
        return None

    is_long = str(side).upper() in {"LONG", "BUY_LONG", "BUY"}
    move = (exit_price - entry_price) if is_long else (entry_price - exit_price)

    return move / float(entry_price)


def bar_at_or_before(bars, moment) -> Optional[object]:
    """The last bar at or before `moment`. Bars need not be sorted."""

    best = None

    for bar in bars:
        stamp = getattr(bar, "timestamp", None)

        if stamp is None:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp > moment:
            continue
        if best is None or stamp > best[0]:
            best = (stamp, bar)

    return best[1] if best else None


def bars_on_day(bars, day, *, after=None):
    """Every bar falling on `day`, optionally after a moment."""

    out = []

    for bar in bars:
        stamp = getattr(bar, "timestamp", None)

        if stamp is None:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp.date() != day:
            continue
        if after is not None and stamp <= after:
            continue
        out.append((stamp, bar))

    return sorted(out, key=lambda pair: pair[0])


def close_of(bar) -> Optional[float]:
    value = getattr(bar, "close", None)

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Reporting helpers
# --------------------------------------------------------------------------

def describe(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label:<34} n=0")
        return

    ordered = sorted(values)
    n = len(ordered)
    mean = statistics.mean(ordered)
    print(
        f"  {label:<34} n={n:<5} mean {mean:+.3f}   "
        f"median {statistics.median(ordered):+.3f}   "
        f"sd {(statistics.stdev(ordered) if n > 1 else 0.0):.3f}"
    )
    print(
        f"  {'':<34} min {ordered[0]:+.2f}  p10 {ordered[int(.10*n)]:+.2f}  "
        f"p90 {ordered[min(int(.90*n), n-1)]:+.2f}  max {ordered[-1]:+.2f}"
    )


def histogram(values: list[float]) -> None:
    """A mean carried by two outliers is not an edge, so show the shape."""

    if not values:
        return

    edges = [-99, -3, -2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2, 3, 99]
    counts = [0] * (len(edges) - 1)

    for v in values:
        for i in range(len(edges) - 1):
            if edges[i] <= v < edges[i + 1]:
                counts[i] += 1
                break

    widest = max(counts) or 1
    print("\n  distribution of R under horizon exit")
    for i, count in enumerate(counts):
        lo, hi = edges[i], edges[i + 1]
        lo_s = "-inf" if lo == -99 else f"{lo:+.1f}"
        hi_s = "+inf" if hi == 99 else f"{hi:+.1f}"
        bar = "#" * int(40 * count / widest)
        print(f"    [{lo_s:>5}, {hi_s:>5})  {count:>4}  {bar}")


# --------------------------------------------------------------------------
# The test
# --------------------------------------------------------------------------

def run() -> int:
    print("=" * 78)
    print("PRE-REGISTERED TEST -- fixed-horizon exit versus the bracket")
    print("=" * 78)
    print(f"horizon            : {HORIZON_DAYS} days (SHADOW_MAX_DAYS, not swept)")
    print(f"seed               : {RANDOM_SEED}")
    print(f"control replications: {CONTROL_REPLICATIONS}")
    print(f"metric             : average R per setup (book definition)")

    rows = shadow_trades.load_rows(shadow_trades.SHADOW_FILE)
    now = datetime.now(timezone.utc)
    print(f"\nrows in the equity shadow book: {len(rows)}")

    # ---- gather bars once, for every symbol in the book
    symbols = sorted({r["symbol"] for r in rows if r.get("symbol")})
    earliest = min(
        (shadow_trades.parse_time(r["logged_at"]) for r in rows
         if shadow_trades.parse_time(r["logged_at"])),
        default=None,
    )
    print(f"distinct symbols: {len(symbols)}   earliest setup: {earliest}")
    print("\nfetching bars (this is the whole book, not a filtered subset)...")

    bars_by_symbol = shadow_trades.fetch_bars_for(symbols, earliest, now)
    print(f"symbols with bars returned: {len(bars_by_symbol)}")

    horizon_r: list[float] = []
    horizon_pct: list[float] = []
    bracket_r: list[float] = []
    paired = []            # (row, horizon R, bracket R)
    unscorable = defaultdict(int)
    scorable_rows = []

    for row in rows:
        logged = shadow_trades.parse_time(row.get("logged_at", ""))

        if logged is None:
            unscorable["unparseable logged_at"] += 1
            continue

        horizon_end = logged + timedelta(days=HORIZON_DAYS)

        if horizon_end > now:
            unscorable["horizon has not elapsed yet"] += 1
            continue

        bars = bars_by_symbol.get(row["symbol"], [])

        if not bars:
            unscorable["no bars returned for symbol"] += 1
            continue

        exit_bar = bar_at_or_before(bars, horizon_end)
        exit_price = close_of(exit_bar) if exit_bar is not None else None

        if exit_price is None:
            unscorable["no priced bar at or before horizon"] += 1
            continue

        try:
            entry = float(row["reference_price"])
            stop = float(row["stop_price"])
        except (TypeError, ValueError, KeyError):
            unscorable["unparseable entry or stop"] += 1
            continue

        hr = r_at_exit(entry_price=entry, stop_price=stop,
                       exit_price=exit_price, side=row["side"])
        hp = pct_at_exit(entry_price=entry, exit_price=exit_price,
                         side=row["side"])

        if hr is None:
            unscorable["R not computable (zero risk)"] += 1
            continue

        # the bracket's verdict on the SAME row
        raw = row.get("r_multiple")
        try:
            br = float(raw) if raw not in ("", None) else None
        except (TypeError, ValueError):
            br = None

        horizon_r.append(hr)
        horizon_pct.append(hp)
        scorable_rows.append(row)

        if br is not None:
            bracket_r.append(br)
            paired.append((row, hr, br))

    scorable = len(horizon_r)
    print(f"\nscorable at horizon : {scorable}")
    print(f"unscorable          : {sum(unscorable.values())}")
    for reason, count in sorted(unscorable.items(), key=lambda kv: -kv[1]):
        print(f"    {count:>4}  {reason}")

    if scorable == 0:
        print("\nNothing scorable. Test void.")
        return 1

    # ---- ARM 1: horizon exit
    print("\n" + "=" * 78)
    print("ARM 1 -- HORIZON EXIT (bands ignored)")
    print("=" * 78)
    describe("horizon exit, all rows", horizon_r)
    wins = sum(1 for v in horizon_r if v > 0)
    print(f"  win rate: {100*wins/scorable:.1f}%  ({wins}/{scorable})")
    histogram(horizon_r)

    # ---- ARM 2: the bracket on the same rows
    print("\n" + "=" * 78)
    print("ARM 2 -- THE BRACKET, same rows")
    print("=" * 78)
    describe("bracket", bracket_r)
    paired_h = [h for _, h, _ in paired]
    paired_b = [b for _, _, b in paired]
    print(f"\n  paired rows (both arms scorable): {len(paired)}")
    if paired:
        ph, pb = statistics.mean(paired_h), statistics.mean(paired_b)
        print(f"  horizon {ph:+.3f}   bracket {pb:+.3f}   "
              f"difference {ph - pb:+.3f}")

    # ---- ARM 3: seeded random-entry control
    print("\n" + "=" * 78)
    print("ARM 3 -- SEEDED RANDOM-ENTRY CONTROL")
    print("=" * 78)
    print("  same symbols, same dates, same horizon, same stop distance;")
    print("  only the entry TIME is drawn at random.")

    rng = random.Random(RANDOM_SEED)
    control_means = []
    control_draw_failures = 0

    for _ in range(CONTROL_REPLICATIONS):
        draw = []
        for row in scorable_rows:
            logged = shadow_trades.parse_time(row["logged_at"])
            bars = bars_by_symbol.get(row["symbol"], [])
            same_day = bars_on_day(bars, logged.date())

            if not same_day:
                continue

            _, entry_bar = same_day[rng.randrange(len(same_day))]
            entry_px = close_of(entry_bar)

            if entry_px is None:
                continue

            try:
                orig_entry = float(row["reference_price"])
                orig_stop = float(row["stop_price"])
            except (TypeError, ValueError):
                continue

            if not orig_entry:
                continue

            stop_fraction = abs(orig_entry - orig_stop) / orig_entry
            stamp = getattr(entry_bar, "timestamp", logged)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)

            exit_bar = bar_at_or_before(bars, stamp + timedelta(days=HORIZON_DAYS))
            exit_px = close_of(exit_bar) if exit_bar is not None else None

            value = r_at_exit(
                entry_price=entry_px,
                stop_price=entry_px * (1 - stop_fraction),
                exit_price=exit_px,
                side=row["side"],
            )

            if value is not None:
                draw.append(value)

        if draw:
            control_means.append(statistics.mean(draw))
        else:
            control_draw_failures += 1

    if not control_means:
        print("  control produced nothing. Test void.")
        return 1

    ordered = sorted(control_means)
    c_mean = statistics.mean(ordered)
    c_sd = statistics.stdev(ordered) if len(ordered) > 1 else 0.0
    c_lo, c_hi = ordered[int(.05 * len(ordered))], ordered[min(int(.95 * len(ordered)), len(ordered) - 1)]
    print(f"\n  replications      : {len(control_means)}")
    print(f"  control mean R    : {c_mean:+.3f}")
    print(f"  control spread    : sd {c_sd:.3f}   5th-95th [{c_lo:+.3f}, {c_hi:+.3f}]")
    print(f"  spread width      : {c_hi - c_lo:.3f}")

    # ---- ARM 4: the buy-and-hold sleeve
    print("\n" + "=" * 78)
    print("ARM 4 -- THE BUY-AND-HOLD SLEEVE")
    print("=" * 78)

    sleeve_bars = shadow_trades.fetch_bars_for(list(SLEEVE), earliest, now)
    sleeve_returns = []

    for symbol in SLEEVE:
        sb = sleeve_bars.get(symbol, [])
        if not sb:
            print(f"  {symbol}: no bars")
            continue
        per_row = []
        for row in scorable_rows:
            logged = shadow_trades.parse_time(row["logged_at"])
            start_bar = bar_at_or_before(sb, logged)
            end_bar = bar_at_or_before(sb, logged + timedelta(days=HORIZON_DAYS))
            sp, ep = close_of(start_bar) if start_bar else None, close_of(end_bar) if end_bar else None
            if sp and ep:
                per_row.append(ep / sp - 1.0)
        if per_row:
            avg = statistics.mean(per_row)
            sleeve_returns.append(avg)
            print(f"  {symbol}: matched {len(per_row)} windows, "
                  f"average {HORIZON_DAYS}-day return {avg*100:+.3f}%")

    sleeve_avg = statistics.mean(sleeve_returns) if sleeve_returns else None
    horizon_pct_clean = [p for p in horizon_pct if p is not None]
    horizon_pct_avg = statistics.mean(horizon_pct_clean) if horizon_pct_clean else None

    if sleeve_avg is not None and horizon_pct_avg is not None:
        print(f"\n  sleeve average      : {sleeve_avg*100:+.3f}% per {HORIZON_DAYS}-day window")
        print(f"  horizon exit average: {horizon_pct_avg*100:+.3f}% per setup")
        print(f"  difference          : {(horizon_pct_avg - sleeve_avg)*100:+.3f} points")

    # ---- required breakdowns
    print("\n" + "=" * 78)
    print("REQUIRED BREAKDOWNS")
    print("=" * 78)

    by_side = defaultdict(list)
    by_month = defaultdict(list)
    for row, hr, _ in [(r, h, None) for r, h in zip(scorable_rows, horizon_r)]:
        by_side[row["side"].upper()].append(hr)
        by_month[row["logged_at"][:7]].append(hr)

    print("\n  by direction")
    for side in sorted(by_side):
        describe(f"    {side}", by_side[side])

    print("\n  by month")
    for month in sorted(by_month):
        describe(f"    {month}", by_month[month])

    # ---- VERDICT
    print("\n" + "=" * 78)
    print("VERDICT against the pre-registered kill criteria")
    print("=" * 78)

    h_mean = statistics.mean(paired_h) if paired else statistics.mean(horizon_r)
    b_mean = statistics.mean(paired_b) if paired else None
    spread = c_hi - c_lo

    failures = []

    if b_mean is not None:
        margin = h_mean - b_mean
        ok = margin > spread
        print(f"  1. horizon beats bracket by more than the control spread")
        print(f"     margin {margin:+.3f} vs spread {spread:.3f}  -> {'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append("margin over bracket is within the control spread")
    else:
        failures.append("no paired bracket rows")

    ok2 = h_mean > c_mean
    print(f"  2. horizon exceeds the random control")
    print(f"     horizon {h_mean:+.3f} vs control {c_mean:+.3f}  -> {'PASS' if ok2 else 'FAIL'}")
    if not ok2:
        failures.append("does not exceed the random control")

    if sleeve_avg is not None and horizon_pct_avg is not None:
        ok3 = horizon_pct_avg > sleeve_avg
        print(f"  3. horizon exceeds the buy-and-hold sleeve")
        print(f"     horizon {horizon_pct_avg*100:+.3f}% vs sleeve {sleeve_avg*100:+.3f}%"
              f"  -> {'PASS' if ok3 else 'FAIL'}")
        if not ok3:
            failures.append("below the buy-and-hold sleeve")
    else:
        failures.append("sleeve not computable")

    print()
    if failures:
        print("  RESULT: HYPOTHESIS FAILS")
        for f in failures:
            print(f"    - {f}")
        print("\n  Per the registration this is a completed piece of work.")
        print("  No variant is proposed and the horizon is not adjusted.")
    else:
        print("  RESULT: hypothesis survives every kill criterion.")
        print("  Per the registration this means 'worth a second pre-registered")
        print("  test out of sample', NOT 'deploy'.")

    return 0


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

class _Bar:
    def __init__(self, stamp, close):
        self.timestamp = stamp
        self.close = close


def _self_test() -> int:
    failures = []

    def check(label, condition):
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")

    print("R definition matches the book")
    check("a long reaching +2R scores +2",
          abs(r_at_exit(entry_price=100, stop_price=98, exit_price=104,
                        side="LONG") - 2.0) < 1e-9)
    check("a long at its stop scores -1",
          abs(r_at_exit(entry_price=100, stop_price=98, exit_price=98,
                        side="LONG") + 1.0) < 1e-9)
    check("a short is signed the other way",
          abs(r_at_exit(entry_price=100, stop_price=102, exit_price=96,
                        side="SHORT") - 2.0) < 1e-9)

    print("\nAbsent, never zero")
    check("no exit price yields None",
          r_at_exit(entry_price=100, stop_price=98, exit_price=None,
                    side="LONG") is None)
    check("zero risk yields None",
          r_at_exit(entry_price=100, stop_price=100, exit_price=104,
                    side="LONG") is None)

    print("\nBar selection")
    t0 = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)
    bars = [_Bar(t0 + timedelta(hours=i), 100 + i) for i in range(5)]
    check("picks the last bar at or before the moment",
          close_of(bar_at_or_before(bars, t0 + timedelta(hours=2, minutes=30))) == 102)
    check("returns None when every bar is later",
          bar_at_or_before(bars, t0 - timedelta(hours=1)) is None)
    check("unsorted input still yields the latest",
          close_of(bar_at_or_before(list(reversed(bars)),
                                    t0 + timedelta(hours=9))) == 104)
    check("same-day filter finds the day's bars",
          len(bars_on_day(bars, t0.date())) == 5)
    check("and none on a different day",
          bars_on_day(bars, (t0 + timedelta(days=3)).date()) == [])

    print("\nHorizon is the pre-registered one")
    check("horizon equals SHADOW_MAX_DAYS",
          HORIZON_DAYS == shadow_trades.SHADOW_MAX_DAYS)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("All horizon-exit checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    return run()


if __name__ == "__main__":
    sys.exit(main())
