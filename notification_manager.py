"""LOCKBOT completed-trade notification manager v0.2.

Builds detailed completed-trade notifications using centralized
formatting, journal data, and current portfolio performance statistics.

This module does not submit, modify, replace, or cancel broker orders.
"""

from __future__ import annotations

from typing import Any

from notification_formatter import (
    format_money,
    format_percent,
    format_trade_closed,
)
from notifications import (
    NotificationStatus,
    send_smart_notification,
)
from performance_engine import (
    PerformanceStats,
    calculate_performance_stats,
)


def _required_float(
    trade: dict[str, Any],
    field_name: str,
) -> float:
    """Read and validate one required numeric trade field."""

    value = trade.get(field_name)

    if value is None:
        raise ValueError(
            f"Completed trade is missing {field_name!r}."
        )

    try:
        return float(value)

    except (TypeError, ValueError) as error:
        raise ValueError(
            "Completed trade contains an invalid "
            f"{field_name!r} value: {value!r}"
        ) from error


def _required_text(
    trade: dict[str, Any],
    field_name: str,
) -> str:
    """Read and validate one required text trade field."""

    value = str(
        trade.get(field_name, "")
    ).strip()

    if not value:
        raise ValueError(
            f"Completed trade is missing {field_name!r}."
        )

    return value


def _format_duration(
    minutes: float,
) -> str:
    """Convert holding minutes into readable text."""

    total_minutes = max(
        int(round(minutes)),
        0,
    )

    hours, remaining_minutes = divmod(
        total_minutes,
        60,
    )

    if hours and remaining_minutes:
        return f"{hours}h {remaining_minutes}m"

    if hours:
        return f"{hours}h"

    return f"{remaining_minutes}m"


def _format_profit_factor(
    value: float | None,
) -> str:
    """Format profit factor for notifications."""

    if value is None:
        return "∞"

    return f"{value:.2f}"


def _get_event_type(
    gross_pnl: float,
) -> str:
    """Return the appropriate completed-trade event type."""

    if gross_pnl > 0:
        return "TRADE_CLOSED_WIN"

    if gross_pnl < 0:
        return "TRADE_CLOSED_LOSS"

    return "TRADE_CLOSED_BREAKEVEN"


def build_completed_trade_message(
    trade: dict[str, Any],
    stats: PerformanceStats,
) -> tuple[str, str, str]:
    """Build a detailed completed-trade notification."""

    symbol = _required_text(
        trade,
        "symbol",
    ).upper()

    side = _required_text(
        trade,
        "side",
    ).upper()

    exit_reason = _required_text(
        trade,
        "exit_reason",
    ).upper()

    market_regime = _required_text(
        trade,
        "market_regime",
    ).upper()

    strategy_version = _required_text(
        trade,
        "strategy_version",
    )

    entry_price = _required_float(
        trade,
        "entry_price",
    )

    exit_price = _required_float(
        trade,
        "exit_price",
    )

    quantity = _required_float(
        trade,
        "quantity",
    )

    gross_pnl = _required_float(
        trade,
        "gross_pnl",
    )

    return_percent = _required_float(
        trade,
        "return_percent",
    )

    holding_minutes = _required_float(
        trade,
        "holding_minutes",
    )

    confidence = int(
        _required_float(
            trade,
            "confidence",
        )
    )

    formatter_trade = {
        **trade,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "gross_pnl": gross_pnl,
        "return_percent": return_percent,
        "holding_minutes": holding_minutes,
    }

    title, formatted_trade_summary = (
        format_trade_closed(
            formatter_trade
        )
    )

    event_type = _get_event_type(
        gross_pnl
    )

    message = (
        f"{formatted_trade_summary}\n\n"
        "Trade Details\n"
        f"Quantity: {quantity:g}\n"
        f"Exit reason: {exit_reason}\n"
        f"Market regime: {market_regime}\n"
        f"Confidence: {confidence}/100\n"
        f"Strategy: v{strategy_version}\n\n"
        "Overall Performance\n"
        f"Trades: {stats.total_trades}\n"
        f"Record: "
        f"{stats.winning_trades}W - "
        f"{stats.losing_trades}L - "
        f"{stats.breakeven_trades}B\n"
        f"Win rate: "
        f"{stats.win_rate_percent:.2f}%\n"
        f"Net P/L: "
        f"{format_money(stats.net_profit)}\n"
        f"Profit factor: "
        f"{_format_profit_factor(
            stats.profit_factor
        )}\n"
        f"Average return: "
        f"{format_percent(
            stats.average_return_percent
        )}\n"
        f"Average hold: "
        f"{_format_duration(
            stats.average_holding_minutes
        )}\n"
        # Named for what it IS. This is the first journalled trade's
        # entry equity plus the sum of CLOSED profits -- it has never
        # read the account, so it omits every open position and every
        # deposit. Labelled "Estimated equity" it read as a balance, and
        # on 2026-08-14 it told the owner $652.95 against a real $628.09
        # because a $25 options loss was still open. The number is the
        # right one for judging the equity strategy in isolation; only
        # the label was a lie. daily_report.py reads the broker instead;
        # a per-trade notification should not, since it fires on the exit
        # path and must stay fast and offline-safe.
        f"Equity book, closed trades only: "
        f"${stats.estimated_current_equity:,.2f}"
    )

    return (
        title,
        event_type,
        message,
    )


def _print_notification_result(
    symbol: str,
    status: NotificationStatus,
) -> None:
    """Print an accurate notification result."""

    if status is NotificationStatus.SENT:
        print(
            "Completed-trade notification sent "
            f"for {symbol}."
        )

    elif (
        status
        is NotificationStatus.SKIPPED_DUPLICATE
    ):
        print(
            "Completed-trade notification skipped "
            f"for {symbol}: duplicate."
        )

    elif (
        status
        is NotificationStatus.SKIPPED_COOLDOWN
    ):
        print(
            "Completed-trade notification skipped "
            f"for {symbol}: cooldown active."
        )

    else:
        print(
            "Completed-trade notification failed "
            f"for {symbol}."
        )


def send_completed_trade_notification(
    trade: dict[str, Any],
) -> bool:
    """Calculate current stats and send a trade-result alert."""

    symbol = str(
        trade.get("symbol", "UNKNOWN")
    ).strip().upper()

    try:
        stats = calculate_performance_stats()

        (
            title,
            event_type,
            message,
        ) = build_completed_trade_message(
            trade,
            stats,
        )

        symbol = _required_text(
            trade,
            "symbol",
        ).upper()

        exit_reason = _required_text(
            trade,
            "exit_reason",
        ).upper()

        status = send_smart_notification(
            symbol=symbol,
            event_type=event_type,
            title=title,
            message=message,
            reason=exit_reason,
            cooldown_minutes=0,
        )

        _print_notification_result(
            symbol,
            status,
        )

        return status is NotificationStatus.SENT

    except Exception as error:
        print(
            "Completed-trade notification failed "
            f"for {symbol}: "
            f"{type(error).__name__}: {error}"
        )

        return False


def main() -> None:
    """Verify that the module imports correctly."""

    print("=" * 50)
    print(
        "   LOCKBOT NOTIFICATION MANAGER v0.2"
    )
    print("=" * 50)
    print(
        "Centralized formatting : READY"
    )
    print(
        "Completed trade alerts : READY"
    )
    print(
        "Performance integration: READY"
    )
    print(
        "Explicit status handling: READY"
    )
    print(
        "Notification provider  : notifications.py"
    )
    print(
        "Status                 : READY"
    )


if __name__ == "__main__":
    main()