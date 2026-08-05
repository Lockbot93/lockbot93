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

    return "\n".join(lines)


def evaluate(spec: dict, frames: dict, *, reward_ratio: float = 2.0,
             stop_percent: float = 0.02) -> tuple[str, dict]:
    """Backtest one proposal. Returns (verdict, result).

    Verdicts are deliberately cautious. "PROMISING" means it cleared
    breakeven on the sample available, not that it works — the sample is
    days long and the harness says so itself.
    """

    import backtest

    ok, why = validate_spec(spec)

    if not ok:
        return "REJECTED", {"reason": why}

    rule = compile_spec(spec)

    results = backtest.run_rules(
        frames, {spec["name"]: rule},
        stop_percent=stop_percent,
        reward_ratio=reward_ratio,
        max_bars_held=78,
    )

    result = results[0]
    decided = result.decided()
    days, concentration = backtest.concentration(result)

    payload = {
        "trades": len(decided),
        "days": days,
        "busiest_share": round(concentration, 3),
        "win_rate": round(result.win_rate(), 4),
        "expectancy_r": round(result.expectancy(), 4),
        "breakeven": round(backtest.breakeven_rate(reward_ratio, 1.0), 4),
    }

    if len(decided) < 20:
        return "TOO FEW TRADES", payload

    if concentration >= 0.6:
        return "TOO CONCENTRATED", payload

    if result.expectancy() > 0:
        return "PROMISING", payload

    return "NEGATIVE", payload


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
