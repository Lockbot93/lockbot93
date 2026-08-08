"""Does the DATE predict anything? One attempt, pre-registered 2026-08-07.

WHY THIS EXISTS, AND WHY IT GETS ONE SHOT

Owner request: trades driven by "the calendars". Of the four things that
could mean, timing patterns were chosen -- day of week, turn of month,
options-expiry week.

It is worth testing for exactly one reason. THE BAR in CLAUDE.md says a
new idea must use an input not derived from these OHLCV bars, and all
eighteen dead families broke that rule: every one was a smoothing or a
ratio of the same price series, and 864 combinations of them cleared
nothing because you cannot extract more information than the input
contains. **A date is not a price.** Tuesday is not a transformation of
anything already searched.

It is also the most over-fitted claim in retail trading -- the Santa
rally, sell-in-May, the Monday effect -- and nearly all of it is
published, which by McLean & Pontiff means decayed. Slice four years
finely enough and a beautiful Tuesday is guaranteed. So LOCKBOT
pre-registered the pass mark before a single bar was loaded, and this
module implements that registration rather than deciding anything itself.

THE DESIGN DECISION THAT IS LOCKBOT'S, NOT MINE

The obvious test filters the live entry rule by calendar and compares it
against the unfiltered rule. LOCKBOT rejected that: the claim is about
the DATE, so rule-versus-rule confounds the calendar with the entry
logic. If the filtered arm wins you cannot tell which half did it.

So the PRIMARY arms are seeded RANDOM entries in-cell against seeded
RANDOM entries out-of-cell. Nothing but the date differs. The entry rule
returns only as a secondary, and only on a cell that has already passed.

THE CONFOUND THAT MATTERS MOST, ALSO LOCKBOT'S

Calendar cells are not independent draws. Every symbol shares the same
Tuesday. A thousand trades across forty symbols on twenty-five Tuesdays
is closer to twenty-five observations than to a thousand, and treating
them as a thousand inflates significance by roughly the square root of
the cluster size.

So every statistic here is computed DAY-FIRST: average the R within a
calendar day, then treat each day as one observation. The >= 100
distinct-day floor exists for the same reason.
"""

from __future__ import annotations

import math
import os
import random
import statistics as stats
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

PROJECT_FOLDER = os.path.dirname(os.path.abspath(__file__))

# ---- the registration, as constants. Changing these is re-cutting.
SEED = 20260808
MIN_DRAWS = 500
MIN_TRADES = 300
MIN_DISTINCT_DAYS = 100
EDGE_BAR = 0.10
PERCENTILE_BAR = 95
MAX_YEAR_SHARE = 0.40
VOL_TOLERANCE = 0.20
REGISTERED_TESTS = 18          # multiplicity budget, fixed in advance
BOOTSTRAP_DRAWS = 2000

HORIZONS = {
    "overnight": {"max_hold": 3, "stop": 0.02},
    "swing": {"max_hold": 5, "stop": 0.05},
}

REWARD_RATIO = 2.0


# ---------------------------------------------------------------------------
# Calendar features. One dimension each, no conjunctions, ever.
# ---------------------------------------------------------------------------

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")


def is_turn_of_month(day: date, sessions: list[date]) -> bool:
    """Last 3 sessions of a month, or first 3 of the next.

    Defined on TRADING sessions rather than calendar days, because "the
    31st" is not a session in half the months and a calendar definition
    would silently sample different things in different months.
    """

    index = _session_index(sessions, day)

    if index is None:
        return False

    after = sessions[index + 1: index + 4]
    before = sessions[max(0, index - 3): index]

    # First three of a month: fewer than 3 sessions before it share its month.
    first_three = sum(1 for d in before if d.month == day.month) < 3
    # Last three: fewer than 3 sessions after it share its month.
    last_three = sum(1 for d in after if d.month == day.month) < 3

    return bool(first_three or last_three)


def is_expiry_week(day: date) -> bool:
    """The week containing the third Friday -- monthly options expiry."""

    first = day.replace(day=1)
    # Weekday of the 1st; Friday is 4.
    offset = (4 - first.weekday()) % 7
    third_friday = first + timedelta(days=offset + 14)

    monday = day - timedelta(days=day.weekday())
    expiry_monday = third_friday - timedelta(days=third_friday.weekday())

    return monday == expiry_monday


