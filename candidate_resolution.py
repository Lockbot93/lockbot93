"""
candidate_resolution.py  --  find out what the setups LOCKBOT SKIPPED did

WHY THIS EXISTS

    Asked on 2026-08-25 what stood between it and actually improving as a
    trader, LOCKBOT answered: an input with information in it, "the ability
    to test my own gates against what they reject", and rules left standing
    long enough to be judged. This is the second one.

    LOCKBOT ranks roughly forty setups a session and buys about one. The
    other thirty-nine are written to options_shadow_log and never looked at
    again -- 1,955 rows as of tonight. Every gate it owns (delta, spread,
    IV, event risk, cooldown, affordability) is measured only by what it
    ACCEPTS. Nothing has ever asked whether the things it threw away were
    better than the things it kept.

    That is the difference between being well instrumented and learning.

THE CORRECTION THAT SHAPES THIS MODULE

    I described those 1,955 rows as resolvable option trades. They are not.
    A CANDIDATE row is written BEFORE contract selection, so it carries no
    contract and no debit:

        CANDIDATE         1955 rows    0 with a contract
        SHADOW              72         72
        EVENT_RISK           8          8
        SPREAD_NOT_TAKEN     4          4

    Only ~90 rows name a contract. Resolving those needs historical option
    bars for expired contracts, which is the pull-cost problem that killed
    the order-flow family at a measured ~158 hours.

    But every CANDIDATE row names an UNDERLYING and a TIME, and daily stock
    bars are cheap and already in use. So this resolves the question the
    signal is actually making a claim about -- direction of the stock --
    which is also what LOCKBOT specified the skew verdict must be measured
    on, precisely because option P&L confounds selection with spread, theta
    and the exit bands.

WHAT IT WILL NOT DO

    Submit anything. Rewrite the source log -- resolved output goes to a
    SEPARATE derived file keyed by row, so the raw record stays raw and a
    bad resolution pass can be deleted and re-run. Derive a rule: per
    LOCKBOT's boundary on 24e5b01d, "no rule, threshold, or tiering may be
    derived from the resolved candidate pool without a fresh pre-registered
    test on untouched data". This produces evidence, not permission.

THE CONTROL IS NOT OPTIONAL

    A rank is only meaningful against something. Every resolved row carries
    a seeded random pick from the SAME session's candidates, so the
    question is "did the ranking beat picking one at random from what was
    already in front of it" -- not "did the stock go up", which in a rising
    market it usually did. Judging against breakeven instead of a control
    is the benchmark trap that cost this project the r0315 result.

USAGE
    python candidate_resolution.py --self-test
    python candidate_resolution.py --limit 200
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import lockbot_config as config

VERSION = "1.0"

# Fixed at the module level and never derived from a result. Changing it
# after seeing an outcome is the re-cut the pre-registrations forbid.
CONTROL_SEED = 20260825

HORIZONS = (1, 3, 5)

COLUMNS = [
    "row_key", "timestamp", "underlying", "action", "rank",
    "regime", "signal", "quality", "skew", "signal_source",
    "entry_date", "entry_price",
    "ret_1d", "ret_3d", "ret_5d",
    "control_underlying", "control_ret_1d", "control_ret_3d",
    "control_ret_5d", "resolved_at", "rule_param",
]


def output_path() -> Path:
    return Path(getattr(
        config, "CANDIDATE_RESOLUTION_FILE",
        config.PROJECT_FOLDER / "candidate_resolution.csv"))


def source_path() -> Path:
    return Path(getattr(
        config, "OPTIONS_SHADOW_LOG_FILE",
        config.PROJECT_FOLDER / "options_shadow_log.csv"))


def row_key(row: dict[str, Any]) -> str:
    """A stable identity for one shadow row.

    Timestamp plus underlying plus action. Two candidates for the same name
    in the same cycle would collide, but the scanner logs one row per
    underlying per cycle, so this is unique in practice and stays readable
    -- which matters more than a hash nobody can trace back to a row.
    """

    return (f"{(row.get('timestamp') or '')[:19]}|"
            f"{(row.get('underlying') or '').strip().upper()}|"
            f"{(row.get('action') or '').strip()}")


def as_float(value: Any) -> float | None:
    """A number, or None. Never 0.0 for unusable input."""

    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def forward_return(bars: list[dict[str, Any]], entry_index: int,
                   horizon: int) -> float | None:
    """Return from the entry bar's close to `horizon` bars later.

    None when the window runs past the end of the data. A trade that has
    not had time to resolve is NOT a zero -- booking it flat would drag
    every average toward nothing, which is exactly the defect that made
    the 3:1 reward sweep meaningless until timeouts were marked to market.
    """

    if entry_index < 0 or horizon <= 0:
        return None

    target = entry_index + horizon

    if target >= len(bars):
        return None

    start = as_float(bars[entry_index].get("close"))
    end = as_float(bars[target].get("close"))

    if start is None or end is None or start <= 0:
        return None

    return (end - start) / start


def pick_control(candidates: list[str], exclude: str,
                 key: str) -> str | None:
    """A seeded random pick from the SAME session's other candidates.

    Seeded on the row key so the choice is deterministic and a re-run
    produces identical controls -- an unstable control would let a rerun
    quietly change the verdict.

    Drawn from what was ALREADY IN FRONT OF LOCKBOT, not from the whole
    universe: the question is whether its ranking beat choosing at random
    from its own shortlist, which is the only comparison that isolates the
    ranking rather than the screen that produced the shortlist.
    """

    pool = [c for c in candidates if c != exclude]

    if not pool:
        return None

    return random.Random(f"{CONTROL_SEED}|{key}").choice(sorted(pool))


def already_resolved() -> set[str]:
    """Row keys already in the derived file, so a re-run is idempotent."""

    path = output_path()

    if not path.exists():
        return set()

    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return {(row.get("row_key") or "").strip()
                    for row in csv.DictReader(handle)}
    except OSError:
        return set()


def load_candidates(*, min_age_days: int) -> list[dict[str, Any]]:
    """Unresolved CANDIDATE rows old enough to have an outcome."""

    path = source_path()

    if not path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
    done = already_resolved()
    out = []

    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (row.get("action") or "").strip() != "CANDIDATE":
                continue

            if row_key(row) in done:
                continue

            stamp = (row.get("timestamp") or "").strip()

            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue

            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)

            if when > cutoff:
                continue

            out.append(row)

    return out


def resolve(rows: list[dict[str, Any]], bars_for: Any,
            *, verbose: bool = True) -> list[dict[str, Any]]:
    """Attach forward returns and a matched control to each row.

    bars_for(symbol) -> list of {"date": iso, "close": float}, ascending.
    Injected rather than fetched here so the logic is testable offline.
    """

    by_session: dict[str, list[str]] = {}

    for row in rows:
        session = (row.get("timestamp") or "")[:10]
        name = (row.get("underlying") or "").strip().upper()

        if session and name:
            by_session.setdefault(session, []).append(name)

    resolved: list[dict[str, Any]] = []
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for row in rows:
        name = (row.get("underlying") or "").strip().upper()
        session = (row.get("timestamp") or "")[:10]

        if not name or not session:
            continue

        bars = bars_for(name) or []
        index = next((i for i, b in enumerate(bars)
                      if str(b.get("date", ""))[:10] >= session), -1)

        if index < 0:
            continue

        key = row_key(row)
        control = pick_control(by_session.get(session, []), name, key)
        control_bars = bars_for(control) if control else []
        control_index = next(
            (i for i, b in enumerate(control_bars or [])
             if str(b.get("date", ""))[:10] >= session), -1)

        entry_close = as_float(bars[index].get("close"))
        record = {
            "row_key": key,
            "timestamp": row.get("timestamp", ""),
            "underlying": name,
            "action": row.get("action", ""),
            "rank": (row.get("reason") or ""),
            "regime": row.get("regime", ""),
            "signal": row.get("signal", ""),
            "quality": row.get("quality", ""),
            "skew": row.get("skew", ""),
            # The cohort this row belongs to. Rows written before skew
            # shipped carry no source and must never be pooled with rows
            # written after it.
            "signal_source": row.get("signal_source", ""),
            "entry_date": str(bars[index].get("date", ""))[:10],
            "entry_price": "" if entry_close is None else f"{entry_close:.4f}",
            "control_underlying": control or "",
            "resolved_at": stamp,
            "rule_param": str(getattr(config, "OPTIONS_TAKE_PROFIT_PERCENT", "")),
        }

        for horizon in HORIZONS:
            value = forward_return(bars, index, horizon)
            record[f"ret_{horizon}d"] = ("" if value is None
                                         else f"{value:.6f}")
            control_value = (forward_return(control_bars, control_index,
                                            horizon)
                             if control_index >= 0 else None)
            record[f"control_ret_{horizon}d"] = ("" if control_value is None
                                                 else f"{control_value:.6f}")

        resolved.append(record)

    if verbose:
        print(f"  resolved {len(resolved)} of {len(rows)} candidate row(s)")

    return resolved


def append(rows: list[dict[str, Any]]) -> int:
    """Write to the DERIVED file. The source log is never touched."""

    rows = [r for r in rows if r]

    if not rows:
        return 0

    path = output_path()
    exists = path.exists()

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS,
                                extrasaction="ignore")

        if not exists:
            writer.writeheader()

        for row in rows:
            writer.writerow({key: row.get(key, "") for key in COLUMNS})

    return len(rows)


def summarise(path: Path | None = None) -> dict[str, Any]:
    """Rule against control, per horizon. Reports, never decides."""

    path = path or output_path()

    if not path.exists():
        return {}

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    out: dict[str, Any] = {"n": len(rows)}

    for horizon in HORIZONS:
        pairs = [(as_float(r.get(f"ret_{horizon}d")),
                  as_float(r.get(f"control_ret_{horizon}d")))
                 for r in rows]
        # BOTH sides required. Averaging a rule return against a missing
        # control silently compares a full sample to a partial one.
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]

        if not pairs:
            out[f"{horizon}d"] = None
            continue

        rule = sum(a for a, _ in pairs) / len(pairs)
        control = sum(b for _, b in pairs) / len(pairs)
        out[f"{horizon}d"] = {"n": len(pairs), "rule": rule,
                              "control": control, "edge": rule - control}

    return out


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}"
              + (f" - {detail}" if detail and not condition else ""))

    bars = [{"date": f"2026-08-{d:02d}", "close": price}
            for d, price in zip(range(10, 20),
                                [100, 102, 101, 105, 108, 107, 110, 112, 111, 115])]

    print("Forward returns, and what a missing one must NOT become")
    check("one bar ahead", abs(forward_return(bars, 0, 1) - 0.02) < 1e-9,
          str(forward_return(bars, 0, 1)))
    check("three bars ahead", abs(forward_return(bars, 0, 3) - 0.05) < 1e-9)
    # A window that has not closed is not a flat trade. Booking it at 0.0
    # is what made the 3:1 reward sweep meaningless.
    check("a window past the data end is None, never 0.0",
          forward_return(bars, 8, 5) is None)
    check("a zero entry price gives None, not a division",
          forward_return([{"date": "x", "close": 0}, {"date": "y", "close": 5}],
                         0, 1) is None)

    print()
    print("The control is seeded, stable, and drawn from the same session")
    pool = ["AAPL", "MSFT", "NVDA", "F"]
    first = pick_control(pool, "F", "key-1")
    check("it never picks the row's own symbol", first != "F", str(first))
    check("the same key gives the same control every run",
          pick_control(pool, "F", "key-1") == first)
    check("a different row gets an independent draw",
          pick_control(pool, "F", "key-2") is not None)
    check("a session with one candidate has no control",
          pick_control(["F"], "F", "k") is None)

    print()
    print("Row identity is stable and readable")
    row = {"timestamp": "2026-08-24T18:20:17+00:00", "underlying": "nok",
           "action": "CANDIDATE"}
    check("case is normalised", "NOK" in row_key(row), row_key(row))
    check("the same row keys the same twice", row_key(row) == row_key(row))

    print()
    print("Resolution attaches both sides, or neither")

    def fake_bars(symbol):
        return bars if symbol in ("F", "NOK") else []

    rows = [{"timestamp": "2026-08-10T14:00:00+00:00", "underlying": "F",
             "action": "CANDIDATE", "reason": "rank 1 of 2"},
            {"timestamp": "2026-08-10T14:00:00+00:00", "underlying": "NOK",
             "action": "CANDIDATE", "reason": "rank 2 of 2"}]
    out = resolve(rows, fake_bars, verbose=False)
    check("both rows resolved", len(out) == 2, str(len(out)))
    check("a forward return is attached", out[0]["ret_1d"] != "")
    check("and a control return beside it",
          out[0]["control_ret_1d"] != "", str(out[0]))
    check("the cohort tag is carried even when absent",
          "signal_source" in out[0])

    print()
    print("Summaries need BOTH sides of every pair")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "r.csv"

        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            w.writerow({"row_key": "a", "ret_1d": "0.05",
                        "control_ret_1d": "0.02"})
            # Rule return present, control missing. Including it would
            # compare a full sample against a partial one.
            w.writerow({"row_key": "b", "ret_1d": "0.10",
                        "control_ret_1d": ""})

        got = summarise(p)
        check("the unpaired row is excluded", got["1d"]["n"] == 1,
              str(got["1d"]))
        check("edge is rule minus control",
              abs(got["1d"]["edge"] - 0.03) < 1e-9, str(got["1d"]))

    print()
    print("It writes only its own derived file")
    source = Path(__file__).read_text(encoding="utf-8").split("def _self_test")[0]
    check("no order submission", "submit_order" not in source)
    check("the source log is never opened for writing",
          'source_path()' in source and 'source_path().open("w"' not in source)
    check("one appender", source.count('output_path()\n') >= 1
          and source.count('.open("a"') == 1)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All candidate-resolution checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve the setups LOCKBOT ranked and did not take")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--min-age-days", type=int, default=7)
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    print(f"CANDIDATE RESOLUTION v{VERSION}")

    pending = load_candidates(min_age_days=args.min_age_days)[:args.limit]
    print(f"  {len(pending)} unresolved candidate row(s) old enough to judge")

    if not pending:
        print("  nothing to do")
        return 0

    from dotenv import load_dotenv

    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import Adjustment

    import os

    load_dotenv(dotenv_path=str(config.PROJECT_FOLDER / ".env"))
    client = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"),
                                       os.getenv("ALPACA_SECRET_KEY"))
    cache: dict[str, list[dict[str, Any]]] = {}

    def bars_for(symbol: str) -> list[dict[str, Any]]:
        if symbol in cache:
            return cache[symbol]

        try:
            # Adjustment.ALL, always. RAW is the default and it is wrong
            # for every purpose this project has -- a split reads as a
            # 67% collapse and invents a catastrophic loss.
            got = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=[symbol], timeframe=TimeFrame.Day,
                start=datetime.now(timezone.utc) - timedelta(days=90),
                adjustment=Adjustment.ALL))
            data = got.data.get(symbol, []) if hasattr(got, "data") else []
            cache[symbol] = [{"date": b.timestamp.date().isoformat(),
                              "close": float(b.close)} for b in data]
        except Exception as error:                          # noqa: BLE001
            print(f"    {symbol}: {type(error).__name__}")
            cache[symbol] = []

        return cache[symbol]

    written = append(resolve(pending, bars_for))
    print(f"  wrote {written} row(s) to {output_path().name}")

    stats = summarise()

    if stats.get("n"):
        print()
        print(f"  {stats['n']} resolved so far")
        print(f"  {'horizon':<9}{'n':>6}{'ranked':>10}{'random':>10}{'edge':>10}")

        for horizon in HORIZONS:
            block = stats.get(f"{horizon}d")

            if not block:
                continue

            print(f"  {str(horizon) + 'd':<9}{block['n']:>6}"
                  f"{block['rule']:>10.2%}{block['control']:>10.2%}"
                  f"{block['edge']:>+10.2%}")

        print()
        print("  Edge is the ranking against a random pick from the SAME")
        print("  session's shortlist. It is evidence, not permission: no")
        print("  rule may be cut from this pool without a fresh")
        print("  pre-registration on untouched data.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
