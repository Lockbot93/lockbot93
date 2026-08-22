"""
reconcile.py  --  assume the broker is right and the books might be wrong

WHY THIS EXISTS

    Every expensive defect in the week of 2026-08-14 was the same event
    from the owner's side: LOCKBOT told him something that was not true.

      "you're up $2.95 today"      he was down $23
      "XLF closed at breakeven"    it lost $23
      "that trade cost $0.31"      it cost $0.33
      "the position is protected"  it had no stop for five hours
      "daily loss is -11.56%"      it was -9.50%, and that number
                                   blocked entries for a whole morning

    Each was found late, by hand, usually a day after it mattered. Each
    had a broker record that disagreed with the books the entire time.

    THE INSIGHT THAT BUILT THIS. The check that actually caught the
    entry-price defect was not a code audit -- it was comparing recorded
    limits against actual fills and noticing four orders had filled ABOVE
    their own limit, which is impossible. That is a fact about reality,
    not about code shape, and it would have surfaced the bug whatever
    caused it: duplicated logic, wrong logic, or the broker behaving in a
    way nobody predicted.

    A pattern-matching audit only finds shapes somebody already thought
    to describe. This finds disagreements.

THE STANCE

    The broker is the source of truth. Every disagreement is reported as
    the books being wrong, because they are the thing that can be wrong
    in a way that costs money. If the broker is genuinely the one at
    fault, that is worth knowing too and reads the same way here.

WHAT IT WILL NOT DO

    Repair anything. It reports. A reconciler that corrects the books
    silently is a reconciler that can hide the defect it was built to
    expose -- and one that corrects a POSITION is placing trades.

USAGE
    python reconcile.py             compare the books against the broker
    python reconcile.py --self-test offline checks
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import lockbot_config as config

VERSION = "1.0"

# A disagreement smaller than this is rounding, not a defect. Option
# dollars round to the cent, so anything at or under a cent is noise.
CENT = 0.011


@dataclass
class Disagreement:
    what: str
    books: Any
    broker: Any
    detail: str = ""
    severity: str = "WARN"          # WARN | CRITICAL

    def __str__(self) -> str:
        return (f"{self.what}: books say {self.books}, "
                f"broker says {self.broker}"
                + (f" -- {self.detail}" if self.detail else ""))


@dataclass
class Reconciliation:
    checks_run: int = 0
    disagreements: list[Disagreement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def agree(self) -> bool:
        return not self.disagreements

    @property
    def critical(self) -> list[Disagreement]:
        return [d for d in self.disagreements if d.severity == "CRITICAL"]

    def summary(self) -> str:
        if self.agree:
            return f"Books and broker agree across {self.checks_run} checks."

        return (f"{len(self.disagreements)} disagreement(s) across "
                f"{self.checks_run} checks: "
                + "; ".join(d.what for d in self.disagreements))

    def fingerprint(self) -> str:
        """Transitions only -- a daily identical line is one nobody reads."""

        return "|".join(sorted(f"{d.what}:{d.books}:{d.broker}"
                               for d in self.disagreements))


def _rows(path: Any) -> list[dict[str, str]]:
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def _f(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The checks. Each returns disagreements; none repairs anything.
# ---------------------------------------------------------------------------

def check_positions(client: Any, out: Reconciliation) -> None:
    """Does LOCKBOT track what the broker actually holds?

    The XLF spread sat at the broker for five and a half hours with
    nothing tracking it, which for an option means no stop of any kind.
    """

    import position_filters

    out.checks_run += 1

    held = {str(p.symbol).upper()
            for p in position_filters.option_positions(client.get_all_positions())}

    try:
        state = json.loads(
            Path(config.OPTIONS_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    tracked: set[str] = set()

    for position in state.values():
        for key in ("long_symbol", "short_symbol"):
            symbol = position.get(key)

            if symbol:
                tracked.add(str(symbol).upper())

    orphaned = held - tracked
    ghosts = tracked - held

    if orphaned:
        out.disagreements.append(Disagreement(
            "untracked option legs", "not held", sorted(orphaned),
            "options have no broker-side stop, so these are unprotected",
            severity="CRITICAL"))

    if ghosts:
        out.disagreements.append(Disagreement(
            "tracked legs the broker does not hold", sorted(ghosts), "none",
            "the book claims a position that is gone"))


def check_fills_against_limits(out: Reconciliation) -> None:
    """Did anything fill ABOVE its own limit?

    Impossible for a limit order, and therefore proof that the price
    submitted was not the price recorded. This is the check that caught
    the entry-pricing defect on 2026-08-21.
    """

    out.checks_run += 1

    path = getattr(config, "EXECUTION_LIMIT_ATTEMPTS_FILE",
                   config.PROJECT_FOLDER / "execution_limit_attempts.csv")

    for row in _rows(path):
        limit, fill = _f(row.get("limit_price")), _f(row.get("fill_price"))

        if limit is None or fill is None:
            continue

        if fill > limit + CENT:
            out.disagreements.append(Disagreement(
                f"{row.get('symbol', '?')} filled above its own limit",
                f"limit {limit:.4f}", f"fill {fill:.4f}",
                "a limit order cannot do this -- the submitted price was "
                "not the recorded price",
                severity="CRITICAL"))


def check_journal_against_orders(client: Any, out: Reconciliation) -> None:
    """Does each closed trade's recorded exit match the order that closed it?

    The XLF spread was journalled at a $32 credit -- a breakeven that
    never happened -- because the close path used the position's
    high-water mark instead of the fill.
    """

    out.checks_run += 1

    rows = _rows(config.OPTIONS_COMPLETED_FILE)

    if not rows:
        out.notes.append("no closed option trades to check")
        return

    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    try:
        orders = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, limit=500))
    except Exception as error:                          # noqa: BLE001
        out.notes.append(f"could not read broker orders: "
                         f"{type(error).__name__}")
        return

    # Closing fills, keyed by the legs they touched.
    closes: dict[str, list[float]] = {}

    for order in orders:
        price = _f(getattr(order, "filled_avg_price", None))

        if price is None:
            continue

        for leg in (order.legs or []):
            closes.setdefault(str(leg.symbol).upper(), []).append(abs(price))

    for row in rows[-20:]:                              # recent trades only
        recorded = _f(row.get("exit_credit"))
        symbol = str(row.get("long_symbol") or "").upper()

        if recorded is None or symbol not in closes:
            continue

        # The exit is the closing fill; entry fills are also in this list,
        # so match on the closest rather than assuming order.
        candidates = [p * 100 for p in closes[symbol]]
        nearest = min(candidates, key=lambda p: abs(p - recorded))

        if abs(nearest - recorded) > max(CENT, recorded * 0.02):
            out.disagreements.append(Disagreement(
                f"{row.get('underlying', '?')} recorded exit",
                f"${recorded:.2f}",
                f"nearest broker fill ${nearest:.2f}",
                "the journalled exit does not match any fill on this leg"))


def check_cash_against_journal(client: Any, out: Reconciliation) -> None:
    """Does the day's journalled P&L explain the day's equity move?

    A gap here is what a fabricated ledger row looks like from outside.
    On 2026-08-20 the books showed -$24 against a -$59 account move; the
    missing $23 was XLF booked as breakeven, and that wrong number
    blocked entries all morning.
    """

    out.checks_run += 1

    from datetime import datetime, timezone

    account = client.get_account()
    equity, previous = _f(account.equity), _f(account.last_equity)

    if equity is None or previous is None or previous <= 0:
        out.notes.append("no previous close to compare against")
        return

    moved = equity - previous

    # WHICH SESSION DOES THAT MOVE BELONG TO?
    #
    # Not necessarily today. last_equity is the broker's previous close and
    # it does not roll while the market is shut, so on a Saturday the move
    # still describes Friday. Comparing it against today's (empty) journal
    # reported a $52 discrepancy on the first run of this module -- a clean
    # false positive, and precisely the noise that teaches a reader to skip
    # the report. The check is worth nothing if it cries wolf on a weekend.
    #
    # So: the move is attributed to the most recent session that actually
    # has journalled trades, unless the market is open now, in which case
    # it is today's by definition.
    rows = _rows(config.OPTIONS_COMPLETED_FILE)
    dates = sorted({(r.get("exit_time") or "")[:10] for r in rows
                    if (r.get("exit_time") or "").strip()}, reverse=True)

    today = datetime.now(timezone.utc).date().isoformat()

    try:
        market_open = bool(client.get_clock().is_open)
    except Exception:                                   # noqa: BLE001
        market_open = False

    if market_open or today in dates or not dates:
        session = today
    else:
        session = dates[0]
        out.notes.append(
            f"market closed; attributing the equity move to {session}, "
            "the last session with trades")

    realised = sum(
        _f(r.get("profit_loss")) or 0.0
        for r in rows
        if (r.get("exit_time") or "")[:10] == session)

    import position_filters

    unrealised = sum(
        _f(getattr(p, "unrealized_intraday_pl", None)) or 0.0
        for p in position_filters.option_positions(client.get_all_positions()))

    explained = realised + unrealised
    gap = moved - explained

    # Only worth reporting when the gap is material against the account.
    if abs(gap) > max(1.0, abs(previous) * 0.01):
        out.disagreements.append(Disagreement(
            "today's move is not explained by the books",
            f"${explained:+.2f} journalled",
            f"${moved:+.2f} at the broker",
            f"${gap:+.2f} unaccounted for"))


def reconcile(client: Any = None) -> Reconciliation:
    """Compare every book LOCKBOT keeps against what the broker says."""

    out = Reconciliation()

    if client is None:
        from dotenv import load_dotenv
        from lockbot_startup_reconciliation import get_trading_client

        load_dotenv(dotenv_path=str(config.PROJECT_FOLDER / ".env"))
        client = get_trading_client()

    for check in (
        lambda: check_positions(client, out),
        lambda: check_fills_against_limits(out),
        lambda: check_journal_against_orders(client, out),
        lambda: check_cash_against_journal(client, out),
    ):
        try:
            check()
        except Exception as error:                      # noqa: BLE001
            out.notes.append(f"a check failed: {type(error).__name__}: {error}")

    return out


def report(out: Reconciliation) -> None:
    print("=" * 68)
    print(f"RECONCILIATION v{VERSION} - the broker is right, the books may not be")
    print("=" * 68)

    for note in out.notes:
        print(f"  note: {note}")

    if out.agree:
        print(f"  {out.summary()}")
        print()
        print("  Agreement means the numbers LOCKBOT reports can be acted on")
        print("  without checking them by hand first. It does not mean the")
        print("  strategy is working.")
        return

    for d in out.disagreements:
        mark = "!!" if d.severity == "CRITICAL" else " *"
        print(f"  {mark} {d}")

    print()
    print(f"  {out.summary()}")
    print("  Reported, never repaired -- a reconciler that fixes the books")
    print("  can hide the defect it exists to expose.")


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}"
              + (f" - {detail}" if detail and not condition else ""))

    print("A fill above its own limit is impossible and must be caught")
    out = Reconciliation()
    original = getattr(config, "EXECUTION_LIMIT_ATTEMPTS_FILE", None)

    import tempfile

    folder = Path(tempfile.mkdtemp())
    path = folder / "attempts.csv"

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["symbol", "limit_price", "fill_price"])
        writer.writeheader()
        # The real 2026-08-21 numbers.
        writer.writerow({"symbol": "SOFI", "limit_price": "0.3100",
                         "fill_price": "0.3300"})
        writer.writerow({"symbol": "TLT", "limit_price": "0.3000",
                         "fill_price": "0.2700"})

    config.EXECUTION_LIMIT_ATTEMPTS_FILE = path

    try:
        check_fills_against_limits(out)
        check("the impossible fill is flagged", len(out.disagreements) == 1,
              str([str(d) for d in out.disagreements]))
        check("and it is CRITICAL",
              out.disagreements and out.disagreements[0].severity == "CRITICAL")
        check("a fill BELOW its limit is not flagged",
              all("TLT" not in d.what for d in out.disagreements))
    finally:
        if original is not None:
            config.EXECUTION_LIMIT_ATTEMPTS_FILE = original
        else:
            delattr(config, "EXECUTION_LIMIT_ATTEMPTS_FILE")

        import shutil

        shutil.rmtree(folder, ignore_errors=True)

    print("\nA cent of drift is rounding, not a defect")
    out2 = Reconciliation()
    folder2 = Path(tempfile.mkdtemp())
    path2 = folder2 / "a.csv"

    with open(path2, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["symbol", "limit_price", "fill_price"])
        writer.writeheader()
        writer.writerow({"symbol": "X", "limit_price": "0.3000",
                         "fill_price": "0.3001"})

    config.EXECUTION_LIMIT_ATTEMPTS_FILE = path2

    try:
        check_fills_against_limits(out2)
        check("a hundredth of a cent is ignored", out2.agree,
              str([str(d) for d in out2.disagreements]))
    finally:
        if original is not None:
            config.EXECUTION_LIMIT_ATTEMPTS_FILE = original

        import shutil

        shutil.rmtree(folder2, ignore_errors=True)

    print("\nIt reports and repairs NOTHING")
    body = Path(__file__).read_text(encoding="utf-8").split("def _self_test")[0]
    check("no order submission", "submit_order" not in body)
    check("no position is closed", "close_position" not in body)
    check("no book is rewritten",
          'open(' not in body.replace('open(path, newline=', 'READ(')
          .replace('open(path, "w", newline=', 'X(') or 'writerow' not in body)
    check("the stance is stated", "broker is the source of truth" in
          (__doc__ or "").lower() or "broker is right" in (__doc__ or "").lower())

    print("\nTransitions only, so a daily identical line is not reported")
    a = Reconciliation(disagreements=[Disagreement("x", 1, 2)])
    b = Reconciliation(disagreements=[Disagreement("x", 1, 2)])
    c = Reconciliation(disagreements=[Disagreement("x", 1, 3)])
    check("identical findings share a fingerprint",
          a.fingerprint() == b.fingerprint())
    check("a changed finding does not", a.fingerprint() != c.fingerprint())
    check("agreement is empty", Reconciliation().fingerprint() == "")

    print("\nAgainst the live account")
    live = reconcile()
    check("every check ran", live.checks_run == 4, str(live.checks_run))
    check("the result is reportable", isinstance(live.summary(), str))
    print(f"        {live.summary()}")

    for d in live.disagreements:
        print(f"        found: {d}")

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1

    print("All reconciliation checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare LOCKBOT's books against the broker")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    report(reconcile())

    return 0


if __name__ == "__main__":
    sys.exit(main())
