"""LockBot autonomous controller v0.3."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from lockbot_core import create_state
from notifications import send_notification
from scanner_state import load_state
from startup_reconciliation import run_startup_reconciliation
from self_repair import attempt_component_repair


# ============================================================
# Configuration
# ============================================================

LOCKBOT_CONTROLLER_VERSION = "0.3"

CYCLE_INTERVAL_SECONDS = 300
CYCLE_ERROR_RETRY_DELAY_SECONDS = 60
HOURLY_HEALTH_REPORT_SECONDS = 3600

MAX_COMPONENT_ATTEMPTS = 3
COMPONENT_RETRY_DELAY_SECONDS = 10

MAX_SCANNER_STATE_AGE_SECONDS = 360
CONTROLLER_MUTEX_NAME = (
    "Local\\LOCKBOT_AUTONOMOUS_CONTROLLER"
)

ERROR_ALREADY_EXISTS = 183

# ============================================================
# Project paths
# ============================================================

PROJECT_FOLDER = Path(__file__).resolve().parent

SCANNER_FILE = PROJECT_FOLDER / "market_scanner.py"
TRADE_MANAGER_FILE = PROJECT_FOLDER / "trade_manager.py"
HEALTH_MONITOR_FILE = PROJECT_FOLDER / "health_monitor.py"
POSITION_MONITOR_FILE = PROJECT_FOLDER / "position_monitor.py"
STARTUP_RECONCILIATION_FILE = (
    PROJECT_FOLDER / "startup_reconciliation.py"
)
CONTROLLER_LOG_FILE = PROJECT_FOLDER / "lockbot_controller.log"
CONTROLLER_MUTEX_HANDLE: int | None = None
COMPONENT_FAILURE_STATES: dict[str, bool] = {}

# ============================================================
# Single-instance protection
# ============================================================

def acquire_controller_mutex() -> bool:
    """
    Allow only one LOCKBOT controller process to run.

    Returns True when this process successfully acquires the
    controller mutex. Returns False when another controller
    instance is already running.
    """

    global CONTROLLER_MUTEX_HANDLE

    kernel32 = ctypes.windll.kernel32

    mutex_handle = kernel32.CreateMutexW(
        None,
        False,
        CONTROLLER_MUTEX_NAME,
    )

    if not mutex_handle:
        raise ctypes.WinError()

    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(mutex_handle)
        return False

    CONTROLLER_MUTEX_HANDLE = mutex_handle
    return True


def release_controller_mutex() -> None:
    """Release this process's controller mutex handle."""

    global CONTROLLER_MUTEX_HANDLE

    if CONTROLLER_MUTEX_HANDLE is None:
        return

    ctypes.windll.kernel32.CloseHandle(
        CONTROLLER_MUTEX_HANDLE
    )

    CONTROLLER_MUTEX_HANDLE = None


def get_scanner_state_age_seconds(
    scan_time_text: str | None,
) -> float | None:
    """Calculate the age of the most recent scanner state."""

    if not scan_time_text:
        return None

    try:
        scan_time = datetime.fromisoformat(scan_time_text)

        if scan_time.tzinfo is None:
            scan_time = scan_time.replace(tzinfo=timezone.utc)

        current_time = datetime.now(timezone.utc)

        return (
            current_time - scan_time.astimezone(timezone.utc)
        ).total_seconds()

    except (TypeError, ValueError):
        return None


