"""
build_universe.py  --  rebuild the daily symbol list, in the right order

WHY THIS WRAPPER EXISTS
    universe.csv is produced by two scripts that MUST run in sequence:

        universe.py             rewrites the file from scratch
        universe_volatility.py  reads it back and filters by movement

    Run the other way round, the volatility filter's work is discarded by
    the very next step. The ordering is documented in lockbot_config.py,
    which is the kind of place a scheduler configuration never looks — so
    this file makes the order structural rather than a thing somebody has
    to remember when creating a task.

    It also means the scheduler needs one entry instead of two chained
    ones, and a failure in the first step stops the second rather than
    filtering a stale list.

WHY IT MATTERS
    market_scanner.py takes its symbols from universe.csv and nothing
    else writes it. With no schedule, that file simply ages: on
    2026-07-30 it was 20.8 hours old and the scan had shrunk to 47
    symbols against a configured cap of 150. LOCKBOT was hunting across
    under a third of the market it was set up for, and nothing reported
    it as a fault because nothing was broken — the work just was not
    being done.

USAGE
    python build_universe.py
    python build_universe.py --dry-run     build but do not overwrite
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_FOLDER = Path(__file__).resolve().parent

# Order is load-bearing. universe.py first, always.
STEPS = (
    ("universe.py", "build the ranked symbol list"),
    ("universe_volatility.py", "drop symbols that cannot reach the target"),
)


def run_step(script: str, purpose: str, extra: list[str]) -> bool:
    """Run one step. Returns False on failure so the caller can stop."""

    path = PROJECT_FOLDER / script

    if not path.exists():
        print(f"  {script}: NOT FOUND — skipping the rest.")
        return False

    print(f"\n--- {script} ({purpose}) ---")

    result = subprocess.run(
        [sys.executable, str(path), *extra],
        cwd=str(PROJECT_FOLDER),
    )

    if result.returncode != 0:
        print(f"  {script} exited {result.returncode}.")
        return False

    return True


def main() -> int:
    extra = ["--dry-run"] if "--dry-run" in sys.argv else []

    print("=" * 58)
    print("LOCKBOT UNIVERSE REBUILD")
    print(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
    print("=" * 58)

    for script, purpose in STEPS:
        if not run_step(script, purpose, extra):
            print("\nRebuild INCOMPLETE. universe.csv may be stale or partial.")
            return 1

    universe = PROJECT_FOLDER / "universe.csv"

    if universe.exists():
        try:
            rows = len(universe.read_text(encoding="utf-8-sig").splitlines()) - 1
        except OSError:
            rows = -1

        print("\n" + "=" * 58)
        print(f"universe.csv now holds {rows} symbol(s).")
        print("=" * 58)

    return 0


if __name__ == "__main__":
    sys.exit(main())
