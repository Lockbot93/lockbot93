"""
LOCKBOT Trade Lifecycle Manager v0.5

Responsibilities:
- Register newly submitted LOCKBOT bracket orders.
- Persist pending trade metadata safely.
- Reconcile registered orders with Alpaca.
- Detect completed take-profit and stop-loss exits.
- Prevent duplicate trade-journal records.
- Update performance analytics after completed trades.
- Remove terminal orders that can no longer complete.
- Report operational health through system heartbeats.

This module does not submit, replace, or cancel brokerage orders.
"""

from __future__ import annotations

import csv
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from alpaca.trading.client import TradingClient
from alpaca.common.enums import Sort
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrderByIdRequest, GetOrdersRequest
from dotenv import load_dotenv

from system_heartbeat import (
    mark_module_critical,
    mark_module_degraded,
    mark_module_healthy,
    mark_module_starting,
)
from trade_journal import (
    JOURNAL_FILE,
    initialize_trade_journal,
    record_completed_trade,
)
import lockbot_config as config


# ============================================================
# Module information
# ============================================================

MODULE_NAME = "TRADE_MANAGER"
TRADE_MANAGER_VERSION = "0.6"


# ============================================================
# Project files
# ============================================================

PENDING_TRADES_FILE = config.PENDING_TRADES_FILE

PENDING_COLUMNS = [
    "parent_order_id",
    "strategy_version",
    "symbol",
    "side",
    "market_regime",
    "confidence",
    "position_value",
    "account_equity_at_entry",
    "initial_risk_dollars",
    "stop_loss_percent",
    "take_profit_percent",
    "registered_at",
    "paper_trade",
]


# ============================================================
# Operational models
# ============================================================

@dataclass
class TradeManagerSummary:
    """Metrics collected during one synchronization cycle."""

    pending_before: int = 0
    orders_checked: int = 0
    waiting: int = 0
    trades_recorded: int = 0
    duplicates: int = 0
    terminal_removed: int = 0
    external_exits: int = 0
    unresolvable_removed: int = 0
    analytics_updated: int = 0
    errors: int = 0
    pending_after: int = 0
    duration_seconds: float = 0.0


# ============================================================
# General helpers
# ============================================================

def _enum_text(value: Any) -> str:
    """Return normalized text for Alpaca enums or plain values."""

    raw_value = getattr(value, "value", value)
    return str(raw_value).strip().lower()


def _parse_datetime(value: Any) -> datetime:
    """Convert an Alpaca datetime or ISO string into datetime."""

    if isinstance(value, datetime):
        return value

    if value is None:
        raise ValueError(
            "A required timestamp is missing."
        )

    return datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )


def _safe_float(value: Any, field_name: str) -> float:
    """Convert a required numeric value to float."""

    if value is None:
        raise ValueError(
            f"{field_name} is missing."
        )

    result = float(value)

    if result <= 0:
        raise ValueError(
            f"{field_name} must be greater than zero."
        )

    return result


def _parse_boolean(value: Any) -> bool:
    """Convert persisted text or boolean values to bool."""

    if isinstance(value, bool):
        return value

    return str(value).strip().lower() in {
        "true",
        "1",
        "yes",
    }


def _calculate_realized_profit_loss(
    *,
    side: str,
    entry_price: float,
    exit_price: float,
    quantity: float,
) -> float:
    """Calculate realized profit or loss for a completed trade."""

    normalized_side = side.strip().upper()

    if normalized_side == "LONG":
        profit_loss = (
            exit_price - entry_price
        ) * quantity

    elif normalized_side == "SHORT":
        profit_loss = (
            entry_price - exit_price
        ) * quantity

    else:
        raise ValueError(
            f"Unsupported trade side: {side}"
        )

    return round(profit_loss, 2)


# ============================================================
# Pending-trades registry
# ============================================================

def initialize_pending_trades() -> Path:
    """Create the pending-trades registry when needed."""

    if PENDING_TRADES_FILE.exists():
        return PENDING_TRADES_FILE

    with PENDING_TRADES_FILE.open(
        mode="w",
        newline="",
        encoding="utf-8",
    ) as pending_file:
        writer = csv.DictWriter(
            pending_file,
            fieldnames=PENDING_COLUMNS,
        )
        writer.writeheader()

    return PENDING_TRADES_FILE


def _validate_pending_header(
    fieldnames: list[str] | None,
) -> None:
    """Confirm that the pending registry uses the expected schema."""

    if fieldnames == PENDING_COLUMNS:
        return

    raise RuntimeError(
        "lockbot_pending_trades.csv has an unexpected header. "
        "Back up the file before changing or recreating it."
    )


