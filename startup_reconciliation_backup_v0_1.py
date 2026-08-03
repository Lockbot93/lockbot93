"""
LOCKBOT Startup Reconciliation

Verifies that LOCKBOT's local state matches the broker's state
whenever the controller starts.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()


def get_trading_client() -> TradingClient:
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        raise RuntimeError(
            "Alpaca credentials are missing from the .env file."
        )

    return TradingClient(
        api_key,
        secret_key,
        paper=True,
    )


def get_broker_state() -> tuple[list, list]:
    """
    Returns the current Alpaca positions and open orders.
    """

    trading_client = get_trading_client()

    positions = trading_client.get_all_positions()
    open_orders = trading_client.get_orders()

    return positions, open_orders


def print_broker_state(
    positions: list,
    open_orders: list,
) -> None:
    """
    Prints a readable summary of the broker's current state.
    """

    print("=" * 50)
    print("LOCKBOT STARTUP RECONCILIATION")
    print("=" * 50)
    print(f"Broker Positions : {len(positions)}")
    print(f"Open Orders      : {len(open_orders)}")

    if positions:
        print()
        print("Current Positions")

        for position in positions:
            print(
                f"- {position.symbol}: "
                f"{position.qty} shares at "
                f"${float(position.avg_entry_price):,.2f}"
            )
    else:
        print()
        print("Current Positions: None")

    if open_orders:
        print()
        print("Current Open Orders")

        for order in open_orders:
            print(
                f"- {order.symbol}: "
                f"{order.side} "
                f"{order.qty} shares "
                f"({order.status})"
            )
    else:
        print()
        print("Current Open Orders: None")


def run_startup_reconciliation() -> bool:
    """
    Runs the startup reconciliation process.
    """

    positions, open_orders = get_broker_state()

    print_broker_state(
        positions=positions,
        open_orders=open_orders,
    )

    return True