def display_scanner_state() -> bool:
    """Display and validate the most recent scanner state."""

    state = load_state()

    print()
    print("=" * 58)
    print("              LOCKBOT SCANNER STATUS")
    print("=" * 58)

    if not state:
        print("Scanner State : NOT AVAILABLE")
        print("State Status  : INVALID")
        print("=" * 58)
        return False

    scan_time_text = state.get("scan_time")
    state_age_seconds = get_scanner_state_age_seconds(
        scan_time_text
    )

    print(f"Last Scan     : {scan_time_text or 'UNKNOWN'}")

    if state_age_seconds is None:
        print("State Age     : UNKNOWN")
        print("State Status  : INVALID")
        state_is_healthy = False

    elif state_age_seconds <= MAX_SCANNER_STATE_AGE_SECONDS:
        print(f"State Age     : {state_age_seconds:.1f} seconds")
        print("State Status  : FRESH")
        state_is_healthy = True

    else:
        print(f"State Age     : {state_age_seconds:.1f} seconds")
        print("State Status  : STALE")
        state_is_healthy = False

    print(f"Market Open   : {state.get('market_open', False)}")

    print(
        f"Equity        : "
        f"${state.get('account_equity', 0.0):,.2f}"
    )

    print(
        f"Buying Power  : "
        f"${state.get('buying_power', 0.0):,.2f}"
    )

    print(
        f"Daily P&L     : "
        f"${state.get('daily_pnl', 0.0):,.2f}"
    )

    print(
        f"Daily P&L %   : "
        f"{state.get('daily_pnl_percent', 0.0):.2f}%"
    )

    print(
        f"Loss Limit    : "
        f"{state.get('daily_loss_limit_hit', False)}"
    )

    symbols = state.get("symbols", {})

    if not symbols:
        print()
        print("Symbol Data   : NOT AVAILABLE")
        state_is_healthy = False

    for symbol, result in symbols.items():
        print()
        print(symbol)

        print(
            f"  Signal      : "
            f"{result.get('signal', 'UNKNOWN')}"
        )

        print(
            f"  Confidence  : "
            f"{result.get('confidence', 0)}/100"
        )

        print(
            f"  Approved    : "
            f"{result.get('approved', False)}"
        )

        print(
            f"  Reason      : "
            f"{result.get('approval_reason', 'UNKNOWN')}"
        )

        print(
            f"  Regime      : "
            f"{result.get('market_regime', 'UNKNOWN')}"
        )

        print(
            f"  Last Price  : "
            f"${result.get('latest_price', 0.0):,.2f}"
        )

        print(
            f"  Position    : "
            f"{result.get('position_size', 0)} shares"
        )

    print("=" * 58)

    return state_is_healthy


# ============================================================
# Logging
# ============================================================

def write_log(message: str) -> None:
    """Print a timestamped message and append it to the log."""

    timestamp = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    formatted_message = f"[{timestamp}] {message}"

    print(formatted_message, flush=True)

    with CONTROLLER_LOG_FILE.open(
        mode="a",
        encoding="utf-8",
    ) as log_file:
        log_file.write(formatted_message + "\n")


# ============================================================
# Push notifications
# ============================================================

def send_controller_notification(
    title: str,
    message: str,
) -> None:
    """Send a controller alert without causing a second crash."""

    try:
        send_notification(
            title=title,
            message=message,
        )

    except Exception as notification_error:
        write_log(
            "Push notification failed: "
            f"{type(notification_error).__name__}: "
            f"{notification_error}"
        )


# ============================================================
# Validation
# ============================================================

def validate_required_files() -> None:
    """Confirm that required LockBot files exist."""

    required_files = (
        SCANNER_FILE,
        TRADE_MANAGER_FILE,
        HEALTH_MONITOR_FILE,
        POSITION_MONITOR_FILE,
        STARTUP_RECONCILIATION_FILE,
    )

    missing_files = [
        path.name
        for path in required_files
        if not path.exists()
    ]

    if missing_files:
        raise FileNotFoundError(
            "Required LockBot file(s) missing: "
            + ", ".join(missing_files)
        )


# ============================================================
# Component execution and crash recovery
# ============================================================

