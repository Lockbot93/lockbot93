"""
score_733.py  --  score the held-out shadow rows against frozen predictions

WHY THIS EXISTS

    PREREG_SHADOW_733.md fixes three predictions about 733 unscored rows
    in the equity shadow book. This scores them, once, and reports.

WHAT MAKES THE FREEZE REAL

    The predictions were committed ALONE, in their own commit, before this
    file existed. This tool prints that commit hash and the CURRENT hash
    of the prediction file. If someone edits a prediction after seeing a
    number, the hashes stop matching and the run says so in its own
    output.

    That is the whole protection. A held-out test is worth exactly the
    difficulty of changing the prediction afterwards, and a promise is not
    difficult to change.

THE TWO HAZARDS IT REFUSES

    PARTIAL READS. Stops book within hours; expiries take the full
    SHADOW_MAX_DAYS window. So a slice read before its rows mature is
    biased toward losses BY CONSTRUCTION -- an error with a known sign,
    which is worse than noise because it moves the answer while looking
    confident. shadow_trades.cohort_maturity refuses those slices and this
    tool refuses to report a number it cannot stand behind.

    CORRELATED ROWS COUNTED AS INDEPENDENT. On 2026-08-29 an analysis
    reported +0.10R on a slice, then retracted: 54 crypto-linked rows
    carried all of it. Every figure here is printed twice, with and
    without the exposure groups, and the predictions are judged on the
    excluded set.

WHAT IT WILL NOT DO

    Submit an order. Write to shadow_trades.csv. Change a setting. It
    reads, scores and prints.

USAGE
    python score_733.py --self-test
    python score_733.py                 score and report
"""

from __future__ import annotations

import argparse
import csv
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import exposure_groups

VERSION = "1.0"

HERE = Path(__file__).resolve().parent
PREREG = HERE / "PREREG_SHADOW_733.md"

# The commit the predictions were frozen at, recorded here at build time.
# Printed alongside the live hash so a later edit is visible.
FROZEN_AT = "c66137a922348dda0b0f08a48db501acf91f4ae9"

# 3:00pm ET. The shadow log stamps UTC, and US equity sessions run
# 13:30-20:00 UTC under EDT, so 19:00 UTC is 3pm ET.
LATE_HOUR_UTC = 19


def as_float(value):
    """A number, or None. Never 0.0 for unusable input -- a booked zero
    would enter an average as a flat trade that never happened."""

    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def freeze_status() -> tuple[bool, str]:
    """Does the prediction file still match what was frozen?"""

    if not PREREG.exists():
        return False, "the pre-registration file is missing"

    try:
        log = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", PREREG.name],
            capture_output=True, text=True, timeout=60, cwd=str(HERE))
        touched = (log.stdout or "").strip()
    except Exception as error:                              # noqa: BLE001
        return False, f"could not read git history ({type(error).__name__})"

    if not touched:
        return False, "the pre-registration is not committed"

    if touched != FROZEN_AT:
        return False, (f"the pre-registration was last changed in {touched[:8]}, "
                       f"not the frozen {FROZEN_AT[:8]} -- a prediction has "
                       "been edited since it was frozen")

    return True, f"predictions frozen at {FROZEN_AT[:8]}, unchanged since"


def load_scored(path: Path) -> list[dict]:
    """Rows that now carry an outcome and an R multiple."""

    if not path.exists():
        return []

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    out = []

    import shadow_trades

    for row in rows:
        # Only genuinely decided rows. UNRESOLVED and PENDING are literal
        # values here, so an emptiness test would have scored 757
        # unfinished setups as if they had outcomes.
        if (row.get("outcome") or "").strip().upper()                 not in shadow_trades.DECIDED_OUTCOMES:
            continue

        if as_float(row.get("r_multiple")) is None:
            continue

        out.append(row)

    return out


def is_late(row: dict) -> bool | None:
    """Entered from 3pm ET onward. None when the stamp cannot be read."""

    stamp = (row.get("logged_at") or "").strip()

    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None

    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    return when.astimezone(timezone.utc).hour >= LATE_HOUR_UTC


def drop_correlated(rows: list[dict]) -> list[dict]:
    """Remove every row in a multi-ticker exposure group.

    Not just crypto. Any symbol sharing a group with another is dropped,
    because the hazard is correlation, not the asset class that happened
    to expose it.
    """

    grouped = {sym for sym, (grp, _) in exposure_groups.GROUPS.items()}

    return [r for r in rows
            if (r.get("symbol") or "").strip().upper() not in grouped]


def mean_r(rows: list[dict]) -> float | None:
    values = [as_float(r.get("r_multiple")) for r in rows]
    values = [v for v in values if v is not None]

    return statistics.mean(values) if values else None


def target_rate(rows: list[dict]) -> float | None:
    if not rows:
        return None

    hits = sum(1 for r in rows
               if (r.get("outcome") or "").strip().upper() == "TARGET")

    return hits / len(rows)