def _read_pending_trades() -> list[dict[str, str]]:
    """Read all currently registered LOCKBOT trades."""

    initialize_pending_trades()

    with PENDING_TRADES_FILE.open(
        mode="r",
        newline="",
        encoding="utf-8-sig",
    ) as pending_file:
        reader = csv.DictReader(pending_file)
        _validate_pending_header(reader.fieldnames)
        return list(reader)


def _write_pending_trades(
    rows: list[dict[str, Any]],
) -> None:
    """Safely replace the pending registry with retry protection."""

    temporary_file = PENDING_TRADES_FILE.with_name(
        f"{PENDING_TRADES_FILE.stem}."
        f"{os.getpid()}."
        f"{time.time_ns()}.tmp"
    )

    try:
        with temporary_file.open(
            mode="w",
            newline="",
            encoding="utf-8",
        ) as pending_file:
            writer = csv.DictWriter(
                pending_file,
                fieldnames=PENDING_COLUMNS,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(rows)
            pending_file.flush()
            os.fsync(pending_file.fileno())

        maximum_attempts = 8

        for attempt in range(1, maximum_attempts + 1):
            try:
                os.replace(
                    temporary_file,
                    PENDING_TRADES_FILE,
                )
                return

            except PermissionError:
                if attempt == maximum_attempts:
                    raise

                wait_seconds = 0.25 * attempt

                print(
                    "Pending-trades file is temporarily locked. "
                    f"Retrying in {wait_seconds:.2f} seconds "
                    f"({attempt}/{maximum_attempts})..."
                )
                time.sleep(wait_seconds)

    finally:
        try:
            if temporary_file.exists():
                temporary_file.unlink()
        except OSError:
            pass


# ============================================================
# Journal duplicate protection
# ============================================================

def _load_journal_trade_ids() -> set[str]:
    """Load all existing journal trade IDs once per cycle."""

    initialize_trade_journal()

    with JOURNAL_FILE.open(
        mode="r",
        newline="",
        encoding="utf-8-sig",
    ) as journal:
        reader = csv.DictReader(journal)

        return {
            str(row.get("trade_id", "")).strip()
            for row in reader
            if str(row.get("trade_id", "")).strip()
        }


# ============================================================
# Trade registration
# ============================================================

def register_bracket_trade(
    *,
    parent_order_id: str,
    strategy_version: str,
    symbol: str,
    side: str,
    market_regime: str,
    confidence: int,
    position_value: float,
    account_equity_at_entry: float,
    initial_risk_dollars: float,
    stop_loss_percent: float,
    take_profit_percent: float,
    paper_trade: bool = True,
) -> bool:
    """
    Register a newly submitted LOCKBOT bracket order.

    Returns True when a new row is added.
    Returns False when that parent order is already registered.
    """

    normalized_order_id = str(
        UUID(str(parent_order_id))
    )
    normalized_side = side.strip().upper()
    normalized_symbol = symbol.strip().upper()

    if normalized_side not in {"LONG", "SHORT"}:
        raise ValueError(
            "side must be LONG or SHORT."
        )

    if not strategy_version.strip():
        raise ValueError(
            "strategy_version cannot be blank."
        )

    if not normalized_symbol:
        raise ValueError(
            "symbol cannot be blank."
        )

    if not 0 <= confidence <= 100:
        raise ValueError(
            "confidence must be between 0 and 100."
        )

    if position_value <= 0:
        raise ValueError(
            "position_value must be greater than zero."
        )

    if account_equity_at_entry <= 0:
        raise ValueError(
            "account_equity_at_entry must be greater than zero."
        )

    if initial_risk_dollars < 0:
        raise ValueError(
            "initial_risk_dollars cannot be negative."
        )

    if not 0 < stop_loss_percent < 1:
        raise ValueError(
            "stop_loss_percent must be a decimal between 0 and 1."
        )

    if not 0 < take_profit_percent < 1:
        raise ValueError(
            "take_profit_percent must be a decimal between 0 and 1."
        )

    pending_rows = _read_pending_trades()

    if any(
        row.get("parent_order_id") == normalized_order_id
        for row in pending_rows
    ):
        return False

    pending_rows.append(
        {
            "parent_order_id": normalized_order_id,
            "strategy_version": strategy_version.strip(),
            "symbol": normalized_symbol,
            "side": normalized_side,
            "market_regime": market_regime.strip().upper(),
            "confidence": confidence,
            "position_value": round(position_value, 2),
            "account_equity_at_entry": round(
                account_equity_at_entry,
                2,
            ),
            "initial_risk_dollars": round(
                initial_risk_dollars,
                2,
            ),
            "stop_loss_percent": stop_loss_percent,
            "take_profit_percent": take_profit_percent,
            "registered_at": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "paper_trade": paper_trade,
        }
    )

    _write_pending_trades(pending_rows)
    return True


# ============================================================
# Alpaca order interpretation
# ============================================================

def _find_filled_exit_leg(order: Any) -> Any | None:
    """Find the most recently filled closing leg."""

    legs = list(order.legs or [])

    filled_legs = [
        leg
        for leg in legs
        if _enum_text(leg.status) == "filled"
    ]

    if not filled_legs:
        return None

    return max(
        filled_legs,
        key=lambda leg: (
            leg.filled_at
            or leg.updated_at
            or leg.submitted_at
        ),
    )


def _determine_exit_reason(exit_leg: Any) -> str:
    """Classify the completed bracket exit."""

    order_type = _enum_text(exit_leg.type)

    if order_type == "limit":
        return "TAKE_PROFIT"

    if order_type in {"stop", "stop_limit"}:
        return "STOP_LOSS"

    return f"EXIT_{order_type.upper() or 'UNKNOWN'}"


TERMINAL_ORDER_STATUSES = {
    "canceled",
    "cancelled",
    "expired",
    "rejected",
}


def _parent_order_is_terminal(order: Any) -> bool:
    """
    Return True when an unfilled parent order cannot complete later.

    Filled parent orders remain registered until an exit leg fills.
    """

    return _enum_text(order.status) in TERMINAL_ORDER_STATUSES


def _bracket_was_dismantled(order: Any) -> bool:
    """
    Return True when the entry filled but every exit leg is dead.

    This is the state close_all.py leaves behind: it cancels the open
    orders (both bracket legs) and then liquidates the position. The
    parent order stays 'filled' forever, so _parent_order_is_terminal
    never fires, and no leg ever fills, so _find_filled_exit_leg never
    fires either. Before this check existed, such a row sat in
    lockbot_pending_trades.csv as 'waiting' permanently and its trade
    never reached completed_trades.csv — VTEB and CELH sat there from
    2026-07-28 onward, which is why the completed-trades file was still
    empty after a week of trading.
    """

    if _enum_text(order.status) != "filled":
        return False

    legs = list(order.legs or [])

    if not legs:
        return False

    return all(
        _enum_text(leg.status) in TERMINAL_ORDER_STATUSES
        for leg in legs
    )


def _find_external_exit(
    trading_client: TradingClient,
    symbol: str,
    parent_order: Any,
) -> Any | None:
    """
    Find the order that actually closed a position when the bracket did not.

    When the bracket legs are cancelled out from under a filled entry, the
    position is normally closed by something else — close_all.py, or a
    manual liquidation in the Alpaca dashboard. That closing order is a
    separate, unrelated order, so the only way to recover the trade's real
    exit price is to search the symbol's closed orders for the first filled
    order on the opposite side after the entry filled.

    Returns None when no such order exists, which means the position was
    closed in a way this cannot see, or is somehow still open.
    """

    entry_filled_at = parent_order.filled_at

    if entry_filled_at is None:
        return None

    entry_side = _enum_text(parent_order.side)
    closing_side = "sell" if entry_side == "buy" else "buy"

    leg_ids = {
        str(leg.id)
        for leg in (parent_order.legs or [])
    }

    orders = trading_client.get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            symbols=[symbol],
            after=entry_filled_at,
            direction=Sort.ASC,
            limit=500,
        )
    )

    for candidate in orders:
        if str(candidate.id) in leg_ids:
            continue

        if _enum_text(candidate.status) != "filled":
            continue

        if _enum_text(candidate.side) != closing_side:
            continue

        if candidate.filled_at is None or candidate.filled_at <= entry_filled_at:
            continue

        return candidate

    return None


