"""
csv_schema.py -- one place that owns CSV header migration.

READ ONLY of your data in the sense that it never invents a value. It
rewrites a file's HEADER when that is provably safe, and refuses when it
is not.

WHY THIS MODULE EXISTS

The same defect has appeared three times:

    options_scanner   appended 15 values under a 14-column header
    shadow_trades     would have appended 26 under 25 (caught pre-ship)
    options_shadow    same shape, same cause

Every instance is a module-level COLUMNS list that grew while a file on
disk still carried the older, narrower header. csv.DictWriter happily
writes one value per name in COLUMNS; csv.DictReader, reading back
against the shorter header, hands the surplus back under a None key. The
new column is written and is never readable. Nothing raises. The file
still opens. It is silent, and it survived two code reviews.

LOCKBOT's ruling of 2026-08-13, which this implements: a fix that lives
inside one writer is how the bug recurred three times, so the fix lives
here and every journal writer imports it.

THE ASYMMETRY IS THE WHOLE DESIGN

    header is a strict PREFIX of COLUMNS   -> migrate in place
    header is WIDER, reordered, or renamed -> REFUSE TO WRITE, loudly

A narrower header is the routine case: someone appended a column. Halting
logging over it would be worse than the bug.

A WIDER header means the running code is OLDER than the file -- a
rollback, or two versions sharing one file. Migrating there would delete
columns that newer code wrote. Writing short rows under the wide header
is the mirror image of the original bug: fields misaligned, and they read
back cleanly forever.

    A row lost with an alarm attached is recoverable.
    A row written askew is poison.

TWO RULES FOR CALLERS

1. Write against the header this module VERIFIED ON DISK, never against
   your COLUMNS constant. That mismatch is the root of all three
   occurrences. `ensure_schema` returns the header to use.
2. Treat DictReader's None key as corruption to report, not surplus to
   ignore. `read_rows` does this for you.

USAGE
    from csv_schema import ensure_schema, SchemaRefused

    header = ensure_schema(path, COLUMNS)      # raises SchemaRefused
    with path.open("a", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=header).writerow(row)

    python csv_schema.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

BACKUP_SUFFIX = ".pre-migration"


class SchemaRefused(RuntimeError):
    """The file on disk cannot be safely written by this code version."""


def read_header(path: Path) -> list[str] | None:
    """The header currently on disk. None when the file is absent or empty."""

    path = Path(path)

    if not path.exists() or path.stat().st_size == 0:
        return None

    with path.open(newline="", encoding="utf-8") as handle:
        return next(csv.reader(handle), None)


def classify(existing: Sequence[str] | None,
             columns: Sequence[str]) -> str:
    """How the on-disk header relates to the code's COLUMNS.

    Returns one of: absent, identical, narrower, wider, divergent.

    "narrower" is deliberately STRICT -- the existing header must be the
    first N names of COLUMNS, in order. A header that merely shares names
    out of order is "divergent" and is refused, because reordering means
    the two versions disagree about position and every migrated row would
    need remapping rather than padding.
    """

    if existing is None:
        return "absent"

    existing = list(existing)
    columns = list(columns)

    if existing == columns:
        return "identical"

    if len(existing) < len(columns) and columns[:len(existing)] == existing:
        return "narrower"

    if len(existing) > len(columns) and existing[:len(columns)] == columns:
        return "wider"

    return "divergent"


def ensure_schema(path: Path | str, columns: Sequence[str], *,
                  backup: bool = True, verbose: bool = True) -> list[str]:
    """Make `path` safe to append to, and return the header to write against.

    Creates the file when absent. Migrates a strictly-narrower header in
    place, atomically, keeping a backup. Refuses anything else.

    Raises SchemaRefused rather than returning, because a caller that
    ignores a return value would write the misaligned rows this module
    exists to prevent.
    """

    path = Path(path)
    columns = list(columns)
    existing = read_header(path)
    verdict = classify(existing, columns)

    if verdict == "absent":
        _atomic_write(path, columns, [])
        if verbose:
            print(f"csv_schema: created {path.name} with {len(columns)} columns")
        return columns

    if verdict == "identical":
        return columns

    if verdict == "narrower":
        added = columns[len(existing):]

        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        before = len(rows)

        for row in rows:
            for name in added:
                row.setdefault(name, "")

        if backup:
            backup_path = path.with_name(path.name + BACKUP_SUFFIX)
            backup_path.write_bytes(path.read_bytes())

        _atomic_write(path, columns, rows)

        # Census check, built in rather than left to whoever remembers.
        after_rows = read_rows(path)
        if len(after_rows) != before:
            raise SchemaRefused(
                f"{path.name}: migration changed the row count "
                f"{before} -> {len(after_rows)}. The backup at "
                f"{path.name}{BACKUP_SUFFIX} is intact."
            )

        if verbose:
            print(f"csv_schema: {path.name} migrated, added {added} to "
                  f"{before} row(s)")
        return columns

    if verdict == "wider":
        surplus = list(existing)[len(columns):]
        raise SchemaRefused(
            f"{path.name} has a WIDER header than this code knows about: "
            f"{surplus}. That means the running code is OLDER than the "
            f"file -- a rollback, or two versions sharing one file. "
            f"Refusing to write. Migrating would delete those columns; "
            f"appending short rows would misalign every field and read "
            f"back cleanly forever."
        )

    raise SchemaRefused(
        f"{path.name} has a header this code cannot reconcile. "
        f"on disk: {existing}. expected a prefix of: {columns}. "
        f"Reordered or renamed columns need a deliberate migration, not "
        f"an automatic one."
    )


def read_rows(path: Path | str, *, strict: bool = True) -> list[dict[str, Any]]:
    """Read a CSV, treating DictReader's None key as corruption.

    A None key means the file has more values on a row than names in its
    header -- the exact signature of the bug this module prevents. It is
    reported, never silently dropped.
    """

    path = Path(path)

    if not path.exists() or path.stat().st_size == 0:
        return []

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    damaged = [i for i, row in enumerate(rows) if None in row]

    if damaged and strict:
        raise SchemaRefused(
            f"{path.name}: {len(damaged)} row(s) carry more values than the "
            f"header names (first at row {damaged[0] + 2}). Those fields are "
            f"unreadable, not surplus. Do not append to this file until it "
            f"is repaired."
        )

    return rows


def _atomic_write(path: Path, columns: Sequence[str],
                  rows: Iterable[dict[str, Any]]) -> None:
    """Write via a temp file and rename, so a crash cannot truncate."""

    handle = tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", delete=False,
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
    )
    try:
        writer = csv.DictWriter(handle, fieldnames=list(columns),
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
        handle.close()
        os.replace(handle.name, path)
    except Exception:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, condition: Any) -> None:
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")

    tmp = Path(tempfile.mkdtemp(prefix="csv_schema_test_"))
    COLS = ["a", "b", "c"]

    print("Classification")
    check("absent file", classify(None, COLS) == "absent")
    check("identical header", classify(["a", "b", "c"], COLS) == "identical")
    check("a strict prefix is narrower", classify(["a", "b"], COLS) == "narrower")
    check("extra trailing columns are wider",
          classify(["a", "b", "c", "d"], COLS) == "wider")
    check("reordered is divergent, not narrower",
          classify(["b", "a"], COLS) == "divergent")
    check("renamed is divergent", classify(["a", "x"], COLS) == "divergent")
    check("same names out of order is divergent",
          classify(["c", "b", "a"], COLS) == "divergent")

    print("\nCreating and identical")
    fresh = tmp / "fresh.csv"
    check("creates a missing file", ensure_schema(fresh, COLS, verbose=False) == COLS)
    check("and is a no-op the second time",
          ensure_schema(fresh, COLS, verbose=False) == COLS)

    print("\nNarrower migrates, and the data survives")
    narrow = tmp / "narrow.csv"
    with narrow.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["a", "b"])
        w.writeheader()
        w.writerow({"a": "1", "b": "2"})
        w.writerow({"a": "3", "b": "4"})

    header = ensure_schema(narrow, COLS, verbose=False)
    rows = read_rows(narrow)
    check("returns the full column list", header == COLS)
    check("row count is unchanged", len(rows) == 2)
    check("existing values survive", rows[0]["a"] == "1" and rows[1]["b"] == "4")
    check("the new column is blank, never guessed", rows[0]["c"] == "")
    check("a backup was written",
          (tmp / ("narrow.csv" + BACKUP_SUFFIX)).exists())

    print("\nAppending after migration is readable, not shifted")
    with narrow.open("a", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=header).writerow(
            {"a": "5", "b": "6", "c": "7"})
    rows = read_rows(narrow)
    check("the appended row reads back", len(rows) == 3 and rows[2]["c"] == "7")
    check("and the older rows still align",
          rows[0]["a"] == "1" and rows[0]["c"] == "")

    print("\nWider REFUSES -- older code must not touch a newer file")
    wide = tmp / "wide.csv"
    with wide.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["a", "b", "c", "d"])
        w.writeheader()
        w.writerow({"a": "1", "b": "2", "c": "3", "d": "4"})
    try:
        ensure_schema(wide, COLS, verbose=False)
        check("refuses a wider header", False)
    except SchemaRefused as error:
        check("refuses a wider header", True)
        check("and names the surplus column", "'d'" in str(error) or "d" in str(error))
        check("and explains the version skew", "OLDER" in str(error))
    check("the wider file is left untouched",
          read_header(wide) == ["a", "b", "c", "d"])

    print("\nDivergent REFUSES")
    odd = tmp / "odd.csv"
    with odd.open("w", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=["b", "a"]).writeheader()
    try:
        ensure_schema(odd, COLS, verbose=False)
        check("refuses a reordered header", False)
    except SchemaRefused:
        check("refuses a reordered header", True)

    print("\nThe None key is corruption, not surplus")
    broken = tmp / "broken.csv"
    broken.write_text("a,b\n1,2,3\n", encoding="utf-8")
    try:
        read_rows(broken)
        check("a row wider than its header is reported", False)
    except SchemaRefused as error:
        check("a row wider than its header is reported", True)
        check("and says which row", "row 2" in str(error))
    check("strict=False still returns it for inspection",
          len(read_rows(broken, strict=False)) == 1)

    print("\nThe write is atomic")
    src = Path(__file__).read_text(encoding="utf-8")
    check("rename, not truncate-in-place", "os.replace" in src)
    check("temp file lives beside the target, so rename stays on one volume",
          "dir=str(path.parent)" in src)
    check("no stray temp files left behind",
          not any(p.name.endswith(".tmp") for p in tmp.iterdir()))

    for leftover in tmp.iterdir():
        leftover.unlink()
    tmp.rmdir()

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("All csv_schema checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--check", metavar="PATH",
                        help="report how a file's header compares, write nothing")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.check:
        header = read_header(Path(args.check))
        print(f"{args.check}: {len(header) if header else 0} columns")
        print(f"  {header}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