def _self_test() -> int:
    failures: list[str] = []

    def check(label, condition, detail=""):
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}"
              + (f" - {detail}" if detail and not condition else ""))

    print("3pm ET is read from a UTC stamp")
    check("18:59 UTC is early",
          is_late({"logged_at": "2026-08-20T18:59:00+00:00"}) is False)
    check("19:00 UTC is late",
          is_late({"logged_at": "2026-08-20T19:00:00+00:00"}) is True)
    check("an unreadable stamp is None, not early",
          is_late({"logged_at": "nope"}) is None)

    print()
    print("Correlated rows are dropped by GROUP, not by asset class")
    rows = [{"symbol": "IBIT"}, {"symbol": "BITO"}, {"symbol": "LQD"},
            {"symbol": "AGG"}, {"symbol": "NVDA"}, {"symbol": "AAPL"}]
    kept = drop_correlated(rows)
    check("bitcoin names dropped",
          not any(r["symbol"] in ("IBIT", "BITO") for r in kept))
    # The hazard is correlation, not crypto. Bonds group too.
    check("bond names dropped as well",
          not any(r["symbol"] in ("LQD", "AGG") for r in kept),
          str([r["symbol"] for r in kept]))
    check("ungrouped names survive",
          {r["symbol"] for r in kept} == {"NVDA", "AAPL"},
          str([r["symbol"] for r in kept]))

    print()
    print("Averages refuse unusable input rather than booking a zero")
    check("a missing r_multiple is not counted as 0.0",
          mean_r([{"r_multiple": "1.0"}, {"r_multiple": ""}]) == 1.0)
    check("no usable rows gives None, not 0.0",
          mean_r([{"r_multiple": ""}]) is None)
    check("an empty slice gives None", mean_r([]) is None)
    check("target rate on nothing is None", target_rate([]) is None)
    check("target rate counts only TARGET",
          target_rate([{"outcome": "TARGET"}, {"outcome": "STOP"}]) == 0.5)

    print()
    print("The freeze is checked, not asserted")
    ok, why = freeze_status()
    check("the prediction file is still the frozen one", ok, why)

    print()
    print("It reads and reports, nothing more")
    src = Path(__file__).read_text(encoding="utf-8").split("def _self_test")[0]
    check("no order submission", "submit_order" not in src)
    check("it never writes the shadow book",
          'shadow_trades.csv"' not in src.replace('path.open(newline=', ''))
    check("no config writes", "lockbot_config" not in src)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All score-733 checks passed.")
    return 0


def report(rows: list[dict], label: str) -> dict:
    """Print one arm and return its numbers."""

    early = [r for r in rows if is_late(r) is False]
    late = [r for r in rows if is_late(r) is True]

    out = {
        "n": len(rows),
        "all": mean_r(rows),
        "early_n": len(early), "early": mean_r(early),
        "late_n": len(late), "late": mean_r(late),
    }
    out["gap"] = (None if out["early"] is None or out["late"] is None
                  else out["early"] - out["late"])

    def fmt(v, spec="+.3f"):
        return "n/a" if v is None else format(v, spec)

    print(f"  {label}")
    print(f"    all           {out['n']:>5} rows   mean R {fmt(out['all'])}")
    print(f"    before 3pm ET {out['early_n']:>5} rows   mean R {fmt(out['early'])}")
    print(f"    3pm ET on     {out['late_n']:>5} rows   mean R {fmt(out['late'])}")
    print(f"    gap (early - late)              {fmt(out['gap'])}")

    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score the held-out shadow rows")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    print(f"SCORE 733 v{VERSION}")

    frozen, why = freeze_status()
    print(f"  freeze: {'OK' if frozen else 'BROKEN'} -- {why}")

    if not frozen:
        print()
        print("  REFUSING TO SCORE. A held-out test whose prediction may have")
        print("  moved is not a held-out test.")
        return 1

    import shadow_trades

    path = HERE / "shadow_trades.csv"
    scored = load_scored(path)
    print(f"  {len(scored)} rows carry an outcome and an R multiple")

    mature, detail = shadow_trades.cohort_maturity(scored)
    print(f"  maturity: {'OK' if mature else 'BLOCKED'} -- {detail}")

    if not mature:
        print()
        print("  REFUSING TO REPORT. Stops resolve fastest, so an immature")
        print("  slice is biased toward losses by construction. Re-run when")
        print("  every row has passed its window.")
        return 1

    print()
    both = report(scored, "WITH correlated names")
    print()
    clean = report(drop_correlated(scored), "WITHOUT correlated names (JUDGED)")

    print()
    print("  VERDICT against the predictions frozen at "
          f"{FROZEN_AT[:8]}, judged on the excluded set:")

    gap = clean["gap"]
    p1 = gap is not None and gap >= 0.15
    print(f"    P1  early beats late by >= 0.15R      "
          f"{'PASS' if p1 else 'FAIL'}  "
          f"({'n/a' if gap is None else format(gap, '+.3f')})")

    p3 = clean["all"] is not None and clean["all"] < 0
    print(f"    P3  the book stays negative           "
          f"{'PASS' if p3 else 'FAIL'}  "
          f"({'n/a' if clean['all'] is None else format(clean['all'], '+.3f')})")

    print("    P2  floor-pinned stops                 needs the bracket join")
    print()
    print("  P2 and the sub-period sign check are not computed here. Both")
    print("  need joins this tool does not do, and reporting a partial")
    print("  verdict as a full one is the failure this whole exercise is")
    print("  about.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
