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
    reading. The report prints the ambiguous-excluded rate beside it so the
    assumption is visible rather than buried; today they are the same number,
    because zero rows in 427 are ambiguous. Fills are assumed at the exact
    stop or target price, so real slippage would make these results slightly
    worse than they look.

WHY EXPIRED EXISTS (2026-08-10)
    A setup whose window ran out without touching either band was written
    back as UNRESOLVED with a fresh resolved_at. Two things followed, and
    both were invisible.

    rows_needing_resolution accepts UNRESOLVED, so every aged-out row was
    re-queued and re-fetched on every run, forever — 59 of them by the time
    it was found, against a 10-day window that had long since closed.

    Worse, they were censored rather than excluded. The decided sample is
    whatever touched a band inside 10 days, which is the FAST movers; the
    setups that went nowhere simply left the statistics. That biases the
    win rate on a sample already known to be thin.

    EXPIRED is terminal, so the re-fetch stops, and it carries a
    mark-to-market R so the slow population is visible. It is deliberately
    NOT counted in the win rate — a target-touch rate must keep meaning a
    target-touch rate — and appears instead as a separate ALL-IN line.

    The mark is None, never 0.0, when it cannot be computed. `simulate_symbol`
    once left timed-out trades at an r_multiple of 0.0 and thereby claimed
    they broke even; a default value is a claim.
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

# One owner for CSV header migration across every journal. See its
# docstring for why this is not solved inside each writer.
import csv_schema

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

# --------------------------------------------------------------------------
# Which POOL produced a setup
# --------------------------------------------------------------------------
#
# LOCKBOT's ruling of 2026-08-08, made a hard precondition of the
# broad-market expansion (c6812f3a) and binding on any change to the scan
# population: every shadow row must carry a pool-generation field BEFORE a
# new pool goes live, so pre- and post-change populations are segmented and
# never pooled in a win-rate statistic. Shipping a pool change without the
# tag contaminates the only forward measurement this project has.
#
# WHY IT IS DERIVED AND NOT A CONSTANT SOMEONE BUMPS
#
# A hand-maintained generation number is a thing to forget, and this
# project's failure mode is precisely the switch that reads as configuration
# while controlling nothing. So the generation is a fingerprint of the RULES
# that define the pool. Change a threshold and the generation changes by
# itself; change nothing and it stays put. It is impossible to widen the
# universe without the rows recording that you did.
#
# The fingerprint covers the pool DEFINITION, not the resulting symbols.
# universe.csv churns daily as names cross the liquidity line, and a
# content hash would mint a new generation every morning, which is the
# opposite of useful.
POOL_RULE_KEYS = (
    "UNIVERSE_MIN_PRICE",
    "UNIVERSE_MAX_PRICE",
    "UNIVERSE_MIN_ATR_PERCENT",
    "UNIVERSE_MAX_ATR_PERCENT",
    "UNIVERSE_MIN_AVG_DOLLAR_VOLUME",
    "UNIVERSE_TOP_N",
    "MAX_SCAN_SYMBOLS",
    "UNIVERSE_ALLOWED_EXCHANGES",
    "UNIVERSE_OPTIONABLE_ONLY",      # absent today; appears when it ships
)


def pool_rules() -> dict:
    """The live values of every constant that defines the scan pool."""

    out = {}

    for key in POOL_RULE_KEYS:
        value = _cfg(key, None)
        if value is None:
            continue
        out[key] = list(value) if isinstance(value, (list, tuple)) else value

    return out


def pool_generation(rules: Optional[dict] = None) -> str:
    """Short stable fingerprint of the pool definition.

    Same rules -> same string, across machines and runs. Different rules ->
    different string, with no human in the loop.
    """

    import hashlib
    import json

    payload = json.dumps(pool_rules() if rules is None else rules,
                         sort_keys=True, separators=(",", ":"))

    return "pool_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def describe_pool(rules: Optional[dict] = None) -> str:
    """Human-readable pool description, so a future reader knows WHAT
    changed rather than only that something did."""

    live = pool_rules() if rules is None else rules

    return "; ".join(f"{k}={live[k]}" for k in sorted(live))

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
    # Which POOL produced this setup, added 2026-08-12. See the note beside
    # POOL_RULE_KEYS. Rows written before it carry blanks, which is itself
    # the legacy generation and is reported as such rather than merged into
    # whatever the pool happens to be today.
    "pool_generation",
]

