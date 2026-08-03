"""
LOCKBOT Trade Grader v0.2

Reads completed trades from completed_trades.csv
and assigns each trade an R-multiple and letter grade.

This module is read-only.
It does not submit, modify, replace, or cancel orders.
"""

from __future__ import annotations

import csv
from pathlib import Path


COMPLETED_TRADES_FILE = Path(__file__).with_name(
    "completed_trades.csv"
)


def calculate_r_multiple(
    profit_loss: float,
    initial_risk_dollars: float,
) -> float:
    """Calculate the trade's R-multiple."""

    if initial_risk_dollars <= 0:
        return 0.0

    return profit_loss / initial_risk_dollars


def assign_grade(r_multiple: float) -> str:
    """Convert an R-multiple into a letter grade."""

    if r_multiple >= 2.0:
        return "A+"

    if r_multiple >= 1.5:
        return "A"

    if r_multiple >= 1.0:
        return "B"

    if r_multiple > 0:
        return "C"

    if r_multiple > -1.0:
        return "D"

    return "F"


def grade_trade(
    profit_loss: float,
    initial_risk_dollars: float,
) -> tuple[float, str]:
    """Return the R-multiple and letter grade."""

    r_multiple = calculate_r_multiple(
        profit_loss,
        initial_risk_dollars,
    )

    grade = assign_grade(r_multiple)

    return r_multiple, grade


def load_completed_trades() -> list[dict[str, str]]:
    """Load completed trades from the CSV file."""

    if not COMPLETED_TRADES_FILE.exists():
        return []

    with COMPLETED_TRADES_FILE.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as csv_file:

        reader = csv.DictReader(csv_file)

        return list(reader)


def display_trade_grades(
    trades: list[dict[str, str]],
) -> None:
    """Display the grade for each completed trade."""

    print("=" * 70)
    print("LOCKBOT TRADE GRADER v0.2")
    print("=" * 70)

    if not trades:
        print("Completed Trades : 0")
        print("Status           : WAITING FOR COMPLETED TRADES")
        return

    print(f"Completed Trades : {len(trades)}")
    print("-" * 70)

    for trade in trades:

        symbol = trade.get("symbol", "UNKNOWN")
        side = trade.get("side", "UNKNOWN")
        trade_id = trade.get("trade_id", "UNKNOWN")

        try:
            profit_loss = float(
                trade.get("profit_loss", "0") or 0
            )

            initial_risk = float(
                trade.get("initial_risk_dollars", "0") or 0
            )

        except ValueError:
            print(
                f"{symbol} | Trade ID: {trade_id} | "
                "Status: INVALID NUMERIC DATA"
            )
            continue

        r_multiple, grade = grade_trade(
            profit_loss,
            initial_risk,
        )

        print(
            f"{symbol:<6} | "
            f"{side:<5} | "
            f"P/L: ${profit_loss:>9.2f} | "
            f"Risk: ${initial_risk:>9.2f} | "
            f"R: {r_multiple:>6.2f}R | "
            f"Grade: {grade}"
        )


def main() -> None:
    """Run the completed-trade grading report."""

    trades = load_completed_trades()

    display_trade_grades(trades)


if __name__ == "__main__":
    main()