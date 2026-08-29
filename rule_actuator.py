"""
rule_actuator.py  --  let a verdict actually undo the experiment it judged

WHY THIS EXISTS

    The owner's framing, 2026-08-29: "Lockbot needs to be able to learn
    and implement. As long as it evolves as it learns then every day is a
    win on some level."

    The learning half works. LOCKBOT reads its own logs nightly and finds
    defects its engineer missed. The implementing half had a broken wire,
    and the engineer broke it: two guarantees were written into config as
    PROSE with no code behind them.

        lockbot_config, on the spread gate:
            "COSTING_MONEY ... and this reverts to 0.05"
        lockbot_config, on the profit lock:
            "if it reads COSTING_MONEY at n=30 the lock comes out
             regardless of who approved it"

    Neither reverted anything. health_monitor marks the module DEGRADED on
    a COSTING_MONEY verdict, so it is visible -- but visible is not acted
    on. A guarantee that exists only as prose is the same defect class as
    an unenforced pre-registration, which this project has already paid
    for once: the 08-06 crypto clause failure sat unactioned for three
    weeks while the rule was still cited as the best result available.

THE DESIGN CONSTRAINT, which is LOCKBOT's and is the whole of it

    An actuator sits on top of a measurement stack that keeps being wrong
    -- six tests this month pinned values a legitimate change moved. The
    answer is not to withhold the actuator. It is that EVERYTHING THE
    ACTUATOR CAN DO MUST BE SOMETHING WE WOULD WANT DONE EVEN ON A WRONG
    VERDICT.

    A restrictive revert on a false COSTING_MONEY costs a good parameter
    value, and a page says so immediately. Recoverable. The loosening
    direction, the exit path and self-modification are excluded ABSOLUTELY
    rather than gated, because those are where a wrong verdict does damage
    no stability count can undo.

    So the loop the owner asked for exists -- measure, judge, act -- and
    the acting is confined to undoing our own experiments. That is the
    only kind of implementation a learning system should be trusted with
    before its measurements have earned more.

WHAT IT WILL NEVER TOUCH (LOCKBOT, e3df3371)

    * PAPER_TRADING and LIVE_TRADING_ENABLED, structurally, not merely by
      omission from a list.
    * ANYTHING ON THE OPTIONS EXIT PATH -- the stop, the confirm cycles,
      the exit bands, the profit lock. Options have no broker-side stop;
      the software IS the stop. A COSTING_MONEY verdict on any of those
      always files for a human.
    * Any cap in the loosening direction, MAX_DAILY_LOSS_PERCENT included.
    * Its own floors, schedule, kill switch and caps. No self-modification.

USAGE
    python rule_actuator.py --self-test
    python rule_actuator.py                 evaluate, act or file
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lockbot_config as config

VERSION = "1.0"

# Consecutive scheduled runs a COSTING_MONEY verdict must survive. One
# reading of a metric computed from a moving sample is not a verdict, the
# same reasoning that governs OPTIONS_STOP_CONFIRM_CYCLES.
REQUIRED_CONSECUTIVE = 3

# Global ceiling. Even a correct actuator should not reshape the system in
# an afternoon while nobody is watching.
MAX_ACTUATIONS_PER_DAY = 1

# Settings this may revert, with the baseline RECORDED HERE rather than
# inferred from history. LOCKBOT: "revert target is the baseline persisted
# at change-ship time, never inferred" -- an inferred baseline is a guess
# about what we used to believe.
#
# `restrictive` is the direction that REDUCES exposure. The actuator fires
# only that way; a loosening revert files for a human even when the
# verdict is sound.
REVERTABLE: dict[str, dict[str, Any]] = {
    "spread_ceiling": {
        "setting": "OPTIONS_MAX_SPREAD_PERCENT",
        "baseline": 0.05,
        "shipped": 0.08,
        "restrictive": "lower",
        "note": "0.05 -> 0.08 on 2026-08-26 with this revert promised in "
                "the config comment. This is that promise, in code.",
    },
    "options_loss_cooldown": {
        "setting": "OPTIONS_LOSS_COOLDOWN_SESSIONS",
        "baseline": 5,
        "shipped": 1,
        "restrictive": "higher",
        "note": "cut 5 -> 1 on 2026-08-21. Reverting BENCHES more names, "
                "which reduces exposure, so it is the restrictive way.",
    },
}

# Rules whose verdicts ALWAYS file for a human, whatever they say. Named
# individually rather than left to a rule of thumb, because a rule of
# thumb is how the exit path gets touched by accident.
ALWAYS_FILE = {
    "stop_confirm_cycles": "options exit path -- the software IS the stop",
    "underlying_stop_buffer": "options exit path",
    "profit_lock": "options exit path -- an exit band",
    "single_legs_only": "structural: changes the instrument, not a dial",
    "entry_limit_fraction": "changes order pricing, not an exposure cap",
}

# Never touchable by any path, actuator or otherwise. Checked explicitly so
# a future edit to REVERTABLE cannot quietly admit one.
FORBIDDEN = {
    "PAPER_TRADING",
    "LIVE_TRADING_ENABLED",
    "MAX_DAILY_LOSS_PERCENT",
    "OPTIONS_STOP_LOSS_PERCENT",
    "OPTIONS_TAKE_PROFIT_PERCENT",
    "OPTIONS_STOP_CONFIRM_CYCLES",
    "OPTIONS_PROFIT_LOCK_ARM_PERCENT",
    "OPTIONS_PROFIT_LOCK_FLOOR_PERCENT",
}


def state_path() -> Path:
    return Path(getattr(config, "RULE_ACTUATOR_STATE_FILE",
                        config.PROJECT_FOLDER / "rule_actuator_state.json"))


def enabled() -> bool:
    """The kill switch. Default ON, and it lives on the runtime allowlist
    so LOCKBOT or the owner can disarm it without a code change."""

    return bool(getattr(config, "RULE_ACTUATOR_ENABLED", True))


def load_state() -> dict[str, Any]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def save_state(state: dict[str, Any]) -> None:
    try:
        state_path().write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError:
        pass


def is_restrictive(rule_id: str, current: Any, target: Any) -> bool | None:
    """Does moving current -> target REDUCE exposure? None if unknowable.

    None is a refusal, not a maybe. Every caller treats it as "do not
    fire", because a direction this cannot determine is one it must not
    act on.
    """

    spec = REVERTABLE.get(rule_id)

    if not spec:
        return None

    try:
        current, target = float(current), float(target)
    except (TypeError, ValueError):
        return None

    if spec["restrictive"] == "lower":
        return target < current

    if spec["restrictive"] == "higher":
        return target > current

    return None


def eligible(rule_id: str, review: Any, state: dict[str, Any]) -> tuple[bool, str]:
    """May this verdict fire the actuator? Every failure returns a reason.

    Fails CLOSED throughout: anything ambiguous, unreadable or unlisted
    files for a human instead.
    """

    if not enabled():
        return False, "actuator disarmed by kill switch"

    if rule_id in ALWAYS_FILE:
        return False, f"always files -- {ALWAYS_FILE[rule_id]}"

    spec = REVERTABLE.get(rule_id)

    if not spec:
        return False, "no recorded baseline; a revert target must never be inferred"

    if spec["setting"] in FORBIDDEN:
        return False, f"{spec['setting']} is never touchable"

    if getattr(review, "status", None) != "COSTING_MONEY":
        return False, f"verdict is {getattr(review, 'status', '?')}, not COSTING_MONEY"

    entry = state.get("rules", {}).get(rule_id, {})

    if entry.get("actuated_at"):
        return False, (f"already actuated once on {entry['actuated_at'][:10]}; "
                       "a second needs a human re-arm")

    run = int(entry.get("consecutive", 0))

    if run < REQUIRED_CONSECUTIVE:
        return False, (f"COSTING_MONEY on {run} of {REQUIRED_CONSECUTIVE} "
                       "consecutive runs")

    # The sample must not be shrinking. A verdict computed on fewer
    # observations than last time means rows were dropped or recomputed,
    # and the comparison is not like-for-like.
    n_now = int(getattr(review, "n", 0) or 0)
    n_before = int(entry.get("last_n", 0) or 0)

    if n_now < n_before:
        return False, f"sample shrank from {n_before} to {n_now}"

    floor = int(getattr(review, "floor_n", 0) or 0)

    if floor and n_now < floor:
        return False, f"n={n_now} below the registered floor of {floor}"

    today = datetime.now(timezone.utc).date().isoformat()

    if int(state.get("actuations", {}).get(today, 0)) >= MAX_ACTUATIONS_PER_DAY:
        return False, f"daily cap of {MAX_ACTUATIONS_PER_DAY} already used"

    current = getattr(config, spec["setting"], None)
    restrictive = is_restrictive(rule_id, current, spec["baseline"])

    if restrictive is None:
        return False, "cannot determine the direction of the move"

    if not restrictive:
        return False, ("the revert would LOOSEN exposure; a human decides "
                       "those even on a sound verdict")

    return True, (f"COSTING_MONEY on {run} runs at n={n_now}; reverting "
                  f"{spec['setting']} {current} -> {spec['baseline']}")


def record_verdict(state: dict[str, Any], rule_id: str, review: Any) -> dict[str, Any]:
    """Advance or reset the consecutive-verdict counter for one rule."""

    rules = state.setdefault("rules", {})
    entry = rules.setdefault(rule_id, {})
    status = getattr(review, "status", None)

    if status == "COSTING_MONEY":
        entry["consecutive"] = int(entry.get("consecutive", 0)) + 1
    else:
        entry["consecutive"] = 0

    entry["last_status"] = status
    entry["last_n"] = int(getattr(review, "n", 0) or 0)
    entry["last_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return state


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}"
              + (f" - {detail}" if detail and not condition else ""))

    class R:
        def __init__(self, status="COSTING_MONEY", n=40, floor_n=30):
            self.status, self.n, self.floor_n = status, n, floor_n

    def armed(rule="spread_ceiling", runs=REQUIRED_CONSECUTIVE, n=40):
        return {"rules": {rule: {"consecutive": runs, "last_n": n}}}

    print("The exit path is never actuated, whatever the verdict says")
    for rule in ("stop_confirm_cycles", "underlying_stop_buffer",
                 "profit_lock", "single_legs_only"):
        ok, why = eligible(rule, R(), armed(rule))
        check(f"{rule} files instead of firing", not ok, why)

    print()
    print("Nothing forbidden can be reached, even if listed by mistake")
    for name in ("PAPER_TRADING", "LIVE_TRADING_ENABLED",
                 "MAX_DAILY_LOSS_PERCENT", "OPTIONS_STOP_LOSS_PERCENT"):
        check(f"{name} is in FORBIDDEN", name in FORBIDDEN)
    check("no revertable setting is also forbidden",
          not {v["setting"] for v in REVERTABLE.values()} & FORBIDDEN,
          str({v["setting"] for v in REVERTABLE.values()} & FORBIDDEN))

    print()
    print("Only the restrictive direction fires")
    check("tightening the spread gate is restrictive",
          is_restrictive("spread_ceiling", 0.08, 0.05) is True)
    check("loosening it is not",
          is_restrictive("spread_ceiling", 0.05, 0.08) is False)
    check("a longer cooldown is restrictive",
          is_restrictive("options_loss_cooldown", 1, 5) is True)
    # None means refuse, never "maybe".
    check("an unreadable direction is None, not a guess",
          is_restrictive("spread_ceiling", "x", 0.05) is None)
    check("an unlisted rule has no direction",
          is_restrictive("not_a_rule", 1, 2) is None)

    print()
    print("A verdict must survive repetition, on a sample that is not shrinking")
    ok, why = eligible("spread_ceiling", R(), armed(runs=1))
    check("one COSTING_MONEY run does not fire", not ok, why)
    ok, why = eligible("spread_ceiling", R(), armed(runs=REQUIRED_CONSECUTIVE))
    check(f"{REQUIRED_CONSECUTIVE} consecutive runs do", ok, why)

    ok, why = eligible("spread_ceiling", R(n=20),
                       {"rules": {"spread_ceiling":
                                  {"consecutive": 3, "last_n": 40}}})
    check("a shrinking sample refuses", not ok, why)

    ok, why = eligible("spread_ceiling", R(n=10, floor_n=30), armed(n=10))
    check("below the registered floor refuses", not ok, why)

    print()
    print("Once, then a human")
    state = armed()
    state["rules"]["spread_ceiling"]["actuated_at"] = "2026-08-29T15:35:00"
    ok, why = eligible("spread_ceiling", R(), state)
    check("a second actuation needs re-arming", not ok, why)

    today = datetime.now(timezone.utc).date().isoformat()
    capped = armed()
    capped["actuations"] = {today: MAX_ACTUATIONS_PER_DAY}
    ok, why = eligible("spread_ceiling", R(), capped)
    check("the daily cap holds", not ok, why)

    print()
    print("Anything else files, and a healthy rule does nothing")
    ok, why = eligible("spread_ceiling", R(status="WORKING"), armed())
    check("a WORKING verdict does not fire", not ok, why)
    ok, why = eligible("unknown_rule", R(), armed("unknown_rule"))
    check("a rule with no recorded baseline refuses", not ok, why)

    print()
    print("The counter advances and resets honestly")
    s = record_verdict({}, "spread_ceiling", R())
    check("a COSTING_MONEY run counts",
          s["rules"]["spread_ceiling"]["consecutive"] == 1)
    s = record_verdict(s, "spread_ceiling", R(status="NEUTRAL"))
    check("anything else resets it to zero",
          s["rules"]["spread_ceiling"]["consecutive"] == 0)

    print()
    print("It cannot trade, and it cannot edit itself")
    src = Path(__file__).read_text(encoding="utf-8").split("def _self_test")[0]
    check("no order submission", "submit_order" not in src)
    check("no position closing", "close_position" not in src)
    check("it does not write lockbot_config",
          "lockbot_config.py" not in src.replace("import lockbot_config", ""))
    check("REQUIRED_CONSECUTIVE is not settable at runtime",
          "RULE_ACTUATOR_REQUIRED" not in src)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All rule-actuator checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Act on a COSTING_MONEY verdict, or file it")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    print(f"RULE ACTUATOR v{VERSION}")
    print(f"  armed: {enabled()}")

    import rule_registry

    state = load_state()
    live = rule_registry.run_review()

    for review in getattr(live, "rules", []):
        rule_id = getattr(review, "rule_id", "?")
        state = record_verdict(state, rule_id, review)
        ok, why = eligible(rule_id, review, state)
        mark = "WOULD ACT" if ok else "no action"
        print(f"  {rule_id:<26} {getattr(review,'status','?'):<18} {mark}: {why}")

    save_state(state)

    print()
    print("  Actuation is confined to undoing our own experiments, in the")
    print("  restrictive direction only. Everything else files for a human.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