# Rows predating the tag. Named rather than left as "" so the report can
# say what it is instead of showing an empty cell.
POOL_LEGACY = "pool_untagged"

OUTCOME_PENDING = "PENDING"
OUTCOME_TARGET = "TARGET"
OUTCOME_STOP = "STOP"
OUTCOME_AMBIGUOUS = "AMBIGUOUS"
OUTCOME_UNRESOLVED = "UNRESOLVED"
OUTCOME_NO_DATA = "NO_DATA"

# A setup whose SHADOW_MAX_DAYS window ran out without touching either
# band. UNRESOLVED means "not decided YET"; EXPIRED means "never will
# be". They were the same string until 2026-08-10, which cost twice:
# rows_needing_resolution re-queued every aged-out row on every run, and
# the aged-out population was censored from every statistic rather than
# merely excluded from the win rate.
OUTCOME_EXPIRED = "EXPIRED"


# --------------------------------------------------------------------------
# Recording (called by market_scanner.py)
# --------------------------------------------------------------------------

def ensure_file(path: Path = SHADOW_FILE) -> List[str]:
    """Make the file safe to write, and RETURN THE HEADER to write against.

    Delegates to csv_schema, which owns this for every journal in the
    project. Converted 2026-08-13 on LOCKBOT's ruling, after it found a
    live hole in the hand-rolled version this replaced.

    THE HOLE, kept on record because the comment that caused it read as
    prudence: the old code computed `missing = [c for c in COLUMNS if c
    not in existing]` and returned early when nothing was missing, with
    the comment "reordered or extra: leave alone". A WIDER header -- one
    written by NEWER code -- produces no missing columns, so it took that
    branch, left the header alone, and let record_candidates append rows
    of 26 values under a 27-column header. Demonstrated: the appended row
    reads back with the newer column as None, silently unpopulated, no
    exception, file opens fine.

    Identifying the case and choosing to ignore it is how the defect
    survived. csv_schema REFUSES it instead, because a wider header means
    the running code is older than the file.
    """

    return csv_schema.ensure_schema(path, COLUMNS, verbose=True)


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
        # Write against the header csv_schema VERIFIED ON DISK, never
        # against COLUMNS. That mismatch is the root of all three
        # occurrences of the askew-write bug.
        header = ensure_file(path)

        # Computed once per cycle, not per row: it is a property of the
        # pool definition, and every setup in one scan came from the same
        # pool by construction.
        generation = pool_generation()

        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=header)

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
                    "pool_generation": generation,
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

    # The last close seen inside the window, so an expiring row can be
    # marked to market instead of vanishing. None when no bar carried
    # one -- absent, not zero.
    last_close: Optional[float] = None


