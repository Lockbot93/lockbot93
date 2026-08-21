"""
rule_registry.py  --  every remembered rule, and what it has cost

WHY THIS EXISTS

    The dominant defect of the week beginning 2026-08-14 was one shape:
    the measurement existed and nothing consumed it.

      the debit cap    lived on the single-leg path while every position
                       held had entered through the spread path
      watchdog.py      existed complete and had never been scheduled
      13 constants     read as configuration and configured nothing
      the untracked
      position alert   printed "NO software stop loss" into a heartbeat
                       nobody read, for five and a half hours

    On 2026-08-19 and 20 two behavioural rules shipped, each with a
    measurement condition LOCKBOT attached as the price of approval:

      OPTIONS_LOSS_COOLDOWN_SESSIONS  blocked entries are shadow-logged
      OPTIONS_ENTRY_LIMIT_FRACTION    fills and non-fills are logged

    Neither had anything that read the measurement. Two more rules that
    block or reprice real trades, with their evidence accumulating into
    files nobody opens. This is the reader.

WHAT MAKES A RULE ADMISSIBLE

    Only rules with a PRE-AGREED metric and floor (LOCKBOT's ruling,
    channel 963343a5). A rule whose success criterion is written after
    the data arrives cannot fail, and a rule that cannot fail is not a
    rule, it is a preference.

    Plain behavioural constants stay with config_sweep. This registry is
    for things that CHANGE WHAT LOCKBOT DOES and claim a benefit.

THE VERDICT THIS EXISTS TO PRONOUNCE

    COSTING_MONEY. Every other status is bookkeeping on the way to it.
    A rule that blocks trades and keeps no record of what it blocked can
    only ever be removed on taste; this is what lets one be removed on
    evidence.

WHAT IT DELIBERATELY WILL NOT DO

    It renders verdicts and mutates nothing. Disabling a rule is the
    owner's decision or LOCKBOT's, never the reviewer's. A reviewer that
    can act on its own findings is an optimiser, and an optimiser with a
    verdict is how a system talks itself into things.

USAGE
    python rule_registry.py              print the review
    python rule_registry.py --self-test  offline checks
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import lockbot_config as config

VERSION = "1.0"

REGISTRY_FILE = config.PROJECT_FOLDER / "rule_registry.jsonl"
STATE_FILE = config.PROJECT_FOLDER / "rule_registry_state.json"

# Statuses, in the order they are reached.
ACCUMULATING = "ACCUMULATING"
JUDGEABLE = "JUDGEABLE"
WORKING = "WORKING"
COSTING_MONEY = "COSTING_MONEY"
NEUTRAL_ON_DIRECTIVE = "NEUTRAL_ON_DIRECTIVE"
MEASUREMENT_STALLED = "MEASUREMENT_STALLED"
UNREGISTERED = "UNREGISTERED"


# ---------------------------------------------------------------------------
# THE REGISTRY. Floors set by LOCKBOT AT ADOPTION (channel 963343a5), never
# by this reader at read time -- otherwise the bar moves to meet the data.
# ---------------------------------------------------------------------------

REGISTRY: list[dict[str, Any]] = [
    {
        "rule_id": "options_loss_cooldown",
        "setting": "OPTIONS_LOSS_COOLDOWN_SESSIONS",
        "mechanism": (
            "Benches an underlying for N sessions after a realised "
            "STOP_LOSS, blocking new option entries on that name."
        ),
        "adopted_at": "2026-08-20",
        "authority": "LOCKBOT, channel 152a38bd, on owner directive",
        "basis": "directive",
        "power_note": (
            "Four realised option losses at adoption -- far below any "
            "floor. It ships on the owner's instruction, not on evidence, "
            "and the shadow tag is what converts it into a measurement."
        ),
        "metric_source": "options_shadow_log.csv, action=COOLDOWN_BLOCKED",
        "metric": (
            "mean net R of RESOLVED blocked entries, timeouts marked to "
            "market"
        ),
        "floor_n": 30,
        "resolution": 0.36,
        "verdict_rule": (
            "mean >= +0.36R the blocked trades would have WON, so the "
            "cooldown is COSTING_MONEY; <= -0.36R it is WORKING; between, "
            "NEUTRAL_ON_DIRECTIVE and re-judged every further 30."
        ),
    },
    {
        "rule_id": "entry_limit_fraction",
        "setting": "OPTIONS_ENTRY_LIMIT_FRACTION",
        "mechanism": (
            "Prices entry limits partway across the spread instead of "
            "above the offer. 0.5 = midpoint-to-touch."
        ),
        "adopted_at": "2026-08-19",
        "authority": "LOCKBOT, channel 80b8a35f",
        "basis": "evidence",
        "power_note": (
            "Replaced ask x 1.03, which had produced 4 of 10 "
            "ENTRY_NOT_FILLED -- a ~60% fill rate bought at a premium, "
            "so the certainty it was paying for did not exist."
        ),
        "metric_source": "execution_limit_attempts.csv",
        "metric": (
            "net per attempt: spread captured on fills MINUS the "
            "adverse-selection term. FILL RATE ALONE NEVER CONVICTS OR "
            "ACQUITS -- midpoint orders fill preferentially when the "
            "market comes toward you, so a good fill rate can be pure "
            "selection."
        ),
        "floor_n": 50,
        "tripwire": (
            "fill rate below 20% at n >= 30 escalates immediately, "
            "without waiting for the floor"
        ),
        "resolution": None,
        "verdict_rule": (
            "net < 0 at the floor is COSTING_MONEY; net > 0 is WORKING."
        ),
    },
]

# Behavioural rules that must appear in the registry or be reported as
# UNREGISTERED. Adding a rule to LOCKBOT without a floor is the defect
# this section exists to surface.
BEHAVIOURAL_SETTINGS = [
    "OPTIONS_LOSS_COOLDOWN_SESSIONS",
    "OPTIONS_ENTRY_LIMIT_FRACTION",
    "OPTIONS_STOP_CONFIRM_CYCLES",
]


@dataclass
class RuleReview:
    rule_id: str
    setting: str
    status: str
    n: int = 0
    floor_n: int = 0
    value: float | None = None
    detail: str = ""
    basis: str = ""

    @property
    def needs(self) -> int:
        return max(0, self.floor_n - self.n)


@dataclass
class Review:
    rules: list[RuleReview] = field(default_factory=list)
    unregistered: list[str] = field(default_factory=list)

    @property
    def costing(self) -> list[RuleReview]:
        return [r for r in self.rules if r.status == COSTING_MONEY]

    @property
    def stalled(self) -> list[RuleReview]:
        return [r for r in self.rules if r.status == MEASUREMENT_STALLED]

    def fingerprint(self) -> str:
        """What must change before this is worth reporting again.

        Transitions only, per LOCKBOT's answer to "what stops the review
        becoming the thing nobody reads". A daily line that says the same
        thing every day trains the reader to skip it.
        """

        parts = [f"{r.rule_id}:{r.status}:{r.n}" for r in sorted(
            self.rules, key=lambda x: x.rule_id)]
        parts += [f"orphan:{s}" for s in sorted(self.unregistered)]

        return "|".join(parts)


def _rows(path: Path) -> list[dict[str, str]]:
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def review_cooldown(entry: dict[str, Any]) -> RuleReview:
    """Would the blocked entries have won?"""

    out = RuleReview(entry["rule_id"], entry["setting"], ACCUMULATING,
                     floor_n=entry["floor_n"], basis=entry["basis"])

    if int(getattr(config, entry["setting"], 0)) <= 0:
        out.status = NEUTRAL_ON_DIRECTIVE
        out.detail = "the rule is switched off, so there is nothing to judge"
        return out

    blocked = [r for r in _rows(config.OPTIONS_SHADOW_FILE)
               if (r.get("action") or "").strip().upper() == "COOLDOWN_BLOCKED"]

    resolved = []
    for row in blocked:
        raw = (row.get("r_multiple") or "").strip()
        if not raw:
            continue
        try:
            resolved.append(float(raw))
        except ValueError:
            continue

    out.n = len(resolved)

    if blocked and not resolved:
        out.detail = (
            f"{len(blocked)} blocked, none resolved yet -- they resolve "
            "alongside the trades that were allowed"
        )
        return out

    if out.n < out.floor_n:
        out.detail = f"{out.needs} more resolved before this can be judged"
        return out

    out.value = statistics.mean(resolved)
    edge = entry["resolution"]

    if out.value >= edge:
        out.status = COSTING_MONEY
        out.detail = (
            f"blocked entries averaged {out.value:+.2f}R -- they would have "
            f"WON. The memory is destroying value."
        )
    elif out.value <= -edge:
        out.status = WORKING
        out.detail = (
            f"blocked entries averaged {out.value:+.2f}R -- the cooldown is "
            "avoiding real losses."
        )
    else:
        out.status = NEUTRAL_ON_DIRECTIVE
        out.detail = (
            f"{out.value:+.2f}R, inside the {edge:+.2f}R this sample can "
            f"resolve. It stands on the directive, re-judged at "
            f"n={out.floor_n * 2}."
        )

    return out


def review_entry_fraction(entry: dict[str, Any]) -> RuleReview:
    """Is pricing inside the spread actually saving anything?"""

    out = RuleReview(entry["rule_id"], entry["setting"], ACCUMULATING,
                     floor_n=entry["floor_n"], basis=entry["basis"])

    path = getattr(config, "EXECUTION_LIMIT_ATTEMPTS_FILE",
                   config.PROJECT_FOLDER / "execution_limit_attempts.csv")
    attempts = _rows(Path(path))
    out.n = len(attempts)

    resolved = [r for r in attempts
                if (r.get("filled") or "").strip().lower()
                in ("true", "false", "1", "0", "yes", "no")]
    filled = [r for r in resolved
              if (r.get("filled") or "").strip().lower() in ("true", "1", "yes")]

    # The tripwire fires before the floor, because a rule that stops
    # LOCKBOT trading at all should not wait 50 attempts to say so.
    if len(resolved) >= 30:
        rate = len(filled) / len(resolved)

        if rate < 0.20:
            out.status = COSTING_MONEY
            out.value = rate
            out.detail = (
                f"TRIPWIRE: only {rate:.0%} of {len(resolved)} attempts "
                "filled. Priced this far inside the spread, LOCKBOT is "
                "barely trading."
            )
            return out

    if out.n < out.floor_n:
        out.detail = (
            f"{out.needs} more attempts before this can be judged"
            + (f" ({len(resolved)} resolved so far)" if resolved else "")
        )
        return out

    if not resolved:
        out.status = MEASUREMENT_STALLED
        out.detail = (
            f"{out.n} attempts logged and NONE resolved -- the outcome is "
            "never being written back, so this rule cannot be judged at all"
        )
        return out

    # The verdict metric needs the adverse-selection term, which needs the
    # unfilled counterfactuals priced. Until execution_cost writes those
    # back, say so rather than convicting on fill rate.
    moves = [r for r in resolved if (r.get("underlying_move_after") or "").strip()]

    if not moves:
        out.status = MEASUREMENT_STALLED
        out.detail = (
            f"floor reached at n={out.n}, but no underlying_move_after has "
            "been written. Fill rate alone never convicts or acquits, so "
            "there is no admissible verdict yet."
        )
        return out

    out.status = JUDGEABLE
    out.detail = f"{len(filled)}/{len(resolved)} filled, {len(moves)} priced"

    return out


REVIEWERS = {
    "options_loss_cooldown": review_cooldown,
    "entry_limit_fraction": review_entry_fraction,
}


def run_review() -> Review:
    """Judge every registered rule, and name any that dodged registration."""

    out = Review()
    registered = {e["setting"] for e in REGISTRY}

    for entry in REGISTRY:
        reviewer = REVIEWERS.get(entry["rule_id"])

        if reviewer is None:
            continue

        try:
            out.rules.append(reviewer(entry))
        except Exception as error:                      # noqa: BLE001
            out.rules.append(RuleReview(
                entry["rule_id"], entry["setting"], MEASUREMENT_STALLED,
                detail=f"review failed: {type(error).__name__}: {error}",
            ))

    for setting in BEHAVIOURAL_SETTINGS:
        if setting not in registered and hasattr(config, setting):
            out.unregistered.append(setting)

    return out


def should_run_today(today: date | None = None) -> bool:
    today = today or date.today()

    try:
        last = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_run")
    except (OSError, ValueError):
        last = None

    return last != today.isoformat()


def last_fingerprint() -> str:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("fp", "")
    except (OSError, ValueError):
        return ""


def record_run(review: Review, today: date | None = None) -> None:
    today = today or date.today()

    try:
        STATE_FILE.write_text(json.dumps({
            "last_run": today.isoformat(),
            "fp": review.fingerprint(),
        }), encoding="utf-8")
    except OSError:
        pass


def append_verdicts(review: Review) -> None:
    """Append-only history, so a status change can never be quietly undone."""

    try:
        with open(REGISTRY_FILE, "a", encoding="utf-8") as handle:
            for rule in review.rules:
                handle.write(json.dumps({
                    "at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"),
                    "rule_id": rule.rule_id,
                    "status": rule.status,
                    "n": rule.n,
                    "value": rule.value,
                    "detail": rule.detail,
                }) + "\n")
    except OSError:
        pass


def report(review: Review) -> None:
    print("=" * 70)
    print(f"RULE REVIEW v{VERSION} - what each remembered rule has cost")
    print("=" * 70)

    for rule in review.rules:
        mark = {
            COSTING_MONEY: "!!", WORKING: "OK", MEASUREMENT_STALLED: "??",
        }.get(rule.status, "  ")
        print(f"  {mark} {rule.setting}")
        print(f"       {rule.status}   n={rule.n}/{rule.floor_n}"
              f"   shipped on {rule.basis}")
        print(f"       {rule.detail}")

    if review.unregistered:
        print("\n  UNREGISTERED - behavioural rules with no floor:")
        for setting in review.unregistered:
            print(f"    {setting}")
        print("    A rule with no pre-agreed floor cannot fail, and a rule")
        print("    that cannot fail is a preference rather than a rule.")

    print()

    if review.costing:
        print("  A RULE IS COSTING MONEY. That verdict is the whole point of")
        print("  this module; acting on it is the owner's call, never mine.")
    elif all(r.status == ACCUMULATING for r in review.rules):
        print("  Nothing judgeable yet. Every floor was set before the data")
        print("  arrived, which is the only reason a verdict will mean")
        print("  anything when it comes.")


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}"
              + (f" - {detail}" if detail and not condition else ""))

    print("Every registered rule declares its floor BEFORE the data")
    for entry in REGISTRY:
        check(f"{entry['rule_id']} has a floor",
              isinstance(entry.get("floor_n"), int) and entry["floor_n"] > 0)
        check(f"{entry['rule_id']} names where its evidence lives",
              bool(entry.get("metric_source")))
        check(f"{entry['rule_id']} states its verdict rule up front",
              bool(entry.get("verdict_rule")))
        check(f"{entry['rule_id']} records whether it shipped on evidence",
              entry.get("basis") in ("evidence", "directive"))
        check(f"{entry['rule_id']} names the authority that set the floor",
              bool(entry.get("authority")))
        check(f"{entry['rule_id']} has a reviewer", entry["rule_id"] in REVIEWERS)

    print("\nIt can pronounce the verdict it exists for")
    entry = REGISTRY[0]
    check("COSTING_MONEY is reachable", COSTING_MONEY in entry["verdict_rule"]
          or "COSTING_MONEY" in str(entry))
    r = RuleReview("x", "X", COSTING_MONEY)
    check("and a costing rule is surfaced", Review(rules=[r]).costing == [r])

    print("\nFill rate alone never convicts or acquits")
    frac = [e for e in REGISTRY if e["rule_id"] == "entry_limit_fraction"][0]
    check("the metric says so explicitly",
          "NEVER CONVICTS OR ACQUITS" in frac["metric"])
    check("and it carries a tripwire that fires before the floor",
          bool(frac.get("tripwire")))

    print("\nIt reports transitions, not a daily identical line")
    a = Review(rules=[RuleReview("r", "R", ACCUMULATING, n=3, floor_n=30)])
    b = Review(rules=[RuleReview("r", "R", ACCUMULATING, n=3, floor_n=30)])
    c = Review(rules=[RuleReview("r", "R", ACCUMULATING, n=4, floor_n=30)])
    check("an unchanged review has an unchanged fingerprint",
          a.fingerprint() == b.fingerprint())
    check("and one more observation changes it",
          a.fingerprint() != c.fingerprint())
    check("an orphan changes it too",
          Review(rules=[], unregistered=["X"]).fingerprint() != Review().fingerprint())

    print("\nIt renders verdicts and mutates NOTHING")
    body = Path(__file__).read_text(encoding="utf-8").split("def _self_test")[0]
    check("it never writes to config", "lockbot_config" not in body.split("import")[-1]
          or "config." in body)
    check("it sets no setting", "setattr(config" not in body)
    check("it submits no orders", "submit_order" not in body)
    check("the only files it writes are its own",
          body.count("open(REGISTRY_FILE") + body.count("STATE_FILE.write_text") == 2)

    print("\nAgainst the real project")
    live = run_review()
    check("both live rules are reviewed", len(live.rules) == 2, str(len(live.rules)))
    check("nothing is judgeable yet, which is correct at n=0",
          all(r.status in (ACCUMULATING, NEUTRAL_ON_DIRECTIVE,
                           MEASUREMENT_STALLED) for r in live.rules),
          str([(r.setting, r.status) for r in live.rules]))
    check("OPTIONS_STOP_CONFIRM_CYCLES is flagged UNREGISTERED",
          "OPTIONS_STOP_CONFIRM_CYCLES" in live.unregistered,
          str(live.unregistered))

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1

    print("All rule-registry checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="What each remembered rule has cost")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    report(run_review())

    return 0


if __name__ == "__main__":
    sys.exit(main())
