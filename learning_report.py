"""
LOCKBOT Learning Report v1.0

A READ-ONLY report that looks for patterns in LOCKBOT's completed,
graded trades and surfaces them as evidence for a human to consider —
it never changes any setting, threshold, or configuration itself, and
it never submits, modifies, or cancels any order.

Safety design (please read before using this):
- It refuses to report anything at all until there are at least
  MINIMUM_TOTAL_TRADES completed trades, because patterns drawn from
  a small sample are usually just noise wearing a pattern's costume.
- Within that, each individual breakdown group (e.g. "confidence
  90-100") is only reported if it has at least MINIMUM_GROUP_TRADES
  trades of its own — a single lucky or unlucky small group doesn't
  get treated as a real signal.
- It never phrases anything as an instruction ("raise this
  threshold"). It only ever describes what the data shows, with the
  sample size attached, so you can judge for yourself how much weight
  to give it.

Usage:
    python learning_report.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from performance_engine import load_completed_trades
from trade_grader import calculate_r_multiple

# Below this many total completed trades, no breakdown is shown at
# all — just a plain "not enough data yet" message. 30 is a common
# rule-of-thumb floor for even beginning to trust a win-rate estimate;
# it is still a small sample, not a guarantee, which is why every
# breakdown below also carries its own trade count.
MINIMUM_TOTAL_TRADES = 30

# Below this many trades, an individual group (e.g. one confidence
# bucket, one regime) is skipped in the breakdown entirely, rather
# than reported on a handful of trades.
MINIMUM_GROUP_TRADES = 10


@dataclass
class GroupStats:
    label: str
    count: int
    win_rate_percent: float
    average_r_multiple: float
    average_pnl: float


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def compute_group_stats(label: str, trades: list[dict[str, Any]]) -> GroupStats | None:
    """Compute stats for one group of trades, or None if too small to report."""

    if len(trades) < MINIMUM_GROUP_TRADES:
        return None

    pnl_values = [to_float(t.get("profit_loss")) for t in trades]
    r_multiples = [
        calculate_r_multiple(
            to_float(t.get("profit_loss")),
            to_float(t.get("initial_risk_dollars")),
        )
        for t in trades
    ]

    wins = sum(1 for pnl in pnl_values if pnl > 0)
    win_rate = wins / len(trades) * 100
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0
    avg_pnl = sum(pnl_values) / len(pnl_values) if pnl_values else 0.0

    return GroupStats(
        label=label,
        count=len(trades),
        win_rate_percent=round(win_rate, 1),
        average_r_multiple=round(avg_r, 2),
        average_pnl=round(avg_pnl, 2),
    )


def print_group_table(title: str, groups: list[GroupStats | None]) -> None:
    valid_groups = [g for g in groups if g is not None]
    skipped_count = len(groups) - len(valid_groups)

    print()
    print(title)
    print("-" * len(title))

    if not valid_groups:
        print(
            f"  No group here yet has {MINIMUM_GROUP_TRADES}+ trades — "
            "nothing reportable."
        )
        return

    print(f"  {'Group':<22}{'Trades':<9}{'Win Rate':<11}{'Avg R':<9}Avg P/L")
    for g in sorted(valid_groups, key=lambda g: g.label):
        print(
            f"  {g.label:<22}{g.count:<9}{g.win_rate_percent:>6.1f}%   "
            f"{g.average_r_multiple:>6.2f}R  ${g.average_pnl:,.2f}"
        )

    if skipped_count:
        print(
            f"  ({skipped_count} smaller group(s) not shown — "
            f"fewer than {MINIMUM_GROUP_TRADES} trades each)"
        )


def bucket_by_confidence(trade: dict[str, Any]) -> str:
    confidence = to_int(trade.get("confidence"))

    if confidence >= 95:
        return "95-100"
    if confidence >= 90:
        return "90-94"
    if confidence >= 85:
        return "85-89"
    if confidence >= 80:
        return "80-84"
    return "below 80"


def group_trades(trades: list[dict[str, Any]], key_func) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        key = key_func(trade)
        grouped.setdefault(key, []).append(trade)
    return grouped


def main() -> None:
    print("=" * 60)
    print("       LOCKBOT LEARNING REPORT v1.0")
    print("=" * 60)
    print("This is a read-only report. It changes nothing on its own.")

    trades = load_completed_trades()
    total = len(trades)

    print()
    print(f"Completed trades available : {total}")
    print(f"Minimum required to report : {MINIMUM_TOTAL_TRADES}")

    if total < MINIMUM_TOTAL_TRADES:
        print()
        print(
            f"Not enough completed trades yet ({total} of "
            f"{MINIMUM_TOTAL_TRADES} needed). No patterns will be "
            "reported — with this few trades, any apparent pattern is "
            "much more likely to be noise than a real signal. Check "
            "back once more trades have completed."
        )
        print("=" * 60)
        return

    # --- Overall baseline, for comparison ---
    overall = compute_group_stats("ALL TRADES", trades)
    print()
    print("Overall Baseline")
    print("-" * 16)
    if overall:
        print(
            f"  {overall.count} trades — {overall.win_rate_percent}% win rate, "
            f"{overall.average_r_multiple}R average, ${overall.average_pnl:,.2f} average P/L"
        )

    # --- By confidence score ---
    by_confidence = group_trades(trades, bucket_by_confidence)
    print_group_table(
        "By Confidence Score",
        [compute_group_stats(label, group) for label, group in by_confidence.items()],
    )

    # --- By side (long vs short) ---
    by_side = group_trades(trades, lambda t: str(t.get("side", "UNKNOWN")).upper())
    print_group_table(
        "By Side (Long vs Short)",
        [compute_group_stats(label, group) for label, group in by_side.items()],
    )

    # --- By market regime ---
    by_regime = group_trades(trades, lambda t: str(t.get("market_regime", "UNKNOWN")).upper())
    print_group_table(
        "By Market Regime",
        [compute_group_stats(label, group) for label, group in by_regime.items()],
    )

    # --- By exit reason ---
    by_exit_reason = group_trades(trades, lambda t: str(t.get("exit_reason", "UNKNOWN")).upper())
    print_group_table(
        "By Exit Reason",
        [compute_group_stats(label, group) for label, group in by_exit_reason.items()],
    )

    # --- By symbol ---
    by_symbol = group_trades(trades, lambda t: str(t.get("symbol", "UNKNOWN")).upper())
    print_group_table(
        "By Symbol",
        [compute_group_stats(label, group) for label, group in by_symbol.items()],
    )

    print()
    print("=" * 60)
    print(
        "Reminder: these are observations, not instructions. Even a "
        f"reportable group ({MINIMUM_GROUP_TRADES}+ trades) is still a "
        "fairly small sample — treat a single run of this report as a "
        "starting point for a conversation, not a verdict. Nothing "
        "here has been changed automatically."
    )
    print("Status: COMPLETE")


if __name__ == "__main__":
    main()