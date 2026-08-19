"""
execution_cost.py  --  LOCKBOT execution cost measurement   v1.0

READ ONLY. Places no orders. Writes no existing file. Pure measurement plus
its own CSV outputs.

WHY THIS EXISTS
    The options shadow book models entry at the ask and exit at the bid -- the
    worst case on both sides. It records the QUOTED spread and never records
    where a fill actually landed. Those are different numbers and only the
    second one is money.

    Measured so far: quoted spread ate 11% of an average winner ($3.02 against
    $26.93), with a 40% win rate against a 41.2% breakeven. The gap to breakeven
    is 1.2 points and the spread cost is 11 points. So execution cost is the
    largest single controllable term in the options book.

    This is cost reduction, NOT edge discovery. It cannot make a negative-edge
    signal positive. It is what lets any real edge survive -- published anomaly
    returns decay roughly 93% once trading costs are applied.

WHAT IT MEASURES
    1. Realized vs quoted spread -- where fills land relative to the mid.
    2. Mid-limit fill simulation -- would a limit at the mid have filled, and
       what did it cost when it did.
    3. Time of day -- spread by session bucket.
    4. Within-underlying microstructure -- strike roundness and monthly vs
       weekly expiry, same stock.

    Plus the guard that makes 2 honest: ADVERSE SELECTION. A resting buy limit
    fills when the market comes down to you, which is disproportionately when it
    is about to keep going down. Unfilled attempts are not a random sample.
    Section 2 is void without the fill-rate and unfilled-outcome figures.

PROJECT CONVENTIONS OBSERVED
    * None, never a default. A value that cannot be computed is None, never 0.0.
      (simulate_symbol timeouts, aged-out shadow rows, calculate_r_multiple.)
    * Units are explicit. Fractions internally, percent only at print time.
      A 971% spread once came from mixing the two.
    * Callables raise. A wrapped-but-never-executed call must not look like data.
    * Missing data is unknown, never a pass.
    * Self-tests run offline, no network, no account.

USAGE
    python execution_cost.py --self-test
    python execution_cost.py --report            reads the CSVs it owns
    python execution_cost.py --report --save
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

VERSION = "1.0"

# Files this module owns. It writes nothing else.
FILL_LOG = "execution_fills.csv"
LIMIT_ATTEMPT_LOG = "execution_limit_attempts.csv"
QUOTE_SAMPLE_LOG = "execution_quote_samples.csv"
REPORT_OUT = "execution_cost_report.csv"

# US equity option session, Eastern time.
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)

# The session is EASTERN and this project stores everything in UTC.
#
# The first version read `stamp.hour` directly, which is correct only for a
# naive Eastern timestamp and wrong for every row this project has ever
# written. Measured against the real options shadow log: of 713 live-session
# quotes, 575 bucketed as outside the session, ZERO landed in first_15m or
# open_hour, and a 13:50 UTC quote -- 09:50 Eastern, the widest part of the
# day -- was filed as midday.
#
# That is fatal for section 3, whose entire purpose is finding the open
# expensive, and it silently corrupts section 4's midday control.
#
# An AWARE timestamp is converted. A NAIVE one is assumed to be Eastern
# already, which keeps every hand-written test reading as it looks.
EASTERN = ZoneInfo("America/New_York")

# Session buckets as (label, minutes_from_open_start, minutes_from_open_end).
SESSION_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("first_15m", 0, 15),
    ("open_hour", 15, 60),
    ("midday", 60, 300),
    ("last_hour", 300, 375),
    ("last_15m", 375, 391),
)

# Subgroup floor, IMPORTED rather than copied.
#
# The first version hardcoded 10 with a self-test asserting == 10, which
# would keep passing after the shared value moved. That is precisely the
# drift invariant #1 exists to prevent: "never define a local copy of a
# shared setting in a module." learning_report owns this number.
try:
    from learning_report import MINIMUM_GROUP_TRADES
except Exception:                                    # pragma: no cover
    MINIMUM_GROUP_TRADES = 10

CONTRACT_MULTIPLIER = 100


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def _reject_callable(value: Any, what: str) -> None:
    """A function object must never be mistaken for data."""
    if callable(value):
        raise TypeError(
            f"{what} received a function instead of a value. Something wrapped "
            "the call and never executed it."
        )


def as_float(value: Any) -> float | None:
    """Best-effort float. None on anything unusable. Never 0.0 as a fallback."""
    _reject_callable(value, "as_float")
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


# ---------------------------------------------------------------------------
# 1. quoted spread and realized spread
# ---------------------------------------------------------------------------

def mid_price(bid: Any, ask: Any) -> float | None:
    """Midpoint of a two-sided quote. None if the quote is unusable."""
    b, a = as_float(bid), as_float(ask)
    if b is None or a is None:
        return None
    if b <= 0 or a <= 0:
        return None
    if a < b:  # crossed
        return None
    return (b + a) / 2.0


def quoted_spread_fraction(bid: Any, ask: Any) -> float | None:
    """(ask - bid) / mid, as a FRACTION. None if unusable.

    This is the full round-trip cost if you buy at the ask and sell at the bid.
    """
    b, a = as_float(bid), as_float(ask)
    mid = mid_price(b, a)
    if mid is None:
        return None
    return (a - b) / mid


def slippage_from_mid_fraction(fill_price: Any, bid: Any, ask: Any,
                               side: str) -> float | None:
    """How far a fill landed from the mid, as a fraction of the mid.

    POSITIVE means you paid worse than the mid. NEGATIVE means better.
    Sign is normalised so that "worse for you" is always positive, whether you
    were buying or selling.
    """
    fill = as_float(fill_price)
    mid = mid_price(bid, ask)
    if fill is None or mid is None or fill <= 0:
        return None
    side = (side or "").strip().lower()
    if side in ("buy", "long", "bto", "buy_to_open"):
        signed = fill - mid
    elif side in ("sell", "short", "stc", "sell_to_close"):
        signed = mid - fill
    else:
        return None
    return signed / mid


def realized_round_trip_fraction(entry_fill: Any, entry_bid: Any, entry_ask: Any,
                                 exit_fill: Any, exit_bid: Any,
                                 exit_ask: Any) -> float | None:
    """Total execution cost actually paid across both legs, as a fraction.

    Zero would mean both legs filled exactly at the mid. The quoted spread is
    what you pay if both legs cross fully.
    """
    entry = slippage_from_mid_fraction(entry_fill, entry_bid, entry_ask, "buy")
    exit_ = slippage_from_mid_fraction(exit_fill, exit_bid, exit_ask, "sell")
    if entry is None or exit_ is None:
        return None
    return entry + exit_


def cost_capture_ratio(realized: float | None,
                       quoted: float | None) -> float | None:
    """Realized cost as a share of the quoted spread.

    1.0 = paid the full quoted spread (crossed both sides).
    0.5 = paid half.
    0.0 = filled at the mid both times.
    Values above 1.0 mean worse than crossing, which happens on a moving market.
    """
    if realized is None or quoted is None or quoted <= 0:
        return None
    return realized / quoted


# ---------------------------------------------------------------------------
# 2. mid-limit fill simulation
# ---------------------------------------------------------------------------

@dataclass
class LimitAttempt:
    """One attempt to fill a limit order, filled or not.

    underlying_move_after is signed IN THE DIRECTION THE TRADE WANTED. Positive
    means the trade would have been right. It is recorded for filled AND
    unfilled attempts, because comparing the two is the only way to detect
    adverse selection.
    """
    attempt_id: str
    symbol: str
    option_symbol: str
    side: str
    limit_price: float
    quote_bid: float | None
    quote_ask: float | None
    filled: bool | None                 # None = unknown, never assumed False
    fill_price: float | None = None
    seconds_to_fill: float | None = None
    underlying_move_after: float | None = None
    window_seconds: int | None = None
    note: str = ""


def would_limit_fill(limit_price: Any, side: str,
                     subsequent_quotes: Sequence[tuple[Any, Any]]
                     ) -> tuple[bool | None, float | None]:
    """Would a resting limit have filled against a series of later quotes?

    Returns (filled, fill_price). (None, None) when the inputs cannot answer
    the question -- an empty quote series proves nothing, exactly as an empty
    recorder file cannot prove no position is open.

    Conservative rule: a BUY limit fills only if the ASK comes down to or below
    the limit. Touching the mid is not a fill.
    """
    limit = as_float(limit_price)
    if limit is None or limit <= 0:
        return None, None
    side = (side or "").strip().lower()
    if side not in ("buy", "long", "bto", "buy_to_open",
                    "sell", "short", "stc", "sell_to_close"):
        return None, None
    if not subsequent_quotes:
        return None, None

    buying = side in ("buy", "long", "bto", "buy_to_open")
    saw_usable_quote = False
    for bid, ask in subsequent_quotes:
        b, a = as_float(bid), as_float(ask)
        if b is None or a is None or b <= 0 or a <= 0 or a < b:
            continue
        saw_usable_quote = True
        if buying and a <= limit:
            return True, limit
        if not buying and b >= limit:
            return True, limit

    if not saw_usable_quote:
        return None, None
    return False, None


def fill_rate(attempts: Iterable[LimitAttempt]) -> tuple[int, int, int]:
    """(filled, unfilled, unknown). Unknown is never folded into unfilled."""
    filled = unfilled = unknown = 0
    for a in attempts:
        if a.filled is True:
            filled += 1
        elif a.filled is False:
            unfilled += 1
        else:
            unknown += 1
    return filled, unfilled, unknown


def adverse_selection(attempts: Sequence[LimitAttempt]) -> dict[str, Any]:
    """Compare what happened AFTER filled attempts vs unfilled ones.

    If unfilled attempts moved more favourably than filled ones, the limit
    strategy is systematically catching the trades that were about to go wrong.
    That is a real cost and it does not show up in the fill price.

    Returns medians and the gap, or None values where a group is too small.
    Suppression is reported, never silently hidden.
    """
    filled = [a.underlying_move_after for a in attempts
              if a.filled is True and a.underlying_move_after is not None]
    missed = [a.underlying_move_after for a in attempts
              if a.filled is False and a.underlying_move_after is not None]

    out: dict[str, Any] = {
        "filled_n": len(filled),
        "unfilled_n": len(missed),
        "filled_median_move": None,
        "unfilled_median_move": None,
        "gap": None,
        "suppressed": [],
        "verdict": "insufficient data",
    }
    if len(filled) >= MINIMUM_GROUP_TRADES:
        out["filled_median_move"] = statistics.median(filled)
    else:
        out["suppressed"].append(f"filled n={len(filled)} < {MINIMUM_GROUP_TRADES}")
    if len(missed) >= MINIMUM_GROUP_TRADES:
        out["unfilled_median_move"] = statistics.median(missed)
    else:
        out["suppressed"].append(
            f"unfilled n={len(missed)} < {MINIMUM_GROUP_TRADES}")

    if out["filled_median_move"] is not None and out["unfilled_median_move"] is not None:
        out["gap"] = out["filled_median_move"] - out["unfilled_median_move"]
        if out["gap"] < 0:
            out["verdict"] = ("ADVERSE SELECTION PRESENT -- the attempts that "
                              "did not fill went on to do better")
        else:
            out["verdict"] = "no adverse selection detected in this sample"
    return out


# ---------------------------------------------------------------------------
# 3. time of day
# ---------------------------------------------------------------------------

def minutes_from_open(stamp: Any) -> int | None:
    """Minutes since 9:30 EASTERN. None if unusable.

    An aware timestamp is converted to Eastern first. A naive one is taken
    as already Eastern. Reading `.hour` off a UTC string was the original
    defect -- see the note beside EASTERN.
    """
    _reject_callable(stamp, "minutes_from_open")
    if isinstance(stamp, datetime):
        dt = stamp
    elif isinstance(stamp, str) and stamp.strip():
        try:
            dt = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is not None:
        dt = dt.astimezone(EASTERN)

    delta = (dt.hour * 60 + dt.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)
    return delta


def session_bucket(stamp: Any) -> str | None:
    """Which part of the session a timestamp falls in. None if outside it."""
    mins = minutes_from_open(stamp)
    if mins is None:
        return None
    for label, lo, hi in SESSION_BUCKETS:
        if lo <= mins < hi:
            return label
    return None


def spread_by_bucket(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Median quoted spread per session bucket.

    Each sample needs timestamp, bid, ask. Rows that cannot be placed or priced
    are counted as unplaced rather than dropped silently.
    """
    grouped: dict[str, list[float]] = {label: [] for label, _, _ in SESSION_BUCKETS}
    unplaced = 0
    unpriced = 0
    for row in samples:
        bucket = session_bucket(row.get("timestamp"))
        spread = quoted_spread_fraction(row.get("bid"), row.get("ask"))
        if bucket is None:
            unplaced += 1
            continue
        if spread is None:
            unpriced += 1
            continue
        grouped[bucket].append(spread)

    out: dict[str, Any] = {"unplaced": unplaced, "unpriced": unpriced, "buckets": {}}
    for label, values in grouped.items():
        if len(values) >= MINIMUM_GROUP_TRADES:
            out["buckets"][label] = {
                "n": len(values),
                "median_spread": statistics.median(values),
                "suppressed": False,
            }
        else:
            out["buckets"][label] = {
                "n": len(values),
                "median_spread": None,
                "suppressed": True,
            }
    return out


