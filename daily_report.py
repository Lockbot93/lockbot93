"""LOCKBOT daily performance report v0.1.

Reads completed trades from the LOCKBOT trade journal, calculates
performance for the current local date, and sends a Pushover report.

This module does not submit, modify, replace, or cancel broker orders.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import lockbot_config as config
from notifications import send_smart_notification
from performance_engine import (
    PerformanceStats,
    calculate_performance_stats,
    load_completed_trades,
)


def _parse_trade_datetime(value: Any) -> datetime:
    """Convert a journal timestamp into a datetime object."""

    if isinstance(value, datetime):
        return value

    if value is None:
        raise ValueError(
            "A required trade timestamp is missing."
        )

    timestamp_text = str(value).strip()

    if not timestamp_text:
        raise ValueError(
            "A required trade timestamp is blank."
        )

    return datetime.fromisoformat(
        timestamp_text.replace("Z", "+00:00")
    )


def _trade_local_date(trade: dict[str, Any]) -> date:
    """Return the completed trade's local exit date."""

    exit_time = _parse_trade_datetime(
        trade.get("exit_time")
    )

    if exit_time.tzinfo is not None:
        exit_time = exit_time.astimezone()

    return exit_time.date()


def get_trades_for_date(
    report_date: date,
) -> list[dict[str, Any]]:
    """Return completed trades whose exits occurred on one date."""

    completed_trades = load_completed_trades()

    return [
        trade
        for trade in completed_trades
        if _trade_local_date(trade) == report_date
    ]


def _format_money(value: float) -> str:
    """Format a signed dollar value."""

    if value > 0:
        return f"+${value:,.2f}"

    if value < 0:
        return f"-${abs(value):,.2f}"

    return "$0.00"


def _format_percent(value: float) -> str:
    """Format a signed percentage."""

    if value > 0:
        return f"+{value:.2f}%"

    if value < 0:
        return f"{value:.2f}%"

    return "0.00%"


def _format_profit_factor(
    value: float | None,
) -> str:
    """Format profit factor for the report."""

    if value is None:
        return "∞"

    return f"{value:.2f}"


def _format_duration(minutes: float) -> str:
    """Convert minutes into readable hours and minutes."""

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


def _trade_pnl(
    trade: dict[str, Any],
) -> float:
    """Return one trade's gross profit or loss."""

    return float(
        trade.get("gross_pnl", 0.0)
    )


def _describe_trade(
    trade: dict[str, Any] | None,
) -> str:
    """Build a short description of one trade."""

    if trade is None:
        return "None"

    symbol = str(
        trade.get("symbol", "UNKNOWN")
    ).strip().upper()

    side = str(
        trade.get("side", "UNKNOWN")
    ).strip().upper()

    pnl = _trade_pnl(trade)

    return (
        f"{symbol} {side} "
        f"{_format_money(pnl)}"
    )


def _daily_title(
    report_date: date,
    daily_stats: PerformanceStats,
) -> str:
    """Choose the daily report notification title.

    Judged on the WHOLE account, not on closed equity trades alone.

    On 2026-08-14 this returned "LOCKBOT DAILY PROFIT" with a green dot
    while the account was down $23: two small equity trades had closed
    green and an open option position was down $25. The title is the only
    part most readers see -- it arrives as a phone notification -- so a
    headline computed from a subset of the book is the most consequential
    place in this file to get it wrong, not the least.

    Falls back to the closed-trade view when the broker is unreachable,
    and marks it, because an unlabelled fallback is how the original
    defect read as fact.
    """

    snapshot = broker_snapshot()

    if snapshot is not None:
        change = snapshot["equity"] - snapshot["previous_equity"]

        if change > 0:
            return "🟢 LOCKBOT DAILY PROFIT"
        if change < 0:
            return "🔴 LOCKBOT DAILY LOSS"
        return "⚪ LOCKBOT DAILY BREAKEVEN"

    if daily_stats.total_trades == 0:
        return "📊 LOCKBOT DAILY REPORT"

    if daily_stats.net_profit > 0:
        return "🟢 LOCKBOT CLOSED TRADES UP (account unknown)"

    if daily_stats.net_profit < 0:
        return "🔴 LOCKBOT CLOSED TRADES DOWN (account unknown)"

    return "⚪ LOCKBOT DAILY BREAKEVEN"


