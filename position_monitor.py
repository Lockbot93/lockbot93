"""
LOCKBOT Position Monitor v0.8

Monitors open Alpaca paper positions and evaluates stop-loss,
break-even, and trailing-stop conditions for state tracking,
notifications, and module heartbeat health.

IMPORTANT — MONITORING/ALERTING ONLY:
LOCKBOT's sole exit mechanism is the bracket order (stop-loss and
take-profit legs) submitted by market_scanner.py at entry time — those
exits happen automatically at the broker and are picked up by
trade_manager.py's reconciliation. This module previously also had the
ability to independently submit its own exit (a full market close) at
different, tighter thresholds than the bracket order's — two exit
mechanisms racing on the same position with no coordination, which
could produce partial/duplicate-exit states downstream. It now only
evaluates and alerts; it never calls close_position() or writes a
completed trade to the journal. If a genuine dynamic-exit strategy is
wanted later, it should replace the bracket order, not run alongside it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpaca.trading.client import TradingClient
from dotenv import load_dotenv
from retry_utils import with_retries
from position_filters import equity_positions

from notifications import send_notification
from system_heartbeat import (
    mark_module_critical,
    mark_module_degraded,
    mark_module_healthy,
    mark_module_starting,
)
import lockbot_config as config


load_dotenv()

MODULE_NAME = "POSITION_MONITOR"
POSITION_MONITOR_VERSION = "0.8"

# Sourced from lockbot_config.py. These are informational-only
# thresholds used to decide when to send an alert — see the module
# docstring above for why this module never submits its own exit.
BREAK_EVEN_TRIGGER = config.BREAK_EVEN_TRIGGER_PERCENT * 100
TRAILING_STOP_TRIGGER = config.TRAILING_STOP_TRIGGER_PERCENT * 100
TRAILING_STOP_PERCENT = config.TRAILING_STOP_DISTANCE_PERCENT * 100
STOP_LOSS_PERCENT = config.MONITOR_STOP_LOSS_PERCENT * 100

PROJECT_FOLDER = Path(__file__).resolve().parent
POSITION_STATE_FILE = config.POSITION_STATE_FILE


@dataclass
class MonitorMetrics:
    positions_checked: int = 0
    positions_holding: int = 0
    exit_signals: int = 0
    alerts_sent: int = 0
    positions_failed: int = 0
    state_save_errors: int = 0
    journal_errors: int = 0


@dataclass
class PositionEvaluation:
    symbol: str
    quantity: float
    entry_price: float
    current_price: float
    highest_price: float
    highest_gain_percent: float
    break_even_active: bool
    trailing_stop_active: bool
    trailing_stop_price: float
    market_value: float
    unrealized_pl: float
    unrealized_pl_percent: float
    exit_decision: str
    exit_reason: str


def get_trading_client() -> TradingClient:
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        raise RuntimeError(
            "Alpaca API keys were not found in the .env file."
        )

    return TradingClient(api_key, secret_key, paper=True)


def load_position_state() -> dict[str, dict[str, Any]]:
    if not POSITION_STATE_FILE.exists():
        return {}

    try:
        with POSITION_STATE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, TypeError):
        return {}


def save_position_state(state: dict[str, dict[str, Any]]) -> bool:
    try:
        with POSITION_STATE_FILE.open("w", encoding="utf-8") as file:
            json.dump(state, file, indent=4)
        return True
    except OSError as error:
        print(
            "Position State   : ERROR - "
            f"could not save state: {error}"
        )
        return False


def calculate_position_evaluation(
    position: Any,
    position_state: dict[str, dict[str, Any]],
) -> PositionEvaluation:
    symbol = str(position.symbol)
    quantity = float(position.qty)
    entry_price = float(position.avg_entry_price)
    current_price = float(position.current_price)
    market_value = float(position.market_value)
    unrealized_pl = float(position.unrealized_pl)
    unrealized_pl_percent = float(position.unrealized_plpc) * 100

    if entry_price <= 0:
        raise ValueError(
            f"{symbol} has an invalid average entry price: {entry_price}."
        )

    saved_position = position_state.get(symbol, {})
    saved_entry_price = float(
        saved_position.get("entry_price", entry_price)
    )

    if abs(saved_entry_price - entry_price) > 0.01:
        saved_highest_price = entry_price
    else:
        saved_highest_price = float(
            saved_position.get("highest_price", entry_price)
        )

    highest_price = max(
        saved_highest_price,
        entry_price,
        current_price,
    )

    highest_gain_percent = (
        (highest_price - entry_price) / entry_price
    ) * 100

    break_even_active = (
        highest_gain_percent >= BREAK_EVEN_TRIGGER
    )
    trailing_stop_active = (
        highest_gain_percent >= TRAILING_STOP_TRIGGER
    )
    trailing_stop_price = highest_price * (
        1 - TRAILING_STOP_PERCENT / 100
    )

    first_seen_time = saved_position.get(
        "first_seen_time",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    position_state[symbol] = {
        "entry_price": entry_price,
        "first_seen_time": first_seen_time,
        "highest_price": highest_price,
        "highest_gain_percent": highest_gain_percent,
        "break_even_active": break_even_active,
        "trailing_stop_active": trailing_stop_active,
        "trailing_stop_price": trailing_stop_price,
    }

    if unrealized_pl_percent <= STOP_LOSS_PERCENT:
        exit_decision = "STOP_LOSS"
        exit_reason = (
            f"Loss reached {unrealized_pl_percent:.2f}%."
        )
    elif trailing_stop_active and current_price <= trailing_stop_price:
        exit_decision = "TRAILING_STOP"
        exit_reason = (
            f"Price fell to ${current_price:.2f}, below the "
            f"${trailing_stop_price:.2f} trailing-stop level."
        )
    elif break_even_active and current_price <= entry_price:
        exit_decision = "BREAK_EVEN"
        exit_reason = (
            "Position previously gained at least "
            f"{BREAK_EVEN_TRIGGER:.2f}% and returned to "
            f"the ${entry_price:.2f} entry price."
        )
    else:
        exit_decision = "HOLD"
        if trailing_stop_active:
            exit_reason = (
                "Trailing-stop protection is active at "
                f"${trailing_stop_price:.2f}."
            )
        elif break_even_active:
            exit_reason = (
                "Break-even protection is active at "
                f"${entry_price:.2f}."
            )
        else:
            exit_reason = (
                "Position remains inside the allowed range. "
                "Break-even protection activates after a "
                f"{BREAK_EVEN_TRIGGER:.2f}% gain."
            )

    return PositionEvaluation(
        symbol=symbol,
        quantity=quantity,
        entry_price=entry_price,
        current_price=current_price,
        highest_price=highest_price,
        highest_gain_percent=highest_gain_percent,
        break_even_active=break_even_active,
        trailing_stop_active=trailing_stop_active,
        trailing_stop_price=trailing_stop_price,
        market_value=market_value,
        unrealized_pl=unrealized_pl,
        unrealized_pl_percent=unrealized_pl_percent,
        exit_decision=exit_decision,
        exit_reason=exit_reason,
    )


def print_position(result: PositionEvaluation) -> None:
    print(f"\nSymbol          : {result.symbol}")
    print(f"Quantity        : {result.quantity:g}")
    print(f"Average Entry   : ${result.entry_price:.2f}")
    print(f"Current Price   : ${result.current_price:.2f}")
    print(f"Highest Price   : ${result.highest_price:.2f}")
    print(f"Highest Gain    : {result.highest_gain_percent:.2f}%")
    print(f"Break-Even      : {result.break_even_active}")
    print(f"Trailing Stop   : ${result.trailing_stop_price:.2f}")
    print(f"Trailing Active : {result.trailing_stop_active}")
    print(f"Market Value    : ${result.market_value:.2f}")
    print(f"Unrealized P/L  : ${result.unrealized_pl:.2f}")
    print(f"Unrealized P/L% : {result.unrealized_pl_percent:.2f}%")
    print(f"Exit Decision   : {result.exit_decision}")
    print(f"Exit Reason     : {result.exit_reason}")


def print_summary(
    metrics: MonitorMetrics,
    duration_seconds: float,
    heartbeat_status: str,
) -> None:
    print()
    print("=" * 50)
    print("Position Summary")
    print("-" * 50)
    print(f"Positions Checked : {metrics.positions_checked}")
    print(f"Holding           : {metrics.positions_holding}")
    print(f"Exit Signals      : {metrics.exit_signals}")
    print(f"Alerts Sent       : {metrics.alerts_sent}")
    print(f"Position Errors   : {metrics.positions_failed}")
    print(f"State Save Errors : {metrics.state_save_errors}")
    print(f"Journal Errors    : {metrics.journal_errors}")
    print(f"Duration          : {duration_seconds:.2f} seconds")
    print(f"Heartbeat         : {heartbeat_status}")
    print("=" * 50)


def handle_exit_decision(
    trading_client: TradingClient,
    result: PositionEvaluation,
    position_state: dict[str, dict[str, Any]],
    metrics: MonitorMetrics,
) -> bool:
    """
    Send an informational alert when this module's own thresholds
    would have suggested an exit.

    This module NEVER submits an exit order and NEVER writes to the
    trade journal — the bracket order is LOCKBOT's sole exit
    mechanism, and trade_manager.py is the sole writer of completed
    trades. See the module docstring for why. Returns True only to
    indicate an alert was sent, for metrics purposes.
    """

    if result.exit_decision not in {
        "STOP_LOSS",
        "BREAK_EVEN",
        "TRAILING_STOP",
    }:
        return False

    print(
        "Informational Alert : "
        f"{result.exit_decision} threshold reached "
        "(monitoring only — the bracket order owns the real exit)."
    )

    send_notification(
        title=f"LockBot {result.exit_decision} (informational)",
        message=(
            f"Symbol: {result.symbol}\n"
            f"Quantity: {abs(result.quantity):g}\n"
            f"Entry: ${result.entry_price:.2f}\n"
            f"Current price: ${result.current_price:.2f}\n"
            f"Highest price: ${result.highest_price:.2f}\n"
            f"Highest gain: {result.highest_gain_percent:.2f}%\n"
            f"Break-even active: {result.break_even_active}\n"
            f"Trailing stop level: ${result.trailing_stop_price:.2f}\n"
            f"Unrealized P/L: ${result.unrealized_pl:.2f}\n"
            f"Reason: {result.exit_reason}\n\n"
            "This is informational only. The bracket order's own "
            "stop-loss/take-profit legs are LOCKBOT's authoritative "
            "exit mechanism for this position."
        ),
    )

    return True

def build_heartbeat_details(
    metrics: MonitorMetrics,
    duration_seconds: float,
) -> dict[str, Any]:
    return {
        "version": POSITION_MONITOR_VERSION,
        "positions_checked": metrics.positions_checked,
        "positions_holding": metrics.positions_holding,
        "exit_signals": metrics.exit_signals,
        "alerts_sent": metrics.alerts_sent,
        "positions_failed": metrics.positions_failed,
        "state_save_errors": metrics.state_save_errors,
        "journal_errors": metrics.journal_errors,
        "paper_exits_enabled": config.ENABLE_PAPER_EXITS,
        "duration_seconds": round(duration_seconds, 3),
    }


def run_position_monitor() -> None:
    started_at = time.perf_counter()
    metrics = MonitorMetrics()

    mark_module_starting(
        MODULE_NAME,
        message="Position monitor is starting.",
        details={
            "version": POSITION_MONITOR_VERSION,
            "paper_exits_enabled": config.ENABLE_PAPER_EXITS,
        },
    )

    try:
        trading_client = get_trading_client()

        print("=" * 50)
        print(
            "LOCKBOT POSITION MONITOR "
            f"v{POSITION_MONITOR_VERSION}"
        )
        print("=" * 50)

        # Equity only. Option contracts are managed by options_manager.py,
        # which holds their software stop; evaluating one here against
        # stock stop-loss percentages would be meaningless.
        positions = equity_positions(
            with_retries(trading_client.get_all_positions)()
        )
        position_state = load_position_state()
        open_symbols: set[str] = set()

        if not positions:
            print("No open paper positions.")
            position_state = {}
        else:
            for position in positions:
                symbol = str(
                    getattr(position, "symbol", "UNKNOWN")
                )
                open_symbols.add(symbol)

                try:
                    result = calculate_position_evaluation(
                        position=position,
                        position_state=position_state,
                    )
                    metrics.positions_checked += 1
                    print_position(result)

                    if result.exit_decision == "HOLD":
                        metrics.positions_holding += 1
                    else:
                        metrics.exit_signals += 1

                    if handle_exit_decision(
                        trading_client=trading_client,
                        result=result,
                        position_state=position_state,
                        metrics=metrics,
                    ):
                        metrics.alerts_sent += 1

                except Exception as position_error:
                    metrics.positions_failed += 1
                    print()
                    print(f"Position Error  : {symbol}")
                    print(
                        "Error           : "
                        f"{type(position_error).__name__}: "
                        f"{position_error}"
                    )

        position_state = {
            symbol: state
            for symbol, state in position_state.items()
            if symbol in open_symbols
        }

        if not save_position_state(position_state):
            metrics.state_save_errors += 1

        duration_seconds = time.perf_counter() - started_at
        details = build_heartbeat_details(metrics, duration_seconds)

        if (
            metrics.positions_failed
            or metrics.state_save_errors
            or metrics.journal_errors
        ):
            mark_module_degraded(
                MODULE_NAME,
                message=(
                    "Position monitor completed with "
                    f"{metrics.positions_failed} position error(s), "
                    f"{metrics.state_save_errors} state-save error(s), "
                    f"and {metrics.journal_errors} journal error(s)."
                ),
                details=details,
            )
            heartbeat_status = "DEGRADED"
        else:
            mark_module_healthy(
                MODULE_NAME,
                message=(
                    "Position monitor completed successfully for "
                    f"{metrics.positions_checked} position(s)."
                ),
                details=details,
            )
            heartbeat_status = "HEALTHY"

        print_summary(metrics, duration_seconds, heartbeat_status)

    except Exception as error:
        duration_seconds = time.perf_counter() - started_at
        details = build_heartbeat_details(metrics, duration_seconds)
        details["exception_type"] = type(error).__name__

        mark_module_critical(
            MODULE_NAME,
            message=(
                "Position monitor failed with an unhandled exception: "
                f"{type(error).__name__}: {error}"
            ),
            details=details,
        )

        print()
        print("=" * 50)
        print("POSITION MONITOR FAILURE")
        print("-" * 50)
        print(f"Error Type       : {type(error).__name__}")
        print(f"Error Message    : {error}")
        print(f"Duration         : {duration_seconds:.2f} seconds")
        print("Heartbeat        : CRITICAL")
        print("=" * 50)
        raise


def main() -> None:
    run_position_monitor()


if __name__ == "__main__":
    main()