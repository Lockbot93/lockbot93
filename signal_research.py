"""
signal_research.py — why does the entry signal lose?

WHY THIS EXISTS

As of 2026-08-02 the shadow replay says the entry signal is not merely
unproven, it is losing: 19 wins in 94 decided setups against a 33.3%
breakeven at 2:1 reward:risk. The probability of a shortfall that large
under a true breakeven rate is 0.0037, so this is no longer a sample-size
argument. Building anything on top of that signal -- more symbols, more
capital, options, a live broker -- multiplies a measured loss.

The useful question is therefore not "what else can LOCKBOT do" but
"where exactly does the picking go wrong". This module answers that from
shadow_trades.csv alone. It places no orders and changes no state.

DISCIPLINE, BECAUSE 94 ROWS IS NOT MANY

Slicing 94 outcomes by regime, hour, confidence and side produces roughly
twenty comparisons. At p<0.05 you expect one false positive per twenty
purely by chance, so a single interesting-looking bucket proves nothing.
Every split here reports its sample size, and buckets below MIN_BUCKET are
labelled untrustworthy rather than quietly ranked. The report states the
multiple-comparison problem out loud instead of leaving the reader to
remember it.

The one hypothesis worth real care is inversion: if the signal is wrong
four times in five, taking the other side looks profitable. That result is
partly manufactured by the replay itself, which resolves AMBIGUOUS bars --
where a single 5-minute bar spans both stop and target -- as losses. Every
ambiguous bar is a free win handed to the inverse. This module quantifies
that subsidy instead of ignoring it.
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from math import comb
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_FOLDER = Path(__file__).resolve().parent
SHADOW_FILE = PROJECT_FOLDER / "shadow_trades.csv"

MARKET_TIMEZONE = ZoneInfo("America/New_York")

# Below this a bucket is reported but never ranked or acted on.
MIN_BUCKET = 15

# The replay's payout: target is 2R away, stop is 1R away.
REWARD_R = 2.0
RISK_R = 1.0
BREAKEVEN = RISK_R / (REWARD_R + RISK_R)

WIN = "TARGET"
LOSS = "STOP"
AMBIGUOUS = "AMBIGUOUS"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_rows(path: Path | None = None) -> list[dict[str, Any]]:
    """Read the shadow log, tolerating a missing file."""

    source = path or SHADOW_FILE

    if not source.exists():
        return []

    with source.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def decided(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Setups that reached a stop or a target."""

    return [row for row in rows if row.get("outcome") in (WIN, LOSS)]


# ---------------------------------------------------------------------------
# Statistics -- exact, because the buckets are small
# ---------------------------------------------------------------------------

def binomial_tail(wins: int, n: int, rate: float) -> float:
    """P(observing <= wins successes in n trials at this true rate)."""

    if n <= 0:
        return 1.0

    return sum(
        comb(n, k) * rate**k * (1.0 - rate) ** (n - k)
        for k in range(0, wins + 1)
    )


def fisher_two_sided(a_w: int, a_n: int, b_w: int, b_n: int) -> float:
    """Exact two-sided p-value for two win rates being the same."""

    total = a_n + b_n
    wins = a_w + b_w

    if total == 0 or wins == 0 or wins == total:
        return 1.0

    def probability(k: int) -> float:
        return comb(a_n, k) * comb(b_n, wins - k) / comb(total, wins)

    observed = probability(a_w)
    low = max(0, wins - b_n)
    high = min(a_n, wins)

    return sum(
        probability(k)
        for k in range(low, high + 1)
        if probability(k) <= observed + 1e-12
    )


def win_rate(rows: list[dict[str, Any]]) -> float:
    """Fraction of decided setups that reached target."""

    if not rows:
        return 0.0

    return sum(1 for row in rows if row["outcome"] == WIN) / len(rows)