def run_component_once(
    script_path: Path,
    component_name: str,
) -> bool:
    """Run one LockBot component one time."""

    write_log(f"Starting {component_name}.")

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(PROJECT_FOLDER),
            check=False,
        )

    except Exception as error:
        write_log(
            f"{component_name} could not start: "
            f"{type(error).__name__}: {error}"
        )
        return False

    if result.returncode == 0:
        write_log(
            f"{component_name} completed successfully."
        )
        return True

    write_log(
        f"{component_name} failed with exit code "
        f"{result.returncode}."
    )

    return False


def run_component_with_recovery(
    script_path: Path,
    component_name: str,
    health_check: Callable[[], bool] | None = None,
) -> bool:
    """
    Run a component and automatically retry when it fails.

    A component is considered successful only when its script
    exits successfully and its optional health check passes.
    """

    for attempt_number in range(
        1,
        MAX_COMPONENT_ATTEMPTS + 1,
    ):
        write_log(
            f"{component_name} attempt "
            f"{attempt_number}/{MAX_COMPONENT_ATTEMPTS}."
        )

        component_ok = run_component_once(
            script_path,
            component_name,
        )

        health_check_ok = True

        if component_ok and health_check is not None:
            try:
                health_check_ok = health_check()
            except Exception as health_error:
                health_check_ok = False
                write_log(
                    f"{component_name} health check crashed: "
                    f"{type(health_error).__name__}: "
                    f"{health_error}"
                )

        if component_ok and health_check_ok:
            previously_failed = COMPONENT_FAILURE_STATES.get(
                component_name,
                False,
            )

            if attempt_number > 1 or previously_failed:
                write_log(
                    f"{component_name} recovered successfully "
                    f"on attempt {attempt_number}."
                )

                send_controller_notification(
                    title=f"LOCKBOT {component_name} Recovered",
                    message=(
                        f"{component_name} recovered automatically.\n\n"
                        f"Successful attempt: "
                        f"{attempt_number}/"
                        f"{MAX_COMPONENT_ATTEMPTS}\n"
                        f"Time: "
                        f"{datetime.now().astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')}"
                    ),
                )

            COMPONENT_FAILURE_STATES[component_name] = False

            return True

        failure_reason_parts: list[str] = []

        if not component_ok:
            failure_reason_parts.append(
                "component execution failed"
            )

        if component_ok and not health_check_ok:
            failure_reason_parts.append(
                "health check failed"
            )

        failure_reason = ", ".join(
            failure_reason_parts
        ) or "unknown failure"

        write_log(
            f"{component_name} attempt "
            f"{attempt_number} was unsuccessful: "
            f"{failure_reason}."
        )

        if attempt_number < MAX_COMPONENT_ATTEMPTS:
            write_log(
                f"Crash recovery will retry "
                f"{component_name} in "
                f"{COMPONENT_RETRY_DELAY_SECONDS} seconds."
            )

            time.sleep(COMPONENT_RETRY_DELAY_SECONDS)

    write_log(
        f"{component_name} could not recover after "
        f"{MAX_COMPONENT_ATTEMPTS} attempts."
    )
    failure_was_already_reported = COMPONENT_FAILURE_STATES.get(
        component_name,
        False,
    )

    COMPONENT_FAILURE_STATES[component_name] = True

    if not failure_was_already_reported:
        send_controller_notification(
            title=f"URGENT: LOCKBOT {component_name} Failed",
            message=(
                f"{component_name} could not recover automatically.\n\n"
                f"Attempts: {MAX_COMPONENT_ATTEMPTS}\n"
                f"Time: "
                f"{datetime.now().astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')}\n\n"
                "LOCKBOT will continue running and try again "
                "during the next controller cycle."
            ),
        )

    return False


# ============================================================
# Scheduling
# ============================================================

def seconds_until_next_cycle(
    interval_seconds: int,
) -> int:
    """Calculate the wait until the next aligned cycle."""

    current_epoch = int(time.time())
    remainder = current_epoch % interval_seconds

    return max(interval_seconds - remainder, 1)


