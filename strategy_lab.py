"""
strategy_lab.py — LOCKBOT proposing entry rules, safely and accountably.

THE GAP THIS CLOSES

lockbot_learn.py already learns, and well: it found the mleg bookkeeping
bug two days before I did, and the missing daily-loss gate the night
before it cost money. But four of its five hypotheses have been about its
own plumbing. The one about the strategy was contradicted. It can say
"this code is wrong". It has never said "this rule might work".

WHY RULES ARE DATA HERE, NOT CODE

The obvious implementation is to let the model write a Python function.
That puts generated code in the path that decides how money is spent, on
a machine holding broker credentials, and there is no review step that
reliably catches a subtly wrong comparison.

So a proposal is a SPEC: a list of comparisons drawn from a fixed set of
indicator fields and operators. compile_spec turns it into a callable.
Nothing is eval'd, nothing is exec'd, and a spec referencing a field that
does not exist is rejected rather than crashing mid-backtest.

The cost is expressiveness — this grammar cannot express every idea. That
is an acceptable trade for never executing generated code.

THE DISCIPLINE THAT MAKES IT HONEST

Every proposal is recorded with its backtest result, including the ones
that fail. That matters more than it sounds: if twenty rules are proposed
and one backtests well, that one is what chance looks like. Tracking the
generator's own hit rate is the difference between a research loop and a
machine for fooling yourself.

Nothing here trades, and nothing auto-deploys. A proposal that survives
becomes a candidate for a human to look at.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_FOLDER = Path(__file__).resolve().parent
PROPOSALS_FILE = PROJECT_FOLDER / "strategy_proposals.jsonl"

# Only these fields may be referenced. They are exactly what
# indicators.add_indicators produces plus the trend label, so a spec
# cannot reach for data that will not be there at decision time.
FIELDS = {
    "close", "open", "high", "low",
    "ema_9", "ema_21", "vwap", "rsi",
    "macd", "macd_signal", "atr", "volume", "volume_avg_20",
}

# Numeric literals are allowed on the right-hand side. Operators are a
# closed set; there is no way to express arbitrary computation.
OPERATORS = {">", "<", ">=", "<=", "between", "outside"}

TRENDS = {"BULLISH", "BEARISH", "ANY"}

MAX_CONDITIONS = 6

# How long a proposal is allowed to be held, in 5-minute bars.
#
# Filed by LOCKBOT itself on 2026-08-05 (agent_channel 4cf2ab9f): every
# proposal was scored at max_bars_held=78, hardcoded, so a rule could
# only ever be tested as a day trade. The machinery for longer holds
# already existed in backtest.run_rules; nothing exposed it.
#
# THE STOP MOVES WITH THE HORIZON, AND IT HAS TO.
#
# A 2% stop is a real constraint over one session and a formality over
# five: given a week, an ordinary name in this universe (1.25-3.00%
# daily movement) will touch 2% in either direction almost surely, so a
# "swing" test at a 2% stop measures how long a coin takes to land, not
# whether the rule works. Widening the stop with the window is what
# keeps the two horizons comparable rather than rigged.
#
# Both remain overridable; these are the defaults that make each
# horizon mean something on its own terms.
HORIZONS = {
    # One session. 78 five-minute bars is 6.5 hours, so a position
    # cannot survive the close -- which is what makes it a day trade.
    "day": {"max_bars_held": 78, "stop_percent": 0.02,
            "history_days": 90},
    # One trading week.
    "swing": {"max_bars_held": 390, "stop_percent": 0.05,
              "history_days": 365},
}

# history_days is here because the horizon decides how much past you
# need, and getting it wrong is silent.
#
# The propose_strategy tool loaded 5 days of bars, hardcoded. Every
# proposal LOCKBOT made on 2026-08-04 came back TOO FEW TRADES with 8-10
# trades each, and it correctly concluded that "the binding constraint
# on rule discovery is history depth, not rule design" -- while the
# depth was a constant nobody had looked at. 180 days of 5-minute bars
# are available on this feed; a search over 40 symbols and 123 sessions
# was run on 2026-08-04 to confirm it.
#
# A swing test is worse than useless on 5 days: one trade can consume
# the entire sample, so the harness reports TOO FEW TRADES forever and
# reads as "no edge" rather than "no data".
#
# The swing depth is 365 rather than 180 because 180 was measured and
# found short. What matters is how many NON-OVERLAPPING holds fit, since
# trades from the same week across different symbols move together and
# are closer to one observation than to many:
#
#     180d  123 sessions  ->  25 holds per symbol
#     365d  251 sessions  ->  50
#     540d  370 sessions  ->  74
#
# 25 is too few to survive the day-concentration test this project
# already applies elsewhere. The self-test asserts the ratio rather than
# the constant, so shortening the history without lengthening the window
# fails loudly instead of quietly producing thin results.

DEFAULT_HORIZON = "day"


def validate_spec(spec: dict) -> tuple[bool, str]:
    """Whether a proposal is well-formed and safe to compile."""

    if not isinstance(spec, dict):
        return False, "spec must be an object."

    name = spec.get("name")

    if not name or not isinstance(name, str):
        return False, "spec needs a name."

    if not spec.get("rationale"):
        return False, (
            "spec needs a rationale. A rule nobody can state a reason for "
            "is a curve fit waiting to happen."
        )

    trend = str(spec.get("trend", "ANY")).upper()

    if trend not in TRENDS:
        return False, f"trend must be one of {sorted(TRENDS)}."

    side = str(spec.get("side", "BUY_LONG")).upper()

    if side not in {"BUY_LONG", "SELL_SHORT"}:
        return False, "side must be BUY_LONG or SELL_SHORT."

    conditions = spec.get("conditions")

    if not isinstance(conditions, list) or not conditions:
        return False, "spec needs at least one condition."

    if len(conditions) > MAX_CONDITIONS:
        return False, (
            f"{len(conditions)} conditions exceeds the {MAX_CONDITIONS} "
            "limit. More conditions fit history better and predict worse."
        )

    for index, condition in enumerate(conditions):
        if not isinstance(condition, dict):
            return False, f"condition {index} is not an object."

        left = condition.get("left")
        operator = condition.get("op")
        right = condition.get("right")

        if left not in FIELDS:
            return False, (
                f"condition {index}: '{left}' is not an available field. "
                f"Available: {', '.join(sorted(FIELDS))}."
            )

        if operator not in OPERATORS:
            return False, (
                f"condition {index}: '{operator}' is not an allowed "
                f"operator. Allowed: {', '.join(sorted(OPERATORS))}."
            )

        if operator in {"between", "outside"}:
            if (not isinstance(right, (list, tuple)) or len(right) != 2
                    or not all(isinstance(v, (int, float)) for v in right)):
                return False, (
                    f"condition {index}: '{operator}' needs two numbers."
                )

            if right[0] >= right[1]:
                return False, (
                    f"condition {index}: range {right} is empty or reversed."
                )
        else:
            if not (right in FIELDS or isinstance(right, (int, float))):
                return False, (
                    f"condition {index}: right side must be a field or a "
                    f"number, got {right!r}."
                )

    return True, ""


def compile_spec(spec: dict) -> Callable[[dict, str], str]:
    """Turn a validated spec into a rule callable.

    The returned function has the same shape backtest.py expects:
    (row, trend) -> "BUY_LONG" | "SELL_SHORT" | "NO_TRADE".

    No eval. Each condition becomes a closure over plain comparisons.
    """

    ok, why = validate_spec(spec)

    if not ok:
        raise ValueError(f"cannot compile: {why}")

    wanted_trend = str(spec.get("trend", "ANY")).upper()
    side = str(spec.get("side", "BUY_LONG")).upper()
    conditions = spec["conditions"]

    def rule(row: dict[str, Any], trend: str) -> str:
        if wanted_trend != "ANY" and trend != wanted_trend:
            return "NO_TRADE"

        for condition in conditions:
            try:
                left = float(row[condition["left"]])
            except (KeyError, TypeError, ValueError):
                return "NO_TRADE"

            operator = condition["op"]
            raw = condition["right"]

            if operator in {"between", "outside"}:
                low, high = float(raw[0]), float(raw[1])
                inside = low < left < high

                if operator == "between" and not inside:
                    return "NO_TRADE"

                if operator == "outside" and inside:
                    return "NO_TRADE"

                continue

            if isinstance(raw, str):
                try:
                    right = float(row[raw])
                except (KeyError, TypeError, ValueError):
                    return "NO_TRADE"
            else:
                right = float(raw)

            if operator == ">" and not left > right:
                return "NO_TRADE"
            if operator == "<" and not left < right:
                return "NO_TRADE"
            if operator == ">=" and not left >= right:
                return "NO_TRADE"
            if operator == "<=" and not left <= right:
                return "NO_TRADE"

        return side

    rule.__name__ = spec["name"].replace(" ", "_")[:40] or "proposed_rule"

    return rule


def describe_spec(spec: dict) -> str:
    """A spec in words, so a person can judge it without reading JSON."""

    parts = []

    for condition in spec.get("conditions", []):
        left, operator, right = (
            condition.get("left"), condition.get("op"), condition.get("right")
        )

        if operator in {"between", "outside"}:
            parts.append(f"{left} {operator} {right[0]} and {right[1]}")
        else:
            parts.append(f"{left} {operator} {right}")

    trend = str(spec.get("trend", "ANY")).upper()
    lead = "" if trend == "ANY" else f"trend is {trend}, and "

    return f"{spec.get('side', 'BUY_LONG')} when {lead}" + ", ".join(parts)


def record_proposal(spec: dict, result: dict | None, verdict: str) -> None:
    """Append a proposal and how it did. Failures included, deliberately.

    A generator whose misses are not recorded looks brilliant. The hit
    rate is the only thing that says whether its ideas are worth reading.
    """

    row = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "name": spec.get("name"),
        "rationale": spec.get("rationale"),
        "spec": spec,
        "verdict": verdict,
        # Recorded at the top level, not only inside `result`, so the
        # scorecard can group by it without digging. Rules can now be
        # tested at more than one holding window, and a scorecard that
        # pools day trades with swing trades reports one number for two
        # different experiments -- and inflates its own denominator,
        # which is the one thing that scorecard exists to keep honest.
        "horizon": (result or {}).get("horizon", DEFAULT_HORIZON),
        # Recorded at the top level for the same reason horizon is: the
        # lab scored everything at 2:1 for 17 proposals, and a scorecard
        # that pools ratios reports several experiments as one.
        "reward_ratio": (result or {}).get("reward_ratio", 2.0),
        "result": result or {},
    }

    try:
        with PROPOSALS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except OSError:
        pass


def load_proposals() -> list[dict]:
    """Every proposal ever made, including the failures."""

    if not PROPOSALS_FILE.exists():
        return []

    rows = []

    for line in PROPOSALS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return rows


def generator_scorecard() -> str:
    """How good the proposer has actually been.

    Reported before any individual result, because a rule that looks good
    means one thing after three proposals and something very different
    after fifty.
    """

    rows = load_proposals()

    if not rows:
        return "No proposals yet."

    verdicts: dict[str, int] = {}

    for row in rows:
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1

    lines = [f"Proposals made: {len(rows)}"]

    for verdict, count in sorted(verdicts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {verdict:<22} {count}")

    promising = verdicts.get("PROMISING", 0)

    if len(rows) >= 5:
        lines.append("")
        lines.append(
            f"  {promising}/{len(rows)} looked promising. At p<0.05 roughly "
            f"{len(rows) * 0.05:.1f} would by chance alone."
        )

    # Split by holding window. The same rule tested at two horizons is
    # two experiments, and pooling them hides which one the result came
    # from -- a rule that works as a swing trade and fails as a day
    # trade reads as one mediocre rule when pooled.
    by_horizon: dict[str, dict[str, int]] = {}

    for row in rows:
        horizon = row.get("horizon") or row.get("result", {}).get(
            "horizon", DEFAULT_HORIZON)
        bucket = by_horizon.setdefault(str(horizon), {})
        bucket[row["verdict"]] = bucket.get(row["verdict"], 0) + 1

    if len(by_horizon) > 1:
        lines.append("")
        lines.append("By holding window:")

        for horizon in sorted(by_horizon):
            counts = by_horizon[horizon]
            total = sum(counts.values())
            good = counts.get("PROMISING", 0)
            lines.append(f"  {horizon:<10} {total:>3} proposal(s), "
                         f"{good} promising")
    elif by_horizon:
        only = next(iter(by_horizon))
        lines.append("")
        lines.append(
            f"  Every proposal was scored at the '{only}' horizon. Rules "
            "have not been compared across holding windows."
        )

    # And by exit ratio, for the same reason. 17 proposals were scored at
    # 2:1 before anything else was tried; pooling ratios would hide that.
    by_ratio: dict[str, dict[str, int]] = {}

    for row in rows:
        ratio = row.get("reward_ratio") or row.get(
            "result", {}).get("reward_ratio", 2.0)
        bucket = by_ratio.setdefault(f"{float(ratio):.2f}:1", {})
        bucket[row["verdict"]] = bucket.get(row["verdict"], 0) + 1

    if len(by_ratio) > 1:
        lines.append("")
        lines.append("By exit ratio:")

        for ratio in sorted(by_ratio):
            counts = by_ratio[ratio]
            total = sum(counts.values())
            lines.append(f"  {ratio:<10} {total:>3} proposal(s), "
                         f"{counts.get('PROMISING', 0)} promising")
    elif by_ratio:
        only = next(iter(by_ratio))
        lines.append("")
        lines.append(
            f"  Every proposal was scored at {only}. The exit structure "
            "is the one constant\n  across all of them and has not been "
            "varied.")

    return "\n".join(lines)


def evaluate(spec: dict, frames: dict, *, reward_ratio: float = 2.0,
             stop_percent: float | None = None,
             horizon: str = DEFAULT_HORIZON) -> tuple[str, dict]:
    """Backtest one proposal. Returns (verdict, result).

    Verdicts are deliberately cautious. "PROMISING" means it cleared
    breakeven on the sample available, not that it works — the sample is
    days long and the harness says so itself.

    `horizon` picks the holding window and its matching stop from
    HORIZONS. An explicit `stop_percent` overrides the horizon's default,
    which is occasionally what you want and usually not — see the note on
    HORIZONS about why the two move together.

    The horizon is returned in the payload and recorded with the
    proposal, because a scorecard that mixes day and swing results is
    comparing different experiments and counting them as one.
    """

    import backtest

    ok, why = validate_spec(spec)

    if not ok:
        return "REJECTED", {"reason": why}

    if horizon not in HORIZONS:
        return "REJECTED", {
            "reason": f"horizon must be one of {sorted(HORIZONS)}."
        }

    settings = HORIZONS[horizon]
    max_bars = settings["max_bars_held"]
    stop = settings["stop_percent"] if stop_percent is None else stop_percent

    rule = compile_spec(spec)

    results = backtest.run_rules(
        frames, {spec["name"]: rule},
        stop_percent=stop,
        reward_ratio=reward_ratio,
        max_bars_held=max_bars,
    )

    result = results[0]
    decided = result.decided()
    days, concentration = backtest.concentration(result)

    # Timeout-inclusive expectancy.
    #
    # A wider target resolves less often, so measuring only DECIDED
    # trades conditions on resolution and flatters wide ratios: the
    # trades that never got anywhere simply vanish from the average.
    # Counting a timeout as 0R keeps the denominator honest across
    # ratios, which is the whole point of sweeping them.
    # Raised by LOCKBOT when this was consulted.
    all_trades = result.trades
    timeouts = [t for t in all_trades if t.outcome == backtest.OUTCOME_OPEN]

    expectancy_all = (
        sum(t.r_multiple for t in decided) / len(all_trades)
        if all_trades else 0.0
    )

    payload = {
        "horizon": horizon,
        "reward_ratio": reward_ratio,
        "max_bars_held": max_bars,
        "stop_percent": stop,
        "target_percent": round(stop * reward_ratio, 4),
        "trades": len(decided),
        "entries": len(all_trades),
        "timeouts": len(timeouts),
        "timeout_share": round(
            len(timeouts) / len(all_trades), 3) if all_trades else 0.0,
        "days": days,
        "busiest_share": round(concentration, 3),
        "win_rate": round(result.win_rate(), 4),
        "expectancy_r": round(result.expectancy(), 4),
        "expectancy_all_r": round(expectancy_all, 4),
        "breakeven": round(backtest.breakeven_rate(reward_ratio, 1.0), 4),
    }

    if len(decided) < 20:
        return "TOO FEW TRADES", payload

    if concentration >= 0.6:
        return "TOO CONCENTRATED", payload

    if result.expectancy() > 0:
        return "PROMISING", payload

    return "NEGATIVE", payload


def sweep_reward_ratios(
    spec: dict,
    frames: dict,
    *,
    horizon: str = DEFAULT_HORIZON,
    ratios: tuple[float, ...] = (1.0, 1.5, 2.0, 3.0),
) -> list[dict]:
    """Score one rule across exit ratios, each against its own control.

    WHY THE CONTROL IS PER RATIO

    Filed by LOCKBOT as item 8e24ae42: the lab scored every proposal at a
    fixed 2:1, so breakeven was always 33.3% and 17 failures shared one
    untested constant. Sweeping the ratio is the obvious fix and it has a
    trap.

    Breakeven IS what a driftless random walk scores. At 1:1 breakeven is
    50% and random entry also scores about 50%; at 3:1 both are 25%. So
    "the rule cleared breakeven at 1:1" can be arithmetic rather than
    edge -- exactly how the lab universe change read as a 167%
    improvement while the gap to random widened.

    So a random-entry control runs at EVERY ratio on identical bars, and
    what is reported is rule minus control. The question becomes "is
    there a ratio at which this entry adds something", not "is there a
    ratio at which the arithmetic flatters it".

    Four ratios, deliberately. At four tests one false pass at p<0.05 is
    already about 18% likely, and wider sweeps buy noise.
    """

    import backtest

    ok, why = validate_spec(spec)

    if not ok:
        return [{"reward_ratio": None, "error": why}]

    rule = compile_spec(spec)
    settings = HORIZONS.get(horizon, HORIZONS[DEFAULT_HORIZON])
    stop = settings["stop_percent"]
    max_bars = settings["max_bars_held"]

    def random_entry(row, trend):
        """Deterministic pseudo-random entry, reproducible across runs."""
        key = int(abs(row["close"]) * 1000) + int(abs(row["rsi"]) * 10)
        return "BUY_LONG" if key % 40 == 0 else "NO_TRADE"

    rows = []

    for ratio in ratios:
        results = backtest.run_rules(
            frames,
            {"rule": rule, "control": random_entry},
            stop_percent=stop,
            reward_ratio=ratio,
            max_bars_held=max_bars,
        )

        scored = {}

        for result in results:
            decided = result.decided()
            entries = result.trades
            wins = sum(1 for t in decided
                       if t.outcome == backtest.OUTCOME_TARGET)

            scored[result.name] = {
                "entries": len(entries),
                "decided": len(decided),
                "timeout_share": (
                    sum(1 for t in entries
                        if t.outcome == backtest.OUTCOME_OPEN) / len(entries)
                    if entries else 0.0),
                "win_rate": wins / len(decided) if decided else 0.0,
                "expectancy_all_r": (
                    sum(t.r_multiple for t in decided) / len(entries)
                    if entries else 0.0),
            }

        rule_score = scored.get("rule", {})
        control_score = scored.get("control", {})

        rows.append({
            "reward_ratio": ratio,
            "breakeven": round(backtest.breakeven_rate(ratio, 1.0), 4),
            "stop_percent": stop,
            "target_percent": round(stop * ratio, 4),
            "rule": rule_score,
            "control": control_score,
            "edge_win_rate": round(
                rule_score.get("win_rate", 0.0)
                - control_score.get("win_rate", 0.0), 4),
            "edge_expectancy": round(
                rule_score.get("expectancy_all_r", 0.0)
                - control_score.get("expectancy_all_r", 0.0), 4),
        })

    return rows


def describe_sweep(rows: list[dict]) -> str:
    """The sweep as a table, with the control beside every ratio."""

    if not rows or rows[0].get("error"):
        return f"sweep failed: {rows[0].get('error') if rows else 'no rows'}"

    lines = [
        f"  {'ratio':>6} {'target':>7} {'b/e':>6} "
        f"{'rule win':>9} {'ctrl win':>9} {'edge':>7} "
        f"{'rule R':>8} {'ctrl R':>8} {'edge R':>8} {'timeout':>8}",
        "  " + "-" * 88,
    ]

    for row in rows:
        rule, control = row["rule"], row["control"]
        lines.append(
            f"  {row['reward_ratio']:>5.2f} {row['target_percent']:>6.1%} "
            f"{row['breakeven']:>5.1%} "
            f"{rule.get('win_rate', 0):>8.1%} "
            f"{control.get('win_rate', 0):>8.1%} "
            f"{row['edge_win_rate']:>+6.1%} "
            f"{rule.get('expectancy_all_r', 0):>+8.3f} "
            f"{control.get('expectancy_all_r', 0):>+8.3f} "
            f"{row['edge_expectancy']:>+8.3f} "
            f"{rule.get('timeout_share', 0):>7.0%}"
        )

    best = max(rows, key=lambda r: r["edge_expectancy"])

    lines.append("")
    lines.append(
        f"  Best ratio by edge over control: {best['reward_ratio']:.2f} "
        f"({best['edge_expectancy']:+.3f}R)")
    lines.append(
        "  'edge' columns are rule MINUS control on identical bars. "
        "Breakeven is\n  shown only for reference -- a random walk scores it "
        "at every ratio.")

    return "\n".join(lines)


def _horizon_self_test(check) -> None:
    """Holding windows, filed by LOCKBOT as agent_channel item 4cf2ab9f."""

    print()
    print("Holding windows")

    check("more than one horizon exists", len(HORIZONS) >= 2,
          str(sorted(HORIZONS)))
    check("the default is a real horizon", DEFAULT_HORIZON in HORIZONS)

    day, swing = HORIZONS["day"], HORIZONS["swing"]

    check("a day trade cannot survive the session",
          day["max_bars_held"] <= 78, str(day["max_bars_held"]))
    check("a swing trade can hold for days",
          swing["max_bars_held"] > day["max_bars_held"] * 3,
          str(swing["max_bars_held"]))

    # The point of the whole change: the stop has to widen with the
    # window or the longer horizon just measures noise.
    check("the stop widens with the window",
          swing["stop_percent"] > day["stop_percent"],
          f"{day['stop_percent']} vs {swing['stop_percent']}")

    check("and so does the history required",
          swing["history_days"] > day["history_days"],
          f"{day['history_days']} vs {swing['history_days']}")

    # 5 days was the old hardcoded depth and it starved every proposal.
    check("no horizon is starved of history",
          all(h["history_days"] >= 30 for h in HORIZONS.values()))

    # A swing trade must fit in its own sample many times over, or
    # TOO FEW TRADES reads as "no edge" when it means "no data".
    for name, settings in HORIZONS.items():
        sessions = settings["history_days"] * 5 / 7   # calendar -> trading
        holds = sessions * 78 / settings["max_bars_held"]
        check(f"the {name} window fits its history many times over",
              holds >= 40, f"{holds:.0f} non-overlapping holds")

    check("an unknown horizon is refused",
          evaluate({"name": "x", "rationale": "y",
                    "conditions": [{"left": "rsi", "op": ">", "right": 50}]},
                   {}, horizon="fortnight")[0] == "REJECTED")


def _scorecard_self_test(check) -> None:
    """A scorecard that pools horizons is reporting two experiments as one."""

    print()
    print("The scorecard keeps horizons apart")

    global PROPOSALS_FILE
    import tempfile

    real = PROPOSALS_FILE
    PROPOSALS_FILE = Path(tempfile.mkdtemp()) / "strategy_proposals.jsonl"

    try:
        spec = {"name": "r", "rationale": "why",
                "conditions": [{"left": "rsi", "op": ">", "right": 50}]}

        record_proposal(spec, {"horizon": "day", "trades": 40}, "NEGATIVE")
        record_proposal(spec, {"horizon": "day", "trades": 40}, "NEGATIVE")
        record_proposal(spec, {"horizon": "swing", "trades": 40}, "PROMISING")

        rows = load_proposals()
        check("the horizon is stored at the top level",
              all("horizon" in r for r in rows))

        text = generator_scorecard()
        check("both horizons are reported", "day" in text and "swing" in text,
              text)
        check("and separately", "By holding window" in text, text)

        PROPOSALS_FILE.unlink()
        record_proposal(spec, {"horizon": "day", "trades": 40}, "NEGATIVE")
        text = generator_scorecard()
        check("a single-horizon scorecard says so rather than implying breadth",
              "have not been compared" in text, text)

        # An old row written before horizons existed must not vanish or
        # crash the scorecard.
        with PROPOSALS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": "2026-08-01T00:00:00+00:00", "name": "old",
                "rationale": "r", "spec": spec, "verdict": "NEGATIVE",
                "result": {},
            }) + "\n")

        check("a pre-horizon proposal still counts",
              len(load_proposals()) == 2)
        check("and the scorecard still renders",
              "Proposals made: 2" in generator_scorecard())

    finally:
        PROPOSALS_FILE = real


def _self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    good = {
        "name": "pullback in uptrend",
        "rationale": "buy weakness inside a trend rather than strength",
        "trend": "BULLISH",
        "side": "BUY_LONG",
        "conditions": [
            {"left": "close", "op": "<", "right": "ema_9"},
            {"left": "rsi", "op": "between", "right": [30, 45]},
        ],
    }

    print("Specs are validated before anything runs")

    ok, _ = validate_spec(good)
    check("a well-formed spec is accepted", ok is True)

    ok, why = validate_spec({**good, "rationale": ""})
    check("a rule with no rationale is rejected", ok is False, why)
    check("and says why that matters", "curve fit" in why, why)

    ok, why = validate_spec({
        **good, "conditions": [{"left": "os.system", "op": ">", "right": 1}]
    })
    check("an unknown field is rejected", ok is False, why)

    ok, why = validate_spec({
        **good, "conditions": [{"left": "close", "op": "exec", "right": 1}]
    })
    check("an unknown operator is rejected", ok is False, why)

    ok, why = validate_spec({
        **good,
        "conditions": [{"left": "close", "op": ">", "right": "__import__"}],
    })
    check("a non-field string right-hand side is rejected", ok is False, why)

    ok, why = validate_spec({
        **good, "conditions": [{"left": "rsi", "op": "between", "right": [70, 30]}]
    })
    check("a reversed range is rejected", ok is False, why)

    ok, why = validate_spec({
        **good,
        "conditions": [{"left": "rsi", "op": ">", "right": 1}] * 9,
    })
    check("too many conditions are rejected", ok is False, why)
    check("and the reason names overfitting", "predict worse" in why, why)

    print()
    print("Compiling produces a working rule, without eval")

    rule = compile_spec(good)

    pullback = {"close": 99.0, "ema_9": 101.0, "rsi": 38.0}
    strong = {"close": 103.0, "ema_9": 101.0, "rsi": 65.0}

    check("it fires on the intended setup",
          rule(pullback, "BULLISH") == "BUY_LONG")
    check("and not otherwise", rule(strong, "BULLISH") == "NO_TRADE")
    check("the trend filter is honoured",
          rule(pullback, "BEARISH") == "NO_TRADE")
    check("a missing field is NO_TRADE, not a crash",
          rule({}, "BULLISH") == "NO_TRADE")
    check("a non-numeric value is NO_TRADE",
          rule({"close": "x", "ema_9": 1, "rsi": 40}, "BULLISH") == "NO_TRADE")

    field_to_field = compile_spec({
        **good,
        "conditions": [{"left": "close", "op": ">", "right": "vwap"}],
    })
    check("field-to-field comparison works",
          field_to_field({"close": 10, "vwap": 9}, "BULLISH") == "BUY_LONG")

    outside = compile_spec({
        **good,
        "conditions": [{"left": "rsi", "op": "outside", "right": [40, 60]}],
    })
    check("outside is the inverse of between",
          outside({"rsi": 70}, "BULLISH") == "BUY_LONG"
          and outside({"rsi": 50}, "BULLISH") == "NO_TRADE")

    try:
        compile_spec({"name": "bad", "conditions": []})
        check("compiling an invalid spec raises", False)
    except ValueError:
        check("compiling an invalid spec raises", True)

    print()
    print("Descriptions are readable by a person")

    text = describe_spec(good)
    check("it reads as a sentence", "BUY_LONG when trend is BULLISH" in text,
          text)
    check("and names the conditions", "rsi between 30 and 45" in text, text)

    print()
    print("The generator is scored, not just its best idea")

    global PROPOSALS_FILE
    import tempfile
    real = PROPOSALS_FILE
    PROPOSALS_FILE = Path(tempfile.mkdtemp()) / "proposals.jsonl"

    try:
        check("an empty log says so", "No proposals" in generator_scorecard())

        for i in range(6):
            record_proposal(
                {**good, "name": f"idea {i}"},
                {"win_rate": 0.2},
                "PROMISING" if i == 0 else "NEGATIVE",
            )

        card = generator_scorecard()
        check("every proposal is recorded, not just winners",
              "Proposals made: 6" in card, card)
        check("failures are counted", "NEGATIVE" in card, card)
        check("and chance is stated alongside the hit",
              "by chance alone" in card, card)
        check("proposals reload", len(load_proposals()) == 6)

    finally:
        PROPOSALS_FILE = real

    _horizon_self_test(check)
    _scorecard_self_test(check)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All strategy-lab checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(generator_scorecard())
