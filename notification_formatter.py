"""
LOCKBOT Notification Formatter v1.0

This module is responsible ONLY for formatting
notification titles and messages.

It never sends notifications.
"""

from __future__ import annotations


def format_money(value: float) -> str:
    """Format money with sign."""

    if value > 0:
        return f"+${value:,.2f}"

    if value < 0:
        return f"-${abs(value):,.2f}"

    return "$0.00"


def format_percent(value: float) -> str:
    """Format percentage with sign."""

    if value > 0:
        return f"+{value:.2f}%"

    if value < 0:
        return f"{value:.2f}%"

    return "0.00%"


def format_trade_closed(
    trade: dict,
) -> tuple[str, str]:
    """
    Build a completed-trade notification.
    """

    symbol = trade.get(
        "symbol",
        "UNKNOWN",
    )

    side = trade.get(
        "side",
        "UNKNOWN",
    )

    pnl = float(
        trade.get(
            "gross_pnl",
            0,
        )
    )

    return_percent = float(
        trade.get(
            "return_percent",
            0,
        )
    )

    entry = float(
        trade.get(
            "entry_price",
            0,
        )
    )

    exit_price = float(
        trade.get(
            "exit_price",
            0,
        )
    )

    duration = float(
        trade.get(
            "holding_minutes",
            0,
        )
    )

    if pnl > 0:
        title = "🟢 LOCKBOT WIN"

    elif pnl < 0:
        title = "🔴 LOCKBOT LOSS"

    else:
        title = "⚪ LOCKBOT BREAKEVEN"

    message = (
        f"{symbol} {side}\n\n"
        f"Entry: ${entry:.2f}\n"
        f"Exit : ${exit_price:.2f}\n\n"
        f"P/L: {format_money(pnl)}\n"
        f"Return: {format_percent(return_percent)}\n"
        f"Duration: {duration:.1f} min"
    )

    return title, message


def format_daily_report(
    message: str,
) -> tuple[str, str]:
    """
    Daily report notification.
    """

    return (
        "📊 LOCKBOT DAILY REPORT",
        message,
    )


def format_weekly_report(
    message: str,
) -> tuple[str, str]:
    """
    Weekly report notification.
    """

    return (
        "📈 LOCKBOT WEEKLY REPORT",
        message,
    )


def format_system_warning(
    message: str,
) -> tuple[str, str]:
    """
    System warning notification.
    """

    return (
        "⚠️ LOCKBOT WARNING",
        message,
    )


def format_system_error(
    message: str,
) -> tuple[str, str]:
    """
    Critical error notification.
    """

    return (
        "🚨 LOCKBOT ERROR",
        message,
    )


def format_startup() -> tuple[str, str]:
    """
    Startup notification.
    """

    return (
        "🚀 LOCKBOT ONLINE",
        "LOCKBOT has started successfully.",
    )


def format_shutdown() -> tuple[str, str]:
    """
    Shutdown notification.
    """

    return (
        "🛑 LOCKBOT OFFLINE",
        "LOCKBOT has stopped.",
    )