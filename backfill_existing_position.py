"""
LOCKBOT One-Time Backfill: Register an Existing Broker Position

Use this ONCE to register a position that already exists at Alpaca but
isn't in lockbot_pending_trades.csv (e.g. a manual test trade placed
before LOCKBOT was tracking it). After running this, trade_manager.py's
normal reconciliation will pick up its eventual exit like any other
LOCKBOT-submitted trade.

This script does NOT submit, modify, or cancel any broker order. It
only reads the existing position/orders and writes one row to the
local pending-trades CSV.

Usage:
    python backfill_existing_position.py SPY

If the position was opened as a bracket order (has stop-loss and
take-profit legs attached), this finds that parent order automatically
and derives the real stop/take-profit percentages from it. If no
bracket parent order can be found (e.g. it was a plain market order),
it registers using lockbot_config.py's default bracket percentages
instead and prints a clear warning so you know the percentages are
assumed, not actual.
"""

from __future__ import annotations

import sys
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from dotenv import load_dotenv

import lockbot_config as config
from trade_manager import build_paper_trading_client, register_bracket_trade


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value)).strip().lower()


def find_bracket_parent_order(
    trading_client: TradingClient,
    symbol: str,
) -> Any | None:
    """Find the most recent bracket parent order for one symbol."""

    request = GetOrdersRequest(
        status=QueryOrderStatus.ALL,
        symbols=[symbol],
        limit=100,
        nested=True,
    )

    orders = trading_client.get_orders(filter=request)

    bracket_orders = [
        order
        for order in orders
        if _enum_text(getattr(order, "order_class", "")) == "bracket"
        and _enum_text(order.status) == "filled"
    ]

    if not bracket_orders:
        return None

    return max(bracket_orders, key=lambda order: order.filled_at or order.submitted_at)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python backfill_existing_position.py SYMBOL")
        sys.exit(1)

    symbol = sys.argv[1].strip().upper()

    load_dotenv()
    trading_client = build_paper_trading_client()

    positions = trading_client.get_all_positions()
    position = next((p for p in positions if str(p.symbol).upper() == symbol), None)

    if position is None:
        print(f"No open position found for {symbol}. Nothing to register.")
        sys.exit(1)

    entry_price = float(position.avg_entry_price)
    quantity = abs(float(position.qty))
    side = "LONG" if float(position.qty) > 0 else "SHORT"

    account = trading_client.get_account()
    account_equity_now = float(account.equity)

    parent_order = find_bracket_parent_order(trading_client, symbol)

    if parent_order is not None:
        stop_leg = next(
            (
                leg
                for leg in (parent_order.legs or [])
                if _enum_text(leg.type) in {"stop", "stop_limit"}
            ),
            None,
        )
        profit_leg = next(
            (
                leg
                for leg in (parent_order.legs or [])
                if _enum_text(leg.type) == "limit"
            ),
            None,
        )

        stop_price = float(stop_leg.stop_price) if stop_leg and stop_leg.stop_price else None
        take_profit_price = (
            float(profit_leg.limit_price) if profit_leg and profit_leg.limit_price else None
        )

        stop_loss_percent = (
            abs(entry_price - stop_price) / entry_price
            if stop_price
            else config.BRACKET_STOP_LOSS_PERCENT
        )
        take_profit_percent = (
            abs(take_profit_price - entry_price) / entry_price
            if take_profit_price
            else config.BRACKET_TAKE_PROFIT_PERCENT
        )

        parent_order_id = str(parent_order.id)

        print(f"Found bracket parent order {parent_order_id} for {symbol}.")
        print(f"Derived stop_loss_percent   : {stop_loss_percent:.4f}")
        print(f"Derived take_profit_percent : {take_profit_percent:.4f}")

    else:
        print(
            f"WARNING: No bracket parent order found for {symbol}. "
            "This position was likely opened without a bracket order, "
            "so its real exit will not be auto-detected by "
            "trade_manager.py's reconciliation — you'll need to close "
            "it manually and record it yourself when it exits."
        )
        print(
            "Registering with lockbot_config.py's default bracket "
            "percentages as a placeholder so the trade at least shows "
            "up in the pending registry, but this WILL NOT auto-reconcile."
        )
        parent_order_id = None
        stop_loss_percent = config.BRACKET_STOP_LOSS_PERCENT
        take_profit_percent = config.BRACKET_TAKE_PROFIT_PERCENT

    if parent_order_id is None:
        print()
        print("Cannot register without a real bracket parent order ID —")
        print("trade_manager.py's reconciliation looks up this exact order")
        print("ID at Alpaca to detect the exit. Registering a fake ID would")
        print("just produce reconciliation errors every cycle. Stopping here.")
        sys.exit(1)

    position_value = entry_price * quantity
    initial_risk_dollars = position_value * stop_loss_percent

    registered = register_bracket_trade(
        parent_order_id=parent_order_id,
        strategy_version="MANUAL_BACKFILL",
        symbol=symbol,
        side=side,
        market_regime="MANUAL_BACKFILL",
        confidence=0,
        position_value=position_value,
        account_equity_at_entry=account_equity_now,
        initial_risk_dollars=initial_risk_dollars,
        stop_loss_percent=stop_loss_percent,
        take_profit_percent=take_profit_percent,
        paper_trade=True,
    )

    print()
    if registered:
        print(f"Registered {symbol} ({quantity:g} shares @ ${entry_price:.2f}).")
        print(
            "trade_manager.py will now pick up its exit automatically "
            "on its next run, once the bracket order's stop-loss or "
            "take-profit leg fills."
        )
        print(
            "Note: account_equity_at_entry was approximated using "
            "current equity, not the actual equity at the time this "
            "position was opened, since that historical value is not "
            "recoverable after the fact. This only affects the "
            "position-sizing display for this one backfilled trade, "
            "not its P/L calculation."
        )
    else:
        print(
            f"{symbol}'s parent order was already registered in "
            "lockbot_pending_trades.csv — nothing to do."
        )


if __name__ == "__main__":
    main()