# ---------------------------------------------------------------------------
# 4. within-underlying microstructure
# ---------------------------------------------------------------------------

def third_friday(year: int, month: int) -> date:
    """Standard monthly option expiry."""
    first = date(year, month, 1)
    # weekday(): Monday 0 ... Friday 4
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def is_monthly_expiry(expiry: Any) -> bool | None:
    """True for a standard monthly expiry, False for a weekly, None if unusable."""
    _reject_callable(expiry, "is_monthly_expiry")
    if isinstance(expiry, date) and not isinstance(expiry, datetime):
        d = expiry
    elif isinstance(expiry, datetime):
        d = expiry.date()
    elif isinstance(expiry, str) and expiry.strip():
        try:
            d = date.fromisoformat(expiry.strip())
        except ValueError:
            return None
    else:
        return None
    return d == third_friday(d.year, d.month)


def strike_granularity(strike: Any) -> str | None:
    """Coarsest interval the strike divides evenly into.

    Round strikes tend to carry deeper books than odd ones on the same name.
    """
    s = as_float(strike)
    if s is None or s <= 0:
        return None
    cents = round(s * 100)
    for label, step in (("10", 1000), ("5", 500), ("1", 100), ("0.50", 50)):
        if cents % step == 0:
            return label
    return "other"