def mark_to_market_r(
    *,
    entry_price: Optional[float],
    stop_price: Optional[float],
    last_close: Optional[float],
    side: str,
) -> Optional[float]:
    """
    What a setup was worth in R when its window ran out.

    Timeouts have been booked wrong in this project once already: in
    `simulate_symbol` an unclosed trade kept `r_multiple` at its 0.0
    default, so a position that ended wherever price happened to be was
    recorded as having made exactly nothing. A default value is a claim.

    So this returns None -- not 0.0 -- whenever the mark cannot be
    computed. The caller writes an empty cell, and an unmeasurable
    outcome stays unmeasured rather than becoming a breakeven one.
    """

    try:
        entry = float(entry_price) if entry_price else None
        stop = float(stop_price) if stop_price is not None else None
        close = float(last_close) if last_close is not None else None
    except (TypeError, ValueError):
        return None

    if not entry or stop is None or close is None:
        return None

    risk = abs(entry - stop)

    if not risk:
        return None

    is_long = str(side).upper() in {"LONG", "BUY_LONG", "BUY"}
    move = (close - entry) if is_long else (entry - close)

    return round(move / risk, 4)


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
    last_close = None

    for bar in bars:
        bar_time = getattr(bar, "timestamp", None)

        if bar_time is not None and bar_time <= start_time:
            continue

        high = float(getattr(bar, "high", 0) or 0)
        low = float(getattr(bar, "low", 0) or 0)

        if high <= 0 or low <= 0:
            continue

        checked += 1

        bar_close = getattr(bar, "close", None)

        if bar_close is not None:
            try:
                last_close = float(bar_close)
            except (TypeError, ValueError):
                pass

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
        "last_close": last_close,
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
    """Rewrite the whole book. THE RESOLVER'S WRITER, and the second hole.

    LOCKBOT flagged this one on 2026-08-13: it rewrites with `fieldnames=
    COLUMNS`, so against a WIDER header -- a file touched by newer code --
    it would silently DELETE the surplus columns and every value in them.
    A full rewrite is more destructive than a bad append, not less.

    So it gates on the same check. If csv_schema refuses, nothing is
    written and the exception propagates: the resolver is an offline
    batch job, and stopping it is the correct response to a file this
    code version cannot safely own.
    """

    header = csv_schema.ensure_schema(path, COLUMNS, verbose=False)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in header})


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

        outcome = resolution.outcome
        r_multiple = resolution.r_multiple

        # Out of window and still undecided: it never will be. Give it a
        # terminal outcome so it stops being re-fetched every run, and
        # mark it to market so the population that goes nowhere is
        # visible rather than silently dropped.
        if outcome == OUTCOME_UNRESOLVED:
            outcome = OUTCOME_EXPIRED
            r_multiple = mark_to_market_r(
                entry_price=entry_price,
                stop_price=float(row["stop_price"]),
                last_close=resolution.last_close,
                side=row["side"],
            )

        row["outcome"] = outcome
        row["bars_checked"] = resolution.bars_checked
        row["resolved_at"] = now.isoformat()
        row["r_multiple"] = "" if r_multiple is None else r_multiple

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


def win_rates(rows: List[dict]) -> dict:
    """
    The win rate both ways, because one number hides a judgement call.

    An AMBIGUOUS bar spans stop and target, so 5-minute data cannot say
    which came first and it is booked as a loss. That is the pessimistic
    reading and it stays the headline -- every figure on record, and
    every regime split in the notes, is computed that way.

    Excluding ambiguity instead would be the optimistic reading, and
    silently switching to it would shift every historical comparison at
    once. So both are reported and neither is hidden: the pair bounds
    the true rate rather than point-estimating it.

    In practice this currently changes nothing -- there are zero
    ambiguous rows in 427, because at ~4.5% stops and ~9% targets a
    single bar would need a ~13.5% range. It is reported so that if the
    bracket ever narrows, the assumption is visible rather than buried.
    """

    decided = [r for r in rows
               if r["outcome"] in {OUTCOME_TARGET, OUTCOME_STOP, OUTCOME_AMBIGUOUS}]
    wins = sum(1 for r in decided if r["outcome"] == OUTCOME_TARGET)
    ambiguous = sum(1 for r in decided if r["outcome"] == OUTCOME_AMBIGUOUS)
    unambiguous = len(decided) - ambiguous

    return {
        "decided": len(decided),
        "wins": wins,
        "ambiguous": ambiguous,
        "win_rate": wins / len(decided) if decided else 0.0,
        "win_rate_ex_ambiguous": wins / unambiguous if unambiguous else 0.0,
    }


