"""
LOCKBOT Startup Reconciliation v0.2

Verifies that LOCKBOT's local position state matches the broker's
actual open positions whenever the controller starts.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

from position_filters import equity_positions


load_dotenv()

PROJECT_FOLDER = Path(__file__).resolve().parent
POSITION_STATE_FILE = PROJECT_FOLDER / "position_state.json"

DEFAULT_TRAILING_STOP_PERCENT = 0.005


def get_trading_client() -> TradingClient:
    """Create the Alpaca paper-trading client."""

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
    """Return the current Alpaca positions and open orders."""

    trading_client = get_trading_client()

    # Equity only. position_state.json is the EQUITY tracker — writing an
    # option contract into it would hand position_monitor.py a contract to
    # evaluate against stock stop-loss percentages. options_manager.py
    # keeps its own state for option positions.
    positions = equity_positions(trading_client.get_all_positions())
    open_orders = trading_client.get_orders()

    return positions, open_orders


def load_position_state() -> dict[str, dict[str, Any]]:
    """Load the local position state as a JSON object."""

    if not POSITION_STATE_FILE.exists():
        return {}

    try:
        raw_text = POSITION_STATE_FILE.read_text(
            encoding="utf-8"
        )
        loaded_state = json.loads(raw_text)

    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}

    if not isinstance(loaded_state, dict):
        return {}

    return loaded_state


def save_position_state(
    position_state: dict[str, dict[str, Any]],
) -> None:
    """Save position state with an atomic file replacement."""

    temporary_file = POSITION_STATE_FILE.with_suffix(
        ".json.reconcile.tmp"
    )

    temporary_file.write_text(
        json.dumps(
            position_state,
            indent=4,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    temporary_file.replace(POSITION_STATE_FILE)


def build_new_position_state(position: Any) -> dict[str, Any]:
    """Create safe tracking state for one broker position."""

    entry_price = float(position.avg_entry_price)

    return {
        "entry_price": entry_price,
        "first_seen_time": datetime.now(
            timezone.utc
        ).isoformat(),
        "highest_price": entry_price,
        "highest_gain_percent": 0.0,
        "break_even_active": False,
        "trailing_stop_active": False,
        "trailing_stop_price": (
            entry_price
            * (1.0 - DEFAULT_TRAILING_STOP_PERCENT)
        ),
    }


def reconcile_position_state(
    positions: list,
) -> tuple[int, int, int]:
    """
    Synchronize local position state with the broker.

    Existing valid symbol state is preserved. Missing broker positions
    are added. Local symbols that no longer exist at the broker are
    removed.
    """

    local_state = load_position_state()

    broker_symbols = {
        str(position.symbol).upper()
        for position in positions
    }

    added_count = 0
    preserved_count = 0
    removed_count = 0

    reconciled_state: dict[str, dict[str, Any]] = {}

    for position in positions:
        symbol = str(position.symbol).upper()
        existing_state = local_state.get(symbol)

        if isinstance(existing_state, dict):
            reconciled_state[symbol] = existing_state
            preserved_count += 1
        else:
            reconciled_state[symbol] = build_new_position_state(
                position
            )
            added_count += 1

    for local_symbol in local_state:
        if local_symbol.upper() not in broker_symbols:
            removed_count += 1

    if reconciled_state != local_state:
        save_position_state(reconciled_state)

    return added_count, preserved_count, removed_count


def print_broker_state(
    positions: list,
    open_orders: list,
) -> None:
    """Print a readable summary of the broker state."""

    print("=" * 50)
    print("LOCKBOT STARTUP RECONCILIATION v0.2")
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
    """Run broker and local position-state reconciliation."""

    positions, open_orders = get_broker_state()

    print_broker_state(
        positions=positions,
        open_orders=open_orders,
    )

    added, preserved, removed = reconcile_position_state(
        positions
    )

    print()
    print("Position State Reconciliation")
    print(f"Added From Broker : {added}")
    print(f"Preserved Locally : {preserved}")
    print(f"Removed Stale     : {removed}")
    print(f"State File        : {POSITION_STATE_FILE.name}")
    print("=" * 50)

    return True


if __name__ == "__main__":
    run_startup_reconciliation()