def microstructure_by_underlying(rows: Iterable[dict[str, Any]],
                                 restrict_bucket: str | None = "midday"
                                 ) -> dict[str, Any]:
    """Compare spread within each underlying, by expiry type and strike step.

    Two confounds are controlled here, both found the hard way:

    1. Comparing ACROSS underlyings confounds the name with the contract. Only
       the within-name comparison isolates the choice actually available to a
       trader who has already decided which stock to trade.

    2. Comparing across TIMES OF DAY confounds the contract with the session.
       Section 3 shows spread varying several-fold between the open and midday,
       which is far larger than any weekly-vs-monthly difference. If weekly
       contracts happen to be sampled at different times than monthly ones, the
       comparison measures the clock, not the contract. So by default only one
       session bucket is used, and which one is reported.

    Pass restrict_bucket=None to disable the control -- the result is then
    labelled unreliable rather than presented as a finding.
    """
    per_symbol: dict[str, dict[str, list[float]]] = {}
    skipped = 0
    wrong_bucket = 0
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        spread = quoted_spread_fraction(row.get("bid"), row.get("ask"))
        monthly = is_monthly_expiry(row.get("expiry"))
        step = strike_granularity(row.get("strike"))
        if not symbol or spread is None or monthly is None or step is None:
            skipped += 1
            continue
        if restrict_bucket is not None:
            if session_bucket(row.get("timestamp")) != restrict_bucket:
                wrong_bucket += 1
                continue
        book = per_symbol.setdefault(symbol, {})
        book.setdefault("monthly" if monthly else "weekly", []).append(spread)
        book.setdefault(f"step_{step}", []).append(spread)

    out: dict[str, Any] = {
        "skipped": skipped,
        "outside_bucket": wrong_bucket,
        "bucket_used": restrict_bucket,
        "time_controlled": restrict_bucket is not None,
        "symbols": {},
    }
    for symbol, book in sorted(per_symbol.items()):
        summary: dict[str, Any] = {}
        for group, values in sorted(book.items()):
            if len(values) >= MINIMUM_GROUP_TRADES:
                summary[group] = {"n": len(values),
                                  "median_spread": statistics.median(values),
                                  "suppressed": False}
            else:
                summary[group] = {"n": len(values), "median_spread": None,
                                  "suppressed": True}
        out["symbols"][symbol] = summary
    return out


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def build_fills_from_journals(
    completed: str = "options_completed_trades.csv",
    samples: str = QUOTE_SAMPLE_LOG,
    out: str = FILL_LOG,
    tolerance_minutes: int = 30,
) -> dict[str, Any]:
    """Assemble execution_fills.csv by joining what is already recorded.

    WHY A JOIN RATHER THAN A COLLECTOR

    The book at entry IS captured -- options_scanner's quote sampler writes
    every contract's bid and ask at selection. The FILL is captured too, as
    entry_debit in the completed-trades journal. Nothing writes the two
    side by side, so this puts them together after the fact.

    WHAT IT CANNOT DO, and this is the honest limit: the book at EXIT is
    captured nowhere. options_manager fetches a quote to decide the stop
    and then discards it, and record_completed_option_trade receives only
    the realised credit. So exit_bid and exit_ask are left BLANK, and
    section 1's round-trip figure stays unavailable until something
    records them.

    That change belongs inside options_manager's exit path, which is the
    only stop loss this account has, and it should not be made without
    review. Entry-side slippage is real and usable in the meantime: it
    answers how far a fill landed from the mid, which is the larger half
    of the question.

    Matching is by option symbol and nearest sample within
    tolerance_minutes of the entry. A trade with no sample inside the
    window is reported unmatched rather than paired with a distant quote.
    """

    trades = read_rows(completed)
    quotes = read_rows(samples)

    result: dict[str, Any] = {
        "trades": len(trades), "samples": len(quotes),
        "matched": 0, "unmatched": 0, "written": 0, "reasons": {},
    }

    if not trades:
        result["reasons"]["no completed option trades yet"] = 1
        return result

    if not quotes:
        result["reasons"]["no quote samples yet"] = len(trades)
        result["unmatched"] = len(trades)
        return result

    by_symbol: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}

    for row in quotes:
        symbol = (row.get("option_symbol") or "").strip()
        stamp = _parse_time(row.get("timestamp"))
        if symbol and stamp is not None:
            by_symbol.setdefault(symbol, []).append((stamp, row))

    rows_out: list[dict[str, Any]] = []

    for trade in trades:
        symbol = (trade.get("long_symbol") or "").strip()
        entry_at = _parse_time(trade.get("entry_time"))
        entry_fill = as_float(trade.get("entry_debit"))

        if not symbol or entry_at is None or entry_fill is None:
            result["unmatched"] += 1
            result["reasons"]["trade missing symbol, time or fill"] = (
                result["reasons"].get("trade missing symbol, time or fill", 0) + 1)
            continue

        candidates = by_symbol.get(symbol, [])
        best = None

        for stamp, row in candidates:
            gap = abs((stamp - entry_at).total_seconds()) / 60.0
            if gap <= tolerance_minutes and (best is None or gap < best[0]):
                best = (gap, row)

        if best is None:
            result["unmatched"] += 1
            result["reasons"]["no quote sample within the window"] = (
                result["reasons"].get("no quote sample within the window", 0) + 1)
            continue

        gap, sample = best
        result["matched"] += 1

        # Quotes are per share, the journal's debit is per contract. Put
        # them on one scale or the slippage figure is 100x wrong.
        bid, ask = as_float(sample.get("bid")), as_float(sample.get("ask"))

        rows_out.append({
            "symbol": trade.get("underlying", ""),
            "option_symbol": symbol,
            "entry_time": trade.get("entry_time", ""),
            "entry_fill": entry_fill / CONTRACT_MULTIPLIER,
            "entry_bid": bid,
            "entry_ask": ask,
            "quote_lag_minutes": round(gap, 2),
            # Captured nowhere yet. Blank, never zero.
            "exit_time": trade.get("exit_time", ""),
            "exit_fill": "",
            "exit_bid": "",
            "exit_ask": "",
            "exit_reason": trade.get("exit_reason", ""),
        })

    if rows_out:
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()))
            writer.writeheader()
            writer.writerows(rows_out)
        result["written"] = len(rows_out)

    return result


