"""
lab_universe.py — a symbol pool for the research lab, not for trading.

WHY THIS EXISTS

Filed by LOCKBOT as agent_channel item 7425d2f7, and it was right.

strategy_lab.py backtests whatever price frames it is handed, and
propose_strategy handed it universe.csv: names deliberately filtered to
1.25-3.00% daily movement because that suits the live scanner. The swing
horizon scores a 2:1 reward on a 5% stop, which needs roughly a 10%
favourable move inside a trading week. Names SELECTED for not moving
cannot produce that.

So every swing backtest was capped by the universe rather than by the
rule being tested. Measured at the swing configuration, 83% of entries
never touched either band -- the test was not measuring entry logic at
all, it was measuring how long a stock takes to travel 10%.

WHAT THIS DELIBERATELY DOES NOT DO

Touch universe.csv, or the live scanner, or the 1.25-3.00% band. That
band is correct for what it does: feeding the shadow log, whose
population must stay stable while the regime split accumulates. Widening
it to fix a research problem would invalidate 269 logged setups.

The two pools answer different questions. The live one asks "what can
LOCKBOT trade today". This one asks "where can a rule be measured at
all".

WHERE THE NAMES COME FROM

universe.py builds a raw pool and writes universe_prefilter.csv, then
universe_volatility.py filters it into universe.csv and records every
name's ATR in universe_volatility_report.csv -- including the 95 it
rejected as TOO_WILD. Those rejects are exactly this pool. Nothing needs
fetching; the data is already on disk, discarded by a filter that was
right to discard it for its own purpose.

A HEALTH WARNING ON WHAT THIS FIXES

It makes swing backtests MEANINGFUL. It does not make them WIN. A more
volatile universe raises the ceiling for every rule and for the
random-entry controls equally, so any result here still has to beat its
control. See CLAUDE.md: nine strategy families have now lost to blind
entry, and a different pond does not by itself put fish in it.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import lockbot_config as config

PROJECT_FOLDER = Path(__file__).resolve().parent

PREFILTER_FILE = PROJECT_FOLDER / "universe_prefilter.csv"
REPORT_FILE = PROJECT_FOLDER / "universe_volatility_report.csv"

# The report stores atr_percent as a PERCENT NUMBER (2.49 means 2.49%),
# while config carries fractions. Converting in one place stops the two
# conventions meeting anywhere else.
PERCENT_SCALE = 100.0


def _band() -> tuple[float, float]:
    """The lab's ATR band, as percent numbers to match the report."""

    low = float(getattr(config, "LAB_MIN_ATR_PERCENT", 0.030)) * PERCENT_SCALE
    high = float(getattr(config, "LAB_MAX_ATR_PERCENT", 0.080)) * PERCENT_SCALE

    return low, high


def read_movement(path: Path | None = None) -> dict[str, float]:
    """Every symbol's daily ATR percent, from the volatility report."""

    source = Path(path or REPORT_FILE)

    if not source.exists():
        return {}

    movement: dict[str, float] = {}

    try:
        with source.open(newline="", encoding="utf-8", errors="ignore") as fh:
            for row in csv.DictReader(fh):
                symbol = str(row.get("symbol", "")).strip().upper()
                raw = row.get("atr_percent")

                if not symbol or raw in (None, ""):
                    continue

                try:
                    movement[symbol] = float(raw)
                except ValueError:
                    continue
    except OSError:
        return {}

    return movement


def read_pool(path: Path | None = None) -> list[dict]:
    """The pre-volatility pool, which still holds the rejected names."""

    source = Path(path or PREFILTER_FILE)

    if not source.exists():
        return []

    try:
        with source.open(newline="", encoding="utf-8", errors="ignore") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def select(
    pool: list[dict],
    movement: dict[str, float],
    *,
    low: float | None = None,
    high: float | None = None,
    top_n: int | None = None,
) -> list[dict]:
    """Names inside the lab band, most liquid first.

    Ranked by dollar volume rather than by volatility. Picking the
    WILDEST names would select for the least tradable end of the band,
    and a backtest on names nobody can fill is a different kind of
    fiction from the one being fixed.
    """

    band_low, band_high = _band()
    band_low = band_low if low is None else low
    band_high = band_high if high is None else high
    limit = top_n if top_n is not None else int(
        getattr(config, "LAB_TOP_N", 80))

    kept = []

    for row in pool:
        symbol = str(row.get("symbol", "")).strip().upper()
        atr = movement.get(symbol)

        if atr is None or not (band_low <= atr <= band_high):
            continue

        enriched = dict(row)
        enriched["atr_percent"] = f"{atr:.2f}"
        kept.append(enriched)

    def liquidity(row: dict) -> float:
        try:
            return float(row.get("avg_dollar_volume") or 0.0)
        except ValueError:
            return 0.0

    kept.sort(key=liquidity, reverse=True)

    return kept[:limit] if limit > 0 else kept


def write(rows: list[dict], path: Path | None = None) -> Path:
    """Write the lab pool in universe.csv's format, plus atr_percent.

    Same shape so universe.load_universe reads it without special
    handling; the extra column is there so a person can see at a glance
    that this pool moves.
    """

    target = Path(path or config.LAB_UNIVERSE_FILE)

    fields = ["symbol", "exchange", "last_close", "avg_dollar_volume",
              "avg_share_volume", "bars_used", "shortable", "easy_to_borrow",
              "atr_percent"]

    with target.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()

        for row in rows:
            writer.writerow(row)

    return target