# ============================================================
# Reconciliation
# ============================================================

def synchronize_completed_trades(
    trading_client: TradingClient,
) -> TradeManagerSummary:
    """Check pending orders and journal completed exits."""

    summary = TradeManagerSummary()
    pending_rows = _read_pending_trades()
    summary.pending_before = len(pending_rows)

    remaining_rows: list[dict[str, Any]] = []
    journal_trade_ids = _load_journal_trade_ids()

    for row in pending_rows:
        summary.orders_checked += 1

        parent_order_id = row.get(
            "parent_order_id",
            "",
        )
        symbol = row.get(
            "symbol",
            "UNKNOWN",
        )

        try:
            order = trading_client.get_order_by_id(
                parent_order_id,
                filter=GetOrderByIdRequest(
                    nested=True,
                ),
            )

            exit_leg = _find_filled_exit_leg(order)
            exit_reason_override: str | None = None

            if exit_leg is None:
                if _parent_order_is_terminal(order):
                    summary.terminal_removed += 1
                    print(
                        f"{symbol}: removed terminal parent order "
                        f"with status {_enum_text(order.status).upper()}."
                    )
                    continue

                if _bracket_was_dismantled(order):
                    # The entry filled but both exit legs were cancelled, so
                    # this row can never resolve through its bracket. Recover
                    # the real exit from the order that actually closed it.
                    external_exit = _find_external_exit(
                        trading_client,
                        symbol,
                        order,
                    )

                    if external_exit is None:
                        # No closing order found. Before dropping the row,
                        # check whether the position is actually gone —
                        # because "bracket cancelled" and "trade finished"
                        # are not the same thing. A position whose bracket
                        # was cancelled while it stays OPEN is still a live
                        # trade, and dropping it here would erase LOCKBOT's
                        # only record of an unprotected position.
                        try:
                            still_open = any(
                                str(position.symbol).upper() == symbol.upper()
                                for position in trading_client.get_all_positions()
                            )
                        except Exception:
                            still_open = True  # assume the worst, keep the row

                        if still_open:
                            summary.waiting += 1
                            remaining_rows.append(row)
                            print(
                                f"{symbol}: WARNING — bracket legs are cancelled "
                                "but the position is still open. It has no stop "
                                "loss and no take profit. Keeping it registered; "
                                "re-arm the bracket or close it by hand."
                            )
                            continue

                        summary.unresolvable_removed += 1
                        print(
                            f"{symbol}: bracket legs were cancelled and no "
                            "closing order was found. Removing this row so it "
                            "stops being re-checked forever. This trade cannot "
                            "be journaled — its exit price is unknown."
                        )
                        continue

                    exit_leg = external_exit
                    exit_reason_override = "EXTERNAL_CLOSE"
                    summary.external_exits += 1

                    print(
                        f"{symbol}: bracket legs were cancelled. Recovered the "
                        f"exit from order {external_exit.id}."
                    )

                else:
                    summary.waiting += 1
                    remaining_rows.append(row)
                    continue

            trade_id = str(exit_leg.id)

            if trade_id in journal_trade_ids:
                summary.duplicates += 1
                print(
                    f"{symbol}: completed exit was already journaled."
                )
                continue

            entry_time = _parse_datetime(
                order.filled_at
            )
            exit_time = _parse_datetime(
                exit_leg.filled_at
            )

            entry_price = _safe_float(
                order.filled_avg_price,
                "entry filled average price",
            )
            exit_price = _safe_float(
                exit_leg.filled_avg_price,
                "exit filled average price",
            )
            quantity = _safe_float(
                exit_leg.filled_qty
                or order.filled_qty
                or order.qty,
                "filled quantity",
            )

            exit_reason = exit_reason_override or _determine_exit_reason(
                exit_leg
            )

            realized_profit_loss = (
                _calculate_realized_profit_loss(
                    side=row["side"],
                    entry_price=entry_price,
                    exit_price=exit_price,
                    quantity=quantity,
                )
            )

            record_completed_trade(
                trade_id=trade_id,
                strategy_version=row["strategy_version"],
                symbol=symbol,
                side=row["side"],
                entry_time=entry_time,
                exit_time=exit_time,
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                position_value=float(
                    row["position_value"]
                ),
                account_equity_at_entry=float(
                    row["account_equity_at_entry"]
                ),
                initial_risk_dollars=float(
                    row["initial_risk_dollars"]
                ),
                stop_loss_percent=float(
                    row["stop_loss_percent"]
                ),
                take_profit_percent=float(
                    row["take_profit_percent"]
                ),
                exit_reason=exit_reason,
                market_regime=row["market_regime"],
                confidence=int(row["confidence"]),
                paper_trade=_parse_boolean(
                    row["paper_trade"]
                ),
            )

            journal_trade_ids.add(trade_id)
            summary.trades_recorded += 1
            summary.analytics_updated += 1

            print(
                f"{symbol}: completed trade recorded "
                f"as {trade_id}."
            )
            print(
                f"{symbol}: realized P/L ${realized_profit_loss:.2f}."
            )

            try:
                from notification_manager import (
                    send_completed_trade_notification,
                )
                from performance_engine import load_completed_trades

                completed_rows = load_completed_trades()
                matching_row = next(
                    (
                        row
                        for row in completed_rows
                        if str(row.get("trade_id", "")).strip() == trade_id
                    ),
                    None,
                )

                if matching_row is not None:
                    send_completed_trade_notification(matching_row)

            except Exception as notification_error:
                print(
                    f"{symbol}: completed-trade notification failed: "
                    f"{type(notification_error).__name__}: "
                    f"{notification_error}"
                )

        except Exception as error:
            summary.errors += 1
            remaining_rows.append(row)

            print(
                f"Could not process {symbol} "
                f"order {parent_order_id}: "
                f"{type(error).__name__}: {error}"
            )

    _write_pending_trades(remaining_rows)
    summary.pending_after = len(remaining_rows)

    return summary


