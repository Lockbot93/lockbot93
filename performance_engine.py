"""
LOCKBOT Performance Engine v1.0

Consolidates the two previous, independent performance-tracking
systems (performance_analytics.py's JSON running-totals cache and
performance_stats.py's richer CSV-derived statistics) into one module
with one source of truth: the completed-trades CSV that
trade_journal.py actually writes to.

Previously, performance_stats.py pointed at a different, never-written
filename ("lockbot_trade_journal.csv") — so anything that depended on
it (daily reports, the richer notification pipeline) silently always
saw zero completed trades. This module reads directly from
trade_journal.COMPLETED_TRADES_FILE instead of hardcoding its own copy
of that path, so it cannot drift out of sync again.

This module is read-only. It does not submit, modify, replace, or
cancel brokerage orders.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from trade_journal import COMPLETED_TRADES_FILE


@dataclass(frozen=True)
class PerformanceStats:
    """Calculated statistics for completed LOCKBOT trades."""

    total_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate_percent: float
    gross_profit: float
    gross_loss: float
    net_profit: float
    average_win: float
    average_loss: float
    largest_win: float
    largest_loss: float
    profit_factor: float | None
    expectancy: float
    average_return_percent: float
    average_holding_minutes: float
    longest_winning_streak: int
    longest_losing_streak: int
    starting_equity: float
    estimated_current_equity: float


def _to_float(value: Any, *, field_name: str, row_number: int) -> float:
    """Convert one journal value to a float."""

    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Invalid {field_name!r} value on journal row {row_number}: {value!r}"
        ) from error


def load_completed_trades(
    journal_file: Path = COMPLETED_TRADES_FILE,
) -> list[dict[str, Any]]:
    """Load and validate completed trades from the journal."""

    if not journal_file.exists():
        return []

    with journal_file.open(
        mode="r",
        newline="",
        encoding="utf-8-sig",
    ) as journal:
        reader = csv.DictReader(journal)

        required_columns = {
            "trade_id",
            "profit_loss",
            "return_percent",
            "holding_minutes",
            "account_equity_at_entry",
        }

        available_columns = set(reader.fieldnames or [])
        missing_columns = required_columns - available_columns

        if missing_columns:
            missing_text = ", ".join(sorted(missing_columns))
            raise ValueError(
                f"Trade journal is missing required columns: {missing_text}"
            )

        trades: list[dict[str, Any]] = []

        for row_number, row in enumerate(reader, start=2):
            trade_id = str(row.get("trade_id", "")).strip()

            if not trade_id:
                continue

            trades.append(
                {
                    **row,
                    # NOTE: trade_journal.py's completed-trade schema calls
                    # this column "profit_loss" (performance_stats.py's old
                    # copy expected "gross_pnl" — kept as an alias below for
                    # any older callers).
                    "gross_pnl": _to_float(
                        row.get("profit_loss"),
                        field_name="profit_loss",
                        row_number=row_number,
                    ),
                    "return_percent": _to_float(
                        row.get("return_percent"),
                        field_name="return_percent",
                        row_number=row_number,
                    ),
                    "holding_minutes": _to_float(
                        row.get("holding_minutes"),
                        field_name="holding_minutes",
                        row_number=row_number,
                    ),
                    "account_equity_at_entry": _to_float(
                        row.get("account_equity_at_entry"),
                        field_name="account_equity_at_entry",
                        row_number=row_number,
                    ),
                }
            )

    return trades


def _calculate_streaks(pnl_values: list[float]) -> tuple[int, int]:
    """Calculate longest winning and losing streaks."""

    longest_winning_streak = 0
    longest_losing_streak = 0
    current_winning_streak = 0
    current_losing_streak = 0

    for pnl in pnl_values:
        if pnl > 0:
            current_winning_streak += 1
            current_losing_streak = 0
            longest_winning_streak = max(longest_winning_streak, current_winning_streak)

        elif pnl < 0:
            current_losing_streak += 1
            current_winning_streak = 0
            longest_losing_streak = max(longest_losing_streak, current_losing_streak)

        else:
            current_winning_streak = 0
            current_losing_streak = 0

    return longest_winning_streak, longest_losing_streak


def calculate_performance_stats(
    trades: list[dict[str, Any]] | None = None,
    journal_file: Path = COMPLETED_TRADES_FILE,
) -> PerformanceStats:
    """Calculate LOCKBOT performance statistics."""

    if trades is None:
        trades = load_completed_trades(journal_file=journal_file)

    if not trades:
        return PerformanceStats(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            breakeven_trades=0,
            win_rate_percent=0.0,
            gross_profit=0.0,
            gross_loss=0.0,
            net_profit=0.0,
            average_win=0.0,
            average_loss=0.0,
            largest_win=0.0,
            largest_loss=0.0,
            profit_factor=None,
            expectancy=0.0,
            average_return_percent=0.0,
            average_holding_minutes=0.0,
            longest_winning_streak=0,
            longest_losing_streak=0,
            starting_equity=0.0,
            estimated_current_equity=0.0,
        )

    pnl_values = [float(trade["gross_pnl"]) for trade in trades]
    return_values = [float(trade["return_percent"]) for trade in trades]
    holding_values = [float(trade["holding_minutes"]) for trade in trades]

    winning_values = [pnl for pnl in pnl_values if pnl > 0]
    losing_values = [pnl for pnl in pnl_values if pnl < 0]
    breakeven_values = [pnl for pnl in pnl_values if pnl == 0]

    total_trades = len(pnl_values)
    winning_trades = len(winning_values)
    losing_trades = len(losing_values)
    breakeven_trades = len(breakeven_values)

    gross_profit = sum(winning_values)
    gross_loss = abs(sum(losing_values))
    net_profit = sum(pnl_values)

    win_rate_percent = (winning_trades / total_trades) * 100
    average_win = gross_profit / winning_trades if winning_trades else 0.0
    average_loss = sum(losing_values) / losing_trades if losing_trades else 0.0
    largest_win = max(winning_values) if winning_values else 0.0
    largest_loss = min(losing_values) if losing_values else 0.0

    if gross_loss > 0:
        profit_factor: float | None = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = None
    else:
        profit_factor = 0.0

    expectancy = net_profit / total_trades
    average_return_percent = sum(return_values) / total_trades
    average_holding_minutes = sum(holding_values) / total_trades

    longest_winning_streak, longest_losing_streak = _calculate_streaks(pnl_values)

    starting_equity = float(trades[0]["account_equity_at_entry"])
    estimated_current_equity = starting_equity + net_profit

    return PerformanceStats(
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        breakeven_trades=breakeven_trades,
        win_rate_percent=round(win_rate_percent, 2),
        gross_profit=round(gross_profit, 2),
        gross_loss=round(gross_loss, 2),
        net_profit=round(net_profit, 2),
        average_win=round(average_win, 2),
        average_loss=round(average_loss, 2),
        largest_win=round(largest_win, 2),
        largest_loss=round(largest_loss, 2),
        profit_factor=round(profit_factor, 2) if profit_factor is not None else None,
        expectancy=round(expectancy, 2),
        average_return_percent=round(average_return_percent, 4),
        average_holding_minutes=round(average_holding_minutes, 2),
        longest_winning_streak=longest_winning_streak,
        longest_losing_streak=longest_losing_streak,
        starting_equity=round(starting_equity, 2),
        estimated_current_equity=round(estimated_current_equity, 2),
    )


def get_performance_stats_dict(
    journal_file: Path = COMPLETED_TRADES_FILE,
) -> dict[str, Any]:
    """Return performance statistics as a dictionary."""

    stats = calculate_performance_stats(journal_file=journal_file)
    return asdict(stats)


def _format_money(value: float) -> str:
    """Format a dollar value with its sign."""

    if value > 0:
        return f"+${value:,.2f}"

    if value < 0:
        return f"-${abs(value):,.2f}"

    return "$0.00"


def _format_profit_factor(value: float | None) -> str:
    """Format profit factor for terminal output."""

    if value is None:
        return "\u221e"

    return f"{value:.2f}"


def print_performance_report(stats: PerformanceStats) -> None:
    """Print a readable terminal report."""

    print("=" * 50)
    print("       LOCKBOT PERFORMANCE ENGINE v1.0")
    print("=" * 50)
    print(f"Journal File          : {COMPLETED_TRADES_FILE.name}")
    print(f"Total Trades          : {stats.total_trades}")
    print(f"Winning Trades        : {stats.winning_trades}")
    print(f"Losing Trades         : {stats.losing_trades}")
    print(f"Breakeven Trades      : {stats.breakeven_trades}")
    print(f"Win Rate              : {stats.win_rate_percent:.2f}%")
    print(f"Gross Profit          : {_format_money(stats.gross_profit)}")
    print(f"Gross Loss            : -${stats.gross_loss:,.2f}")
    print(f"Net Profit            : {_format_money(stats.net_profit)}")
    print(f"Average Winner        : {_format_money(stats.average_win)}")
    print(f"Average Loser         : {_format_money(stats.average_loss)}")
    print(f"Largest Winner        : {_format_money(stats.largest_win)}")
    print(f"Largest Loser         : {_format_money(stats.largest_loss)}")
    print(f"Profit Factor         : {_format_profit_factor(stats.profit_factor)}")
    print(f"Expectancy Per Trade  : {_format_money(stats.expectancy)}")
    print(f"Average Return        : {stats.average_return_percent:.4f}%")
    print(f"Average Holding Time  : {stats.average_holding_minutes:.2f} minutes")
    print(f"Longest Win Streak    : {stats.longest_winning_streak}")
    print(f"Longest Loss Streak   : {stats.longest_losing_streak}")
    print(f"Starting Equity       : ${stats.starting_equity:,.2f}")
    print(f"Estimated Equity      : ${stats.estimated_current_equity:,.2f}")
    print("=" * 50)

    if stats.total_trades == 0:
        print("Status                 : NO COMPLETED TRADES")
    else:
        print("Status                 : COMPLETE")


def main() -> None:
    """Run the performance report from the terminal."""

    try:
        stats = calculate_performance_stats()
        print_performance_report(stats)

    except Exception as error:
        print("=" * 50)
        print("       LOCKBOT PERFORMANCE ENGINE v1.0")
        print("=" * 50)
        print("Status                 : ERROR")
        print(f"Error                  : {type(error).__name__}: {error}")
        raise


if __name__ == "__main__":
    main()