# ============================================================
# Main controller
# ============================================================

def run_controller() -> None:
    """Run the autonomous LockBot controller loop."""

    validate_required_files()

    state = create_state()
    state.controller_running = True
    state.system_status = "STARTING"

    print("=" * 58)

    print(
        f"       LOCKBOT AUTONOMOUS CONTROLLER "
        f"v{LOCKBOT_CONTROLLER_VERSION}"
    )

    print("=" * 58)

    print(
        f"Cycle interval : "
        f"{CYCLE_INTERVAL_SECONDS // 60} minutes"
    )

    print(
        f"Recovery       : "
        f"{MAX_COMPONENT_ATTEMPTS} attempts, "
        f"{COMPONENT_RETRY_DELAY_SECONDS}-second delay"
    )

    print(f"Project folder : {PROJECT_FOLDER}")
    print("Stop safely    : Press Ctrl+C")
    print("=" * 58)

    write_log("Controller started.")

    write_log("Starting broker reconciliation.")

    reconciliation_ok = run_startup_reconciliation()

    if not reconciliation_ok:
        raise RuntimeError(
            "Startup broker reconciliation failed."
        )

    write_log("Broker reconciliation completed successfully.")

    send_controller_notification(
        title="LOCKBOT Controller Online",
        message=(
            "LOCKBOT started successfully.\n\n"
            f"Version: {LOCKBOT_CONTROLLER_VERSION}\n"
            f"Cycle interval: "
            f"{CYCLE_INTERVAL_SECONDS // 60} minutes\n"
            f"Recovery attempts: "
            f"{MAX_COMPONENT_ATTEMPTS}\n"
            f"Time: "
            f"{datetime.now().astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')}"
        ),
    )

    cycle_number = 0
    last_hourly_health_report_at = time.monotonic()

    while True:
        cycle_number += 1

        state.scanner_cycles = cycle_number
        state.system_status = "RUNNING"
        state.controller_running = True

        write_log(f"Cycle {cycle_number} started.")

        try:
            scanner_ok = run_component_with_recovery(
                script_path=SCANNER_FILE,
                component_name="Market Scanner",
                health_check=display_scanner_state,
            )

            manager_ok = run_component_with_recovery(
                script_path=TRADE_MANAGER_FILE,
                component_name="Trade Manager",
            )

            position_monitor_ok = run_component_with_recovery(
                script_path=POSITION_MONITOR_FILE,
                component_name="Position Monitor",
            )

            health_ok = run_component_with_recovery(
                script_path=HEALTH_MONITOR_FILE,
                component_name="Health Monitor",
            )

            cycle_healthy = (
                scanner_ok
                and manager_ok
                and position_monitor_ok
                and health_ok
            )

            current_monotonic_time = time.monotonic()

            scanner_snapshot = load_state() or {}
            symbol_snapshot = scanner_snapshot.get("symbols", {})

            active_positions = sum(
                1
                for symbol_data in symbol_snapshot.values()
                if symbol_data.get("position_size", 0) != 0
            )
            if (
                current_monotonic_time
                - last_hourly_health_report_at
                >= HOURLY_HEALTH_REPORT_SECONDS
            ):
                hourly_status = (
                    "HEALTHY"
                    if cycle_healthy
                    else "ERROR"
                )

                send_controller_notification(
                    title="LOCKBOT Hourly Health Report",
                    message=(
                        f"Overall Status: {hourly_status}\n\n"
                        f"Market Scanner: "
                        f"{'HEALTHY' if scanner_ok else 'ERROR'}\n"
                        f"Trade Manager: "
                        f"{'HEALTHY' if manager_ok else 'ERROR'}\n"
                        f"Position Monitor: "
                        f"{'HEALTHY' if position_monitor_ok else 'ERROR'}\n"
                        f"Health Monitor: "
                        f"{'HEALTHY' if health_ok else 'ERROR'}\n\n"
                        f"Market: "
                        f"{'OPEN' if scanner_snapshot.get('market_open', False) else 'CLOSED'}\n"
                        f"Equity: "
                        f"${scanner_snapshot.get('account_equity', 0.0):,.2f}\n"
                        f"Daily P&L: "
                        f"${scanner_snapshot.get('daily_pnl', 0.0):,.2f}\n"
                        f"Active Positions: {active_positions}\n"
                        f"Cycle: {cycle_number}\n"
                        f"Time: "
                        f"{datetime.now().astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')}"
                    ),
                )

                last_hourly_health_report_at = (
                    current_monotonic_time
                )

            if cycle_healthy:
                state.system_status = "HEALTHY"
                state.last_scan_time = (
                    datetime.now().astimezone()
                )

                wait_seconds = seconds_until_next_cycle(
                    CYCLE_INTERVAL_SECONDS
                )

                write_log(
                    f"Cycle {cycle_number} complete. "
                    f"System status: "
                    f"{state.system_status}. "
                    f"Next cycle in approximately "
                    f"{wait_seconds} seconds."
                )

            else:
                state.system_status = "ERROR"
                wait_seconds = (
                    CYCLE_ERROR_RETRY_DELAY_SECONDS
                )

                write_log(
                    f"Cycle {cycle_number} had an error. "
                    f"Market Scanner: {scanner_ok}. "
                    f"Trade Manager: {manager_ok}. "
                    f"Position Monitor: {position_monitor_ok}. "
                    f"Health Monitor: {health_ok}. "
                    f"Starting another cycle in "
                    f"{wait_seconds} seconds."
                )

        except Exception as cycle_error:
            state.system_status = "ERROR"
            wait_seconds = CYCLE_ERROR_RETRY_DELAY_SECONDS

            error_type = type(cycle_error).__name__
            error_message = (
                str(cycle_error)
                or "No error details were provided."
            )

            write_log(
                f"Cycle {cycle_number} crashed unexpectedly: "
                f"{error_type}: {error_message}. "
                f"Controller remains online and will retry "
                f"in {wait_seconds} seconds."
            )

            send_controller_notification(
                title="LOCKBOT Cycle Recovery Activated",
                message=(
                    f"Controller cycle {cycle_number} "
                    f"encountered an unexpected error.\n\n"
                    f"Error type: {error_type}\n"
                    f"Details: {error_message}\n"
                    f"Retry delay: {wait_seconds} seconds\n"
                    f"Time: "
                    f"{datetime.now().astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')}\n\n"
                    "The controller is still running."
                ),
            )

        time.sleep(wait_seconds)


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    try:
        if not acquire_controller_mutex():
            print()
            print("=" * 58)
            print("LOCKBOT CONTROLLER START BLOCKED")
            print("=" * 58)
            print(
                "Another LOCKBOT controller is already running."
            )
            print(
                "This duplicate instance will now close safely."
            )
            print("=" * 58)

            sys.exit(0)

        run_controller()

    except KeyboardInterrupt:
        write_log("Controller stopped safely by the user.")

        print()
        print("LockBot controller stopped.")

    except Exception as error:
        error_type = type(error).__name__
        error_message = (
            str(error)
            or "No error details were provided."
        )

        crash_time = datetime.now().astimezone()

        write_log(
            f"Controller stopped unexpectedly: "
            f"{error_type}: {error_message}"
        )

        send_controller_notification(
            title="URGENT: LOCKBOT Controller Crashed",
            message=(
                "LOCKBOT stopped unexpectedly.\n\n"
                f"Time: "
                f"{crash_time.strftime('%Y-%m-%d %I:%M:%S %p %Z')}\n"
                f"Error type: {error_type}\n"
                f"Details: {error_message}\n\n"
                "Check the computer and "
                "lockbot_controller.log."
            ),
        )

        raise

    finally:
        release_controller_mutex()