def _parse_time(value: Any) -> datetime | None:
    _reject_callable(value, "_parse_time")
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_rows(path: str) -> list[dict[str, Any]]:
    """Read one of this module's own CSVs. Missing file is an empty list."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def attempts_from_rows(rows: Iterable[dict[str, Any]]) -> list[LimitAttempt]:
    out: list[LimitAttempt] = []
    for r in rows:
        filled_raw = (r.get("filled") or "").strip().lower()
        if filled_raw in ("true", "1", "yes"):
            filled: bool | None = True
        elif filled_raw in ("false", "0", "no"):
            filled = False
        else:
            filled = None
        limit = as_float(r.get("limit_price"))
        if limit is None:
            continue
        out.append(LimitAttempt(
            attempt_id=(r.get("attempt_id") or "").strip(),
            symbol=(r.get("symbol") or "").strip().upper(),
            option_symbol=(r.get("option_symbol") or "").strip(),
            side=(r.get("side") or "").strip(),
            limit_price=limit,
            quote_bid=as_float(r.get("quote_bid")),
            quote_ask=as_float(r.get("quote_ask")),
            filled=filled,
            fill_price=as_float(r.get("fill_price")),
            seconds_to_fill=as_float(r.get("seconds_to_fill")),
            underlying_move_after=as_float(r.get("underlying_move_after")),
            window_seconds=int(as_float(r.get("window_seconds")) or 0) or None,
            note=(r.get("note") or "").strip(),
        ))
    return out


def pct(value: float | None, places: int = 2) -> str:
    return "--" if value is None else f"{value * 100:.{places}f}%"


def print_report(fills: list[dict[str, Any]],
                 attempts: list[LimitAttempt],
                 samples: list[dict[str, Any]]) -> None:
    line = "=" * 74
    print(line)
    print(f"EXECUTION COST REPORT  v{VERSION}")
    print(line)
    print("Cost reduction, not edge. This cannot make a negative-edge signal")
    print("positive. It is what lets a real edge survive its own costs.")

    # --- 1. realized vs quoted -------------------------------------------
    print("\n1. WHAT WAS ACTUALLY PAID vs WHAT WAS QUOTED")
    if not fills:
        print(f"   no rows in {FILL_LOG} -- nothing paid yet, nothing to measure")
    else:
        quoted, realized, ratios = [], [], []
        unusable = 0
        for r in fills:
            q = quoted_spread_fraction(r.get("entry_bid"), r.get("entry_ask"))
            rt = realized_round_trip_fraction(
                r.get("entry_fill"), r.get("entry_bid"), r.get("entry_ask"),
                r.get("exit_fill"), r.get("exit_bid"), r.get("exit_ask"))
            ratio = cost_capture_ratio(rt, q)
            if q is None or rt is None:
                unusable += 1
                continue
            quoted.append(q)
            realized.append(rt)
            if ratio is not None:
                ratios.append(ratio)
        print(f"   usable round trips     : {len(realized)}"
              f"   unusable: {unusable}")
        if realized:
            print(f"   median quoted spread   : {pct(statistics.median(quoted))}")
            print(f"   median realized cost   : {pct(statistics.median(realized))}")
            if ratios:
                share = statistics.median(ratios)
                print(f"   share of quoted paid   : {share:.2f}"
                      "   (1.00 = crossed both sides, 0.00 = filled at mid)")

    # --- 2. limit fills and adverse selection ----------------------------
    print("\n2. MID-LIMIT FILLS")
    if not attempts:
        print(f"   no rows in {LIMIT_ATTEMPT_LOG}")
    else:
        f, u, unk = fill_rate(attempts)
        total = f + u + unk
        print(f"   attempts {total}   filled {f}   unfilled {u}   unknown {unk}")
        if total:
            print(f"   fill rate              : {f / total:.1%}"
                  "   (unknown counted in the denominator, not as a miss)")
        adv = adverse_selection(attempts)
        print("\n   ADVERSE SELECTION CHECK -- section 2 is void without this")
        print(f"     filled   median move after : {pct(adv['filled_median_move'])}"
              f"   n={adv['filled_n']}")
        print(f"     unfilled median move after : {pct(adv['unfilled_median_move'])}"
              f"   n={adv['unfilled_n']}")
        if adv["gap"] is not None:
            print(f"     gap (filled - unfilled)    : {pct(adv['gap'])}")
        print(f"     verdict: {adv['verdict']}")
        for note in adv["suppressed"]:
            print(f"     suppressed: {note}")

    # --- 3. time of day ---------------------------------------------------
    print("\n3. SPREAD BY TIME OF DAY")
    if not samples:
        print(f"   no rows in {QUOTE_SAMPLE_LOG}")
    else:
        tod = spread_by_bucket(samples)
        for label, _, _ in SESSION_BUCKETS:
            info = tod["buckets"][label]
            flag = "  [suppressed, n < %d]" % MINIMUM_GROUP_TRADES if info["suppressed"] else ""
            print(f"   {label:<12} n={info['n']:<6} "
                  f"median {pct(info['median_spread'])}{flag}")
        if tod["unplaced"] or tod["unpriced"]:
            print(f"   outside session: {tod['unplaced']}   unpriced: {tod['unpriced']}")

    # --- 4. within-underlying --------------------------------------------
    print("\n4. SAME STOCK, DIFFERENT CONTRACT")
    if not samples:
        print(f"   no rows in {QUOTE_SAMPLE_LOG}")
    else:
        micro = microstructure_by_underlying(samples)
        if micro["time_controlled"]:
            print(f"   controlled for time of day: {micro['bucket_used']} only")
        else:
            print("   WARNING: not controlled for time of day. Section 3 shows")
            print("   spread varying several-fold across the session, which")
            print("   swamps any contract effect. Treat this as unreliable.")
        if not micro["symbols"]:
            print("   no symbol had usable data inside the chosen bucket")
        for symbol, groups in micro["symbols"].items():
            printable = {g: i for g, i in groups.items() if not i["suppressed"]}
            if not printable:
                continue
            parts = [f"{g} {pct(i['median_spread'])} (n={i['n']})"
                     for g, i in sorted(printable.items())]
            print(f"   {symbol:<6} " + "   ".join(parts))
        if micro["skipped"]:
            print(f"   rows skipped for missing expiry/strike: {micro['skipped']}")
        if micro["outside_bucket"]:
            print(f"   rows outside the {micro['bucket_used']} bucket: "
                  f"{micro['outside_bucket']}")

    print("\n" + line)
    print("Caveats, so these numbers are not read as more than they are:")
    print("  - A fill rate above zero is not free money. Read section 2's")
    print("    adverse-selection gap before believing the fill price.")
    print("  - Unknown fills are unknown. They are never counted as misses.")
    print("  - Within-underlying comparison only; across names confounds the")
    print("    stock with the contract.")
    print("  - No orders were placed. This module only reads.")
    print(line)


def write_report(fills: list[dict[str, Any]], attempts: list[LimitAttempt],
                 samples: list[dict[str, Any]], path: str = REPORT_OUT) -> None:
    rows: list[dict[str, Any]] = []
    for r in fills:
        q = quoted_spread_fraction(r.get("entry_bid"), r.get("entry_ask"))
        rt = realized_round_trip_fraction(
            r.get("entry_fill"), r.get("entry_bid"), r.get("entry_ask"),
            r.get("exit_fill"), r.get("exit_bid"), r.get("exit_ask"))
        rows.append({
            "kind": "round_trip",
            "symbol": r.get("symbol", ""),
            "option_symbol": r.get("option_symbol", ""),
            "quoted_spread_fraction": q,
            "realized_cost_fraction": rt,
            "cost_capture_ratio": cost_capture_ratio(rt, q),
        })
    for a in attempts:
        rows.append({
            "kind": "limit_attempt",
            "symbol": a.symbol,
            "option_symbol": a.option_symbol,
            "quoted_spread_fraction": quoted_spread_fraction(a.quote_bid, a.quote_ask),
            "realized_cost_fraction": slippage_from_mid_fraction(
                a.fill_price, a.quote_bid, a.quote_ask, a.side),
            "cost_capture_ratio": None,
        })
    if not rows:
        print("nothing to write -- no usable rows")
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"  detail written to {path}")


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

def self_test() -> int:
    checks: list[tuple[str, bool]] = []

    def check(name: str, cond: Any) -> None:
        checks.append((name, bool(cond)))

    def close(a: float | None, b: float, tol: float = 1e-9) -> bool:
        return a is not None and abs(a - b) < tol

    # --- guards -----------------------------------------------------------
    try:
        as_float(lambda: 1.0)
        check("as_float raises on a callable", False)
    except TypeError:
        check("as_float raises on a callable", True)
    check("as_float rejects junk", as_float("abc") is None)
    check("as_float rejects empty string", as_float("") is None)
    check("as_float rejects None", as_float(None) is None)
    check("as_float keeps a real zero", as_float("0") == 0.0)
    check("as_float rejects NaN", as_float(float("nan")) is None)

    # --- mid and quoted spread -------------------------------------------
    check("mid of 1.00/1.10", close(mid_price(1.00, 1.10), 1.05))
    check("crossed quote gives no mid", mid_price(1.20, 1.10) is None)
    check("zero bid gives no mid", mid_price(0, 1.10) is None)
    check("one-sided quote gives no mid", mid_price(1.00, None) is None)
    check("quoted spread 1.00/1.10 is ~9.52%",
          close(quoted_spread_fraction(1.00, 1.10), 0.10 / 1.05))
    check("tight quote is under 1%", quoted_spread_fraction(5.00, 5.02) < 0.01)
    check("quoted spread is a FRACTION not a percent",
          quoted_spread_fraction(1.00, 1.10) < 1.0)
    check("crossed quote gives no spread",
          quoted_spread_fraction(1.20, 1.10) is None)

    # --- slippage sign convention ----------------------------------------
    check("buying at the ask is positive slippage",
          close(slippage_from_mid_fraction(1.10, 1.00, 1.10, "buy"), 0.05 / 1.05))
    check("buying at the mid is zero slippage",
          close(slippage_from_mid_fraction(1.05, 1.00, 1.10, "buy"), 0.0))
    check("buying below the mid is negative slippage",
          slippage_from_mid_fraction(1.02, 1.00, 1.10, "buy") < 0)
    check("selling at the bid is positive slippage",
          close(slippage_from_mid_fraction(1.00, 1.00, 1.10, "sell"), 0.05 / 1.05))
    check("selling above the mid is negative slippage",
          slippage_from_mid_fraction(1.08, 1.00, 1.10, "sell") < 0)
    check("unknown side gives None",
          slippage_from_mid_fraction(1.05, 1.00, 1.10, "sideways") is None)
    check("missing fill gives None",
          slippage_from_mid_fraction(None, 1.00, 1.10, "buy") is None)

    # --- round trip and capture ratio ------------------------------------
    crossed = realized_round_trip_fraction(1.10, 1.00, 1.10, 1.00, 1.00, 1.10)
    quoted = quoted_spread_fraction(1.00, 1.10)
    check("crossing both sides equals the quoted spread", close(crossed, quoted))
    check("crossing both sides gives a capture ratio of 1.0",
          close(cost_capture_ratio(crossed, quoted), 1.0))
    mid_both = realized_round_trip_fraction(1.05, 1.00, 1.10, 1.05, 1.00, 1.10)
    check("filling at the mid twice costs nothing", close(mid_both, 0.0))
    check("mid fills give a capture ratio of 0.0",
          close(cost_capture_ratio(mid_both, quoted), 0.0))
    check("capture ratio needs a positive quoted spread",
          cost_capture_ratio(0.01, 0.0) is None)
    check("capture ratio of an unknown cost is None",
          cost_capture_ratio(None, quoted) is None)

    # --- limit fill simulation -------------------------------------------
    filled, price = would_limit_fill(1.05, "buy", [(1.02, 1.06), (1.01, 1.05)])
    check("buy limit fills when the ask reaches it", filled is True and price == 1.05)
    filled, _ = would_limit_fill(1.05, "buy", [(1.06, 1.08), (1.07, 1.09)])
    check("buy limit misses when the ask stays above", filled is False)
    filled, _ = would_limit_fill(1.05, "buy", [])
    check("an empty quote series proves nothing", filled is None)
    filled, _ = would_limit_fill(1.05, "buy", [(0, 0), (None, None)])
    check("only unusable quotes proves nothing", filled is None)
    filled, _ = would_limit_fill(1.05, "sell", [(1.06, 1.08)])
    check("sell limit fills when the bid reaches it", filled is True)
    filled, _ = would_limit_fill(1.05, "sell", [(1.01, 1.03)])
    check("sell limit misses when the bid stays below", filled is False)
    check("touching the mid is not a fill",
          would_limit_fill(1.05, "buy", [(1.04, 1.06)])[0] is False)
    check("a bad limit price gives None",
          would_limit_fill(0, "buy", [(1.0, 1.1)])[0] is None)
    check("a bad side gives None",
          would_limit_fill(1.05, "maybe", [(1.0, 1.1)])[0] is None)

    # --- fill rate --------------------------------------------------------
    attempts = [
        LimitAttempt("1", "TLT", "O1", "buy", 1.05, 1.0, 1.1, True, 1.05),
        LimitAttempt("2", "TLT", "O2", "buy", 1.05, 1.0, 1.1, False),
        LimitAttempt("3", "TLT", "O3", "buy", 1.05, 1.0, 1.1, None),
    ]
    check("fill rate counts three ways", fill_rate(attempts) == (1, 1, 1))
    check("unknown is not folded into unfilled", fill_rate(attempts)[1] == 1)

    # --- adverse selection ------------------------------------------------
    # Filled attempts went badly, unfilled ones went well: the trap.
    trap = ([LimitAttempt(str(i), "TLT", "O", "buy", 1.05, 1.0, 1.1, True, 1.05,
                          underlying_move_after=-0.02) for i in range(12)]
            + [LimitAttempt(str(i), "TLT", "O", "buy", 1.05, 1.0, 1.1, False,
                            underlying_move_after=+0.03) for i in range(12)])
    adv = adverse_selection(trap)
    check("adverse selection is detected when misses do better",
          "ADVERSE SELECTION PRESENT" in adv["verdict"])
    check("the gap is negative in the trap case", adv["gap"] < 0)
    check("filled median is recorded", close(adv["filled_median_move"], -0.02))

    healthy = ([LimitAttempt(str(i), "TLT", "O", "buy", 1.05, 1.0, 1.1, True, 1.05,
                             underlying_move_after=+0.03) for i in range(12)]
               + [LimitAttempt(str(i), "TLT", "O", "buy", 1.05, 1.0, 1.1, False,
                               underlying_move_after=-0.01) for i in range(12)])
    check("no adverse selection when fills do better",
          adverse_selection(healthy)["gap"] > 0)

    small = adverse_selection(trap[:4])
    check("a small group is suppressed, not averaged",
          small["filled_median_move"] is None)
    check("suppression is reported rather than hidden",
          len(small["suppressed"]) > 0)
    check("suppressed groups give no verdict",
          small["verdict"] == "insufficient data")
    check("rows with no recorded move are excluded",
          adverse_selection([LimitAttempt("x", "T", "O", "buy", 1.0, 1.0, 1.1,
                                          True, 1.0)])["filled_n"] == 0)

    # --- time of day ------------------------------------------------------
    check("9:30 is minute zero", minutes_from_open("2026-09-03T09:30:00") == 0)
    check("10:00 is minute thirty", minutes_from_open("2026-09-03T10:00:00") == 30)
    check("9:35 is in the first 15", session_bucket("2026-09-03T09:35:00") == "first_15m")
    check("12:00 is midday", session_bucket("2026-09-03T12:00:00") == "midday")
    check("15:50 is the last 15", session_bucket("2026-09-03T15:50:00") == "last_15m")
    check("8:00 is outside the session", session_bucket("2026-09-03T08:00:00") is None)
    check("17:00 is outside the session", session_bucket("2026-09-03T17:00:00") is None)
    check("an unparseable stamp gives None", session_bucket("not a date") is None)
    check("an empty stamp gives None", session_bucket("") is None)

    # The defect that made section 3 unusable on this project's own data.
    # Every file here is UTC; reading .hour off it filed the open as midday.
    check("13:50 UTC is 09:50 Eastern, in the open hour",
          session_bucket("2026-07-30T13:50:19+00:00") == "open_hour")
    check("13:31 UTC is the first 15 minutes",
          session_bucket("2026-07-30T13:31:00+00:00") == "first_15m")
    check("a UTC afternoon stamp is midday, not the last hour",
          session_bucket("2026-07-30T16:00:00+00:00") == "midday")
    check("19:55 UTC is 15:55 Eastern, the last 15",
          session_bucket("2026-07-30T19:55:00+00:00") == "last_15m")
    check("a Z suffix converts the same way",
          session_bucket("2026-07-30T13:50:19Z") == "open_hour")
    check("a naive stamp is still read as Eastern",
          session_bucket("2026-07-30T09:50:00") == "open_hour")
    check("winter dates use EST, not a fixed offset",
          session_bucket("2026-01-15T14:50:00+00:00") == "open_hour")
    check("and the same clock time in summer uses EDT",
          session_bucket("2026-07-15T14:50:00+00:00") == "midday")

    samples = ([{"timestamp": "2026-09-03T09:35:00", "bid": 1.00, "ask": 1.20}
                for _ in range(12)]
               + [{"timestamp": "2026-09-03T12:00:00", "bid": 1.00, "ask": 1.02}
                  for _ in range(12)]
               + [{"timestamp": "2026-09-03T08:00:00", "bid": 1.00, "ask": 1.02}])
    tod = spread_by_bucket(samples)
    check("open spread is wider than midday",
          tod["buckets"]["first_15m"]["median_spread"]
          > tod["buckets"]["midday"]["median_spread"])
    check("out-of-session rows are counted, not dropped", tod["unplaced"] == 1)
    check("an empty bucket is suppressed", tod["buckets"]["last_15m"]["suppressed"])

    # --- expiry and strike ------------------------------------------------
    check("third Friday of Sept 2026 is the 18th",
          third_friday(2026, 9) == date(2026, 9, 18))
    check("third Friday of Jan 2027 is the 15th",
          third_friday(2027, 1) == date(2027, 1, 15))
    check("a third Friday reads as monthly", is_monthly_expiry("2026-09-18") is True)
    check("a non-third Friday reads as weekly", is_monthly_expiry("2026-09-11") is False)
    check("a bad expiry gives None", is_monthly_expiry("2026-13-99") is None)
    check("an empty expiry gives None", is_monthly_expiry("") is None)
    check("a date object works", is_monthly_expiry(date(2026, 9, 18)) is True)
    check("strike 90 divides by 10", strike_granularity(90) == "10")
    check("strike 95 divides by 5", strike_granularity(95) == "5")
    check("strike 92 divides by 1", strike_granularity(92) == "1")
    check("strike 92.50 divides by 0.50", strike_granularity(92.50) == "0.50")
    check("strike 92.37 is other", strike_granularity(92.37) == "other")
    check("a bad strike gives None", strike_granularity("x") is None)

    micro_rows = ([{"symbol": "TLT", "bid": 1.00, "ask": 1.02,
                    "timestamp": "2026-09-03T12:00:00",
                    "expiry": "2026-09-18", "strike": 90} for _ in range(12)]
                  + [{"symbol": "TLT", "bid": 1.00, "ask": 1.15,
                      "timestamp": "2026-09-03T12:05:00",
                      "expiry": "2026-09-11", "strike": 92.37} for _ in range(12)])
    micro = microstructure_by_underlying(micro_rows)
    check("monthly is tighter than weekly on the same name",
          micro["symbols"]["TLT"]["monthly"]["median_spread"]
          < micro["symbols"]["TLT"]["weekly"]["median_spread"])
    check("round strikes are grouped separately",
          "step_10" in micro["symbols"]["TLT"])
    check("unusable microstructure rows are counted",
          microstructure_by_underlying([{"symbol": "", "bid": 1, "ask": 2}])["skipped"] == 1)

    # The time-of-day control. Without it, a weekly sampled at the open looks
    # wide because of the clock, not the contract.
    check("section 4 reports it is time-controlled by default",
          micro["time_controlled"] is True and micro["bucket_used"] == "midday")
    confounded = ([{"symbol": "TLT", "bid": 1.00, "ask": 1.30,
                    "timestamp": "2026-09-03T09:32:00",
                    "expiry": "2026-09-11", "strike": 92.37} for _ in range(12)]
                  + micro_rows)
    controlled = microstructure_by_underlying(confounded)
    check("open-session rows are excluded by the control",
          controlled["outside_bucket"] == 12)
    check("the control keeps the weekly verdict honest",
          close(controlled["symbols"]["TLT"]["weekly"]["median_spread"],
                micro["symbols"]["TLT"]["weekly"]["median_spread"]))
    uncontrolled = microstructure_by_underlying(confounded, restrict_bucket=None)
    check("disabling the control admits the open rows",
          uncontrolled["symbols"]["TLT"]["weekly"]["n"] == 24)
    check("disabling the control is flagged unreliable",
          uncontrolled["time_controlled"] is False)
    check("the uncontrolled weekly figure is inflated by the clock",
          uncontrolled["symbols"]["TLT"]["weekly"]["median_spread"]
          > controlled["symbols"]["TLT"]["weekly"]["median_spread"])
    check("rows with no timestamp are excluded under the control",
          microstructure_by_underlying(
              [{"symbol": "TLT", "bid": 1.0, "ask": 1.02,
                "expiry": "2026-09-18", "strike": 90}])["outside_bucket"] == 1)

    # --- conventions ------------------------------------------------------
    # Asserting == 10 would pass after the shared value moved, which is the
    # drift itself. Compare against the owning module instead.
    try:
        import learning_report
        check("the subgroup floor IS learning_report's, not a copy of it",
              MINIMUM_GROUP_TRADES is learning_report.MINIMUM_GROUP_TRADES
              or MINIMUM_GROUP_TRADES == learning_report.MINIMUM_GROUP_TRADES)
    except Exception:
        check("the subgroup floor IS learning_report's, not a copy of it", False)
    check("percent formatting handles None", pct(None) == "--")
    check("percent formatting scales a fraction", pct(0.0952) == "9.52%")
    check("a missing CSV reads as empty, not an error",
          read_rows("._execution_cost_absent.csv") == [])

    # --- CSV round trip ---------------------------------------------------
    tmp = "._execution_attempts_test.csv"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "attempt_id", "symbol", "option_symbol", "side", "limit_price",
                "quote_bid", "quote_ask", "filled", "fill_price",
                "seconds_to_fill", "underlying_move_after", "window_seconds",
                "note"])
            w.writeheader()
            w.writerow({"attempt_id": "a1", "symbol": "tlt", "option_symbol": "O",
                        "side": "buy", "limit_price": "1.05", "quote_bid": "1.00",
                        "quote_ask": "1.10", "filled": "TRUE", "fill_price": "1.05",
                        "seconds_to_fill": "12", "underlying_move_after": "-0.02",
                        "window_seconds": "3600", "note": ""})
            w.writerow({"attempt_id": "a2", "symbol": "tlt", "option_symbol": "O",
                        "side": "buy", "limit_price": "1.05", "quote_bid": "1.00",
                        "quote_ask": "1.10", "filled": "", "fill_price": "",
                        "seconds_to_fill": "", "underlying_move_after": "",
                        "window_seconds": "", "note": "quote gap"})
        back = attempts_from_rows(read_rows(tmp))
        check("CSV round trip keeps both rows", len(back) == 2)
        check("CSV upcases the symbol", back[0].symbol == "TLT")
        check("CSV parses a true fill", back[0].filled is True)
        check("a blank fill flag reads as unknown", back[1].filled is None)
        check("a blank fill price reads as None", back[1].fill_price is None)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    passed = sum(1 for _, ok in checks if ok)
    print(f"\nSELF TEST  ({passed}/{len(checks)} passed)")
    for name, ok in checks:
        if not ok:
            print(f"  [FAIL] {name}")
    if passed == len(checks):
        print("All offline checks passed. Nothing was sent to any broker.")
        return 0
    print("\nFAILURES ABOVE. Do not run against live data yet.")
    return 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure what options execution actually costs. Read only.")
    parser.add_argument("--self-test", action="store_true",
                        help="offline checks, no files, no network")
    parser.add_argument("--report", action="store_true",
                        help="read this module's CSVs and print the report")
    parser.add_argument("--save", action="store_true",
                        help=f"also write {REPORT_OUT}")
    parser.add_argument("--build-fills", action="store_true",
                        help=f"assemble {FILL_LOG} by joining the completed "
                             "options journal to the quote samples")
    args = parser.parse_args()

    print(f"execution_cost.py v{VERSION}  (read only -- places no orders)")

    if args.self_test:
        return self_test()

    if args.build_fills:
        outcome = build_fills_from_journals()
        print(f"  completed option trades : {outcome['trades']}")
        print(f"  quote samples available : {outcome['samples']}")
        print(f"  matched to a quote      : {outcome['matched']}")
        print(f"  unmatched               : {outcome['unmatched']}")
        for reason, count in outcome["reasons"].items():
            print(f"      {count:>4}  {reason}")
        print(f"  rows written to {FILL_LOG}: {outcome['written']}")
        if outcome["written"]:
            print("\n  exit_bid and exit_ask are BLANK on every row. The book at")
            print("  exit is recorded nowhere -- options_manager fetches a quote")
            print("  to decide the stop and discards it. Entry-side slippage is")
            print("  usable now; the round-trip figure needs that change first.")
        return 0

    if not args.report:
        parser.print_help()
        return 0

    fills = read_rows(FILL_LOG)
    attempts = attempts_from_rows(read_rows(LIMIT_ATTEMPT_LOG))
    samples = read_rows(QUOTE_SAMPLE_LOG)
    print_report(fills, attempts, samples)
    if args.save:
        write_report(fills, attempts, samples)
    return 0


if __name__ == "__main__":
    sys.exit(main())