def cohort_maturity(rows: List[dict], *, now=None) -> tuple[bool, str]:
    """Is every row in this slice old enough to have finished?

    THE 2026-08-29 READING HAZARD. An independent analysis reported week
    35 at -0.93R from 118 stops and 3 targets with ZERO expired, and
    concluded the resolver was censoring results. It was not: the
    resolver is faithful and test-pinned. Those rows were simply still
    INSIDE their SHADOW_MAX_DAYS window.

    But the hazard the analysis identified is real, even though its
    diagnosis was wrong. STOPS RESOLVE FASTEST -- a stop can be booked
    within hours, while an expiry takes the full window -- so any slice
    read before its rows mature is biased toward losses by construction.
    A partial read is not a small error; it has a known sign.

    So the refusal lives here, in the tool that reports, rather than in
    the resolver that was never broken. A slice containing any row too
    young to have finished cannot be summarised at all.
    """

    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=SHADOW_MAX_DAYS)
    young = 0
    unresolved = 0

    for row in rows:
        stamp = (row.get("logged_at") or "").strip()

        if not (row.get("outcome") or "").strip():
            unresolved += 1

        try:
            logged = datetime.fromisoformat(stamp)
        except ValueError:
            # A row whose age cannot be read is a row that cannot be shown
            # to have finished. Counted as young, never skipped -- the
            # first version `continue`d here, so a malformed date passed
            # as mature. Fail closed: the whole point of this gate is that
            # an unreadable row must not license a reading.
            young += 1
            continue

        if logged.tzinfo is None:
            logged = logged.replace(tzinfo=timezone.utc)

        if logged > cutoff:
            young += 1

    if young or unresolved:
        return False, (f"{young} row(s) younger than the {SHADOW_MAX_DAYS}-day "
                       f"window and {unresolved} unresolved -- stops resolve "
                       "fastest, so reading this now is biased toward losses")

    return True, "all rows mature"