# ============================================================
# Alpaca client
# ============================================================

def build_paper_trading_client() -> TradingClient:
    """Build an authenticated Alpaca paper-trading client."""

    load_dotenv()

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        raise RuntimeError(
            "Alpaca API keys were not found in the .env file."
        )

    return TradingClient(
        api_key,
        secret_key,
        paper=True,
    )


# ============================================================
# Reporting and heartbeat
# ============================================================

def _heartbeat_details(
    summary: TradeManagerSummary,
) -> dict[str, Any]:
    """Build heartbeat details for the health dashboard."""

    details = asdict(summary)
    details["version"] = TRADE_MANAGER_VERSION
    return details


def print_summary(
    summary: TradeManagerSummary,
    heartbeat_status: str,
) -> None:
    """Display one synchronization summary."""

    print("=" * 50)
    print(f"        LOCKBOT TRADE MANAGER v{TRADE_MANAGER_VERSION}")
    print("=" * 50)
    print(f"Pending Before  : {summary.pending_before}")
    print(f"Orders Checked  : {summary.orders_checked}")
    print(f"Still Waiting   : {summary.waiting}")
    print(f"Trades Recorded : {summary.trades_recorded}")
    print(f"Analytics Updated: {summary.analytics_updated}")
    print(f"Duplicates      : {summary.duplicates}")
    print(f"Terminal Removed: {summary.terminal_removed}")
    print(f"External Exits  : {summary.external_exits}")
    print(f"Unresolvable    : {summary.unresolvable_removed}")
    print(f"Errors          : {summary.errors}")
    print(f"Pending After   : {summary.pending_after}")
    print(f"Duration        : {summary.duration_seconds:.2f} seconds")
    print(f"Heartbeat       : {heartbeat_status}")
    print("=" * 50)


