"""
rule_search.py — look for an entry rule with an edge, without fooling us.

WHY THIS EXISTS

LOCKBOT's live entry rule has a measured edge that is negative: 20.2%
against a 33.3% breakeven on shares, and worse in options where the
spread is paid twice. Everything built so far reduces what a bad trade
COSTS. Nothing addresses which trade to take. This does.

THE THING THAT MAKES THIS HARD

Searching is easy. Not being fooled by the search is hard.

Test two hundred rules against history and the best one will look
excellent whether or not any of them work, because the best of two
hundred coin-flippers looks like a genius. That is not a subtle
statistical caveat, it is the single most likely outcome of this file,
and every design decision below exists to make it detectable:

  A HOLDOUT THAT IS TOUCHED ONCE. Sessions are split by date. The search
  never sees the holdout. Only a handful of finalists are measured
  against it, once, after ranking is finished and frozen. A rule that
  wins on the search set and dies on the holdout was noise, and this is
  the only way to find that out short of trading it.

  SPLIT BY TIME, NOT AT RANDOM. Random splitting leaks: bars from the
  same session land on both sides, and adjacent 5-minute bars on one
  stock are nearly the same observation. A chronological split also
  tests the thing that actually matters, which is whether a rule
  survives into a market it was not fitted on.

  THE NULL IS STATED FIRST. Before any result is read, the report says
  how many rules this many tests would be expected to throw up by
  chance. A winner that is not clearly better than that number is not a
  finding.

  FAILURES ARE COUNTED. Every rule tried is recorded, not just the
  survivors. "One rule in two hundred cleared breakeven" and "the rule
  cleared breakeven" are the same sentence about very different things.

WHAT A NEGATIVE RESULT MEANS

If nothing survives, that is the honest and most likely answer, and it
is worth more than a fitted rule that loses money slowly. It says the
edge is not in these indicators on this universe at this holding
period, and that looking harder at the same place will not help.

This file is not permitted to change what LOCKBOT trades. It prints
findings. Adopting one is a config change a person makes.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

import backtest
from backtest import (
    MIN_SAMPLE, binomial_tail, breakeven_rate, concentration, run_rules,
)

# How many finalists are carried to the holdout.
#
# Every one spends some of the holdout's credibility -- five tests at
# the 5% level expect one false pass, which is why the report applies a
# Bonferroni correction rather than reading each in isolation. Kept
# small on purpose: the holdout cannot be re-used once it has been read.
FINALISTS = 5

# A finding has to clear this on the holdout AFTER correction.
ALPHA = 0.05


@dataclass
class Finding:
    """One rule, measured on both halves."""

    spec: dict
    train_trades: int
    train_win_rate: float
    train_expectancy: float
    test_trades: int = 0
    test_win_rate: float = 0.0
    test_expectancy: float = 0.0
    test_p_value: float = 1.0
    test_days: int = 0
    test_concentration: float = 0.0
    verdict: str = "NOT_TESTED"


def split_by_session(
    frames: dict[str, Any], *, train_fraction: float = 0.7
) -> tuple[dict[str, Any], dict[str, Any], date | None]:
    """Split every symbol at the same calendar date.

    Splitting each symbol at its own row count would put different dates
    on either side for different symbols, so a rule could see AAPL's
    Tuesday in training and F's Tuesday in the holdout. One market-wide
    boundary keeps the holdout a genuinely later period.
    """

    sessions: set[date] = set()

    for frame in frames.values():
        sessions |= set(frame["timestamp"].dt.date)

    if len(sessions) < 4:
        return frames, {}, None

    ordered = sorted(sessions)
    cut = ordered[int(len(ordered) * train_fraction)]

    train: dict[str, Any] = {}
    test: dict[str, Any] = {}

    for symbol, frame in frames.items():
        dates = frame["timestamp"].dt.date
        before, after = frame[dates < cut], frame[dates >= cut]

        # A frame too short to hold a position teaches nothing and its
        # indicators are still warming up.
        if len(before) >= 60:
            train[symbol] = before

        if len(after) >= 60:
            test[symbol] = after

    return train, test, cut


def generate_candidates() -> list[dict]:
    """Build the search space from ideas, not from arbitrary thresholds.

    Every candidate is a combination of recognised trading concepts:
    which way the trend is, whether momentum confirms, where price sits
    against its own averages, what part of the RSI range it is in, and
    whether volume is heavy or light.

    The volume axis is here for a specific reason. LOCKBOT ranks setups
    by volume ratio, and the shadow data says that ranking is INVERTED:
    the higher-volume half won 21.4% and the lower-volume half 33.3%. If
    that is real rather than noise it should show up here as light-volume
    variants outperforming, on far more data than the 55 trades that
    produced it. If it does not show up, the tiebreaker was noise.
    """

    momentum = [
        ({"left": "macd", "op": ">", "right": "macd_signal"},
         "momentum rising"),
        ({"left": "macd", "op": "<", "right": "macd_signal"},
         "momentum falling"),
    ]

    location = [
        ([{"left": "close", "op": ">", "right": "vwap"},
          {"left": "close", "op": ">", "right": "ema_9"}],
         "price above VWAP and its fast average"),
        ([{"left": "close", "op": "<", "right": "vwap"},
          {"left": "close", "op": "<", "right": "ema_9"}],
         "price below VWAP and its fast average"),
        ([{"left": "close", "op": ">", "right": "vwap"},
          {"left": "close", "op": "<", "right": "ema_9"}],
         "above VWAP but pulled back under the fast average"),
        ([{"left": "close", "op": "<", "right": "vwap"},
          {"left": "close", "op": ">", "right": "ema_9"}],
         "below VWAP but recovering through the fast average"),
    ]

    rsi_bands = [
        ({"left": "rsi", "op": "between", "right": [50, 70]},
         "RSI in the bullish band"),
        ({"left": "rsi", "op": "between", "right": [30, 50]},
         "RSI in the bearish band"),
        ({"left": "rsi", "op": "between", "right": [40, 60]},
         "RSI neutral"),
        ({"left": "rsi", "op": "<", "right": 35},
         "RSI oversold"),
        ({"left": "rsi", "op": ">", "right": 65},
         "RSI overbought"),
        (None, "RSI unrestricted"),
    ]

    volume_filters = [
        ({"left": "volume", "op": ">", "right": "volume_avg_20"},
         "on heavy volume"),
        ({"left": "volume", "op": "<", "right": "volume_avg_20"},
         "on light volume"),
        (None, "at any volume"),
    ]

    # Trend STRENGTH, which nothing in the original space could express.
    #
    # Every condition above asks where price sits relative to an average
    # or whether momentum is rising. None asks how hard the market is
    # moving. ADX answers that without saying which direction, so a rule
    # can now require "a real trend" rather than inferring one from price
    # position -- and equally can require its absence, which is the
    # condition a mean-reversion setup actually wants.
    strength = [
        ({"left": "adx", "op": ">", "right": 25},
         "in a strong trend"),
        ({"left": "adx", "op": "<", "right": 20},
         "in a rangebound market"),
        (None, "at any trend strength"),
    ]

    # Directional pressure, likewise absent. plus_di over minus_di says
    # buyers are pressing, independent of where price sits.
    pressure = [
        ({"left": "plus_di", "op": ">", "right": "minus_di"},
         "with buyers pressing"),
        ({"left": "minus_di", "op": ">", "right": "plus_di"},
         "with sellers pressing"),
        (None, "with no pressure filter"),
    ]

    from strategy_lab import MAX_CONDITIONS

    specs: list[dict] = []

    for side in ("BUY_LONG", "SELL_SHORT"):
        for trend in ("BULLISH", "BEARISH", "ANY"):
            for mom, mom_text in momentum:
                for loc, loc_text in location:
                    for rsi, rsi_text in rsi_bands:
                        for vol, vol_text in volume_filters:
                            for adx, adx_text in strength:
                                for di, di_text in pressure:
                                    conditions = [mom] + list(loc)

                                    for extra in (rsi, vol, adx, di):
                                        if extra is not None:
                                            conditions.append(extra)

                                    # The 6-condition ceiling stays. More
                                    # conditions fit history better and
                                    # predict worse, and raising it to
                                    # admit the new dimensions would trade
                                    # the guard for the search.
                                    if len(conditions) > MAX_CONDITIONS:
                                        continue

                                    specs.append({
                                        "name": f"r{len(specs):04d}",
                                        "side": side,
                                        "trend": trend,
                                        "rationale": (
                                            f"{side.lower().replace('_', ' ')}"
                                            f" when trend is {trend.lower()},"
                                            f" {mom_text}, {loc_text},"
                                            f" {rsi_text}, {vol_text},"
                                            f" {adx_text}, {di_text}"
                                        ),
                                        "conditions": conditions,
                                    })

    return specs


def control_rules() -> dict[str, Callable]:
    """Benchmarks a real rule has to beat, not merely breakeven.

    WHY BREAKEVEN IS THE WRONG BAR

    For a 2:1 bracket breakeven is 33.3%, which is ALSO the probability
    a driftless random walk touches +4% before -2%. In a rising year any
    long-biased rule clears it while contributing nothing, so a search
    ranked on breakeven alone reports drift as an edge.

    Measured on 2026-08-05 over 251 sessions: the live rule scored 34.4%
    and cleared breakeven. Buying on EVERY bar scored 35.2%, and
    entering at random scored 35.9%. The rule was 1.5 points worse than
    not choosing at all, and had looked like a success.

    These run alongside every search so the real bar is visible in the
    same report.
    """

    def always_long(row, trend):
        """Pure drift. Whatever the market did, with no selection."""
        return "BUY_LONG"

    def scattered_long(row, trend):
        """Random-ish entry, deterministic so results reproduce.

        Keyed off the bar's own digits rather than a random number
        generator, which keeps a resumed or repeated run identical.
        """
        key = int(abs(row["close"]) * 1000) + int(abs(row["rsi"]) * 10)
        return "BUY_LONG" if key % 40 == 0 else "NO_TRADE"

    return {
        "__control_always_long": always_long,
        "__control_random_entry": scattered_long,
    }


def compile_all(specs: list[dict]) -> dict[str, Callable]:
    """Compile specs to rules, dropping any that will not validate."""

    from strategy_lab import compile_spec

    rules: dict[str, Callable] = {}

    for spec in specs:
        try:
            rules[spec["name"]] = compile_spec(spec)
        except ValueError:
            continue

    return rules


def summarise(result: Any) -> tuple[int, float, float]:
    """(decided trades, win rate, expectancy in R) for one Result."""

    decided = [
        t for t in result.trades
        if t.outcome in (backtest.OUTCOME_TARGET, backtest.OUTCOME_STOP,
                         backtest.OUTCOME_AMBIGUOUS)
    ]

    if not decided:
        return 0, 0.0, 0.0

    wins = sum(1 for t in decided if t.outcome == backtest.OUTCOME_TARGET)
    expectancy = sum(t.r_multiple for t in decided) / len(decided)

    return len(decided), wins / len(decided), expectancy


def expected_by_chance(rules_tested: int, alpha: float = ALPHA) -> float:
    """How many rules would clear a test at this level on luck alone."""

    return rules_tested * alpha


def search(
    train: dict[str, Any],
    test: dict[str, Any],
    specs: list[dict],
    *,
    stop_percent: float,
    reward_ratio: float,
    max_bars_held: int,
    finalists: int = FINALISTS,
) -> tuple[list[Finding], dict]:
    """Rank on train, freeze, then measure the finalists on the holdout."""

    rules = compile_all(specs)
    by_name = {spec["name"]: spec for spec in specs}

    print(f"Compiled {len(rules)} of {len(specs)} candidate rules.")
    print(f"Searching {len(train)} symbols on the training period...")

    train_results = run_rules(
        train, rules, stop_percent=stop_percent,
        reward_ratio=reward_ratio, max_bars_held=max_bars_held,
    )

    breakeven = breakeven_rate(reward_ratio, 1.0)
    scored: list[Finding] = []
    too_few = 0

    for result in train_results:
        trades, win_rate, expectancy = summarise(result)

        if trades < MIN_SAMPLE:
            too_few += 1
            continue

        scored.append(Finding(
            spec=by_name[result.name],
            train_trades=trades,
            train_win_rate=win_rate,
            train_expectancy=expectancy,
        ))

    # Rank by expectancy, not win rate. A rule can win often and still
    # lose money; R is what the account actually feels.
    scored.sort(key=lambda f: -f.train_expectancy)

    stats = {
        "rules_tested": len(rules),
        "too_few_trades": too_few,
        "ranked": len(scored),
        "cleared_breakeven_on_train": sum(
            1 for f in scored if f.train_win_rate > breakeven),
        "breakeven": breakeven,
        "expected_by_chance": expected_by_chance(len(rules)),
    }

    chosen = scored[:finalists]

    if not test or not chosen:
        return chosen, stats

    print(f"\nHolding out {len(test)} symbols. Measuring {len(chosen)} "
          f"finalist(s) once, against random-entry controls.")

    finalist_rules = {f.spec["name"]: rules[f.spec["name"]] for f in chosen}

    # The controls run on the HOLDOUT, beside the finalists, on identical
    # bars. A finalist that cannot beat blind entry here has found the
    # market going up, not an edge.
    control_results = run_rules(
        test, control_rules(), stop_percent=stop_percent,
        reward_ratio=reward_ratio, max_bars_held=max_bars_held,
    )

    stats["controls"] = {}

    for result in control_results:
        trades, win_rate, expectancy = summarise(result)
        stats["controls"][result.name.replace("__control_", "")] = {
            "trades": trades, "win_rate": win_rate, "expectancy": expectancy,
        }

    test_results = run_rules(
        test, finalist_rules, stop_percent=stop_percent,
        reward_ratio=reward_ratio, max_bars_held=max_bars_held,
    )

    # Bonferroni: several finalists are being read off one holdout.
    corrected = ALPHA / max(len(chosen), 1)

    for result in test_results:
        finding = next(f for f in chosen if f.spec["name"] == result.name)
        trades, win_rate, expectancy = summarise(result)
        days, share = concentration(result)

        finding.test_trades = trades
        finding.test_win_rate = win_rate
        finding.test_expectancy = expectancy
        finding.test_days = days
        finding.test_concentration = share

        if trades < MIN_SAMPLE:
            finding.verdict = "TOO FEW TRADES"
            continue

        # P(a rule with no edge produced at least this many wins).
        wins = round(win_rate * trades)
        finding.test_p_value = 1.0 - binomial_tail(wins - 1, trades, breakeven)

        if win_rate <= breakeven:
            finding.verdict = "FAILED HOLDOUT"
        elif finding.test_p_value < corrected:
            finding.verdict = "SURVIVED"
        else:
            finding.verdict = "NOT SIGNIFICANT"

    stats["corrected_alpha"] = corrected

    return chosen, stats


def report(findings: list[Finding], stats: dict, *, reward_ratio: float) -> None:
    """Print the null first, then the results, then the honest verdict."""

    breakeven = stats["breakeven"]

    print()
    print("=" * 72)
    print("WHAT WOULD HAPPEN IF NOTHING WORKED")
    print("=" * 72)
    print(f"  Rules tested                     : {stats['rules_tested']}")
    print(f"  Dropped for too few trades       : {stats['too_few_trades']}")
    print(f"  Ranked                           : {stats['ranked']}")
    print(f"  Breakeven win rate at {reward_ratio:.2f}:1      : {breakeven:.1%}")
    print(f"  Cleared breakeven in training    : "
          f"{stats['cleared_breakeven_on_train']}")
    print(f"  Expected to clear on chance alone: "
          f"{stats['expected_by_chance']:.0f}")
    print()
    print("  Read those last two together. If they are close, the search")
    print("  found nothing -- it just found the top of a random pile.")

    if not findings:
        print("\nNo rule produced enough trades to rank. Nothing to report.")
        return

    print()
    print("=" * 72)
    print("FINALISTS, MEASURED ONCE ON DATA THE SEARCH NEVER SAW")
    print("=" * 72)
    print(f"  Significance required: p < {stats.get('corrected_alpha', ALPHA):.4f} "
          f"(Bonferroni for {len(findings)} finalists)")
    print()
    print(f"  {'rule':<8} {'train':>16}   {'holdout':>16}   {'p':>7}  verdict")
    print(f"  {'':<8} {'n   win     R':>16}   {'n   win     R':>16}")
    print("  " + "-" * 68)

    for finding in findings:
        print(
            f"  {finding.spec['name']:<8} "
            f"{finding.train_trades:>4} {finding.train_win_rate:>5.1%} "
            f"{finding.train_expectancy:>6.2f}   "
            f"{finding.test_trades:>4} {finding.test_win_rate:>5.1%} "
            f"{finding.test_expectancy:>6.2f}   "
            f"{finding.test_p_value:>7.4f}  {finding.verdict}"
        )

    survivors = [f for f in findings if f.verdict == "SURVIVED"]

    # The real bar. Breakeven is what a rule must clear to not lose
    # money; the controls are what it must clear to have contributed
    # anything, and in a rising market those are very different numbers.
    controls = stats.get("controls") or {}

    if controls:
        print()
        print("=" * 72)
        print("CONTROLS — the bar that actually matters, same holdout bars")
        print("=" * 72)

        for name, data in sorted(controls.items()):
            print(f"  {name:<16} {data['trades']:>5} trades  "
                  f"{data['win_rate']:>6.1%}  {data['expectancy']:>+6.2f}R")

        best_control = max(
            (d["win_rate"] for d in controls.values()), default=0.0)

        print()
        print(f"  A rule must beat {best_control:.1%}, not {breakeven:.1%}.")
        print("  Breakeven is also what a random walk scores, so clearing it")
        print("  in a rising year proves only that the year rose.")

        beaten = [f for f in survivors if f.test_win_rate > best_control]

        if survivors and not beaten:
            print()
            print("  NOTE: every surviving rule scored at or below blind")
            print("  entry. They cleared significance against breakeven and")
            print("  still added nothing. Treat them as drift.")

        survivors = beaten

    print()
    print("=" * 72)
    print("WHAT THIS MEANS")
    print("=" * 72)

    if not survivors:
        print("  Nothing survived the holdout.")
        print()
        print("  This is the honest result and the most common one. The")
        print("  rules that led the search were leading it because of")
        print("  noise, and the holdout is where that becomes visible.")
        print()
        print("  It does NOT mean these indicators can never work. It means")
        print("  they do not work here: this universe, this holding period,")
        print("  these bands. Searching the same space harder will not")
        print("  change the answer -- the search space has to change.")
        return

    print(f"  {len(survivors)} rule(s) beat breakeven on data the search")
    print("  never saw, at a corrected significance level.")
    print()

    for finding in survivors:
        print(f"  {finding.spec['name']}: {finding.spec['rationale']}")
        print(f"      holdout {finding.test_trades} trades, "
              f"{finding.test_win_rate:.1%} win, "
              f"{finding.test_expectancy:+.2f}R, p={finding.test_p_value:.4f}")
        print(f"      across {finding.test_days} sessions, "
              f"{finding.test_concentration:.0%} from the busiest")

        if finding.test_concentration > 0.5:
            print("      WARNING: over half from one session. That is closer")
            print("      to one observation than to a sample.")

    print()
    print("  This is a reason to look further, NOT a reason to trade it.")
    print("  Before any of this reaches live entries:")
    print("    - fills here are simulated at exact stop and target, and")
    print("      pay no spread. Live fills are worse.")
    print("    - a holdout is one period. It is not proof of a future one.")
    print("    - the honest next step is forward testing in shadow mode,")
    print("      where it costs nothing to be wrong.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=40)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--reward", type=float, default=2.0)
    parser.add_argument("--stop", type=float, default=0.02)
    parser.add_argument("--max-bars", type=int, default=24)
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--finalists", type=int, default=FINALISTS)
    args = parser.parse_args()

    from universe import load_universe

    symbols = load_universe()[:args.symbols]

    if not symbols:
        print("No universe to search. Run universe.py first.")
        return 1

    print(f"Loading {len(symbols)} symbols over {args.days} days...")
    frames = backtest.load_history(symbols, days=args.days)

    if not frames:
        print("No history returned.")
        return 1

    train, test, cut = split_by_session(
        frames, train_fraction=args.train_fraction
    )

    bars = sum(len(f) for f in frames.values())
    print(f"  {len(frames)} symbols, {bars:,} bars")
    print(f"  train: {len(train)} symbols before {cut}")
    print(f"  holdout: {len(test)} symbols from {cut}")

    if not test:
        print("\nNo holdout. Refusing to search -- an unvalidated ranking")
        print("is exactly the thing this file exists to avoid producing.")
        return 1

    specs = generate_candidates()

    findings, stats = search(
        train, test, specs,
        stop_percent=args.stop,
        reward_ratio=args.reward,
        max_bars_held=args.max_bars,
        finalists=args.finalists,
    )

    report(findings, stats, reward_ratio=args.reward)

    return 0


def _self_test() -> int:
    import pandas as pd
    from datetime import datetime, timedelta, timezone

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    print("The search space")

    specs = generate_candidates()
    check("candidates are generated", len(specs) > 50, str(len(specs)))

    from strategy_lab import validate_spec

    bad = [s for s in specs if not validate_spec(s)[0]]
    check("every candidate validates", not bad,
          bad[0]["name"] if bad else "")

    rules = compile_all(specs)
    check("every candidate compiles", len(rules) == len(specs),
          f"{len(rules)} of {len(specs)}")

    check("both directions are searched",
          {s["side"] for s in specs} == {"BUY_LONG", "SELL_SHORT"})
    check("light volume is in the space, not just heavy",
          any("light volume" in s["rationale"] for s in specs))
    check("every candidate carries a rationale",
          all(s.get("rationale") for s in specs))
    check("none exceeds the condition limit",
          all(len(s["conditions"]) <= 6 for s in specs))

    print()
    print("The holdout is a later period, not a random sample")

    base = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)

    def frame(days_count):
        rows = []
        for d in range(days_count):
            for b in range(70):
                rows.append({
                    "timestamp": base + timedelta(days=d, minutes=5 * b),
                    "close": 100.0,
                })
        return pd.DataFrame(rows)

    frames = {"A": frame(10), "B": frame(10)}
    train, test, cut = split_by_session(frames, train_fraction=0.7)

    check("both halves are populated", bool(train) and bool(test))

    train_days = set(train["A"]["timestamp"].dt.date)
    test_days = set(test["A"]["timestamp"].dt.date)

    check("no session appears on both sides",
          not (train_days & test_days),
          str(sorted(train_days & test_days)))
    check("the holdout is strictly later",
          min(test_days) > max(train_days))
    check("the boundary is reported", cut is not None)

    # Different symbols must be cut at the same date, or a rule sees one
    # stock's Tuesday in training and another's in the holdout.
    check("every symbol is cut at the same date",
          set(train["A"]["timestamp"].dt.date)
          == set(train["B"]["timestamp"].dt.date))

    short = {"A": frame(2)}
    _, held, _ = split_by_session(short)
    check("too little history yields no holdout", held == {})

    print()
    print("Controls, because breakeven is not the bar")

    controls = control_rules()
    check("blind entry is one of them", "__control_always_long" in controls)
    check("random entry is another", "__control_random_entry" in controls)

    row = {"close": 20.0, "rsi": 55.0}
    check("always-long always enters",
          controls["__control_always_long"](row, "BULLISH") == "BUY_LONG")
    check("and does not care about trend",
          controls["__control_always_long"](row, "BEARISH") == "BUY_LONG")

    # Deterministic, so a repeated or resumed run gives the same answer.
    first = [controls["__control_random_entry"](
        {"close": 20.0 + i / 100, "rsi": 55.0}, "BULLISH") for i in range(400)]
    second = [controls["__control_random_entry"](
        {"close": 20.0 + i / 100, "rsi": 55.0}, "BULLISH") for i in range(400)]

    check("random entry is reproducible", first == second)

    entries = sum(1 for s in first if s == "BUY_LONG")
    check("it enters sometimes but not always", 0 < entries < 400,
          f"{entries}/400")

    check("controls are named so they cannot be mistaken for proposals",
          all(name.startswith("__control_") for name in controls))

    print()
    print("The null is computed, not asserted")

    check("200 rules at 5% expect 10 by chance",
          abs(expected_by_chance(200, 0.05) - 10.0) < 1e-9)
    check("more tests expect more false positives",
          expected_by_chance(400) > expected_by_chance(200))

    print()
    print("Ranking uses expectancy, and thin samples do not rank")

    check("MIN_SAMPLE is inherited, not redefined", MIN_SAMPLE >= 30)
    check("the finalist count is small enough to correct for",
          FINALISTS <= 10)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All rule-search checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    sys.exit(main())
