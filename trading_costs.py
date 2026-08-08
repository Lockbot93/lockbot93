"""What a round trip actually costs, and why every lab number was too good.

WHY THIS EXISTS

First item on LOCKBOT's agenda, 2026-08-07, in its words:

    "lab-proper fills at exact stop/target with zero cost, so every lab
     number including upcoming ratio sweeps is optimistic by an unmeasured
     amount"

`simulate_symbol` fills at exactly the stop or exactly the target and
charges nothing. Nobody pays those prices. A real entry crosses the
spread, a real exit crosses it again, and a stop order fills at the next
available price rather than at the stop. So every result this project has
produced -- eighteen failed families, the reward sweeps, the exit study --
sits above what the same rule would have returned.

That does not overturn the negative results; it makes them worse. It
matters for the positive ones. The trailing-stop lift cleared its bar at
+0.102R against a +0.10R requirement, which is a margin thinner than the
cost assumption underneath it.

THE UNIT IS R, NOT PERCENT, and this is the part that surprises people.

A cost is a fraction of PRICE. An R is a fraction of the STOP DISTANCE. So
the same commission hurts a tight stop far more than a wide one:

    cost in R = round-trip cost percent / stop percent

At 10bp per side and a 2% stop that is 0.020R -- annoying. At the same
10bp against a 0.5% stop it is 0.080R, four times the damage, for an
identical trade. Any sweep that varies the stop while holding the cost
fixed in percent is therefore varying the cost in R without saying so.

WHY THE SPREAD IS MEASURED RATHER THAN ASSUMED

The obvious approach is to read the bid-ask spread. It is not available.
The live feed is `iex`, which carries about 4% of traded volume, and a
resting iex quote on AAPL has been observed at 293.64 / 324.64 -- a $31
spread on the most liquid stock in the world, because so little rests on
that one venue. Using those quotes would produce a cost model wilder than
the thing it is modelling.

So `corwin_schultz` estimates the spread from high-low ranges instead.
The insight of that estimator is that a bar's high-low range contains both
real volatility and the spread, but volatility scales with time while the
spread does not -- so comparing one-period ranges against the two-period
range separates them. It needs only OHLC, which is exactly what this
project has.

It is an estimator, not a measurement, and it is noisy per observation.
Averaged over many bars and symbols it is good enough to answer the
question that matters here: are we out by a basis point, or by half an R?
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

try:
    import lockbot_config as config

    DEFAULT_COST_PER_SIDE = getattr(
        config, "BACKTEST_COST_PER_SIDE_PERCENT", 0.0010
    )
except Exception:  # pragma: no cover
    DEFAULT_COST_PER_SIDE = 0.0010

# Corwin-Schultz constant: 3 - 2*sqrt(2).
_K = 3.0 - 2.0 * math.sqrt(2.0)


def round_trip_r(stop_percent: float,
                 cost_per_side: float | None = None) -> float:
    """What a round trip costs, expressed in R.

    Two sides, so twice the per-side cost, divided by the stop distance
    because that is what one R is worth.

    Returns 0.0 for a non-positive stop rather than raising: this is
    called from the simulation loop, and a bad stop should produce a
    harmless cost rather than kill a backtest halfway through.
    """

    if cost_per_side is None:
        cost_per_side = DEFAULT_COST_PER_SIDE

    try:
        stop_percent = float(stop_percent)
        cost_per_side = float(cost_per_side)
    except (TypeError, ValueError):
        return 0.0

    if stop_percent <= 0 or cost_per_side <= 0:
        return 0.0

    return (2.0 * cost_per_side) / stop_percent


def corwin_schultz(highs: Sequence[float], lows: Sequence[float]) -> float | None:
    """Estimate the proportional round-trip spread from high-low ranges.

    Returns the spread as a fraction of price, or None when there is not
    enough usable data.

    Negative single-pair estimates are clamped to zero, which is the
    standard treatment: the estimator is unbiased in expectation but noisy
    per pair, and a negative spread is not a spread. Clamping first and
    averaging after is what the original paper does.
    """

    if len(highs) != len(lows) or len(highs) < 2:
        return None

    estimates: list[float] = []

    for i in range(len(highs) - 1):
        h1, l1 = highs[i], lows[i]
        h2, l2 = highs[i + 1], lows[i + 1]

        if min(h1, l1, h2, l2) <= 0:
            continue

        if l1 <= 0 or l2 <= 0:
            continue

        # Two single-period ranges, and the range across both.
        beta = math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2
        high2, low2 = max(h1, h2), min(l1, l2)

        if low2 <= 0:
            continue

        gamma = math.log(high2 / low2) ** 2

        alpha = (math.sqrt(2.0 * beta) - math.sqrt(beta)) / _K \
            - math.sqrt(gamma / _K)

        spread = 2.0 * (math.exp(alpha) - 1.0) / (1.0 + math.exp(alpha))

        estimates.append(max(spread, 0.0))

    if not estimates:
        return None

    return sum(estimates) / len(estimates)


def estimate_from_frames(frames: dict[str, Any]) -> dict[str, float]:
    """Per-symbol round-trip spread estimates from a frames dict.

    Accepts anything with `high` and `low` columns -- a DataFrame or a
    list of row dicts -- because the lab hands both around.
    """

    out: dict[str, float] = {}

    for symbol, frame in (frames or {}).items():
        try:
            highs = list(frame["high"])
            lows = list(frame["low"])
        except (TypeError, KeyError, IndexError):
            continue

        estimate = corwin_schultz(
            [float(h) for h in highs], [float(l) for l in lows]
        )

        if estimate is not None:
            out[symbol] = estimate

    return out


def summarise(estimates: dict[str, float],
              stop_percent: float = 0.02) -> str:
    """A readable block: spread per symbol, and what it costs in R."""

    if not estimates:
        return "No spread estimates available."

    values = sorted(estimates.values())
    n = len(values)
    median = values[n // 2]
    mean = sum(values) / n

    lines = [
        f"Spread estimated on {n} symbols (Corwin-Schultz, high-low):",
        f"  median round trip   {median * 100:.3f}%",
        f"  mean round trip     {mean * 100:.3f}%",
        f"  range               {values[0] * 100:.3f}% - "
        f"{values[-1] * 100:.3f}%",
        "",
        f"At a {stop_percent:.1%} stop that median spread alone costs "
        f"{median / stop_percent:.3f}R per round trip,",
        "before any slippage on the stop fill.",
    ]

    return "\n".join(lines)


def _self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))
            failures.append(name)

    print("\nCOST IN R DEPENDS ON THE STOP, WHICH IS THE WHOLE POINT")

    check("10bp a side against a 2% stop is 0.010R",
          abs(round_trip_r(0.02, 0.0010) - 0.10) < 1e-9,
          str(round_trip_r(0.02, 0.0010)))
    check("the same cost against a 0.5% stop hurts four times as much",
          abs(round_trip_r(0.005, 0.0010) / round_trip_r(0.02, 0.0010) - 4.0)
          < 1e-9)
    check("a wider stop dilutes it",
          round_trip_r(0.05, 0.0010) < round_trip_r(0.02, 0.0010))
    check("both sides are charged",
          abs(round_trip_r(0.02, 0.0010) - 2 * (0.0010 / 0.02)) < 1e-12)

    print("\nIT CANNOT BREAK A BACKTEST")
    check("a zero stop costs nothing rather than dividing by zero",
          round_trip_r(0.0, 0.001) == 0.0)
    check("a negative stop is harmless", round_trip_r(-0.02, 0.001) == 0.0)
    check("rubbish input is harmless", round_trip_r("wide", 0.001) == 0.0)
    check("zero cost is zero", round_trip_r(0.02, 0.0) == 0.0)

    print("\nTHE SPREAD ESTIMATOR")

    # A series with NO spread: high/low driven purely by a steady trend.
    clean_h, clean_l = [], []
    price = 100.0
    for _ in range(60):
        clean_h.append(price * 1.01)
        clean_l.append(price * 0.99)
        price *= 1.001

    tight = corwin_schultz(clean_h, clean_l)
    check("a clean series estimates a small spread",
          tight is not None and tight < 0.02, str(tight))

    # The same series with every bar widened -- a bigger spread must read
    # bigger. This is the property that matters; the absolute level of a
    # high-low estimator is not something to over-trust.
    wide_h = [h * 1.02 for h in clean_h]
    wide_l = [l * 0.98 for l in clean_l]
    wide = corwin_schultz(wide_h, wide_l)

    check("widening every bar raises the estimate",
          wide is not None and tight is not None and wide > tight,
          f"tight {tight} wide {wide}")

    check("it is never negative",
          all((corwin_schultz(clean_h[i:i + 2], clean_l[i:i + 2]) or 0) >= 0
              for i in range(0, 40, 2)))

    print("\nIT REFUSES RATHER THAN GUESSES")
    check("one bar is not enough", corwin_schultz([10.0], [9.0]) is None)
    check("nothing is not enough", corwin_schultz([], []) is None)
    check("mismatched lengths refuse",
          corwin_schultz([10.0, 11.0], [9.0]) is None)
    check("zero prices are skipped, not crashed on",
          corwin_schultz([0.0, 0.0], [0.0, 0.0]) is None)
    check("negative prices are skipped",
          corwin_schultz([-1.0, -2.0], [-3.0, -4.0]) is None)

    print("\nFRAMES IN, ESTIMATES OUT")

    frames = {
        "AAA": {"high": clean_h, "low": clean_l},
        "BBB": {"high": wide_h, "low": wide_l},
        "BAD": {"high": [1.0], "low": [0.5]},
        "WORSE": {"close": [1.0, 2.0]},
    }

    estimates = estimate_from_frames(frames)
    check("good symbols are estimated", {"AAA", "BBB"} <= set(estimates))
    check("a one-bar symbol is dropped", "BAD" not in estimates)
    check("a symbol with no high/low is dropped", "WORSE" not in estimates)
    check("an empty frames dict is harmless", estimate_from_frames({}) == {})
    check("None is harmless", estimate_from_frames(None) == {})

    text = summarise(estimates, stop_percent=0.02)
    check("the summary names the R cost", "R per round trip" in text)
    check("an empty summary says so", "No spread" in summarise({}))

    print()

    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All trading-cost checks passed.")

    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--measure", action="store_true",
                        help="estimate the spread on the lab universe")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.measure:
        import backtest
        import lab_universe

        symbols = lab_universe.load()[:40]
        print(f"Measuring {len(symbols)} lab symbols over {args.days} days...")

        frames = backtest.load_history(symbols, days=args.days)
        estimates = estimate_from_frames(frames)

        print()
        print(summarise(estimates))
        print()
        print("Currently charged per side: "
              f"{DEFAULT_COST_PER_SIDE * 100:.3f}%")

        return 0

    print(f"cost per side: {DEFAULT_COST_PER_SIDE * 100:.3f}%")
    for stop in (0.005, 0.01, 0.02, 0.05):
        print(f"  {stop:>6.1%} stop -> {round_trip_r(stop):.3f}R per round trip")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
