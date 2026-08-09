"""
shadow_trades.py — what would have happened if LOCKBOT had taken the others.

THE PROBLEM THIS SOLVES
    On 7/27 LOCKBOT approved 356 setups and had room for 5. Which 5 it picks
    is now almost the whole outcome — and every approved setup scores 100/100,
    so the pick is effectively made by volume ratio alone. Nobody knows whether
    that ranks well.

HOW IT WORKS
    market_scanner.py records every approved setup here, marking whether it
    was actually taken. Later this script replays the price history after each
    one and asks a simple question: did it reach its target, or its stop, first?

    Nothing is traded. No orders, no money, no risk. It only reads price data
    that already exists.

USAGE
    python shadow_trades.py              # resolve what's pending, then report
    python shadow_trades.py --report     # report only, no data fetching
    python shadow_trades.py --self-test  # offline logic check

HONEST LIMITS
    Resolution uses 5-minute bars. When a single bar's high and low span both
    the stop and the target, there's no way to know which came first — those
    are marked AMBIGUOUS and counted as losses, which is the pessimistic
    reading. Fills are assumed at the exact stop or target price, so real
    slippage would make these results slightly worse than they look.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    import lockbot_config as config
except Exception:
    config = None


def _cfg(name, default):
    if config is not None and hasattr(config, name):
        return getattr(config, name)
    return default


PROJECT_FOLDER = Path(__file__).resolve().parent
SHADOW_FILE = Path(_cfg("SHADOW_TRADES_FILE", PROJECT_FOLDER / "shadow_trades.csv"))

# How long a shadow trade gets to reach a level before it's called unresolved.
#
# Raised from 3 to 10 on 2026-07-29. Three days was sized for the fixed
# 2%/4% bracket. Adaptive brackets widened the median setup to a 3.2%
# stop and a 6.4% target, and a name that moves ~2.3% on an average day
# physically cannot travel 6.4% in three days except in a straight line.
#
# That made the horizon a silent filter rather than a timeout: wide
# brackets time out unresolved and never enter the sample, while tight
# ones resolve and do. Every win rate computed from that sample is then
# measuring fast movers, not the strategy. Ten days is roughly the same
# ratio of horizon to target distance that three days gave the old
# bracket. The cost is slower feedback, which is the right trade — a
# late number beats a biased one.
SHADOW_MAX_DAYS = _cfg("SHADOW_MAX_DAYS", 10)

# Don't try to resolve anything younger than this — it needs time to play out.
SHADOW_MIN_AGE_MINUTES = _cfg("SHADOW_MIN_AGE_MINUTES", 60)

SHADOW_BATCH_SIZE = _cfg("SHADOW_BATCH_SIZE", 100)
DATA_FEED = str(_cfg("ALPACA_DATA_FEED", "iex")).lower()

COLUMNS = [
    "shadow_id",
    "logged_at",
    "symbol",
    "side",
    "confidence",
    "volume_ratio",
    "regime",
    "reference_price",
    "stop_price",
    "target_price",
    "taken",
    "outcome",
    "resolved_at",
    "bars_checked",
    "r_multiple",
    # Ranking inputs, added 2026-07-29. Each is written per setup so the
    # report can eventually say which components carry information and
    # which don't — the old ranking could not be evaluated at all,
    # because its only variable input was volume_ratio. Rows written
    # before this exists simply carry blanks.
    "quality",
    "q_trend_strength",
    "q_momentum",
    "q_conviction",
    "q_restraint",
    "q_volume_ratio",
    # Trade ANATOMY, added 2026-08-08 for agent_channel 39a7685e — the
    # owner's directive to study how swing trades actually move rather
    # than only whether a rule wins.
    #
    # resolve_from_bars already walked the price path to decide STOP or
    # TARGET and threw all of it away, so every setup ever logged has had
    # its full story available and recorded only the ending.
    #
    # post_stop_recovered is the one that matters. For a losing trade it
    # asks whether price later reached the original target anyway. If most
    # of the 135 stops recovered, the entries were fine and the stops were
    # too tight, which is fixable. If they did not, the entries were
    # simply wrong. Nothing in this project can currently tell those two
    # apart, and they call for opposite responses.
    "mae_r",               # worst excursion against, in R (<= 0)
    "mfe_r",               # best excursion for, in R (>= 0)
    "bars_to_mfe_peak",
    "post_stop_recovered",
]

OUTCOME_PENDING = "PENDING"
OUTCOME_TARGET = "TARGET"
OUTCOME_STOP = "STOP"
OUTCOME_AMBIGUOUS = "AMBIGUOUS"
OUTCOME_UNRESOLVED = "UNRESOLVED"
OUTCOME_NO_DATA = "NO_DATA"


# --------------------------------------------------------------------------
# Recording (called by market_scanner.py)
# --------------------------------------------------------------------------

def ensure_file(path: Path = SHADOW_FILE) -> None:
    if not path.exists() or path.stat().st_size == 0:
        with path.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=COLUMNS).writeheader()


def record_candidates(candidates: List[dict], path: Path = SHADOW_FILE) -> int:
    """
    Append every approved setup from one scan cycle.

    Each dict needs: logged_at, symbol, side, confidence, volume_ratio,
    regime, reference_price, stop_price, target_price, taken.
    Never raises — a logging failure must not interrupt trading.
    """

    if not candidates:
        return 0

    try:
        ensure_file(path)

        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)

            for candidate in candidates:
                logged_at = candidate.get("logged_at")
                if isinstance(logged_at, datetime):
                    logged_at = logged_at.isoformat()

                writer.writerow({
                    "shadow_id": f"{candidate['symbol']}-{logged_at}",
                    "logged_at": logged_at,
                    "symbol": candidate["symbol"],
                    "side": candidate["side"],
                    "confidence": candidate.get("confidence", ""),
                    "volume_ratio": round(float(candidate.get("volume_ratio", 0)), 4),
                    "regime": candidate.get("regime", ""),
                    "reference_price": round(float(candidate["reference_price"]), 4),
                    "stop_price": round(float(candidate["stop_price"]), 4),
                    "target_price": round(float(candidate["target_price"]), 4),
                    "taken": bool(candidate.get("taken", False)),
                    "outcome": OUTCOME_PENDING,
                    "resolved_at": "",
                    "bars_checked": "",
                    "r_multiple": "",
                    "quality": candidate.get("quality", ""),
                    **{
                        f"q_{name}": round(float(value), 4)
                        for name, value in (
                            candidate.get("quality_components") or {}
                        ).items()
                    },
                })

        return len(candidates)

    except Exception as error:
        print(f"shadow_trades: could not record candidates ({error})")
        return 0


# --------------------------------------------------------------------------
# Resolution logic (pure — this is what --self-test exercises)
# --------------------------------------------------------------------------

@dataclass
class Resolution:
    outcome: str
    bars_checked: int
    r_multiple: Optional[float]

    # The anatomy. None when no entry price was supplied, so every
    # existing caller and self-test behaves exactly as before.
    mae_r: Optional[float] = None
    mfe_r: Optional[float] = None
    bars_to_mfe_peak: Optional[int] = None
    post_stop_recovered: Optional[bool] = None


def resolve_from_bars(
    bars: Iterable,
    side: str,
    stop_price: float,
    target_price: float,
    start_time: datetime,
    entry_price: Optional[float] = None,
) -> Resolution:
    """
    Walk forward through bars and find which level was touched first.

    A bar that spans both levels is AMBIGUOUS — 5-minute data can't say which
    came first, so it's counted as a loss rather than guessed in our favour.

    WHY THIS NO LONGER RETURNS AT THE FIRST TOUCH

    It used to, and that discarded the entire question the owner asked on
    2026-08-08: how do these trades actually move? The outcome is decided
    at the first touch and NOTHING after it was ever seen — including,
    for a losing trade, whether price went on to reach the target anyway.

    So the walk now continues to the end of the window. The outcome is
    still fixed at the first touch, so no verdict changes and every
    existing self-test holds; what changes is that the path is measured
    rather than thrown away.

    Pass `entry_price` to get the anatomy. Without it the extra fields
    stay None and the behaviour is identical to before.
    """

    is_long = str(side).upper() in {"LONG", "BUY_LONG", "BUY"}
    checked = 0

    risk = None

    if entry_price:
        risk = abs(float(entry_price) - float(stop_price)) or None

    outcome: Optional[str] = None
    outcome_r: Optional[float] = None
    bars_at_outcome = 0

    best = worst = None
    bars_to_peak = 0
    recovered = None

    for bar in bars:
        bar_time = getattr(bar, "timestamp", None)

        if bar_time is not None and bar_time <= start_time:
            continue

        high = float(getattr(bar, "high", 0) or 0)
        low = float(getattr(bar, "low", 0) or 0)

        if high <= 0 or low <= 0:
            continue

        checked += 1

        # ---- excursions, measured on every bar in the window
        if risk:
            favourable = (high - entry_price) if is_long else (entry_price - low)
            adverse = (low - entry_price) if is_long else (entry_price - high)

            if best is None or favourable > best:
                best = favourable
                bars_to_peak = checked

            if worst is None or adverse < worst:
                worst = adverse

        if is_long:
            hit_target = high >= target_price
            hit_stop = low <= stop_price
        else:
            hit_target = low <= target_price
            hit_stop = high >= stop_price

        # ---- the outcome is still whatever was touched FIRST
        if outcome is None:
            if hit_target and hit_stop:
                outcome, outcome_r = OUTCOME_AMBIGUOUS, -1.0
            elif hit_target:
                outcome, outcome_r = OUTCOME_TARGET, 2.0
            elif hit_stop:
                outcome, outcome_r = OUTCOME_STOP, -1.0

            if outcome is not None:
                bars_at_outcome = checked

                # The noise-vs-trend flag starts False and is set below if
                # the target is reached later in the window.
                if outcome in (OUTCOME_STOP, OUTCOME_AMBIGUOUS):
                    recovered = False

        # ---- did a stopped-out trade go on to reach its target anyway?
        elif recovered is False and hit_target:
            recovered = True

    if checked == 0:
        return Resolution(OUTCOME_NO_DATA, 0, None)

    anatomy = {
        "mae_r": round(worst / risk, 4) if risk and worst is not None else None,
        "mfe_r": round(best / risk, 4) if risk and best is not None else None,
        "bars_to_mfe_peak": bars_to_peak if risk and best is not None else None,
        "post_stop_recovered": recovered,
    }

    if outcome is not None:
        return Resolution(outcome, bars_at_outcome, outcome_r, **anatomy)

    return Resolution(OUTCOME_UNRESOLVED, checked, None, **anatomy)


def load_rows(path: Path = SHADOW_FILE) -> List[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def save_rows(rows: List[dict], path: Path = SHADOW_FILE) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})


def parse_time(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def rows_needing_resolution(rows: List[dict], now: datetime) -> List[dict]:
    cutoff = now - timedelta(minutes=SHADOW_MIN_AGE_MINUTES)
    pending = []

    for row in rows:
        if row.get("outcome") not in {OUTCOME_PENDING, OUTCOME_UNRESOLVED, OUTCOME_NO_DATA, ""}:
            continue
        logged_at = parse_time(row.get("logged_at", ""))
        if logged_at is None or logged_at > cutoff:
            continue
        pending.append(row)

    return pending


# --------------------------------------------------------------------------
# Alpaca access
# --------------------------------------------------------------------------

def fetch_bars_for(symbols: List[str], start: datetime, end: datetime) -> Dict[str, list]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")

    if not key or not secret:
        raise RuntimeError("Alpaca credentials not found in .env")

    client = StockHistoricalDataClient(key, secret)
    out: Dict[str, list] = {}

    for index in range(0, len(symbols), SHADOW_BATCH_SIZE):
        chunk = symbols[index:index + SHADOW_BATCH_SIZE]
        print(f"  fetching bars for {len(chunk)} symbol(s)…")

        kwargs = dict(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            end=end,
        )

        try:
            from alpaca.data.enums import DataFeed
            kwargs["feed"] = DataFeed.SIP if DATA_FEED == "sip" else DataFeed.IEX
        except Exception:
            pass

        try:
            bar_set = client.get_stock_bars(StockBarsRequest(**kwargs))
        except Exception as error:
            print(f"  batch failed, skipping: {type(error).__name__}: {error}")
            continue

        for symbol, bars in (getattr(bar_set, "data", {}) or {}).items():
            out.setdefault(symbol, []).extend(bars)

    return out


def resolve_pending(path: Path = SHADOW_FILE, bar_fetcher=fetch_bars_for) -> int:
    rows = load_rows(path)

    if not rows:
        print("No shadow trades recorded yet.")
        return 0

    now = datetime.now(timezone.utc)
    pending = rows_needing_resolution(rows, now)

    if not pending:
        print("Nothing pending to resolve.")
        return 0

    print(f"Resolving {len(pending)} shadow trade(s)…")

    earliest = min(parse_time(row["logged_at"]) for row in pending)
    symbols = sorted({row["symbol"] for row in pending})

    bars_by_symbol = bar_fetcher(symbols, earliest, now)

    resolved_count = 0

    for row in pending:
        logged_at = parse_time(row["logged_at"])
        horizon_end = logged_at + timedelta(days=SHADOW_MAX_DAYS)

        bars = [
            bar for bar in bars_by_symbol.get(row["symbol"], [])
            if getattr(bar, "timestamp", now) <= horizon_end
        ]

        try:
            entry_price = float(row.get("reference_price") or 0) or None
        except (TypeError, ValueError):
            entry_price = None

        resolution = resolve_from_bars(
            bars=bars,
            side=row["side"],
            stop_price=float(row["stop_price"]),
            target_price=float(row["target_price"]),
            start_time=logged_at,
            entry_price=entry_price,
        )

        # Still inside its window and undecided — leave it for next time.
        if resolution.outcome == OUTCOME_UNRESOLVED and now < horizon_end:
            row["outcome"] = OUTCOME_UNRESOLVED
            row["bars_checked"] = resolution.bars_checked
            continue

        row["outcome"] = resolution.outcome
        row["bars_checked"] = resolution.bars_checked
        row["resolved_at"] = now.isoformat()
        row["r_multiple"] = "" if resolution.r_multiple is None else resolution.r_multiple

        # The anatomy. Blank rather than a guess when it could not be
        # measured -- an absent excursion must not read as a zero one.
        for field, value in (
            ("mae_r", resolution.mae_r),
            ("mfe_r", resolution.mfe_r),
            ("bars_to_mfe_peak", resolution.bars_to_mfe_peak),
            ("post_stop_recovered", resolution.post_stop_recovered),
        ):
            row[field] = "" if value is None else value

        resolved_count += 1

    save_rows(rows, path)
    print(f"Resolved {resolved_count}.")
    return resolved_count


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _summarize(rows: List[dict]) -> dict:
    decided = [r for r in rows if r["outcome"] in {OUTCOME_TARGET, OUTCOME_STOP, OUTCOME_AMBIGUOUS}]

    if not decided:
        return {"count": 0, "wins": 0, "win_rate": 0.0, "avg_r": 0.0}

    wins = sum(1 for r in decided if r["outcome"] == OUTCOME_TARGET)
    total_r = sum(float(r["r_multiple"]) for r in decided if r["r_multiple"] not in {"", None})

    return {
        "count": len(decided),
        "wins": wins,
        "win_rate": wins / len(decided),
        "avg_r": total_r / len(decided),
    }


def _print_group(label: str, rows: List[dict], min_count: int = 5) -> None:
    stats = _summarize(rows)

    if stats["count"] < min_count:
        print(f"  {label:<22} {stats['count']:>4} decided  (too few to read into)")
        return

    print(
        f"  {label:<22} {stats['count']:>4} decided   "
        f"win rate {stats['win_rate'] * 100:>5.1f}%   "
        f"avg R {stats['avg_r']:>+5.2f}"
    )


def report(path: Path = SHADOW_FILE) -> None:
    rows = load_rows(path)

    print("=" * 62)
    print("        LOCKBOT SHADOW TRADE REPORT")
    print("=" * 62)

    if not rows:
        print("No shadow trades recorded yet.")
        print("They start accumulating the next time the scanner approves a setup.")
        return

    outcomes: Dict[str, int] = {}
    for row in rows:
        outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1

    print(f"Total logged setups : {len(rows)}")
    for outcome, count in sorted(outcomes.items(), key=lambda item: -item[1]):
        print(f"  {outcome:<12}: {count}")

    decided = [r for r in rows if r["outcome"] in {OUTCOME_TARGET, OUTCOME_STOP, OUTCOME_AMBIGUOUS}]

    if not decided:
        print("\nNothing has resolved yet. Run again after a few sessions.")
        return

    taken = [r for r in decided if str(r["taken"]).lower() == "true"]
    passed = [r for r in decided if str(r["taken"]).lower() != "true"]

    print("\nDid LOCKBOT pick the right ones?")
    print("-" * 62)
    _print_group("Setups it took", taken)
    _print_group("Setups it passed on", passed)

    taken_stats = _summarize(taken)
    passed_stats = _summarize(passed)

    if taken_stats["count"] >= 5 and passed_stats["count"] >= 5:
        gap = taken_stats["win_rate"] - passed_stats["win_rate"]
        print()
        if gap > 0.05:
            print(f"  Its picks beat the ones it skipped by {gap * 100:.1f} points.")
            print("  The ranking is adding value.")
        elif gap < -0.05:
            print(f"  Its picks did WORSE than the ones it skipped, by {abs(gap) * 100:.1f} points.")
            print("  The ranking is actively hurting. Worth changing how it chooses.")
        else:
            print("  Its picks and its skips performed about the same.")
            print("  The ranking isn't adding anything — it may as well be random.")

    # Does volume ratio — the current tiebreaker — actually predict anything?
    with_volume = [r for r in decided if r.get("volume_ratio") not in {"", None}]

    if len(with_volume) >= 20:
        ordered = sorted(with_volume, key=lambda r: float(r["volume_ratio"]))
        half = len(ordered) // 2

        print("\nIs volume ratio a good tiebreaker?")
        print("-" * 62)
        _print_group("Lower volume half", ordered[:half])
        _print_group("Higher volume half", ordered[half:])

    by_side: Dict[str, List[dict]] = {}
    for row in decided:
        by_side.setdefault(row["side"], []).append(row)

    if len(by_side) > 1:
        print("\nBy direction")
        print("-" * 62)
        for side, side_rows in sorted(by_side.items()):
            _print_group(side, side_rows)

    print("\n" + "=" * 62)
    print("Reminder: these are simulated fills at exact stop/target prices,")
    print("so real results would be slightly worse. Ambiguous bars count as")
    print("losses. Treat this as directional evidence, not a P&L statement.")


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

class _Bar:
    def __init__(self, timestamp, high, low):
        self.timestamp = timestamp
        self.high = high
        self.low = low


def _self_test() -> int:
    failures = []

    def check(label, condition):
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")

    start = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)

    def bars(*pairs):
        return [
            _Bar(start + timedelta(minutes=5 * (i + 1)), high, low)
            for i, (high, low) in enumerate(pairs)
        ]

    print("Long trades (entry 100, stop 98, target 104):")
    result = resolve_from_bars(bars((101, 100), (104, 102)), "LONG", 98, 104, start)
    check("target hit -> TARGET, +2R", result.outcome == OUTCOME_TARGET and result.r_multiple == 2.0)

    result = resolve_from_bars(bars((101, 100), (100, 97)), "LONG", 98, 104, start)
    check("stop hit -> STOP, -1R", result.outcome == OUTCOME_STOP and result.r_multiple == -1.0)

    result = resolve_from_bars(bars((105, 97),), "LONG", 98, 104, start)
    check("one bar spans both -> AMBIGUOUS, counted as a loss",
          result.outcome == OUTCOME_AMBIGUOUS and result.r_multiple == -1.0)

    result = resolve_from_bars(bars((101, 99), (102, 100)), "LONG", 98, 104, start)
    check("neither level touched -> UNRESOLVED", result.outcome == OUTCOME_UNRESOLVED)

    result = resolve_from_bars([], "LONG", 98, 104, start)
    check("no bars -> NO_DATA", result.outcome == OUTCOME_NO_DATA)

    print("Short trades (entry 100, stop 102, target 96):")
    result = resolve_from_bars(bars((100, 99), (99, 96)), "SHORT", 102, 96, start)
    check("price falls to target -> TARGET", result.outcome == OUTCOME_TARGET)

    result = resolve_from_bars(bars((103, 100),), "SHORT", 102, 96, start)
    check("price rises to stop -> STOP", result.outcome == OUTCOME_STOP)

    print("Ordering:")
    earlier = [_Bar(start - timedelta(minutes=30), 999, 1)] + bars((104, 102))
    result = resolve_from_bars(earlier, "LONG", 98, 104, start)
    check("bars before entry are ignored", result.outcome == OUTCOME_TARGET)

    stop_first = bars((99, 97), (105, 104))
    result = resolve_from_bars(stop_first, "LONG", 98, 104, start)
    check("whichever comes first wins", result.outcome == OUTCOME_STOP)

    # ---- trade anatomy, 39a7685e
    #
    # The verdict must not move. The walk now continues past the first
    # touch to measure the path, and if that changed any outcome the
    # entire shadow record would be rewritten by a measurement change.
    print("Anatomy: the verdict is unchanged by measuring the path")

    check("a stop that later reaches target is STILL a STOP",
          resolve_from_bars(stop_first, "LONG", 98, 104, start,
                            entry_price=100).outcome == OUTCOME_STOP)
    check("and still -1R",
          resolve_from_bars(stop_first, "LONG", 98, 104, start,
                            entry_price=100).r_multiple == -1.0)
    check("bars_checked still counts to the DECISION, not the window",
          resolve_from_bars(stop_first, "LONG", 98, 104, start,
                            entry_price=100).bars_checked == 1)

    print("Anatomy: the noise-vs-trend flag")

    recovered = resolve_from_bars(stop_first, "LONG", 98, 104, start,
                                  entry_price=100)
    check("a stop whose target arrives later is flagged recovered",
          recovered.post_stop_recovered is True)

    never = resolve_from_bars(bars((99, 97), (99, 98)), "LONG", 98, 104,
                              start, entry_price=100)
    check("a stop that never recovers is flagged False",
          never.post_stop_recovered is False)

    won = resolve_from_bars(bars((101, 100), (104, 102)), "LONG", 98, 104,
                            start, entry_price=100)
    check("a winner has no recovery flag at all",
          won.post_stop_recovered is None)

    print("Anatomy: excursions in R")

    # Entry 100, stop 98 -> 1R is $2. High 103 is +1.5R, low 99 is -0.5R.
    path = resolve_from_bars(bars((101, 99), (103, 100)), "LONG", 98, 104,
                             start, entry_price=100)
    check("mfe is measured in R", abs(path.mfe_r - 1.5) < 1e-9)
    check("mae is measured in R and is negative",
          abs(path.mae_r - (-0.5)) < 1e-9)
    check("bars_to_mfe_peak names the bar", path.bars_to_mfe_peak == 2)
    check("mae <= 0 <= mfe", path.mae_r <= 0 <= path.mfe_r)

    print("Anatomy: it stays absent rather than guessing")

    bare = resolve_from_bars(stop_first, "LONG", 98, 104, start)
    check("no entry price means no anatomy, not a zero one",
          bare.mae_r is None and bare.mfe_r is None)
    check("but the outcome is unaffected", bare.outcome == OUTCOME_STOP)
    check("a zero entry price is refused too",
          resolve_from_bars(stop_first, "LONG", 98, 104, start,
                            entry_price=0).mae_r is None)

    check("the new columns are in the schema",
          {"mae_r", "mfe_r", "bars_to_mfe_peak",
           "post_stop_recovered"} <= set(COLUMNS))

    print("File round trip:")
    temp = Path(tempfile.gettempdir()) / "shadow_selftest.csv"
    temp.unlink(missing_ok=True)

    written = record_candidates([
        {"logged_at": start, "symbol": "TEST", "side": "LONG", "confidence": 100,
         "volume_ratio": 1.5, "regime": "TRENDING", "reference_price": 100,
         "stop_price": 98, "target_price": 104, "taken": True},
        {"logged_at": start, "symbol": "OTHER", "side": "LONG", "confidence": 100,
         "volume_ratio": 1.2, "regime": "TRENDING", "reference_price": 50,
         "stop_price": 49, "target_price": 52, "taken": False},
    ], temp)

    check("two rows written", written == 2)
    loaded = load_rows(temp)
    check("rows read back", len(loaded) == 2)
    check("taken flag preserved",
          [r["taken"] for r in loaded] == ["True", "False"])
    check("everything starts PENDING",
          all(r["outcome"] == OUTCOME_PENDING for r in loaded))

    old_enough = rows_needing_resolution(loaded, datetime.now(timezone.utc))
    check("old rows are picked up for resolution", len(old_enough) == 2)

    fresh = load_rows(temp)
    for row in fresh:
        row["logged_at"] = datetime.now(timezone.utc).isoformat()
    check("rows too young are skipped",
          len(rows_needing_resolution(fresh, datetime.now(timezone.utc))) == 0)

    check("recording never raises on bad input",
          record_candidates([{"symbol": "BROKEN"}], temp) == 0)

    temp.unlink(missing_ok=True)

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {failures}")
        return 1
    print("All self-tests passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LOCKBOT shadow trade tracking")
    parser.add_argument("--report", action="store_true", help="report only, don't fetch data")
    parser.add_argument("--self-test", action="store_true", help="offline logic check")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if not args.report:
        try:
            resolve_pending()
        except Exception as error:
            print(f"Could not resolve pending trades: {type(error).__name__}: {error}")
        print()

    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())