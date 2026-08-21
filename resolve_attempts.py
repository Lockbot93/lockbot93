"""
resolve_attempts.py  --  close the loop on entry-limit attempts

WHY THIS EXISTS

    options_scanner writes one row per submitted entry limit: the price,
    both sides of the quote, and the fraction in force. It deliberately
    leaves the OUTCOME blank, because at submission nobody knows it.

    Nothing ever filled it in. On 2026-08-20 the report read:

        attempts 4   filled 0   unfilled 0   unknown 4

    LOCKBOT approved OPTIONS_ENTRY_LIMIT_FRACTION = 0.5 on the condition
    that its effect be measured (channel 80b8a35f). The attempt log was
    built, the reader was built, the registry floor was set at n >= 50 --
    and the middle step, the one that turns an attempt into an
    observation, did not exist. The loop could accumulate rows forever
    and never produce a verdict.

    That is the week's defect in its purest form: measurement that cannot
    complete. It is the reason MEASUREMENT_STALLED is a verdict in
    rule_registry rather than a footnote.

WHAT IT DOES

    Reads the attempts whose outcome is blank, asks the broker what
    happened to each order, and writes back filled, fill_price and
    seconds_to_fill.

WHAT IT WILL NOT DO

    Guess. An order still working is left blank rather than marked
    unfilled -- attempts_from_rows maps blank to None, which counts as
    UNKNOWN in the denominator and never as a miss. Marking a live order
    "unfilled" would manufacture a fill-rate failure for every order that
    simply had not finished yet.

    It writes no orders, cancels nothing, and touches only this one file.

USAGE
    python resolve_attempts.py             resolve what can be resolved
    python resolve_attempts.py --self-test offline checks
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lockbot_config as config

VERSION = "1.0"

# Statuses that mean the order ended without buying anything. Mirrors
# options_manager.TERMINAL_WITHOUT_FILL deliberately -- the same question
# ("is this order dead?") must not be answered two different ways in two
# files. See the 2026-08-19 XLF incident for what divergence costs.
TERMINAL_WITHOUT_FILL = {
    "canceled", "cancelled", "expired", "rejected", "done_for_day",
}


def attempts_path() -> Path:
    return Path(getattr(
        config, "EXECUTION_LIMIT_ATTEMPTS_FILE",
        config.PROJECT_FOLDER / "execution_limit_attempts.csv"))


def _status(order: Any) -> str:
    return str(getattr(order.status, "value", order.status)).lower()


def classify(order: Any) -> tuple[bool | None, float | None, str]:
    """(filled, fill_price, why) for one order. None means still unknown."""

    status = _status(order)

    try:
        qty = float(getattr(order, "filled_qty", 0) or 0)
    except (TypeError, ValueError):
        qty = 0.0

    if qty > 0 or status == "filled":
        price = getattr(order, "filled_avg_price", None)

        try:
            price = abs(float(price)) if price is not None else None
        except (TypeError, ValueError):
            price = None

        return True, price, "filled"

    if status in TERMINAL_WITHOUT_FILL:
        return False, None, status

    # Still live. Blank, not False -- see the module docstring.
    return None, None, f"still {status}"


def seconds_between(submitted: str, filled: Any) -> float | None:
    """Time to fill, or None when either end is missing."""

    if not submitted or filled is None:
        return None

    try:
        start = datetime.fromisoformat(submitted)

        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)

        end = filled if isinstance(filled, datetime) else \
            datetime.fromisoformat(str(filled))

        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        return max(0.0, (end - start).total_seconds())
    except (TypeError, ValueError):
        return None


def resolve(trading_client: Any = None, verbose: bool = True) -> dict[str, Any]:
    """Fill in the outcome of every attempt the broker can answer for."""

    path = attempts_path()
    summary = {"total": 0, "already": 0, "resolved": 0,
               "still_working": 0, "unreadable": 0}

    if not path.exists():
        if verbose:
            print(f"  no attempt log at {path.name}")
        return summary

    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    if not rows:
        return summary

    summary["total"] = len(rows)

    if trading_client is None:
        from dotenv import load_dotenv
        from lockbot_startup_reconciliation import get_trading_client

        load_dotenv(dotenv_path=str(config.PROJECT_FOLDER / ".env"))
        trading_client = get_trading_client()

    changed = False

    for row in rows:
        if (row.get("filled") or "").strip():
            summary["already"] += 1
            continue

        order_id = (row.get("attempt_id") or "").strip()

        if not order_id:
            summary["unreadable"] += 1
            continue

        try:
            order = trading_client.get_order_by_id(order_id)
        except Exception as error:                      # noqa: BLE001
            summary["unreadable"] += 1

            if verbose:
                print(f"  {order_id[:8]}: unreadable "
                      f"({type(error).__name__})")
            continue

        filled, price, why = classify(order)

        if filled is None:
            summary["still_working"] += 1

            if verbose:
                print(f"  {order_id[:8]}: {why} — left blank, not counted "
                      "as a miss")
            continue

        row["filled"] = "true" if filled else "false"

        if price is not None:
            row["fill_price"] = f"{price:.4f}"

        secs = seconds_between(row.get("timestamp", ""),
                               getattr(order, "filled_at", None))

        if secs is not None:
            row["seconds_to_fill"] = f"{secs:.0f}"

        changed = True
        summary["resolved"] += 1

        if verbose:
            print(f"  {order_id[:8]}: {why}"
                  + (f" at {price:.4f}" if price is not None else "")
                  + (f", {secs:.0f}s" if secs is not None else ""))

    if changed:
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    return summary


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}"
              + (f" - {detail}" if detail and not condition else ""))

    class _O:
        def __init__(self, status, qty=0, price=None, filled_at=None):
            self.status = status
            self.filled_qty = qty
            self.filled_avg_price = price
            self.filled_at = filled_at

    print("A live order is left BLANK, never marked unfilled")
    for status in ("new", "accepted", "pending_new", "pending_review",
                   "calculated", "held", "some_future_status"):
        filled, _, _ = classify(_O(status))
        check(f"{status} stays unknown", filled is None, str(filled))

    print("\nOnly a terminal status without a fill counts as a miss")
    for status in ("canceled", "expired", "rejected", "done_for_day"):
        filled, _, _ = classify(_O(status))
        check(f"{status} is a miss", filled is False, str(filled))

    print("\nA fill is a fill, whatever the status says")
    f, p, _ = classify(_O("filled", qty=1, price=0.33))
    check("filled with a price", f is True and p == 0.33, f"{f} {p}")
    f2, _, _ = classify(_O("pending_cancel", qty=1, price=0.30))
    check("a partial fill during a cancel is FILLED", f2 is True, str(f2))
    f3, p3, _ = classify(_O("filled", qty=1, price=-0.16))
    check("a signed multi-leg price is read as magnitude", p3 == 0.16, str(p3))

    print("\nTime to fill")
    secs = seconds_between("2026-08-20T14:25:38+00:00",
                           "2026-08-20T14:32:57+00:00")
    check("computed across the two stamps", secs == 439.0, str(secs))
    check("missing either end gives None",
          seconds_between("", None) is None
          and seconds_between("2026-08-20T14:25:38+00:00", None) is None)
    check("garbage gives None, never 0",
          seconds_between("not-a-date", "also-not") is None)

    print("\nIt agrees with options_manager on what 'dead' means")
    try:
        import options_manager

        check("the terminal sets are identical",
              TERMINAL_WITHOUT_FILL == options_manager.TERMINAL_WITHOUT_FILL,
              f"{TERMINAL_WITHOUT_FILL ^ options_manager.TERMINAL_WITHOUT_FILL}")
    except Exception as error:                          # noqa: BLE001
        check(f"options_manager importable ({error})", False)

    print("\nIt writes nothing but the attempt log")
    body = Path(__file__).read_text(encoding="utf-8").split("def _self_test")[0]
    check("no order submission", "submit_order" not in body)
    check("no cancellation", "cancel_order" not in body)
    check("one file written", body.count('open(path, "w"') == 1)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1

    print("All resolve-attempts checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write back what happened to each entry limit")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    print(f"RESOLVE ENTRY ATTEMPTS v{VERSION}")
    summary = resolve()

    print(f"\n  {summary['total']} attempt(s): "
          f"{summary['already']} already resolved, "
          f"{summary['resolved']} newly resolved, "
          f"{summary['still_working']} still working, "
          f"{summary['unreadable']} unreadable")

    return 0


if __name__ == "__main__":
    sys.exit(main())