def _print_group(label: str, rows: List[dict], min_count: int = 5) -> None:
    mature, why = cohort_maturity(rows)

    if not mature:
        print(f"  {label:<22} {len(rows):>4} rows     NOT READABLE: {why}")
        return

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

    # ---- POOL SEGMENTATION, before any headline number is printed.
    #
    # A win rate spanning two pool definitions is a number about neither of
    # them. This prints the split first and refuses to lead with a pooled
    # figure whenever more than one generation is present.
    by_pool: Dict[str, List[dict]] = {}
    for row in decided:
        by_pool.setdefault(row.get("pool_generation") or POOL_LEGACY, []).append(row)

    print("\nWhich pool produced these setups")
    print("-" * 62)
    print(f"  current pool definition: {pool_generation()}")
    print(f"  {describe_pool()}")
    print()
    for name, group in sorted(by_pool.items(), key=lambda kv: -len(kv[1])):
        stats = _summarize(group)
        label = name + ("  (predates the tag)" if name == POOL_LEGACY else "")
        if stats["count"] >= 5:
            print(f"  {label:<34} {stats['count']:>4} decided   "
                  f"win rate {stats['win_rate'] * 100:>5.1f}%   "
                  f"avg R {stats['avg_r']:>+5.2f}")
        else:
            print(f"  {label:<34} {stats['count']:>4} decided   (too few to read)")

    if len(by_pool) > 1:
        print()
        print("  MORE THAN ONE POOL IS PRESENT. The figures below span all of")
        print("  them and are NOT a statement about any single pool. Read the")
        print("  per-pool lines above instead; the scan population changed,")
        print("  and a win rate across a population change measures the")
        print("  change as much as the strategy.")

    rates = win_rates(decided)

    print("\nHeadline")
    print("-" * 62)
    print(f"  decided setups       : {rates['decided']}")
    print(f"  win rate             : {rates['win_rate'] * 100:.1f}%"
          f"   (ambiguous counted as losses)")

    if rates["ambiguous"]:
        print(f"  win rate excl. ambig : "
              f"{rates['win_rate_ex_ambiguous'] * 100:.1f}%"
              f"   ({rates['ambiguous']} ambiguous excluded)")
    else:
        print("  win rate excl. ambig : same — no ambiguous bars on record")

    # The setups that went nowhere. Kept OUT of the win rate, because a
    # target-touch rate must keep meaning target-touch rate -- but shown,
    # because they are exactly the slow movers the decided sample drops,
    # and their absence is what makes that sample fast-mover-enriched.
    expired = [r for r in rows if r["outcome"] == OUTCOME_EXPIRED]

    if expired:
        marked = [float(r["r_multiple"]) for r in expired
                  if r.get("r_multiple") not in {"", None}]
        decided_r = [float(r["r_multiple"]) for r in decided
                     if r.get("r_multiple") not in {"", None}]

        print(f"  expired (no touch)   : {len(expired)}"
              f"   — excluded from the win rate above")

        if marked:
            print(f"  their avg R at mark  : {sum(marked) / len(marked):+.2f}"
                  f"   ({len(marked)} of {len(expired)} markable)")

            all_in = decided_r + marked
            print(f"  ALL-IN avg R         : {sum(all_in) / len(all_in):+.2f}"
                  f"   (decided + expired at mark, {len(all_in)} setups)")

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
    def __init__(self, timestamp, high, low, close=None):
        self.timestamp = timestamp
        self.high = high
        self.low = low
        # Real Alpaca bars always carry a close; the older tests predate
        # it being needed, so it stays optional.
        self.close = close


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

    # ---- expiry, 2c
    #
    # A row past SHADOW_MAX_DAYS used to be written back as UNRESOLVED
    # with a fresh resolved_at, so rows_needing_resolution picked it up
    # again on every run -- 59 rows re-fetched forever and censored from
    # every statistic. It gets a terminal outcome now.
    print("Pool generation: derived from the rules, not remembered")
    base = {"UNIVERSE_MIN_PRICE": 5.0, "UNIVERSE_MAX_PRICE": 50.0,
            "UNIVERSE_MIN_ATR_PERCENT": 0.0125}
    check("the same rules give the same generation",
          pool_generation(base) == pool_generation(dict(base)))
    check("key order does not change it",
          pool_generation({"b": 2, "a": 1}) == pool_generation({"a": 1, "b": 2}))
    check("widening the price band changes it",
          pool_generation(base)
          != pool_generation(dict(base, UNIVERSE_MAX_PRICE=2000.0)))
    check("dropping the volatility band changes it",
          pool_generation(base)
          != pool_generation({k: v for k, v in base.items()
                              if k != "UNIVERSE_MIN_ATR_PERCENT"}))
    check("adding an optionable-only rule changes it",
          pool_generation(base)
          != pool_generation(dict(base, UNIVERSE_OPTIONABLE_ONLY=True)))
    check("the generation is short and readable",
          pool_generation(base).startswith("pool_")
          and len(pool_generation(base)) == 13)
    check("the live pool has a generation",
          pool_generation().startswith("pool_"))
    check("and a human-readable description of WHAT it is",
          "UNIVERSE_MAX_PRICE" in describe_pool())
    check("the column is in the schema", "pool_generation" in COLUMNS)
    check("untagged rows are named, not left as an empty cell",
          POOL_LEGACY == "pool_untagged")

    # The silent one: appending 26 fields under a 25-column header.
    migrate = Path(tempfile.gettempdir()) / "shadow_migrate_selftest.csv"
    migrate.unlink(missing_ok=True)
    old_columns = [c for c in COLUMNS if c != "pool_generation"]
    with migrate.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=old_columns)
        writer.writeheader()
        writer.writerow({c: "" for c in old_columns} | {
            "shadow_id": "OLD", "symbol": "AAA", "side": "LONG",
            "outcome": OUTCOME_STOP, "r_multiple": -1.0})

    # ---- THE WIDER-HEADER REFUSAL, requested by LOCKBOT before
    # options_scanner is converted. This is the case the hand-rolled
    # ensure_file waved through: a file written by NEWER code, where
    # nothing is "missing" so the early return fired and rows were then
    # appended short under a wide header.
    newer = Path(tempfile.gettempdir()) / "shadow_wider_selftest.csv"
    newer.unlink(missing_ok=True)
    wider_cols = list(COLUMNS) + ["written_by_newer_code"]
    with newer.open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=wider_cols)
        w.writeheader()
        w.writerow({c: "" for c in wider_cols} | {
            "shadow_id": "NEWER", "outcome": OUTCOME_STOP,
            "written_by_newer_code": "KEEP ME"})
    before_bytes = newer.read_bytes()

    refused = False
    try:
        ensure_file(newer)
    except csv_schema.SchemaRefused:
        refused = True
    check("a WIDER header is refused, not waved through", refused)
    check("and the file is left byte-identical", newer.read_bytes() == before_bytes)

    refused_write = False
    try:
        save_rows([{"shadow_id": "X"}], newer)
    except csv_schema.SchemaRefused:
        refused_write = True
    check("save_rows refuses it too, rather than deleting the surplus column",
          refused_write)
    check("the newer column's value survives both attempts",
          "KEEP ME" in newer.read_text(encoding="utf-8"))

    check("record_candidates writes nothing to a refused file",
          record_candidates([{
              "logged_at": start, "symbol": "NOPE", "side": "LONG",
              "confidence": 100, "volume_ratio": 1.0, "regime": "T",
              "reference_price": 100, "stop_price": 98, "target_price": 104,
              "taken": False}], newer) == 0)
    check("so the refused file still holds exactly its one original row",
          len(csv_schema.read_rows(newer)) == 1)
    newer.unlink(missing_ok=True)

    ensure_file(migrate)
    migrated = load_rows(migrate)
    check("an older file gains the new column on contact",
          "pool_generation" in migrated[0])
    check("and its existing row is backfilled blank, never guessed",
          migrated[0]["pool_generation"] == "")
    check("while its recorded verdict is untouched",
          migrated[0]["outcome"] == OUTCOME_STOP
          and migrated[0]["shadow_id"] == "OLD")

    record_candidates([{
        "logged_at": start, "symbol": "NEW", "side": "LONG", "confidence": 100,
        "volume_ratio": 1.5, "regime": "TRENDING", "reference_price": 100,
        "stop_price": 98, "target_price": 104, "taken": False,
    }], migrate)
    after = load_rows(migrate)
    check("a row appended after migration is readable, not shifted",
          len(after) == 2 and after[1]["symbol"] == "NEW")
    check("and carries the live pool generation",
          after[1]["pool_generation"] == pool_generation())
    check("the old row still reads back correctly beside it",
          after[0]["shadow_id"] == "OLD" and after[0]["outcome"] == OUTCOME_STOP)
    migrate.unlink(missing_ok=True)

    print("Expiry: a window that ran out is EXPIRED, not UNRESOLVED")

    try:
        # Entry 100, stop 98 -> 1R is $2. Last close 101 is +0.5R.
        window = [
            _Bar(start + timedelta(minutes=5), 101, 99, close=100.5),
            _Bar(start + timedelta(minutes=10), 102, 100, close=101.0),
        ]
        undecided = resolve_from_bars(window, "LONG", 98, 104, start,
                                      entry_price=100)

        check("an undecided window still reports UNRESOLVED",
              undecided.outcome == OUTCOME_UNRESOLVED)
        check("but it now carries the last close for marking to market",
              undecided.last_close == 101.0)

        check("mark to market is measured in R (long)",
              abs(mark_to_market_r(entry_price=100, stop_price=98,
                                   last_close=101, side="LONG") - 0.5) < 1e-9)
        check("mark to market is measured in R (short)",
              abs(mark_to_market_r(entry_price=100, stop_price=102,
                                   last_close=99, side="SHORT") - 0.5) < 1e-9)
        check("a losing mark to market is negative",
              mark_to_market_r(entry_price=100, stop_price=98,
                               last_close=99, side="LONG") < 0)
        check("no close means no mark to market, not a zero one",
              mark_to_market_r(entry_price=100, stop_price=98,
                               last_close=None, side="LONG") is None)
        check("no entry price means no mark to market either",
              mark_to_market_r(entry_price=None, stop_price=98,
                               last_close=101, side="LONG") is None)

        expiry_file = Path(tempfile.gettempdir()) / "shadow_expiry_selftest.csv"
        expiry_file.unlink(missing_ok=True)

        stale = datetime.now(timezone.utc) - timedelta(days=SHADOW_MAX_DAYS + 2)
        record_candidates([
            {"logged_at": stale, "symbol": "OLD", "side": "LONG",
             "confidence": 100, "volume_ratio": 1.5, "regime": "TRENDING",
             "reference_price": 100, "stop_price": 98, "target_price": 104,
             "taken": False},
        ], expiry_file)

        def _never_touches(symbols, begin, finish):
            return {"OLD": [
                _Bar(stale + timedelta(minutes=5), 101, 99, close=100.5),
                _Bar(stale + timedelta(minutes=10), 102, 100, close=101.0),
            ]}

        resolve_pending(expiry_file, bar_fetcher=_never_touches)
        aged = load_rows(expiry_file)

        check("a row past its window is EXPIRED",
              aged[0]["outcome"] == OUTCOME_EXPIRED)
        check("and is marked to market rather than left blank",
              abs(float(aged[0]["r_multiple"]) - 0.5) < 1e-9)
        check("an EXPIRED row is never queued for resolution again",
              rows_needing_resolution(aged, datetime.now(timezone.utc)) == [])

        check("EXPIRED is not counted as decided",
              _summarize(aged)["count"] == 0)

        mixed = [
            {"outcome": OUTCOME_TARGET, "r_multiple": 2.0},
            {"outcome": OUTCOME_TARGET, "r_multiple": 2.0},
            {"outcome": OUTCOME_STOP, "r_multiple": -1.0},
            {"outcome": OUTCOME_AMBIGUOUS, "r_multiple": -1.0},
        ]
        rates = win_rates(mixed)
        check("headline win rate still counts ambiguous as a loss",
              abs(rates["win_rate"] - 0.5) < 1e-9)
        check("and an ambiguous-excluded rate is reported beside it",
              abs(rates["win_rate_ex_ambiguous"] - (2 / 3)) < 1e-9)
        check("with the ambiguous count named",
              rates["ambiguous"] == 1)

        expiry_file.unlink(missing_ok=True)
    except NameError as error:
        failures.append(f"expiry machinery missing: {error}")
        print(f"  FAIL  expiry machinery missing: {error}")

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
    print("A slice with immature rows cannot be summarised at all")

    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    _now = _dt(2026, 8, 29, 12, tzinfo=_tz.utc)
    _old = (_now - _td(days=SHADOW_MAX_DAYS + 5)).isoformat()
    _new = (_now - _td(days=1)).isoformat()

    mature, why = cohort_maturity(
        [{"logged_at": _old, "outcome": "STOP"},
         {"logged_at": _old, "outcome": "EXPIRED"}], now=_now)
    check("a fully resolved, fully aged slice is readable", mature)

    # THE 08-29 HAZARD. Stops book within hours, expiries take the full
    # window, so a young slice reads as losses by construction.
    mature, why = cohort_maturity(
        [{"logged_at": _old, "outcome": "STOP"},
         {"logged_at": _new, "outcome": "STOP"}], now=_now)
    check("one row inside the window blocks the whole slice", not mature)

    mature, why = cohort_maturity(
        [{"logged_at": _old, "outcome": "STOP"},
         {"logged_at": _old, "outcome": ""}], now=_now)
    check("an unresolved row blocks it too", not mature)

    # A row with an unreadable timestamp must not silently pass as mature.
    mature, why = cohort_maturity(
        [{"logged_at": "not-a-date", "outcome": ""}], now=_now)
    check("an unreadable date still counts as unresolved", not mature)

    check("an empty slice is vacuously mature",
          cohort_maturity([], now=_now)[0])



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