def _load_options_trades() -> list[dict[str, Any]]:
    """Read the options journal, tolerating a missing or unreadable file.

    A reporting failure must never be louder than the thing it reports on,
    so anything unreadable yields no rows rather than an exception.
    """

    path = getattr(config, "OPTIONS_COMPLETED_FILE", None)

    if path is None or not Path(path).exists():
        return []

    try:
        with Path(path).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def _options_trades_for_date(
    trades: list[dict[str, Any]],
    report_date: date,
) -> list[dict[str, Any]]:
    """Options trades that closed on the given local date."""

    matched = []

    for trade in trades:
        stamp = trade.get("exit_time")

        if not stamp:
            continue

        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            continue

        if moment.tzinfo is not None:
            moment = moment.astimezone()

        if moment.date() == report_date:
            matched.append(trade)

    return matched


def _is_real_options_trade(trade: dict[str, Any]) -> bool:
    """Whether a journal row represents a position that actually opened.

    ENTRY_NOT_FILLED rows are orders that never filled. They are journaled
    at cost with zero P&L so they stay visible, but counting them as
    trades would drag the win rate toward a meaningless middle -- LOCKBOT
    logged two of them on 2026-07-30 alone.
    """

    return trade.get("exit_reason") != "ENTRY_NOT_FILLED"


def _open_options_positions() -> list[dict[str, Any]]:
    """Option positions still held, read from the manager's state file."""

    path = getattr(config, "OPTIONS_STATE_FILE", None)

    if path is None or not Path(path).exists():
        return []

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return []

    return list(raw.values()) if isinstance(raw, dict) else []


def _equity_line(overall_stats: Any) -> str:
    """The account balance line, preferring the broker over arithmetic.

    Falls back to the journal reconstruction only when the broker cannot
    be reached, and says so in the label. The old line said "Estimated
    equity" while being read as an account balance; a reader cannot tell
    a $25 discrepancy from a rounding difference unless the source is
    named.
    """

    snapshot = broker_snapshot()

    if snapshot is not None:
        change = snapshot["equity"] - snapshot["previous_equity"]
        base = snapshot["previous_equity"]
        percent = f" ({change / base:+.2%})" if base else ""

        return (
            f"Account equity: ${snapshot['equity']:,.2f}  "
            f"(cash ${snapshot['cash']:,.2f}, from the broker)\n"
            f"WHOLE ACCOUNT TODAY: {_format_money(change)}{percent}  "
            "-- includes open positions"
        )

    return (
        f"Equity from closed trades only: "
        f"${overall_stats.estimated_current_equity:,.2f}  "
        "[BROKER UNREACHABLE -- excludes open positions]"
    )


def broker_snapshot() -> dict[str, Any] | None:
    """The account as the BROKER sees it, not as the journals infer it.

    Written 2026-08-14, when the owner noticed the report claimed
    "+$2.95, estimated equity $652.95" on a day the account was actually
    down $23 at $628.09. Both numbers were defensible in isolation and
    the pair was badly misleading:

      * the P&L counts CLOSED trades, and the only real event that day
        was an OPEN option position losing $25, so the loss was invisible
      * "Estimated equity" was never an account balance at all. It is
        `account_equity_at_entry` of the FIRST journalled trade plus the
        sum of closed profits -- an arithmetic reconstruction that drifts
        further from reality with every open position and every deposit.

    A journal-derived estimate is the right tool for judging a strategy,
    because it isolates what the strategy did. It is the wrong tool for
    answering "how much do I have", and the report was using one label
    for both jobs.

    Returns None when the broker cannot be reached. Callers must show the
    journal estimate LABELLED as an estimate in that case, never silently
    substitute it -- that substitution is the whole defect.

    Cached for the life of the process. Three callers need this -- the
    title, the equity line and the options section -- and three separate
    reads would let a moving price produce a report whose headline
    disagrees with its own body. One report describes one instant.
    """

    global _SNAPSHOT_CACHE

    if _SNAPSHOT_CACHE is not _UNREAD:
        return _SNAPSHOT_CACHE

    _SNAPSHOT_CACHE = _read_broker_snapshot()
    return _SNAPSHOT_CACHE


# Sentinel, because None is a real answer here -- it means the broker was
# reached for and could not be. Using None as "not yet read" would retry
# on every caller precisely when the broker is down.
_UNREAD = object()
_SNAPSHOT_CACHE: Any = _UNREAD