def _session_index(sessions: list[date], day: date) -> int | None:
    lo, hi = 0, len(sessions) - 1

    while lo <= hi:
        mid = (lo + hi) // 2
        if sessions[mid] == day:
            return mid
        if sessions[mid] < day:
            lo = mid + 1
        else:
            hi = mid - 1

    return None


def cells_for(day: date, sessions: list[date]) -> dict[str, bool]:
    """Every registered cell this date belongs to."""

    out = {f"weekday={WEEKDAYS[day.weekday()]}": True} \
        if day.weekday() < 5 else {}

    for name in WEEKDAYS:
        key = f"weekday={name}"
        out.setdefault(key, False)

    out["turn_of_month"] = is_turn_of_month(day, sessions)
    out["expiry_week"] = is_expiry_week(day)

    return out


CELLS = [f"weekday={d}" for d in WEEKDAYS] + ["turn_of_month", "expiry_week"]


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def load_daily(symbols: list[str], days: int) -> dict[str, dict[date, dict]]:
    from alpaca.data.enums import Adjustment
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from dotenv import load_dotenv

    import lockbot_config as config

    load_dotenv(os.path.join(PROJECT_FOLDER, ".env"))

    client = StockHistoricalDataClient(
        os.getenv(config.ALPACA_API_KEY_ENV),
        os.getenv(config.ALPACA_SECRET_KEY_ENV),
    )

    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=days)
    out: dict[str, dict[date, dict]] = {}

    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]

        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed="sip",
            adjustment=Adjustment.ALL,   # never RAW; splits invent crashes
        ))

        for symbol in chunk:
            try:
                rows = bars.data[symbol]
            except (KeyError, AttributeError):
                continue

            series = {
                bar.timestamp.date(): {
                    "open": float(bar.open), "high": float(bar.high),
                    "low": float(bar.low), "close": float(bar.close),
                }
                for bar in rows
            }

            if len(series) > 200:
                out[symbol] = series

    return out


def simulate(series: dict[date, dict], days: list[date], index: int,
             *, stop_percent: float, max_hold: int,
             cost_r: float) -> float | None:
    """One long trade entered at the next open. Returns net R.

    Timeouts are marked to market rather than booked flat -- the same
    correction LOCKBOT forced on the lab, for the same reason: a trade
    that timed out closed at a price, not at zero.
    """

    if index + 1 >= len(days):
        return None

    entry = series[days[index + 1]]["open"]

    if entry <= 0:
        return None

    stop = entry * (1 - stop_percent)
    target = entry * (1 + stop_percent * REWARD_RATIO)
    risk = entry - stop

    if risk <= 0:
        return None

    for offset in range(1, max_hold + 1):
        position = index + 1 + offset

        if position >= len(days):
            break

        bar = series[days[position]]

        hit_stop = bar["low"] <= stop
        hit_target = bar["high"] >= target

        # An intrabar low is NOT rescued by the same bar's high. This is
        # the classic way a backtest flatters itself, and exit_strategies
        # already carries a self-test for it.
        if hit_stop:
            return -1.0 - cost_r
        if hit_target:
            return REWARD_RATIO - cost_r

    last = series[days[min(index + 1 + max_hold, len(days) - 1)]]["close"]

    return ((last - entry) / risk) - cost_r


# ---------------------------------------------------------------------------
# Statistics, all day-clustered
# ---------------------------------------------------------------------------

def day_means(trades: list[tuple[date, float]]) -> dict[date, float]:
    grouped: dict[date, list[float]] = defaultdict(list)

    for day, r in trades:
        grouped[day].append(r)

    return {d: sum(v) / len(v) for d, v in grouped.items()}


def cluster_test(in_days: dict[date, float],
                 out_days: dict[date, float]) -> tuple[float, float]:
    """Welch t on DAY means. Returns (difference, t)."""

    a, b = list(in_days.values()), list(out_days.values())

    if len(a) < 2 or len(b) < 2:
        return 0.0, 0.0

    ma, mb = stats.mean(a), stats.mean(b)
    va, vb = stats.variance(a), stats.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))

    return ma - mb, ((ma - mb) / se if se else 0.0)