def expectancy_r(rows: list[dict[str, Any]]) -> float:
    """Average R per setup at the replay's fixed 2:1 payout."""

    if not rows:
        return 0.0

    return sum(
        REWARD_R if row["outcome"] == WIN else -RISK_R for row in rows
    ) / len(rows)


# ---------------------------------------------------------------------------
# Slicing
# ---------------------------------------------------------------------------

def trading_day(row: dict[str, Any]) -> str | None:
    """The exchange-local date a setup was logged on."""

    stamp = row.get("logged_at")

    if not stamp:
        return None

    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.astimezone(MARKET_TIMEZONE).date().isoformat()


def concentration(rows: list[dict[str, Any]]) -> dict:
    """How much of the sample comes from its single busiest day.

    The binomial test below treats every setup as an independent trial.
    Setups logged on the same day are nothing of the sort -- they are the
    same market, the same session, often the same move, and momentum
    longs taken hours apart on one sideways afternoon fail together. When
    one day supplies most of the sample, "94 trades" is closer to "one
    day observed 94 times", and any p-value computed from it is far more
    confident than the evidence deserves.

    On 2026-08-02 the sample was 94 decided setups, 82 of them from a
    single session. That does not make the loss unreal, but it does mean
    the honest sample size is days, not setups.
    """

    days = group_by(rows, trading_day)

    if not days:
        return {"days": 0, "total": len(rows)}

    busiest, busiest_rows = max(days.items(), key=lambda item: len(item[1]))

    return {
        "days": len(days),
        "total": len(rows),
        "busiest_day": busiest,
        "busiest_count": len(busiest_rows),
        "share": len(busiest_rows) / len(rows) if rows else 0.0,
    }


def market_hour(row: dict[str, Any]) -> str | None:
    """The exchange-local hour a setup was logged in."""

    stamp = row.get("logged_at")

    if not stamp:
        return None

    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    local = moment.astimezone(MARKET_TIMEZONE)

    return f"{local.hour:02d}:00-{local.hour:02d}:59 ET"


def confidence_bucket(row: dict[str, Any]) -> str | None:
    """Coarse confidence bands, since the raw score clusters at 100."""

    raw = row.get("confidence")

    try:
        score = float(raw)
    except (TypeError, ValueError):
        return None

    if score >= 100:
        return "100 (max)"
    if score >= 90:
        return "90-99"
    if score >= 80:
        return "80-89"

    return "below 80"


QUALITY_COMPONENTS = (
    "quality",
    "q_trend_strength",
    "q_momentum",
    "q_conviction",
    "q_restraint",
    "q_volume_ratio",
)


def component_split(rows: list[dict[str, Any]], column: str) -> dict | None:
    """Split decided setups at a component's median and compare halves.

    market_scanner.py ranks setups on these components, so if the ranking
    carries information the upper half should win more often than the
    lower half. If both halves look the same, the component is noise and
    ranking on it is theatre.

    Returns None when too few rows carry the column -- it was only added
    on 2026-07-29, so setups logged before that have nothing to split.
    """

    usable = []

    for row in rows:
        raw = row.get(column)

        if raw in (None, ""):
            continue

        try:
            usable.append((float(raw), row))
        except (TypeError, ValueError):
            continue

    if len(usable) < MIN_BUCKET * 2:
        return {"column": column, "n": len(usable), "enough": False}

    usable.sort(key=lambda pair: pair[0])
    midpoint = len(usable) // 2

    lower = [row for _, row in usable[:midpoint]]
    upper = [row for _, row in usable[midpoint:]]

    lower_wins = sum(1 for row in lower if row["outcome"] == WIN)
    upper_wins = sum(1 for row in upper if row["outcome"] == WIN)

    return {
        "column": column,
        "n": len(usable),
        "enough": True,
        "lower_n": len(lower),
        "upper_n": len(upper),
        "lower_rate": win_rate(lower),
        "upper_rate": win_rate(upper),
        "lower_r": expectancy_r(lower),
        "upper_r": expectancy_r(upper),
        "p": fisher_two_sided(upper_wins, len(upper), lower_wins, len(lower)),
        "span": (usable[0][0], usable[-1][0]),
    }


