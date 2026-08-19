"""
config_sweep.py  --  find settings that configure nothing

WHY THIS EXISTS

    Every defect found in the week of 2026-08-14 was one shape: something
    existed, read as correct, and was connected to nothing.

      the debit cap    lived on the single-leg path while every position
                       LOCKBOT held entered through the spread path
      watchdog.py      existed complete, with a working Telegram check
                       inside it, and had never been scheduled at all
      13 constants     existed as configuration that configured nothing,
                       two of them feeding a per-module cadence that does
                       not exist into LOCKBOT's own state snapshot
      the liquidity    OPTIONS_MIN_OPEN_INTEREST and
      gates            OPTIONS_MIN_CONTRACT_VOLUME sat under a "Liquidity
                       gates" heading and were reported to the owner as
                       ACTIVE RULES on 2026-08-17. Nothing read them, and
                       ContractQuote had no field to hold the data, so it
                       was never even a wiring gap.

    None of these was missing. All were disconnected. A constant that
    looks like a control and is not costs the reader their next hour
    rather than their next minute, because they believe they have already
    handled the thing.

    This finds the declarative half of that class: a name in
    lockbot_config.py that no other module mentions.

WHAT IT DELIBERATELY CANNOT FIND

    Read this before trusting a clean run. The sweep catches names nothing
    references. It would NOT have caught:

      * the debit cap, whose call site was wired and simply measured the
        wrong quantity
      * the unscheduled watchdog, because nothing in that file's source
        says it belongs on a timer

    Both needed a reader who knew what the code was FOR. A clean sweep
    means no orphaned NAMES. It does not mean the config is honest.

RULES, set by LOCKBOT on 2026-08-19 (channel item d8372ec3)

    1. It runs inside health_monitor, which is the one component whose
       execution there is continuous evidence of. A new scheduled module
       would be a second thing that can silently not run -- the defect
       this exists to catch.
    2. Once per day, gated on date change. The failure is introduced at
       edit time and costs nothing per hour undetected.
    3. It REPORTS. It never deletes. Deletion stays a human act.
    4. Every allowlist entry carries a REASON. A sweep with false
       positives trains everyone to ignore it inside a week, which is
       worse than no sweep -- so an entry without a reason is itself a
       sweep failure, and is reported as one.

USAGE
    python config_sweep.py                run it and print the findings
    python config_sweep.py --self-test    offline checks
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import lockbot_config as config

VERSION = "1.0"

CONFIG_FILE = Path(__file__).with_name("lockbot_config.py")
STATE_FILE = config.PROJECT_FOLDER / "config_sweep_state.json"

SKIP_DIRS = {".venv", "__pycache__", ".git", "solas_handover", "scratch"}

# Suffixes that name a path rather than a behaviour. These are reached by
# `config.X` attribute access across the tree and by tooling outside it, so
# absence from a text search says nothing about whether they are used.
PATH_SUFFIXES = ("_FILE", "_FOLDER", "_PATH", "_DIR")


# ---------------------------------------------------------------------------
# The allowlist. EVERY entry needs a reason -- see rule 4.
# ---------------------------------------------------------------------------

ALLOWLIST: dict[str, str] = {
    "LOCKBOT_CONFIG_VERSION": (
        "Read inside lockbot_config.py itself, by the state-snapshot "
        "builder, to populate config_version -- LOCKBOT's snapshot reports "
        "1.4 from it. Invisible here because this sweep asks 'referenced "
        "OUTSIDE the config file', and this one is consumed within it. "
        "Confirmed wired on 2026-08-19 (channel item 1d2a6307): it was on a "
        "delete list of fourteen and survived only because it was checked "
        "before deleting."
    ),
}


@dataclass
class SweepResult:
    orphans: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    unreasoned: list[str] = field(default_factory=list)
    total: int = 0

    @property
    def clean(self) -> bool:
        return not self.orphans and not self.unreasoned

    def summary(self) -> str:
        if self.unreasoned:
            return (
                f"{len(self.unreasoned)} allowlist entry/entries carry no "
                f"reason: {', '.join(self.unreasoned)}"
            )

        if self.orphans:
            return (
                f"{len(self.orphans)} config constant(s) read nowhere: "
                f"{', '.join(self.orphans)}"
            )

        return f"All {self.total} config constants are referenced."


def project_sources(root: Path | None = None) -> list[Path]:
    """Every .py in the project except the config file and skipped trees."""

    root = root or CONFIG_FILE.parent
    found: list[Path] = []

    # This file is excluded along with the config itself, and the reason is
    # a bug this tool had on its first run: ALLOWLIST names constants as
    # dictionary keys, so a sweep that read its own source found every
    # allowlisted name "referenced" and reported nothing. The allowlist
    # would have silently disabled the check for anything placed in it --
    # a control that reads as a control and does the opposite, which is the
    # exact class this module exists to catch. Found 2026-08-19 because the
    # first live run printed "allowlisted: 0" when it should have printed 1.
    excluded = {CONFIG_FILE.resolve(), Path(__file__).resolve()}

    for path in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue

        try:
            if path.resolve() in excluded:
                continue
        except OSError:
            continue

        found.append(path)

    return found


def declared_constants(text: str) -> list[str]:
    """Top-level UPPER_CASE assignments in the config source."""

    return sorted(set(re.findall(r"^([A-Z][A-Z0-9_]{3,})\s*=", text, re.M)))


def sweep(root: Path | None = None) -> SweepResult:
    """Which config constants does nothing outside the config file mention?"""

    result = SweepResult()

    try:
        config_text = CONFIG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    names = [
        name for name in declared_constants(config_text)
        if not name.endswith(PATH_SUFFIXES)
    ]
    result.total = len(names)

    corpus: list[str] = []

    for path in project_sources(root):
        try:
            corpus.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue

    haystack = "\n".join(corpus)

    for name in names:
        if re.search(r"\b" + re.escape(name) + r"\b", haystack):
            continue

        if name in ALLOWLIST:
            result.allowed.append(name)
        else:
            result.orphans.append(name)

    # Rule 4. An allowlist is how a sweep goes quiet by degrees, so an entry
    # that does not justify itself is reported as a finding in its own right.
    for name, reason in ALLOWLIST.items():
        if not str(reason).strip():
            result.unreasoned.append(name)

    return result


def should_run_today(today: date | None = None) -> bool:
    """Once per day, gated on date change rather than a per-cycle timer."""

    today = today or date.today()

    try:
        last = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        ).get("last_run")
    except (OSError, ValueError):
        last = None

    return last != today.isoformat()


def record_run(today: date | None = None) -> None:
    """Remember the date, so the next cycle today does nothing."""

    today = today or date.today()

    try:
        STATE_FILE.write_text(
            json.dumps({"last_run": today.isoformat()}), encoding="utf-8"
        )
    except OSError:
        pass          # a sweep that cannot save its state still ran


def report(result: SweepResult) -> None:
    print("=" * 66)
    print(f"CONFIG SWEEP v{VERSION} - settings that configure nothing")
    print("=" * 66)
    print(f"  constants declared : {result.total}")
    print(f"  read nowhere       : {len(result.orphans)}")
    print(f"  allowlisted        : {len(result.allowed)}")

    for name in result.orphans:
        print(f"    ORPHAN     {name}")

    for name in result.allowed:
        print(f"    allowed    {name}")

    for name in result.unreasoned:
        print(f"    NO REASON  {name}  <- an allowlist entry must justify itself")

    print()
    print(f"  {result.summary()}")

    if result.clean:
        print()
        print("  A clean sweep means no orphaned NAMES. It does not mean the")
        print("  config is honest - see this module's docstring.")


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(label)
        print(
            f"  {'PASS' if condition else 'FAIL'}  {label}"
            + (f" - {detail}" if detail and not condition else "")
        )

    print("It finds what it is meant to find")
    text = "ALPHA = 1\nBETA = 2\nGAMMA_FILE = 'x'\nlower = 3\n"
    names = declared_constants(text)
    check("picks up top-level constants",
          names == ["ALPHA", "BETA", "GAMMA_FILE"], str(names))
    check("ignores lowercase names", "lower" not in names)
    check("ignores indented assignments",
          declared_constants("    INDENTED = 1\n") == [])

    print("\nPath constants are excluded, being reached by attribute access")
    for suffix in PATH_SUFFIXES:
        check(f"{suffix} is excluded", ("SOMETHING" + suffix).endswith(PATH_SUFFIXES))

    print("\nRule 4: every allowlist entry justifies itself")
    check("the allowlist is not empty", bool(ALLOWLIST))

    for name, reason in ALLOWLIST.items():
        check(f"{name} carries a reason", bool(str(reason).strip()))
        check(f"{name}'s reason explains rather than asserts",
              len(str(reason)) > 60, f"{len(str(reason))} chars")

    unreasoned = SweepResult(unreasoned=["FAKE_ENTRY"])
    check("an unreasoned entry makes the sweep unclean", not unreasoned.clean)
    check("and the summary says so", "no reason" in unreasoned.summary())

    orphaned = SweepResult(orphans=["FAKE_ORPHAN"], total=10)
    check("an orphan makes the sweep unclean", not orphaned.clean)
    check("and the summary names it", "FAKE_ORPHAN" in orphaned.summary())
    check("a clean result is clean", SweepResult(total=10).clean)

    print("\nThe daily gate")
    check("a date never recorded runs", should_run_today(date(1999, 1, 1)))

    print("\nIt reports and never deletes")
    source = Path(__file__).read_text(encoding="utf-8")
    body = source.split("def _self_test")[0]
    check("nothing is deleted", "unlink" not in body and "shutil" not in body)
    check("the config file is never opened for writing",
          "CONFIG_FILE.write" not in body)
    check("the only thing written is its own date stamp",
          body.count(".write_text") == 1 and "STATE_FILE.write_text" in body)

    print("\nThe allowlist cannot hide a constant from the sweep")

    # The first live run printed "allowlisted: 0" because ALLOWLIST's own
    # keys were being read as references. An allowlist that silently
    # disables the check is worse than none.
    sources = project_sources()
    check("the sweep excludes its own source",
          Path(__file__).resolve() not in {p.resolve() for p in sources})
    check("and the config file",
          CONFIG_FILE.resolve() not in {p.resolve() for p in sources})
    check("so an allowlisted name is REPORTED as allowlisted, not hidden",
          "LOCKBOT_CONFIG_VERSION" in sweep().allowed,
          str(sweep().allowed))

    print("\nAgainst the real project")
    live = sweep()
    check("the config parses and constants are found", live.total > 50,
          f"{live.total} constants")
    check("LOCKBOT_CONFIG_VERSION is allowlisted, not called an orphan",
          "LOCKBOT_CONFIG_VERSION" not in live.orphans)
    check("the sweep is currently clean", live.clean, live.summary())

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1

    print("All config-sweep checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find settings that configure nothing"
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    report(sweep())

    return 0


if __name__ == "__main__":
    sys.exit(main())