# ============================================================
# Main workflow
# ============================================================

def run_trade_manager() -> TradeManagerSummary:
    """Run one complete trade-manager synchronization cycle."""

    started_at = time.perf_counter()
    summary = TradeManagerSummary()

    mark_module_starting(
        MODULE_NAME,
        message="Trade manager is starting.",
        details={
            "version": TRADE_MANAGER_VERSION,
        },
    )

    try:
        initialize_pending_trades()
        initialize_trade_journal()

        client = build_paper_trading_client()
        summary = synchronize_completed_trades(client)

        summary.duration_seconds = round(
            time.perf_counter() - started_at,
            3,
        )

        details = _heartbeat_details(summary)

        if summary.errors > 0:
            mark_module_degraded(
                MODULE_NAME,
                message=(
                    "Trade manager completed with "
                    f"{summary.errors} reconciliation error(s)."
                ),
                details=details,
            )
            heartbeat_status = "DEGRADED"

        else:
            mark_module_healthy(
                MODULE_NAME,
                message=(
                    "Trade manager completed successfully. "
                    f"{summary.trades_recorded} trade(s) recorded "
                    f"and {summary.analytics_updated} analytics "
                    "update(s) completed."
                ),
                details=details,
            )
            heartbeat_status = "HEALTHY"

        print_summary(
            summary=summary,
            heartbeat_status=heartbeat_status,
        )

        return summary

    except Exception as error:
        summary.duration_seconds = round(
            time.perf_counter() - started_at,
            3,
        )

        details = _heartbeat_details(summary)
        details["exception_type"] = type(error).__name__

        mark_module_critical(
            MODULE_NAME,
            message=(
                "Trade manager failed with an "
                "unhandled exception."
            ),
            details=details,
        )

        print()
        print("=" * 50)
        print("TRADE MANAGER FAILURE")
        print("-" * 50)
        print(f"Error Type    : {type(error).__name__}")
        print(f"Error Message : {error}")
        print(
            f"Duration      : "
            f"{summary.duration_seconds:.2f} seconds"
        )
        print("Heartbeat     : CRITICAL")
        print("=" * 50)

        raise


def main() -> None:
    """Program entry point."""

    run_trade_manager()


if __name__ == "__main__":
    main()