def group_by(rows: list[dict[str, Any]], key) -> dict[str, list[dict[str, Any]]]:
    """Bucket decided setups by a key function, dropping unusable rows."""

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        label = key(row)

        if label is None:
            continue

        buckets[label].append(row)

    return dict(buckets)


# ---------------------------------------------------------------------------
# Inversion, with the replay's own bias priced in
# ---------------------------------------------------------------------------

def inversion_estimate(rows: list[dict[str, Any]], ambiguous_count: int) -> dict:
    """What taking the opposite side would have paid, honestly bounded.

    Flipping the trade swaps the distances: the original's 1R stop becomes
    the inverse's target, and its 2R target becomes the inverse's stop. So
    every original loss pays the inverse +1R and every original win costs
    it -2R, measured in the ORIGINAL R.

    The optimistic figure takes the replay at face value. The pessimistic
    figure assumes every AMBIGUOUS bar -- currently scored as a loss, and
    therefore as a free inverse win -- actually went the other way. The
    truth is between them, and if the pessimistic end is not positive the
    idea does not survive its own measurement error.
    """

    total = len(rows)

    if total == 0:
        return {"n": 0}

    wins = sum(1 for row in rows if row["outcome"] == WIN)
    losses = total - wins

    optimistic = (losses * RISK_R - wins * REWARD_R) / total

    # Worst case: every ambiguous bar was really a target, so it should
    # have counted as an inverse loss rather than an inverse win.
    adjusted_total = total + ambiguous_count
    adjusted_wins = wins + ambiguous_count
    adjusted_losses = losses

    pessimistic = (
        adjusted_losses * RISK_R - adjusted_wins * REWARD_R
    ) / adjusted_total if adjusted_total else 0.0

    return {
        "n": total,
        "wins": wins,
        "losses": losses,
        "ambiguous": ambiguous_count,
        "optimistic_r": optimistic,
        "pessimistic_r": pessimistic,
        "survives": pessimistic > 0,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_split(title: str, buckets: dict[str, list[dict[str, Any]]],
                baseline: list[dict[str, Any]]) -> None:
    """Print one slice of the sample against the overall baseline."""

    print()
    print(title)
    print("-" * 74)

    if not buckets:
        print("  no data")
        return

    print(f"  {'bucket':<22} {'n':>4} {'win rate':>9} {'avg R':>7} "
          f"{'vs rest p':>10}  note")

    base_wins = sum(1 for row in baseline if row["outcome"] == WIN)
    base_n = len(baseline)

    ordered = sorted(buckets.items(), key=lambda item: -len(item[1]))

    for label, rows in ordered:
        n = len(rows)
        wins = sum(1 for row in rows if row["outcome"] == WIN)
        rest_w = base_wins - wins
        rest_n = base_n - n

        p = fisher_two_sided(wins, n, rest_w, rest_n) if rest_n else 1.0

        if n < MIN_BUCKET:
            note = "too small to read"
        elif p < 0.05:
            note = "stands out (see caveat)"
        else:
            note = ""

        print(f"  {label:<22} {n:>4} {win_rate(rows):>8.1%} "
              f"{expectancy_r(rows):>+7.2f} {p:>10.3f}  {note}")


def build_report(rows: list[dict[str, Any]]) -> int:
    """Print the full research report. Returns a process exit code."""

    print("=" * 74)
    print("        LOCKBOT SIGNAL RESEARCH")
    print("=" * 74)

    if not rows:
        print("shadow_trades.csv is empty or missing. Nothing to analyse.")
        return 1

    sample = decided(rows)
    ambiguous_count = sum(1 for row in rows if row.get("outcome") == AMBIGUOUS)

    print(f"  logged setups     : {len(rows)}")
    print(f"  decided           : {len(sample)}")
    print(f"  ambiguous         : {ambiguous_count} "
          "(scored as losses by the replay)")

    if not sample:
        print("\nNothing has resolved yet. Run shadow_trades.py first.")
        return 1

    wins = sum(1 for row in sample if row["outcome"] == WIN)
    rate = win_rate(sample)
    p_value = binomial_tail(wins, len(sample), BREAKEVEN)

    print()
    print("OVERALL")
    print("-" * 74)
    print(f"  win rate          : {rate:.1%}  ({wins}/{len(sample)})")
    print(f"  breakeven needed  : {BREAKEVEN:.1%}  at {REWARD_R:.0f}:1")
    print(f"  expectancy        : {expectancy_r(sample):+.3f} R per setup")
    print(f"  P(this bad or worse if truly breakeven) = {p_value:.4f}")

    if p_value < 0.05:
        print("  verdict           : the loss is real, not a cold streak")
    else:
        print("  verdict           : still inside normal variation")

    spread = concentration(sample)

    print()
    print("HOW INDEPENDENT IS THIS SAMPLE?")
    print("-" * 74)
    print(f"  trading days covered : {spread['days']}")

    if spread.get("busiest_day"):
        print(f"  busiest day          : {spread['busiest_day']} "
              f"with {spread['busiest_count']} of {spread['total']} setups "
              f"({spread['share']:.0%})")

    if spread.get("share", 0.0) >= 0.5:
        print()
        print("  WARNING: most of the sample is one session. The p-value")
        print("  above assumes every setup is an independent trial, and")
        print("  same-day setups are not -- they share one market and one")
        print("  move, so they fail together. Read the effective sample as")
        print(f"  roughly {spread['days']} day(s), not {spread['total']} trades,")
        print("  and treat the significance above as an upper bound on how")
        print("  certain this is.")
    elif spread["days"] < 10:
        print()
        print("  Note: few enough days that regime luck still dominates.")

    print_split("BY TRADING DAY", group_by(sample, trading_day), sample)
    print_split("BY MARKET REGIME", group_by(sample, lambda r: r.get("regime") or None), sample)
    print_split("BY HOUR OF DAY", group_by(sample, market_hour), sample)
    print_split("BY CONFIDENCE", group_by(sample, confidence_bucket), sample)
    print_split("BY DIRECTION", group_by(sample, lambda r: r.get("side") or None), sample)

    print()
    print("DOES THE RANKING CARRY ANY INFORMATION?")
    print("-" * 74)
    print("  Each component split at its median: upper half vs lower half.")
    print()
    print(f"  {'component':<20} {'n':>4} {'lower':>8} {'upper':>8} {'p':>8}  note")

    for column in QUALITY_COMPONENTS:
        result = component_split(sample, column)

        if result is None or not result["enough"]:
            got = result["n"] if result else 0
            print(f"  {column:<20} {got:>4} {'-':>8} {'-':>8} {'-':>8}  "
                  f"needs {MIN_BUCKET * 2}+ decided setups")
            continue

        note = "separates" if result["p"] < 0.05 else "no signal"

        print(f"  {column:<20} {result['n']:>4} "
              f"{result['lower_rate']:>7.1%} {result['upper_rate']:>7.1%} "
              f"{result['p']:>8.3f}  {note}")

    print()
    print("INVERSION -- would doing the opposite work?")
    print("-" * 74)

    inverted = inversion_estimate(sample, ambiguous_count)

    print(f"  taking the other side of all {inverted['n']} setups:")
    print(f"    best case  {inverted['optimistic_r']:+.3f} R per setup "
          "(replay taken at face value)")
    print(f"    worst case {inverted['pessimistic_r']:+.3f} R per setup "
          f"(all {inverted['ambiguous']} ambiguous bars go the other way)")

    if inverted["ambiguous"] == 0:
        print("    no ambiguous bars in this sample, so the two agree.")

    print()

    if inverted["survives"]:
        print("  Survives its own measurement error -- worth a real test.")
        print("  It is still NOT free money: this ignores spread, slippage")
        print("  and commission, and a 2:1 loser inverts to a 1:2 winner,")
        print("  which needs a high hit rate to stay ahead of costs.")
    else:
        print("  Does NOT survive its own measurement error. The apparent")
        print("  edge is inside the margin created by scoring ambiguous")
        print("  bars as losses. Do not trade this.")

    print()
    print("=" * 74)
    print("HOW TO READ THIS")
    print("-" * 74)
    print("  Roughly twenty comparisons appear above. At p<0.05 about one")
    print("  in twenty looks significant by chance alone, so a single")
    print("  standout bucket is not a finding -- it is the expected noise.")
    print(f"  Buckets under {MIN_BUCKET} setups are labelled and should not be")
    print("  ranked at all. Treat anything here as a hypothesis to test on")
    print("  new data, never as a rule to deploy.")
    print("=" * 74)

    return 0


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

    print("Statistics")

    # A fair coin flipped 10 times landing 5 heads is unremarkable.
    check(
        "binomial tail is ~0.5 at the expected count",
        0.4 < binomial_tail(5, 10, 0.5) < 0.7,
        f"{binomial_tail(5, 10, 0.5):.3f}",
    )
    check(
        "binomial tail is tiny for a large shortfall",
        binomial_tail(0, 50, 0.5) < 1e-10,
    )
    # The real 2026-08-02 number.
    check(
        "reproduces the 19/94 result",
        abs(binomial_tail(19, 94, 1 / 3) - 0.00366) < 0.0005,
        f"{binomial_tail(19, 94, 1/3):.5f}",
    )
    check(
        "identical groups are not significant",
        fisher_two_sided(10, 20, 10, 20) > 0.9,
    )
    check(
        "opposite groups are significant",
        fisher_two_sided(20, 20, 0, 20) < 0.001,
    )
    check("empty sample does not divide by zero", binomial_tail(0, 0, 0.5) == 1.0)

    print()
    print("Rates")

    rows = [{"outcome": WIN}, {"outcome": LOSS}, {"outcome": LOSS}]
    check("win rate counts targets", abs(win_rate(rows) - 1 / 3) < 1e-9)
    check(
        "expectancy is zero at breakeven",
        abs(expectancy_r(rows) - 0.0) < 1e-9,
        f"{expectancy_r(rows)}",
    )
    check("empty rows are safe", win_rate([]) == 0.0 and expectancy_r([]) == 0.0)

    print()
    print("Inversion")

    # 1 win, 4 losses: inverse gains 4x1R and loses 1x2R over 5 = +0.4R
    losing = [{"outcome": WIN}] + [{"outcome": LOSS}] * 4
    flat = inversion_estimate(losing, ambiguous_count=0)
    check(
        "inverse of a 20% loser looks positive",
        abs(flat["optimistic_r"] - 0.4) < 1e-9,
        f"{flat['optimistic_r']}",
    )
    check(
        "with no ambiguity both bounds agree",
        abs(flat["optimistic_r"] - flat["pessimistic_r"]) < 1e-9,
    )

    # Enough ambiguous bars must be able to erase the apparent edge.
    subsidised = inversion_estimate(losing, ambiguous_count=10)
    check(
        "ambiguous bars drag the worst case down",
        subsidised["pessimistic_r"] < flat["optimistic_r"],
        f"{subsidised['pessimistic_r']:.3f}",
    )
    check(
        "a heavily subsidised edge does not survive",
        subsidised["survives"] is False,
        f"{subsidised['pessimistic_r']:.3f}",
    )
    check("empty inversion is safe", inversion_estimate([], 0)["n"] == 0)

    print()
    print("Bucketing")

    check(
        "hour uses exchange time, not UTC",
        market_hour({"logged_at": "2026-07-30T17:10:14+00:00"}) == "13:00-13:59 ET",
        str(market_hour({"logged_at": "2026-07-30T17:10:14+00:00"})),
    )
    check(
        "a naive timestamp is treated as UTC",
        market_hour({"logged_at": "2026-07-30T17:10:14"}) == "13:00-13:59 ET",
    )
    check("a missing timestamp is dropped", market_hour({}) is None)
    check("a broken timestamp is dropped", market_hour({"logged_at": "nope"}) is None)
    check(
        "confidence buckets at the top",
        confidence_bucket({"confidence": "100"}) == "100 (max)",
    )
    check("non-numeric confidence is dropped", confidence_bucket({"confidence": ""}) is None)

    grouped = group_by(
        [{"regime": "A"}, {"regime": "A"}, {"regime": "B"}, {"regime": None}],
        lambda r: r.get("regime") or None,
    )
    check("group_by buckets correctly", len(grouped["A"]) == 2 and len(grouped["B"]) == 1)
    check("group_by drops unusable rows", sum(len(v) for v in grouped.values()) == 3)

    print()
    print("Loading")

    check("a missing file returns no rows", load_rows(Path("does_not_exist.csv")) == [])

    print()
    print("Ranking components")

    thin = component_split([{"quality": "1", "outcome": WIN}], "quality")
    check("too little data reports honestly", thin["enough"] is False, str(thin))

    # A component that perfectly predicts outcome must separate.
    perfect = (
        [{"quality": str(i), "outcome": LOSS} for i in range(20)]
        + [{"quality": str(50 + i), "outcome": WIN} for i in range(20)]
    )
    strong = component_split(perfect, "quality")
    check("a perfect predictor separates", strong["enough"] and strong["p"] < 0.001,
          str(strong.get("p")))
    check(
        "and the upper half is the winning half",
        strong["upper_rate"] > strong["lower_rate"],
    )

    # A component unrelated to outcome must not.
    noise = [
        {"quality": str(i), "outcome": WIN if i % 2 else LOSS}
        for i in range(40)
    ]
    weak = component_split(noise, "quality")
    check("pure noise does not separate", weak["enough"] and weak["p"] > 0.05,
          str(weak.get("p")))
    check(
        "non-numeric component values are skipped",
        component_split(
            [{"quality": "n/a", "outcome": WIN}] * 40, "quality"
        )["n"] == 0,
    )
    check(
        "a missing column reports zero rows",
        component_split([{"outcome": WIN}] * 40, "q_nonexistent")["n"] == 0,
    )

    print()
    print("Sample independence")

    lopsided = (
        [{"logged_at": "2026-07-28T15:00:00+00:00"}] * 82
        + [{"logged_at": "2026-07-29T15:00:00+00:00"}] * 12
    )
    spread = concentration(lopsided)
    check("counts distinct trading days", spread["days"] == 2, str(spread["days"]))
    check(
        "finds the busiest day",
        spread["busiest_day"] == "2026-07-28" and spread["busiest_count"] == 82,
        str(spread),
    )
    check("flags the concentration", spread["share"] > 0.5, f"{spread['share']:.2f}")

    even = [
        {"logged_at": f"2026-07-{day:02d}T15:00:00+00:00"}
        for day in range(1, 21)
    ]
    check("an even spread is not flagged", concentration(even)["share"] < 0.5)
    check("empty input is safe", concentration([])["days"] == 0)
    check(
        "trading day uses exchange time",
        trading_day({"logged_at": "2026-07-31T02:00:00+00:00"}) == "2026-07-30",
        str(trading_day({"logged_at": "2026-07-31T02:00:00+00:00"})),
    )
    check(
        "decided() keeps only stops and targets",
        len(decided([{"outcome": WIN}, {"outcome": "UNRESOLVED"},
                     {"outcome": LOSS}, {"outcome": AMBIGUOUS}])) == 2,
    )

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All signal-research checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    sys.exit(build_report(load_rows()))