def _read_broker_snapshot() -> dict[str, Any] | None:
    """Do the actual broker read. Call broker_snapshot() instead."""

    try:
        import os

        import lockbot_config as config
        from alpaca.trading.client import TradingClient
        from dotenv import load_dotenv

        import position_filters

        load_dotenv()

        client = TradingClient(
            os.getenv(config.ALPACA_API_KEY_ENV),
            os.getenv(config.ALPACA_SECRET_KEY_ENV),
            paper=config.PAPER_TRADING,
        )

        account = client.get_account()
        positions = client.get_all_positions()

        legs: dict[str, float] = {}
        for leg in position_filters.option_positions(positions):
            legs[str(leg.symbol).upper()] = float(leg.market_value or 0.0)

        return {
            "equity": float(account.equity),
            # last_equity is the broker's own mark at the previous close,
            # which is the only day-change figure that includes open
            # positions. Deriving it from the journals cannot work: an
            # unclosed trade contributes nothing to a journal by
            # definition, and that is exactly what went unreported.
            "previous_equity": float(account.last_equity),
            "cash": float(account.cash),
            "option_legs": legs,
        }
    except Exception:  # noqa: BLE001 -- a report must still print offline
        return None


def open_position_value(position: dict, legs: dict[str, float]) -> float | None:
    """Net market value of one tracked option position, or None.

    A spread is two broker rows and its worth is the sum of them: the long
    leg positive, the short leg negative. Returns None when any leg is
    missing rather than reporting a partial figure -- a half-priced spread
    is not a smaller number, it is a wrong one.
    """

    symbols = [
        str(position.get(key) or "").upper()
        for key in ("long_symbol", "short_symbol")
        if position.get(key)
    ]

    if not symbols or any(symbol not in legs for symbol in symbols):
        return None

    return sum(legs[symbol] for symbol in symbols)


def build_options_section(report_date: date) -> str:
    """Build the options half of the daily report.

    Options P&L is kept in its own files and its own section so the two
    strategies can be judged independently -- mixing them would hide a
    losing one inside a winning one. Until 2026-08-02 this report covered
    equities only, which meant it was blind to the sole strategy actually
    trading.
    """

    all_trades = [t for t in _load_options_trades() if _is_real_options_trade(t)]
    today = _options_trades_for_date(all_trades, report_date)
    open_positions = _open_options_positions()

    def net(trades: list[dict[str, Any]]) -> float:
        total = 0.0

        for trade in trades:
            try:
                total += float(trade.get("profit_loss") or 0.0)
            except (TypeError, ValueError):
                continue

        return total

    def wins(trades: list[dict[str, Any]]) -> int:
        count = 0

        for trade in trades:
            try:
                if float(trade.get("profit_loss") or 0.0) > 0:
                    count += 1
            except (TypeError, ValueError):
                continue

        return count

    lines = ["", "Options"]

    if not all_trades and not open_positions:
        lines.append("No options activity yet.")
        return "\n".join(lines)

    if today:
        today_wins = wins(today)
        lines.append(f"Closed today: {len(today)}")
        lines.append(
            f"Record: {today_wins}W - {len(today) - today_wins}L"
        )
        lines.append(f"Net P/L: {_format_money(net(today))}")

        for trade in today:
            try:
                pnl = float(trade.get("profit_loss") or 0.0)
                percent = float(trade.get("return_percent") or 0.0)
            except (TypeError, ValueError):
                continue

            lines.append(
                f"  {trade.get('underlying', '?')} "
                f"{trade.get('strategy', '?')}: "
                f"{_format_money(pnl)} ({percent:+.1f}%) "
                f"{trade.get('exit_reason', '')}"
            )
    else:
        lines.append("No options trades closed today.")

    if open_positions:
        # An open position was previously reported at COST only, which
        # made a losing trade indistinguishable from a winning one and
        # let a $25 loss sit unmentioned under a "+$2.95" headline.
        snapshot = broker_snapshot()
        legs = (snapshot or {}).get("option_legs", {})

        lines.append("")
        lines.append(f"Open positions: {len(open_positions)}")

        unrealized = 0.0
        priced = 0

        for position in open_positions:
            try:
                debit = float(position.get("entry_debit") or 0.0)
            except (TypeError, ValueError):
                debit = 0.0

            filled = position.get("entry_filled")
            state = "" if filled else "  [entry not filled yet]"

            value = open_position_value(position, legs)

            if value is None:
                worth = "  worth: no quote"
            else:
                change = value - debit
                percent = f" ({change / debit:+.1%})" if debit else ""
                worth = f"  now ${value:,.2f}  {_format_money(change)}{percent}"
                unrealized += change
                priced += 1

            lines.append(
                f"  {position.get('underlying', '?')} "
                f"{position.get('strategy', '?')} "
                f"exp {position.get('expiration', '?')} "
                f"cost ${debit:,.2f}{state}"
            )
            lines.append(f"   {worth}")

        if priced:
            lines.append(
                f"Unrealized on open options: {_format_money(unrealized)}"
                + ("" if priced == len(open_positions)
                   else f"  ({priced} of {len(open_positions)} priced)")
            )

    if all_trades:
        total_wins = wins(all_trades)
        lines.append("")
        lines.append(f"Options to date: {len(all_trades)} closed")
        lines.append(
            f"Record: {total_wins}W - {len(all_trades) - total_wins}L"
        )
        lines.append(f"Net P/L: {_format_money(net(all_trades))}")

        # Options exits run at +50%/-35%, a 1.43:1 payout, so the honest
        # breakeven is 41.2% rather than the equity side's 33.3%.
        lines.append(
            f"Win rate: {total_wins / len(all_trades) * 100:.1f}% "
            "(needs 41.2% to break even)"
        )

    return "\n".join(lines)