def bootstrap_percentile(in_days: dict[date, float],
                         out_days: dict[date, float],
                         seed: int) -> float:
    """Where the observed edge sits in the control's own distribution.

    Resamples DAYS from the out-of-cell pool, in blocks the size of the
    in-cell day count, and asks how often a random slice of ordinary days
    beats the real cell. This is the "95th percentile" clause: it asks
    whether this Tuesday is unusual among arbitrary collections of days,
    not merely different from their average.
    """

    pool = list(out_days.values())
    n = len(in_days)

    if n < 2 or len(pool) < n:
        return 0.0

    rng = random.Random(seed)
    base = stats.mean(pool)
    observed = stats.mean(list(in_days.values())) - base
    beaten = 0

    for _ in range(BOOTSTRAP_DRAWS):
        draw = [pool[rng.randrange(len(pool))] for _ in range(n)]
        if (stats.mean(draw) - base) >= observed:
            beaten += 1

    return 100.0 * (1.0 - beaten / BOOTSTRAP_DRAWS)


def year_split(trades: list[tuple[date, float]]) -> dict[int, tuple[int, float]]:
    grouped: dict[int, list[float]] = defaultdict(list)

    for day, r in trades:
        grouped[day.year].append(r)

    return {y: (len(v), sum(v) / len(v)) for y, v in sorted(grouped.items())}


