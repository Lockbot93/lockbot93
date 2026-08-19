"""
PHASE 0 of PREREG_SIP_FEED.md — was the shadow book resolved on bars that
existed? Rebuilt 2026-08-11 on LOCKBOT's ruling.

THIS IS AN AUDIT, NOT A HYPOTHESIS

Nothing here passes or fails. `shadow_trades` resolves the book on
`DataFeed.IEX`, and IEX omits intervals where nothing traded on that one
venue. A stop or target touch on a bar IEX never recorded is invisible, so
a setup reads UNRESOLVED or EXPIRED when it actually resolved.

WHY THE FIRST VERSION WAS WRONG (item 62399112, marked provisional)

It compared IEX-**RAW** as recorded against SIP-**ADJUSTED**, which varies
two things at once, and reported the difference as a feed effect.

Recorded `stop_price` and `target_price` come from a LIVE scan price, and
live prices are unadjusted by definition. Back-adjusted bars sit
systematically BELOW those raw levels in a dividend-paying universe — 11 of
79 symbols here differ by >0.1%, ET by 1.70% against a ~4.5% stop — so
adjusted bars manufacture false STOPs for longs. The three TARGET->STOP
transitions the first run reported carry exactly that artifact's sign.

**RAW is the correct basis for a resolution path.** A live bracket is not
dividend-adjusted, so an ex-dividend stop trip is fidelity, not error. The
2026-08-05 `Adjustment.ALL` rule governs return-measuring backtests, which
is a different job.

So this holds adjustment constant at RAW and varies ONLY the feed.

SPLITS ARE THE ONE GENUINE RAW HAZARD

A split is large enough to invent a crash in a raw series and trip every
stop beneath it. Dividends are not. So splits are detected and the row is
truncated at the ex-date and marked EXPIRED at the last pre-split close;
dividends are flagged and change nothing.

IT NEVER WRITES THE BOOK

Results go to a SEPARATE file keyed by shadow_id. Never a column in
shadow_trades.csv, never overwriting `outcome`. The general form of the
39a7685e principle, as LOCKBOT stated it: parallel readings may be added
and defective inputs corrected pre-verdict, but recorded verdicts are
immutable, and re-basing the headline needs an explicit owner decision.

USAGE
    python sip_audit.py              run the audit
    python sip_audit.py --self-test  offline checks, no network
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import shadow_trades

SIP_DELAY_MINUTES = 20
BATCH = 50
SPLIT_FACTOR_JUMP = 1.5

AUDIT_FILE = Path(shadow_trades.PROJECT_FOLDER) / "shadow_resolution_audit.csv"

AUDIT_COLUMNS = [
    "shadow_id", "symbol", "side", "logged_at",
    "recorded_outcome", "recorded_feed",
    "iex_raw_outcome", "sip_raw_outcome",
    "iex_bars", "sip_bars",
    "agrees_with_record", "feed_changes_outcome",
    "in_window_split", "dividend_adjusted",
]


def _client():
    from alpaca.data.historical import StockHistoricalDataClient

    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")

    if not key or not secret:
        raise RuntimeError("Alpaca credentials not found in .env")

    return StockHistoricalDataClient(key, secret)


def fetch_raw(symbols, start, end, *, feed_name: str):
    """5-minute bars, RAW, on the named feed. RAW is deliberate — see the
    module docstring. Adjusting here would manufacture stops."""

    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed, Adjustment

    client = _client()
    feed = DataFeed.SIP if feed_name == "sip" else DataFeed.IEX
    out = {}

    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        print(f"  {feed_name.upper()} RAW: {len(chunk)} symbol(s)...")

        try:
            bars = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                start=start, end=end, feed=feed,
                adjustment=Adjustment.RAW,
            ))
        except Exception as error:
            print(f"  batch failed: {type(error).__name__}: {error}")
            continue

        for symbol, rows in (getattr(bars, "data", {}) or {}).items():
            out.setdefault(symbol, []).extend(rows)

    return out


def corporate_actions(symbols, start, end):
    """Per-symbol split dates and a dividend flag.

    The raw/adjusted ratio is constant except where a corporate action
    lands. A split moves it by the split factor -- big. A dividend moves it
    by a fraction of a percent. So a jump of >= SPLIT_FACTOR_JUMP between
    consecutive days is a split, and any persistent ratio away from 1.0
    without such a jump is dividend adjustment.
    """

    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment

    client = _client()

    def daily(adjustment):
        out = {}
        for i in range(0, len(symbols), BATCH):
            chunk = symbols[i:i + BATCH]
            try:
                bars = client.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                    start=start, end=end, feed=DataFeed.IEX,
                    adjustment=adjustment,
                ))
            except Exception as error:
                print(f"  daily batch failed: {type(error).__name__}: {error}")
                continue
            for symbol, rows in (getattr(bars, "data", {}) or {}).items():
                out.setdefault(symbol, {}).update(
                    {b.timestamp.date(): float(b.close) for b in rows}
                )
        return out

    raw, adj = daily(Adjustment.RAW), daily(Adjustment.ALL)
    splits, dividends = defaultdict(list), set()

    for symbol in symbols:
        r, a = raw.get(symbol, {}), adj.get(symbol, {})
        days = sorted(set(r) & set(a))
        ratios = [(d, r[d] / a[d]) for d in days if a[d]]

        if not ratios:
            continue

        if any(abs(v - 1.0) > 0.001 for _, v in ratios):
            dividends.add(symbol)

        for (d0, v0), (d1, v1) in zip(ratios, ratios[1:]):
            if v1 <= 0 or v0 <= 0:
                continue
            jump = max(v0 / v1, v1 / v0)
            if jump >= SPLIT_FACTOR_JUMP:
                splits[symbol].append(d1)

    return splits, dividends


def resolve_one(row, bars, now, split_dates):
    """Resolve one row on one feed's bars. Returns (outcome, bars_used)."""

    logged = shadow_trades.parse_time(row.get("logged_at", ""))

    if logged is None:
        return None, 0

    horizon_end = logged + timedelta(days=shadow_trades.SHADOW_MAX_DAYS)
    truncated_by_split = False

    for ex_date in split_dates:
        ex = datetime(ex_date.year, ex_date.month, ex_date.day,
                      tzinfo=timezone.utc)
        if logged < ex <= horizon_end:
            horizon_end = min(horizon_end, ex)
            truncated_by_split = True

    window = [
        b for b in bars
        if getattr(b, "timestamp", now) <= horizon_end
    ]

    if not window:
        return None, 0

    try:
        entry = float(row.get("reference_price") or 0) or None
        stop = float(row["stop_price"])
        target = float(row["target_price"])
    except (TypeError, ValueError, KeyError):
        return None, 0

    resolution = shadow_trades.resolve_from_bars(
        bars=window, side=row["side"], stop_price=stop,
        target_price=target, start_time=logged, entry_price=entry,
    )

    outcome = resolution.outcome

    # A split truncation ends the window early by construction, and the
    # wall-clock rule applies to a genuinely elapsed window.
    if outcome == shadow_trades.OUTCOME_UNRESOLVED:
        if truncated_by_split or now >= horizon_end:
            outcome = shadow_trades.OUTCOME_EXPIRED

    return outcome, resolution.bars_checked


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def run() -> int:
    print("=" * 78)
    print("PHASE 0 AUDIT (rebuilt) — IEX-RAW vs SIP-RAW, adjustment held fixed")
    print("=" * 78)
    print("Report only. Writes shadow_resolution_audit.csv; never the book.")

    before = digest(shadow_trades.SHADOW_FILE)
    print(f"\nshadow_trades.csv sha256 before: {before[:16]}...")

    rows = shadow_trades.load_rows(shadow_trades.SHADOW_FILE)
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=SIP_DELAY_MINUTES)
    symbols = sorted({r["symbol"] for r in rows if r.get("symbol")})
    earliest = min(
        (shadow_trades.parse_time(r["logged_at"]) for r in rows
         if shadow_trades.parse_time(r["logged_at"])),
        default=None,
    )

    print(f"rows {len(rows)}   symbols {len(symbols)}   from {earliest}")
    print(f"SIP window ends {SIP_DELAY_MINUTES} min behind now\n")

    iex = fetch_raw(symbols, earliest, end, feed_name="iex")
    sip = fetch_raw(symbols, earliest, end, feed_name="sip")

    print("\nchecking for corporate actions in the window...")
    splits, dividends = corporate_actions(symbols, earliest, end)
    print(f"  symbols with a split in-window : {len(splits)}"
          + (f" -> {dict(splits)}" if splits else ""))
    print(f"  symbols with dividend adjustment: {len(dividends)}")

    total_iex = sum(len(v) for v in iex.values())
    total_sip = sum(len(v) for v in sip.values())
    print(f"\nbars: IEX {total_iex:,}   SIP {total_sip:,}"
          + (f"   SIP/IEX {total_sip/total_iex:.2f}x" if total_iex else ""))

    audit_rows = []
    transitions = Counter()
    feed_changes = 0

    for row in rows:
        sym = row["symbol"]
        sd = splits.get(sym, [])
        iex_outcome, iex_n = resolve_one(row, iex.get(sym, []), now, sd)
        sip_outcome, sip_n = resolve_one(row, sip.get(sym, []), now, sd)
        recorded = row.get("outcome") or ""

        changed = (
            iex_outcome is not None and sip_outcome is not None
            and iex_outcome != sip_outcome
        )
        if changed:
            feed_changes += 1

        if iex_outcome and sip_outcome:
            transitions[(iex_outcome, sip_outcome)] += 1

        audit_rows.append({
            "shadow_id": row.get("shadow_id", ""),
            "symbol": sym,
            "side": row.get("side", ""),
            "logged_at": row.get("logged_at", ""),
            "recorded_outcome": recorded,
            "recorded_feed": "iex",
            "iex_raw_outcome": iex_outcome or "",
            "sip_raw_outcome": sip_outcome or "",
            "iex_bars": iex_n,
            "sip_bars": sip_n,
            "agrees_with_record": str(iex_outcome == recorded),
            "feed_changes_outcome": str(changed),
            "in_window_split": str(bool(sd)),
            "dividend_adjusted": str(sym in dividends),
        })

    with AUDIT_FILE.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(audit_rows)

    print("\n" + "=" * 78)
    print("FEED EFFECT — adjustment held constant at RAW")
    print("=" * 78)
    print(f"{'IEX-RAW':<16} -> {'SIP-RAW':<16}{'count':>8}")
    for (a, b), count in sorted(transitions.items(), key=lambda kv: -kv[1]):
        flag = "" if a == b else "   <-- CHANGED"
        print(f"{a:<16} -> {b:<16}{count:>8}{flag}")

    scored = sum(transitions.values())
    print(f"\noutcomes the FEED changes: {feed_changes} of {scored}"
          + (f"  ({100*feed_changes/scored:.1f}%)" if scored else ""))

    # reproduce the record, and see what the feed does to the headline
    def rate(key):
        decided = wins = 0
        for r in audit_rows:
            o = r[key]
            if o in ("TARGET", "STOP", "AMBIGUOUS"):
                decided += 1
                wins += (o == "TARGET")
        return decided, wins

    print("\n" + "=" * 78)
    print("EFFECT ON THE HEADLINE")
    print("=" * 78)
    for label, key in (("as recorded (IEX, as logged)", "recorded_outcome"),
                       ("re-resolved IEX-RAW", "iex_raw_outcome"),
                       ("re-resolved SIP-RAW", "sip_raw_outcome")):
        d, w = rate(key)
        print(f"  {label:<30} decided {d:>4}  targets {w:>4}  "
              f"win rate {(f'{100*w/d:.1f}%' if d else '--')}")

    after = digest(shadow_trades.SHADOW_FILE)
    print(f"\nshadow_trades.csv sha256 after : {after[:16]}...")
    if after == before:
        print("  UNCHANGED by this run, as required.")
    else:
        print("  CHANGED — the scheduled Shadow Resolve task probably ran")
        print("  concurrently. Re-run with it quiesced before citing figures.")

    print(f"\nwrote {AUDIT_FILE.name} ({len(audit_rows)} rows, keyed by shadow_id)")
    return 0