def build_portfolio_section() -> str:
    """The buy-and-hold book, which the trading sections cannot see.

    position_filters hides reserved ETF symbols from the trading engine
    on purpose, and that hiding reaches the report too: a daily summary
    built from completed TRADES will never mention a position that is
    never traded.

    As of 2026-08-06 the ETF sleeve holds more of the account than the
    trading strategy does, so a report that omits it describes the
    smaller half. Reads the broker rather than a journal, because
    buy-and-hold produces no journal entries by design.
    """

    try:
        import lockbot_config as config

        if not getattr(config, "ETF_PORTFOLIO_ENABLED", False):
            return ""

        import os

        from alpaca.trading.client import TradingClient
        from dotenv import load_dotenv

        from position_filters import reserved_symbols

        load_dotenv()

        client = TradingClient(
            os.getenv(config.ALPACA_API_KEY_ENV),
            os.getenv(config.ALPACA_SECRET_KEY_ENV),
            paper=config.PAPER_TRADING,
        )

        reserved = reserved_symbols()
        held = [
            position for position in client.get_all_positions()
            if str(getattr(position, "symbol", "")).upper() in reserved
        ]

        if not held:
            return "\n\nPortfolio\nNothing held yet."

        value = sum(float(p.market_value) for p in held)
        cost = sum(float(p.cost_basis) for p in held)
        pnl = value - cost

        lines = ["", "", "Portfolio (buy and hold)"]

        for position in sorted(held, key=lambda p: p.symbol):
            lines.append(
                f"  {position.symbol} x{position.qty} "
                f"${float(position.market_value):,.2f} "
                f"({float(position.unrealized_plpc) * 100:+.2f}%)"
            )

        lines.append(f"Value: ${value:,.2f}  "
                     f"P/L: {_format_money(pnl)}")

        return "\n".join(lines)

    except Exception as error:
        # Never let the portfolio lookup break the report. The trading
        # numbers matter more and this is the only section needing the
        # broker.
        return f"\n\nPortfolio\nUnavailable: {type(error).__name__}"