def evaluate_cell(name: str,
                  inside: list[tuple[date, float]],
                  outside: list[tuple[date, float]],
                  in_atr: float, out_atr: float,
                  seed: int) -> dict[str, Any]:
    """Every clause of the registration, applied in order."""

    verdict: list[str] = []

    in_days, out_days = day_means(inside), day_means(outside)
    edge, t = cluster_test(in_days, out_days)
    pct = bootstrap_percentile(in_days, out_days, seed)
    years = year_split(inside)

    total = len(inside)
    biggest_year = (max((n for n, _ in years.values()), default=0) / total
                    if total else 1.0)
    same_sign = (all(r > 0 for _, r in years.values())
                 or all(r < 0 for _, r in years.values())) if years else False

    if total < MIN_TRADES:
        verdict.append(f"under {MIN_TRADES} trades")
    if len(in_days) < MIN_DISTINCT_DAYS:
        verdict.append(f"under {MIN_DISTINCT_DAYS} distinct days")
    if edge < EDGE_BAR:
        verdict.append(f"edge {edge:+.3f} below +{EDGE_BAR:.2f}")
    if pct < PERCENTILE_BAR:
        verdict.append(f"{pct:.0f}th pct below {PERCENTILE_BAR}th")
    if not same_sign:
        verdict.append("sign differs across years")
    if biggest_year > MAX_YEAR_SHARE:
        verdict.append(f"one year is {biggest_year:.0%} of trades")

    vol_gap = (abs(in_atr - out_atr) / out_atr) if out_atr else 0.0
    if vol_gap > VOL_TOLERANCE:
        verdict.append(f"volatility differs {vol_gap:.0%}")

    return {
        "cell": name, "trades": total, "days": len(in_days),
        "edge": edge, "t": t, "percentile": pct,
        "in_r": stats.mean(list(in_days.values())) if in_days else 0.0,
        "out_r": stats.mean(list(out_days.values())) if out_days else 0.0,
        "years": years, "vol_gap": vol_gap,
        "passed": not verdict, "why": verdict,
    }


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def run(*, years: int = 4, symbol_cap: int = 78) -> int:
    import lab_universe
    import trading_costs

    symbols = lab_universe.load()[:symbol_cap]

    print("=" * 78)
    print("CALENDAR TIMING — pre-registered 2026-08-07, ONE attempt")
    print("=" * 78)
    print(f"  seed {SEED}, {len(symbols)} symbols, {years} years")
    print(f"  primary arms: seeded RANDOM in-cell vs seeded RANDOM out-of-cell")
    print(f"  costs charged, timeouts marked to market, day-clustered stats")
    print(f"  multiplicity budget: {REGISTERED_TESTS} tests, "
          f"Bonferroni t threshold ~{_bonferroni_t():.2f}\n")

    bars = load_daily(symbols, years * 365)
    print(f"  loaded {len(bars)} symbols, "
          f"{sum(len(s) for s in bars.values()):,} symbol-days")

    sessions = sorted({d for s in bars.values() for d in s})
    print(f"  {len(sessions)} distinct sessions "
          f"{sessions[0]} to {sessions[-1]}\n")

    # ATR% per symbol-day, for the volatility clause.
    atr: dict[tuple[str, date], float] = {}
    for symbol, series in bars.items():
        for day, bar in series.items():
            if bar["close"] > 0:
                atr[(symbol, day)] = (bar["high"] - bar["low"]) / bar["close"]

    rng = random.Random(SEED)

    # The entry pool: every (symbol, session) that can support a trade.
    pool: list[tuple[str, int, date]] = []
    for symbol, series in bars.items():
        days = sorted(series)
        for i in range(20, len(days) - 8):
            pool.append((symbol, i, days[i]))

    rng.shuffle(pool)
    print(f"  entry pool {len(pool):,} symbol-sessions\n")

    results: list[dict] = []

    for horizon, settings in HORIZONS.items():
        cost_r = trading_costs.round_trip_r(settings["stop"])

        trades: list[tuple[str, date, float]] = []

        for symbol, index, day in pool:
            series = bars[symbol]
            days = sorted(series)

            r = simulate(series, days, index,
                         stop_percent=settings["stop"],
                         max_hold=settings["max_hold"],
                         cost_r=cost_r)

            if r is not None:
                trades.append((symbol, day, r))

        print("=" * 78)
        print(f"{horizon.upper()}  —  {settings['max_hold']}-day hold, "
              f"{settings['stop']:.0%} stop, cost {cost_r:.3f}R")
        print("=" * 78)
        print(f"  {len(trades):,} simulated entries\n")

        print(f"  {'cell':<22} {'trades':>7} {'days':>6} {'in R':>7} "
              f"{'out R':>7} {'edge':>7} {'pct':>5} {'t':>6}  verdict")
        print("  " + "-" * 90)

        for cell in CELLS:
            inside, outside = [], []
            in_vol, out_vol = [], []

            for symbol, day, r in trades:
                member = cells_for(day, sessions).get(cell, False)
                (inside if member else outside).append((day, r))
                v = atr.get((symbol, day))
                if v is not None:
                    (in_vol if member else out_vol).append(v)

            if not inside or not outside:
                continue

            outcome = evaluate_cell(
                cell, inside, outside,
                stats.mean(in_vol) if in_vol else 0.0,
                stats.mean(out_vol) if out_vol else 0.0,
                SEED,
            )
            outcome["horizon"] = horizon
            results.append(outcome)

            why = "PASS" if outcome["passed"] else outcome["why"][0]

            print(f"  {cell:<22} {outcome['trades']:>7,} "
                  f"{outcome['days']:>6} {outcome['in_r']:>+7.3f} "
                  f"{outcome['out_r']:>+7.3f} {outcome['edge']:>+7.3f} "
                  f"{outcome['percentile']:>4.0f} {outcome['t']:>+6.2f}  {why}")

        print()

    return _verdict(results)


def _bonferroni_t() -> float:
    """Two-tailed t threshold at 0.05 corrected for the registered tests."""

    # Normal approximation is fine at these day counts.
    target = 0.05 / REGISTERED_TESTS
    lo, hi = 0.0, 8.0

    for _ in range(80):
        mid = (lo + hi) / 2
        p = 2 * (1 - 0.5 * (1 + math.erf(mid / math.sqrt(2))))
        if p > target:
            lo = mid
        else:
            hi = mid

    return (lo + hi) / 2