def load(path: Path | None = None) -> list[str]:
    """Symbols in the lab pool, or an empty list if it is not built."""

    source = Path(path or config.LAB_UNIVERSE_FILE)

    if not source.exists():
        return []

    try:
        with source.open(newline="", encoding="utf-8", errors="ignore") as fh:
            return [
                str(row["symbol"]).strip().upper()
                for row in csv.DictReader(fh)
                if row.get("symbol")
            ]
    except OSError:
        return []


def build() -> list[dict]:
    """Build the lab pool from data already on disk."""

    pool = read_pool()
    movement = read_movement()

    if not pool or not movement:
        return []

    return select(pool, movement)


def describe(rows: list[dict]) -> str:
    """A readable summary of what was selected."""

    if not rows:
        return ("No lab pool. Run universe.py then universe_volatility.py "
                "first — this reads what they leave behind.")

    moves = sorted(float(r["atr_percent"]) for r in rows)
    low, high = _band()

    lines = [
        f"{len(rows)} symbols, {low:.2f}%-{high:.2f}%/day",
        f"  movement: {moves[0]:.2f}% to {moves[-1]:.2f}%, "
        f"median {moves[len(moves) // 2]:.2f}%",
        "",
        "  most liquid:",
    ]

    for row in rows[:10]:
        lines.append(f"    {row['symbol']:<7} {float(row['atr_percent']):>5.2f}%/day  "
                     f"${float(row.get('avg_dollar_volume') or 0):>15,.0f}")

    return "\n".join(lines)


def _self_test() -> int:
    """Offline checks. No network, no credentials."""

    import tempfile

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    pool = [
        {"symbol": "QUIET", "avg_dollar_volume": "9000000"},
        {"symbol": "MID", "avg_dollar_volume": "5000000"},
        {"symbol": "FAST", "avg_dollar_volume": "8000000"},
        {"symbol": "WILD", "avg_dollar_volume": "7000000"},
        {"symbol": "NODATA", "avg_dollar_volume": "6000000"},
    ]

    movement = {"QUIET": 1.5, "MID": 4.0, "FAST": 5.0, "WILD": 25.0}

    print("Selection")

    picked = select(pool, movement, low=3.0, high=8.0)
    symbols = [r["symbol"] for r in picked]

    check("names inside the band are kept",
          set(symbols) == {"MID", "FAST"}, str(symbols))
    check("a name below the band is excluded", "QUIET" not in symbols)
    check("a name above the band is excluded", "WILD" not in symbols)
    check("a name with no movement data is excluded",
          "NODATA" not in symbols)

    # Ranked by liquidity, not by volatility: picking the wildest would
    # select the least tradable end of the band.
    check("the most liquid comes first", symbols[0] == "FAST", str(symbols))

    check("top_n caps the pool",
          len(select(pool, movement, low=3.0, high=8.0, top_n=1)) == 1)
    check("the movement figure is carried through",
          picked[0]["atr_percent"] == "5.00", picked[0]["atr_percent"])

    print()
    print("The band comes from config, in the report's units")

    low, high = _band()
    check("config fractions become percent numbers",
          abs(low - config.LAB_MIN_ATR_PERCENT * 100) < 1e-9
          and abs(high - config.LAB_MAX_ATR_PERCENT * 100) < 1e-9,
          f"{low} {high}")
    check("the lab band sits ABOVE the live band",
          config.LAB_MIN_ATR_PERCENT >= config.UNIVERSE_MAX_ATR_PERCENT,
          f"lab min {config.LAB_MIN_ATR_PERCENT} vs "
          f"live max {config.UNIVERSE_MAX_ATR_PERCENT}")

    # The whole point: a rule needing a 10% move in a week is plausible
    # at 3%/day and impossible at 1.5%.
    weekly = config.LAB_MIN_ATR_PERCENT * 5
    check("the band can reach a swing target in a week",
          weekly >= 0.10, f"{weekly:.1%} over 5 days")

    print()
    print("Round trip")

    folder = Path(tempfile.mkdtemp())
    target = folder / "lab_universe.csv"

    write(picked, target)
    check("the file is written", target.exists())
    check("and reads back as symbols",
          set(load(target)) == {"MID", "FAST"}, str(load(target)))

    check("a missing file loads as empty",
          load(folder / "absent.csv") == [])

    # universe.load_universe must read it without special handling.
    try:
        from universe import load_universe

        check("universe.load_universe can read it",
              set(load_universe(str(target))) == {"MID", "FAST"},
              str(load_universe(str(target))))
    except Exception as error:
        check("universe.load_universe can read it", False, str(error))

    print()
    print("Missing inputs are handled, not guessed at")

    check("no report means no movement data",
          read_movement(folder / "absent.csv") == {})
    check("no pool means no rows", read_pool(folder / "absent.csv") == [])
    check("and build returns nothing rather than raising",
          isinstance(build(), list))
    check("describe says what to run",
          "universe.py" in describe([]))

    print()
    print("The live universe is a different file")

    check("the lab writes somewhere else entirely",
          Path(config.LAB_UNIVERSE_FILE) != Path(config.UNIVERSE_FILE),
          f"{config.LAB_UNIVERSE_FILE} vs {config.UNIVERSE_FILE}")

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All lab-universe checks passed.")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true",
                        help="write lab_universe.csv")
    parser.add_argument("--show", action="store_true",
                        help="describe the current pool")
    parser.add_argument("--self-test", action="store_true")

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.build:
        rows = build()

        if not rows:
            print(describe(rows))
            return 1

        path = write(rows)
        print(describe(rows))
        print(f"\nWrote {path}")
        return 0

    if args.show:
        symbols = load()

        if not symbols:
            print("Not built. Run with --build.")
            return 1

        print(f"{len(symbols)} symbols: {', '.join(symbols[:20])}"
              + (" ..." if len(symbols) > 20 else ""))
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    sys.exit(main())