def build_daily_report_message(
    *,
    report_date: date,
    daily_trades: list[dict[str, Any]],
    daily_stats: PerformanceStats,
    overall_stats: PerformanceStats,
) -> str:
    """Build the complete daily performance message."""

    # Built once and appended to whichever branch runs. Equity having no
    # trades says nothing about options, and for most of the last week
    # options were the only thing trading at all.
    options_section = build_options_section(report_date)
    portfolio_section = build_portfolio_section()
    equity_line = _equity_line(overall_stats)

    if not daily_trades:
        return (
            f"Date: {report_date:%B %d, %Y}\n\n"
            "No completed equity trades today.\n\n"
            "Overall Equity Performance\n"
            f"Total trades: {overall_stats.total_trades}\n"
            f"Record: "
            f"{overall_stats.winning_trades}W - "
            f"{overall_stats.losing_trades}L - "
            f"{overall_stats.breakeven_trades}B\n"
            f"Win rate: "
            f"{overall_stats.win_rate_percent:.2f}%\n"
            f"Net P/L: "
            f"{_format_money(overall_stats.net_profit)}\n"
            f"{equity_line}\n"
            f"{options_section}"
            f"{portfolio_section}"
        )

    best_trade = max(
        daily_trades,
        key=_trade_pnl,
    )

    worst_trade = min(
        daily_trades,
        key=_trade_pnl,
    )

    return (
        f"Date: {report_date:%B %d, %Y}\n\n"
        "Today's Performance\n"
        f"Trades: {daily_stats.total_trades}\n"
        f"Record: "
        f"{daily_stats.winning_trades}W - "
        f"{daily_stats.losing_trades}L - "
        f"{daily_stats.breakeven_trades}B\n"
        f"Win rate: "
        f"{daily_stats.win_rate_percent:.2f}%\n\n"
        f"Gross profit: "
        f"{_format_money(daily_stats.gross_profit)}\n"
        f"Gross loss: "
        f"-${daily_stats.gross_loss:,.2f}\n"
        f"Net P/L: "
        f"{_format_money(daily_stats.net_profit)}\n"
        f"Average return: "
        f"{_format_percent(
            daily_stats.average_return_percent
        )}\n"
        f"Profit factor: "
        f"{_format_profit_factor(
            daily_stats.profit_factor
        )}\n"
        f"Average duration: "
        f"{_format_duration(
            daily_stats.average_holding_minutes
        )}\n\n"
        f"Best trade: "
        f"{_describe_trade(best_trade)}\n"
        f"Worst trade: "
        f"{_describe_trade(worst_trade)}\n\n"
        "Overall Equity Performance\n"
        f"Total trades: {overall_stats.total_trades}\n"
        f"Overall win rate: "
        f"{overall_stats.win_rate_percent:.2f}%\n"
        f"Overall net P/L: "
        f"{_format_money(overall_stats.net_profit)}\n"
        f"{equity_line}\n"
        f"{options_section}"
        f"{portfolio_section}"
    )


def generate_daily_report(
    report_date: date | None = None,
) -> tuple[str, str]:
    """Generate the daily title and report message."""

    selected_date = (
        report_date
        if report_date is not None
        else datetime.now().astimezone().date()
    )

    daily_trades = get_trades_for_date(
        selected_date
    )

    daily_stats = calculate_performance_stats(
        trades=daily_trades
    )

    overall_stats = calculate_performance_stats()

    title = _daily_title(
        selected_date,
        daily_stats,
    )

    message = build_daily_report_message(
        report_date=selected_date,
        daily_trades=daily_trades,
        daily_stats=daily_stats,
        overall_stats=overall_stats,
    )

    return title, message


def send_daily_report(
    report_date: date | None = None,
    *,
    force: bool = False,
) -> bool:
    """Generate and send the daily Pushover report."""

    selected_date = (
        report_date
        if report_date is not None
        else datetime.now().astimezone().date()
    )

    title, message = generate_daily_report(
        report_date=selected_date
    )

    notification_sent = send_smart_notification(
        symbol="LOCKBOT",
        event_type="DAILY_REPORT",
        title=title,
        message=message,
        reason=selected_date.isoformat(),
        force=force,
        cooldown_minutes=0,
    )

    if notification_sent:
        print(
            "LOCKBOT daily report sent successfully."
        )
    else:
        print(
            "LOCKBOT daily report was not sent. "
            "It may have been skipped as a duplicate "
            "or the notification service may have failed."
        )

    return notification_sent


def main() -> None:
    """Generate, display, and send today's daily report."""

    report_date = datetime.now().astimezone().date()

    print("=" * 50)
    print("         LOCKBOT DAILY REPORT v0.1")
    print("=" * 50)

    try:
        title, message = generate_daily_report(
            report_date=report_date
        )

        print(f"Report Date : {report_date.isoformat()}")
        print(f"Title       : {title}")
        print("-" * 50)
        print(message)
        print("-" * 50)

        sent = send_daily_report(
            report_date=report_date
        )

        print(
            f"Notification: "
            f"{'SENT' if sent else 'NOT SENT'}"
        )
        print("Status      : COMPLETE")

    except Exception as error:
        print("Status      : ERROR")
        print(
            f"Error       : "
            f"{type(error).__name__}: {error}"
        )
        raise


if __name__ == "__main__":
    main()