def _verdict(results: list[dict]) -> int:
    print("=" * 78)
    print("VERDICT — against the registration, not against hindsight")
    print("=" * 78)

    passed = [r for r in results if r["passed"]]
    threshold = _bonferroni_t()

    # The registration requires day-clustered significance, which after
    # multiplicity means clearing the corrected threshold.
    survivors = [r for r in passed if abs(r["t"]) >= threshold]

    print(f"  cells tested          {len(results)}")
    print(f"  cleared every clause  {len(passed)}")
    print(f"  and Bonferroni t>={threshold:.2f}  {len(survivors)}")

    if not results:
        print("\n  Nothing ran. Not a result.")
        return 1

    best = max(results, key=lambda r: r["edge"])
    print(f"\n  best cell by edge: {best['cell']} ({best['horizon']}) "
          f"{best['edge']:+.3f}R, {best['percentile']:.0f}th pct")
    print(f"    blocked by: {', '.join(best['why']) if best['why'] else 'nothing'}")

    if survivors:
        print("\n  SURVIVORS:")
        for r in survivors:
            print(f"    {r['cell']} ({r['horizon']}) {r['edge']:+.3f}R")
        print("\n  Registration: any pass is SHADOW ONLY, pending forward")
        print("  confirmation and an explicit owner decision. It is NOT a")
        print("  licence to trade it.")
        return 0

    print("\n  NOTHING PASSED.")
    print("  Per the registration, this KILLS the calendar family on")
    print("  equities at both horizons, permanently, with no re-cut path.")
    print("  A single good-looking cell that missed a clause counts for")
    print("  nothing, and re-slicing after seeing this is the rescue")
    print("  behaviour the registration exists to forbid.")

    return 0


def _self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))
            failures.append(name)

    print("\nEXPIRY WEEK")
    # August 2026: the 1st is a Saturday, so the third Friday is the 21st.
    check("the third Friday is expiry", is_expiry_week(date(2026, 8, 21)))
    check("so is its Monday", is_expiry_week(date(2026, 8, 17)))
    check("the week before is not", not is_expiry_week(date(2026, 8, 14)))
    check("the week after is not", not is_expiry_week(date(2026, 8, 28)))

    print("\nTURN OF MONTH, ON SESSIONS NOT CALENDAR DAYS")
    sessions = []
    d = date(2026, 6, 1)
    while d < date(2026, 9, 1):
        if d.weekday() < 5:
            sessions.append(d)
        d += timedelta(days=1)

    check("the first session of a month is turn",
          is_turn_of_month(sessions[0], sessions))
    check("the last session of a month is turn",
          is_turn_of_month([s for s in sessions if s.month == 6][-1], sessions))
    mid = [s for s in sessions if s.month == 7][10]
    check("mid-month is not", not is_turn_of_month(mid, sessions))

    print("\nDAY CLUSTERING IS THE POINT")
    same_day = [(date(2026, 8, 3), 1.0)] * 500
    check("500 trades on one day collapse to one observation",
          len(day_means(same_day)) == 1)

    spread = [(date(2026, 8, 1) + timedelta(days=i), 1.0) for i in range(50)]
    check("50 trades on 50 days stay 50", len(day_means(spread)) == 50)

    a = {date(2026, 1, 1) + timedelta(days=i): 1.0 for i in range(60)}
    b = {date(2026, 1, 1) + timedelta(days=i): 0.0 for i in range(60)}
    diff, t = cluster_test(a, b)
    check("a real difference is detected", abs(diff - 1.0) < 1e-9)

    print("\nTHE BAR REJECTS A GOOD-LOOKING CELL THAT MISSES A CLAUSE")
    thin = [(date(2026, 1, 1) + timedelta(days=i), 5.0) for i in range(20)]
    fat = [(date(2026, 1, 1) + timedelta(days=i), 0.0) for i in range(400)]
    out = evaluate_cell("thin", thin, fat, 0.02, 0.02, SEED)
    check("a huge edge on 20 trades still fails", not out["passed"])
    check("and says why", any("trades" in w for w in out["why"]),
          str(out["why"]))

    print("\nMULTIPLICITY IS APPLIED")
    check("the Bonferroni threshold is stricter than 1.96",
          _bonferroni_t() > 1.96, str(_bonferroni_t()))

    print()

    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All calendar-timing checks passed.")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--years", type=int, default=4)
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.run:
        return run(years=args.years)

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
