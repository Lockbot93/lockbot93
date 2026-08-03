"""
LOCKBOT CORE

Purpose:
Central application state for LockBot.

This file will eventually coordinate:
- Market Scanner
- Trade Manager
- Risk Engine
- Notifications
- Performance Engine

Version: 0.8
"""

from dataclasses import dataclass
from datetime import datetime


LOCKBOT_VERSION = "0.8"


@dataclass
class LockBotState:
    """Shared runtime state for LockBot."""

    market_open: bool = False
    account_equity: float = 0.0
    buying_power: float = 0.0
    open_positions: int = 0

    completed_trades: int = 0
    pending_trades: int = 0

    scanner_cycles: int = 0

    controller_running: bool = False

    system_status: str = "INITIALIZING"

    version: str = LOCKBOT_VERSION

    last_scan_time: datetime | None = None
    last_trade_time: datetime | None = None
    last_notification_time: datetime | None = None


def startup_banner() -> None:
    """Display the LockBot Core startup banner."""

    print("=" * 58)
    print(f"              LOCKBOT CORE v{LOCKBOT_VERSION}")
    print("=" * 58)
    print("Status : INITIALIZING")
    print("=" * 58)


def create_state() -> LockBotState:
    """Create a fresh shared state object."""

    return LockBotState()


if __name__ == "__main__":
    startup_banner()

    state = create_state()

    print()
    print("Core initialized successfully.")
    print(f"Market Open : {state.market_open}")
    print(f"Equity      : ${state.account_equity:,.2f}")
    print(f"Buying Power: ${state.buying_power:,.2f}")
    print(f"Buying Power: ${state.buying_power:,.2f}")
    print()
    print("Core State")
    print(f"Version            : {state.version}")
    print(f"System Status      : {state.system_status}")
    print(f"Controller Running : {state.controller_running}")
    print(f"Scanner Cycles     : {state.scanner_cycles}")
    print(f"Pending Trades     : {state.pending_trades}")
    print(f"Completed Trades   : {state.completed_trades}")