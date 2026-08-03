"""
LOCKBOT Signal Log Analyzer v1.0

Reads signals.csv (every scan decision LOCKBOT has logged, whether or
not a trade was approved) and summarizes where the approval funnel is
actually bottlenecking — real evidence from your live run, instead of
guessing at which threshold to change.

This is read-only. It does not submit, modify, or cancel any order,
and does not change any configuration.

Usage:
    python analyze_signals.py
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from statistics import mean, median

SIGNALS_FILE = Path(__file__).with_name("signals.csv")


def load_rows() -> list[dict[str, str]]:
    if not SIGNALS_FILE.exists():
        print(f"{SIGNALS_FILE.name} does not exist yet. Nothing to analyze.")
        return []

    with SIGNALS_FILE.open(mode="r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_bool(value: str) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def print_section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def main() -> None:
    rows = load_rows()

    if not rows:
        return

    total = len(rows)
    print("=" * 60)
    print("       LOCKBOT SIGNAL LOG ANALYZER v1.0")
    print("=" * 60)
    print(f"Total logged scan decisions : {total}")

    # --- Signal type breakdown ---
    signal_counts = Counter(row.get("signal", "UNKNOWN") for row in rows)
    print_section("Signal Type Breakdown")
    for signal, count in signal_counts.most_common():
        print(f"  {signal:<12}: {count:>5} ({count / total * 100:.1f}%)")

    # --- Approval breakdown ---
    approved_rows = [row for row in rows if to_bool(row.get("trade_approved", ""))]
    rejected_rows = [row for row in rows if not to_bool(row.get("trade_approved", ""))]

    print_section("Approval Summary")
    print(f"  Approved : {len(approved_rows)} ({len(approved_rows) / total * 100:.1f}%)")
    print(f"  Rejected : {len(rejected_rows)} ({len(rejected_rows) / total * 100:.1f}%)")

    # --- Rejection reason breakdown (the real bottleneck finder) ---
    rejection_reasons = Counter(
        row.get("approval_reason", "UNKNOWN") for row in rejected_rows
    )
    print_section("Rejection Reason Breakdown (all rejections)")
    for reason, count in rejection_reasons.most_common():
        print(f"  {reason:<32}: {count:>5} ({count / len(rejected_rows) * 100:.1f}%)")

    # --- Same breakdown, excluding the market-closed noise, since that
    #     gate isn't about strategy calibration at all ---
    tradable_hours_rejections = [
        row for row in rejected_rows
        if row.get("approval_reason") not in {"MARKET_IS_CLOSED", "DATA_IS_STALE"}
    ]

    if tradable_hours_rejections:
        reasons_during_market_hours = Counter(
            row.get("approval_reason", "UNKNOWN")
            for row in tradable_hours_rejections
        )
        print_section(
            "Rejection Reason Breakdown (market-hours only, "
            "excludes MARKET_IS_CLOSED / DATA_IS_STALE)"
        )
        for reason, count in reasons_during_market_hours.most_common():
            print(
                f"  {reason:<32}: {count:>5} "
                f"({count / len(tradable_hours_rejections) * 100:.1f}%)"
            )
    else:
        print_section("Rejection Reason Breakdown (market-hours only)")
        print("  No rejections occurred during open market hours yet.")

    # --- Confidence score distribution for rejections that got past
    #     every earlier gate and were only blocked by confidence ---
    confidence_blocked = [
        row for row in rejected_rows
        if row.get("approval_reason") == "CONFIDENCE_TOO_LOW"
    ]

    if confidence_blocked:
        scores = [to_float(row.get("confidence_score")) for row in confidence_blocked]
        print_section("Confidence Scores Among CONFIDENCE_TOO_LOW Rejections")
        print(f"  Count  : {len(scores)}")
        print(f"  Mean   : {mean(scores):.1f}")
        print(f"  Median : {median(scores):.1f}")
        print(f"  Min    : {min(scores):.1f}")
        print(f"  Max    : {max(scores):.1f}")
        near_misses = sorted(scores, reverse=True)[:10]
        print(f"  Closest near-misses (top 10): {[round(s, 1) for s in near_misses]}")

    # --- Volume ratio distribution for rejections blocked on volume ---
    volume_blocked = [
        row for row in rejected_rows
        if row.get("approval_reason") == "LOW_VOLUME"
    ]

    if volume_blocked:
        ratios = [to_float(row.get("volume_ratio")) for row in volume_blocked]
        print_section("Volume Ratios Among LOW_VOLUME Rejections")
        print(f"  Count  : {len(ratios)}")
        print(f"  Mean   : {ratios and mean(ratios):.3f}")
        print(f"  Median : {ratios and median(ratios):.3f}")
        print(f"  Max    : {max(ratios):.3f}  (threshold is MIN_VOLUME_RATIO)")

    # --- Regime blocks, split out since these are structural, not
    #     about a single numeric threshold ---
    regime_blocked = [
        row for row in rejected_rows
        if str(row.get("approval_reason", "")).startswith("REGIME_")
    ]

    if regime_blocked:
        regime_reason_counts = Counter(
            row.get("approval_reason", "UNKNOWN") for row in regime_blocked
        )
        print_section("Regime-Related Rejections")
        for reason, count in regime_reason_counts.most_common():
            print(f"  {reason:<32}: {count:>5}")

    # --- What almost worked: signals that were BUY_LONG/SELL_SHORT
    #     (not NO_TRADE) and were rejected for exactly one reason ---
    near_misses_by_symbol = Counter(
        row.get("symbol", "UNKNOWN")
        for row in rejected_rows
        if row.get("signal") in {"BUY_LONG", "SELL_SHORT"}
        and row.get("approval_reason") not in {"MARKET_IS_CLOSED", "DATA_IS_STALE", "NO_VALID_SIGNAL"}
    )

    if near_misses_by_symbol:
        print_section("Near-Miss Signals by Symbol (had a real signal, still rejected)")
        for symbol, count in near_misses_by_symbol.most_common():
            print(f"  {symbol:<6}: {count}")

    # --- How close did SETUP_NOT_CONFIRMED rows actually come? ---
    # The scanner requires ALL FOUR of these simultaneously:
    #   BULLISH: close > ema_9, close > vwap, 50 < rsi < 70, macd > macd_signal
    #   BEARISH: close < ema_9, close < vwap, 30 < rsi < 50, macd < macd_signal
    # This counts how many of the 4 conditions were actually met on
    # rows where the trend was at least directional but the full
    # combination didn't line up — tells us whether the logic is
    # reasonably calibrated but simply rare, or consistently missing
    # by a wide margin.
    setup_not_confirmed = [
        row for row in rows
        if row.get("signal_reason") == "SETUP_NOT_CONFIRMED"
    ]

    trend_counts = Counter(row.get("trend_5m", "UNKNOWN") for row in rows)
    print_section("5-Minute Trend Direction (all scans)")
    for trend, count in trend_counts.most_common():
        print(f"  {trend:<10}: {count:>5} ({count / total * 100:.1f}%)")

    if setup_not_confirmed:
        condition_met_counts: Counter[int] = Counter()
        which_condition_failed: Counter[str] = Counter()
        skipped_neutral = 0

        for row in setup_not_confirmed:
            trend = row.get("trend_5m")
            close = to_float(row.get("latest_close"))
            ema_9 = to_float(row.get("ema_9"))
            vwap = to_float(row.get("vwap"))
            rsi = to_float(row.get("rsi"))
            macd = to_float(row.get("macd"))
            macd_signal = to_float(row.get("macd_signal"))

            if trend == "BULLISH":
                conditions = {
                    "close > ema_9": close > ema_9,
                    "close > vwap": close > vwap,
                    "50 < rsi < 70": 50 < rsi < 70,
                    "macd > macd_signal": macd > macd_signal,
                }
            elif trend == "BEARISH":
                conditions = {
                    "close < ema_9": close < ema_9,
                    "close < vwap": close < vwap,
                    "30 < rsi < 50": 30 < rsi < 50,
                    "macd < macd_signal": macd < macd_signal,
                }
            else:
                skipped_neutral += 1
                continue

            met = sum(1 for ok in conditions.values() if ok)
            condition_met_counts[met] += 1

            for label, ok in conditions.items():
                if not ok:
                    which_condition_failed[label] += 1

        directional_count = len(setup_not_confirmed) - skipped_neutral

        print_section(
            "Near-Miss Analysis: SETUP_NOT_CONFIRMED Rows "
            "(directional trend but full setup didn't align)"
        )
        print(f"  Directional (BULLISH/BEARISH) rows : {directional_count}")
        print(f"  Skipped (trend was NEUTRAL)         : {skipped_neutral}")
        print()
        print("  Conditions met out of 4 (higher = closer to qualifying):")
        for met in sorted(condition_met_counts, reverse=True):
            count = condition_met_counts[met]
            pct = count / directional_count * 100 if directional_count else 0
            print(f"    {met}/4 conditions met : {count:>5} ({pct:.1f}%)")
        print()
        print("  Which single condition failed most often (rows failing exactly that one):")
        for label, count in which_condition_failed.most_common():
            print(f"    {label:<24}: {count}")

    # --- INVALID_PREVIOUS_EQUITY timing — is this a startup artifact
    #     or an ongoing issue? ---
    invalid_equity_rows = [
        row for row in rows
        if row.get("approval_reason") == "INVALID_PREVIOUS_EQUITY"
    ]

    if invalid_equity_rows:
        timestamps = sorted(row.get("timestamp", "") for row in invalid_equity_rows if row.get("timestamp"))
        by_date = Counter(ts[:10] for ts in timestamps)

        print_section("INVALID_PREVIOUS_EQUITY Timing (is this a startup artifact?)")
        print(f"  Total occurrences : {len(invalid_equity_rows)}")
        print(f"  Earliest          : {timestamps[0] if timestamps else 'N/A'}")
        print(f"  Latest            : {timestamps[-1] if timestamps else 'N/A'}")
        print("  By date:")
        for date, count in sorted(by_date.items()):
            print(f"    {date}: {count}")

        all_dates = sorted({row.get("timestamp", "")[:10] for row in rows if row.get("timestamp")})
        distinct_occurrence_dates = sorted(by_date)

        if all_dates and len(distinct_occurrence_dates) == 1 and distinct_occurrence_dates[0] == all_dates[0]:
            print()
            print(
                "  All occurrences are on the FIRST logged date — consistent with "
                "a fresh paper account not yet having a previous trading day's "
                "closing equity to compare against. Should not recur."
            )
        elif len(distinct_occurrence_dates) == 1:
            print()
            print(
                f"  All occurrences are concentrated on a SINGLE day "
                f"({distinct_occurrence_dates[0]}) that is NOT the first day "
                "of this log — this does NOT look like a startup artifact. "
                "Something specific to that day is worth checking (e.g. an "
                "Alpaca API hiccup, or account.last_equity coming back as "
                "0/None during that window specifically). Check whether it's "
                "recurred on any day since."
            )
        else:
            print()
            print(
                f"  Occurrences span {len(distinct_occurrence_dates)} different "
                "days — this does NOT look like a one-time startup artifact. "
                "Worth investigating further."
            )

    print()
    print("=" * 60)
    print("Status: COMPLETE")


if __name__ == "__main__":
    main()