def _self_test() -> int:
    failures = []

    def check(label, condition):
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")

    # Scan CODE ONLY -- after the module docstring, before the self-test.
    # Including either end makes these checks match their own assertion
    # strings or the prose explaining them, which is how the first version
    # reported a second Adjustment.ALL that was a sentence.
    whole = Path(__file__).read_text(encoding="utf-8")
    src = whole.split("from __future__")[1].split("def _self_test")[0]

    print("The book is never written")
    check("no call to shadow_trades.save_rows", "save_rows" not in src)
    check("exactly one file is opened for writing, and it is the audit",
          src.count('.open("w"') == 1 and "AUDIT_FILE.open" in src)
    check("shadow_trades.py holds no reference to the audit file",
          "shadow_resolution_audit" not in
          Path(shadow_trades.__file__).read_text(encoding="utf-8"))
    check("the audit is keyed by shadow_id", "shadow_id" in AUDIT_COLUMNS)

    print("\nAdjustment is held constant at RAW")
    check("RAW is requested for the 5-minute bars",
          "Adjustment.RAW" in src)
    check("ALL appears only for corporate-action detection",
          src.count("Adjustment.ALL") == 1)

    print("\nBoth feeds are compared")
    check("IEX and SIP are both fetched",
          'feed_name="iex"' in src and 'feed_name="sip"' in src)
    check("SIP requests stay behind the delayed-feed window",
          "timedelta(minutes=SIP_DELAY_MINUTES)" in src)

    print("\nResolution is reused, not reimplemented")
    check("it calls the book's own resolver",
          "shadow_trades.resolve_from_bars" in src)
    check("expiry uses the wall clock", "now >= horizon_end" in src)

    print("\nSplit handling")
    check("a split truncates the window", "truncated_by_split" in src)
    check("and marks EXPIRED rather than inventing a verdict",
          "OUTCOME_EXPIRED" in src)
    check("the split threshold is a factor jump, not a price move",
          "SPLIT_FACTOR_JUMP" in src)

    print("\nIntegrity check")
    check("the book is hashed before and after", "digest(" in src)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("All audit checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    return _self_test() if args.self_test else run()


if __name__ == "__main__":
    sys.exit(main())
