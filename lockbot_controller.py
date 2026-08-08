"""LockBot autonomous controller v0.3."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

from daily_report import send_daily_report
from lockbot_core import create_state
from notifications import send_notification
from scanner_state import load_state
from startup_reconciliation import run_startup_reconciliation
from self_repair import attempt_component_repair
import lockbot_config as config


load_dotenv()

LOCKBOT_CONTROLLER_VERSION = "0.4"

# Sourced from lockbot_config.py — the single source of truth.
CYCLE_INTERVAL_SECONDS = config.SCAN_INTERVAL_SECONDS
PREMARKET_CHECK_SECONDS = config.PREMARKET_CHECK_SECONDS

CYCLE_ERROR_RETRY_DELAY_SECONDS = 60
HOURLY_HEALTH_REPORT_SECONDS = 3600

DAILY_REPORT_HOUR = 15
DAILY_REPORT_MINUTE = 10

MAX_COMPONENT_ATTEMPTS = 3
COMPONENT_RETRY_DELAY_SECONDS = 10

MAX_SCANNER_STATE_AGE_SECONDS = 360
CONTROLLER_MUTEX_NAME = "Local\\LOCKBOT_AUTONOMOUS_CONTROLLER"

ERROR_ALREADY_EXISTS = 183

PROJECT_FOLDER = Path(__file__).resolve().parent

SCANNER_FILE = PROJECT_FOLDER / "market_scanner.py"
TRADE_MANAGER_FILE = PROJECT_FOLDER / "trade_manager.py"
HEALTH_MONITOR_FILE = PROJECT_FOLDER / "health_monitor.py"
POSITION_MONITOR_FILE = PROJECT_FOLDER / "position_monitor.py"
EQUITY_TIME_STOP_FILE = PROJECT_FOLDER / "equity_time_stop.py"
OPTIONS_MANAGER_FILE = PROJECT_FOLDER / "options_manager.py"
OPTIONS_SCANNER_FILE = PROJECT_FOLDER / "options_scanner.py"
STARTUP_RECONCILIATION_FILE = PROJECT_FOLDER / "startup_reconciliation.py"
CONTROLLER_LOG_FILE = PROJECT_FOLDER / "lockbot_controller.log"
CONTROLLER_MUTEX_HANDLE: int | None = None
COMPONENT_FAILURE_STATES: dict[str, bool] = {}


def acquire_controller_mutex() -> bool:
    """
    Allow only one LOCKBOT controller process to run.

    Returns True when this process successfully acquires the
    controller mutex. Returns False when another controller
    instance is already running.
    """

    global CONTROLLER_MUTEX_HANDLE

    kernel32 = ctypes.windll.kernel32

    mutex_handle = kernel32.CreateMutexW(None, False, CONTROLLER_MUTEX_NAME)

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

    ctypes.windll.kernel32.CloseHandle(CONTROLLER_MUTEX_HANDLE)
    CONTROLLER_MUTEX_HANDLE = None


def get_scanner_state_age_seconds(scan_time_text: str | None) -> float | None:
    """Calculate the age of the most recent scanner state."""

    if not scan_time_text:
        return None

    try:
        scan_time = datetime.fromisoformat(scan_time_text)

        if scan_time.tzinfo is None:
            scan_time = scan_time.replace(tzinfo=timezone.utc)

        current_time = datetime.now(timezone.utc)

        return (current_time - scan_time.astimezone(timezone.utc)).total_seconds()

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
    state_age_seconds = get_scanner_state_age_seconds(scan_time_text)

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
    print(f"Equity        : ${state.get('account_equity', 0.0):,.2f}")
    print(f"Buying Power  : ${state.get('buying_power', 0.0):,.2f}")
    print(f"Daily P&L     : ${state.get('daily_pnl', 0.0):,.2f}")
    print(f"Daily P&L %   : {state.get('daily_pnl_percent', 0.0):.2f}%")
    print(f"Loss Limit    : {state.get('daily_loss_limit_hit', False)}")

    symbols = state.get("symbols", {})

    # An empty symbols map is NORMAL, not a fault. market_scanner.py only
    # writes per-symbol detail for candidates that reach stage two, and
    # most cycles have none — every cycle with the market closed has none
    # by definition.
    #
    # Treating that as a failed health check cost 26 self-repairs and 80
    # wasted retries between 2026-07-27 and 07-29, plus a Pushover alert
    # each time, while the scanner was working perfectly. The controller
    # was repairing a component that was never broken.
    #
    # Real health is: the scanner ran, wrote its state, and did so
    # recently. Those are checked above. Whether it found anything worth
    # trading is a market outcome, not a fault.
    if not symbols:
        print()
        print("Symbol Data   : none advanced to stage two this cycle (normal)")

    for symbol, result in symbols.items():
        print()
        print(symbol)
        print(f"  Signal      : {result.get('signal', 'UNKNOWN')}")
        print(f"  Confidence  : {result.get('confidence', 0)}/100")
        print(f"  Approved    : {result.get('approved', False)}")
        print(f"  Reason      : {result.get('approval_reason', 'UNKNOWN')}")
        print(f"  Regime      : {result.get('market_regime', 'UNKNOWN')}")
        print(f"  Last Price  : ${result.get('latest_price', 0.0):,.2f}")
        print(f"  Position    : {result.get('position_size', 0)} shares")

    print("=" * 58)

    return state_is_healthy


def write_log(message: str) -> None:
    """Print a timestamped message and append it to the log."""

    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    formatted_message = f"[{timestamp}] {message}"

    print(formatted_message, flush=True)

    with CONTROLLER_LOG_FILE.open(mode="a", encoding="utf-8") as log_file:
        log_file.write(formatted_message + "\n")


def send_controller_notification(title: str, message: str) -> None:
    """Send a controller alert without causing a second crash."""

    try:
        send_notification(title=title, message=message)

    except Exception as notification_error:
        write_log(
            "Push notification failed: "
            f"{type(notification_error).__name__}: {notification_error}"
        )


def validate_required_files() -> None:
    """Confirm that required LockBot files exist."""

    required_files = [
        SCANNER_FILE,
        TRADE_MANAGER_FILE,
        HEALTH_MONITOR_FILE,
        POSITION_MONITOR_FILE,
        STARTUP_RECONCILIATION_FILE,
    ]

    if getattr(config, "OPTIONS_ENABLED", False):
        required_files.append(OPTIONS_MANAGER_FILE)
        required_files.append(OPTIONS_SCANNER_FILE)

    missing_files = [path.name for path in required_files if not path.exists()]

    if missing_files:
        raise FileNotFoundError(
            "Required LockBot file(s) missing: " + ", ".join(missing_files)
        )


def run_component_once(script_path: Path, component_name: str) -> bool:
    """Run one LockBot component one time."""

    write_log(f"Starting {component_name}.")

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(PROJECT_FOLDER),
            check=False,
        )

    except Exception as error:
        write_log(f"{component_name} could not start: {type(error).__name__}: {error}")
        return False

    if result.returncode == 0:
        write_log(f"{component_name} completed successfully.")
        return True

    write_log(f"{component_name} failed with exit code {result.returncode}.")

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

    for attempt_number in range(1, MAX_COMPONENT_ATTEMPTS + 1):
        write_log(f"{component_name} attempt {attempt_number}/{MAX_COMPONENT_ATTEMPTS}.")

        component_ok = run_component_once(script_path, component_name)

        health_check_ok = True

        if component_ok and health_check is not None:
            try:
                health_check_ok = health_check()
            except Exception as health_error:
                health_check_ok = False
                write_log(
                    f"{component_name} health check crashed: "
                    f"{type(health_error).__name__}: {health_error}"
                )

        if component_ok and health_check_ok:
            previously_failed = COMPONENT_FAILURE_STATES.get(component_name, False)

            if attempt_number > 1 or previously_failed:
                write_log(
                    f"{component_name} recovered successfully on attempt {attempt_number}."
                )

                send_controller_notification(
                    title=f"LOCKBOT {component_name} Recovered",
                    message=(
                        f"{component_name} recovered automatically.\n\n"
                        f"Successful attempt: {attempt_number}/{MAX_COMPONENT_ATTEMPTS}\n"
                        f"Time: {datetime.now().astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')}"
                    ),
                )

            COMPONENT_FAILURE_STATES[component_name] = False

            return True

        failure_reason_parts: list[str] = []

        if not component_ok:
            failure_reason_parts.append("component execution failed")

        if component_ok and not health_check_ok:
            failure_reason_parts.append("health check failed")

        failure_reason = ", ".join(failure_reason_parts) or "unknown failure"

        write_log(
            f"{component_name} attempt {attempt_number} was unsuccessful: {failure_reason}."
        )

        if attempt_number < MAX_COMPONENT_ATTEMPTS:
            write_log(
                f"Crash recovery will retry {component_name} in "
                f"{COMPONENT_RETRY_DELAY_SECONDS} seconds."
            )
            time.sleep(COMPONENT_RETRY_DELAY_SECONDS)

    write_log(f"{component_name} could not recover after {MAX_COMPONENT_ATTEMPTS} attempts.")
    write_log(f"Starting controlled self-repair for {component_name}.")

    repair_result = attempt_component_repair(
        script_path=script_path,
        component_name=component_name,
    )

    write_log(f"Self-repair result: {repair_result.action_taken}")
    write_log(repair_result.details)

    if repair_result.successful:
        write_log(f"Attempting one final execution of {component_name} after self-repair.")

        if run_component_once(script_path, component_name):
            COMPONENT_FAILURE_STATES[component_name] = False

            send_controller_notification(
                title=f"LOCKBOT {component_name} Self-Repaired",
                message=f"{component_name} recovered after controlled self-repair.",
            )

            return True

    failure_was_already_reported = COMPONENT_FAILURE_STATES.get(component_name, False)
    COMPONENT_FAILURE_STATES[component_name] = True

    if not failure_was_already_reported:
        send_controller_notification(
            title=f"URGENT: LOCKBOT {component_name} Failed",
            message=(
                f"{component_name} could not recover automatically.\n\n"
                f"Attempts: {MAX_COMPONENT_ATTEMPTS}\n"
                f"Time: {datetime.now().astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')}\n\n"
                "LOCKBOT will continue running and try again during the next controller cycle."
            ),
        )

    return False


def get_market_clock_client() -> TradingClient:
    """Build a lightweight Alpaca client for market-clock checks."""

    api_key = os.getenv(config.ALPACA_API_KEY_ENV)
    secret_key = os.getenv(config.ALPACA_SECRET_KEY_ENV)

    if not api_key or not secret_key:
        raise RuntimeError("Alpaca API keys were not found in the .env file.")

    return TradingClient(api_key, secret_key, paper=config.PAPER_TRADING)


def seconds_until_market_open_or_backoff(trading_client: TradingClient) -> int | None:
    """
    Return None when the market is open (proceed with the cycle now).

    Otherwise return how long to wait before checking again: a short
    PREMARKET_CHECK_SECONDS wait when today's open is still ahead, or
    a longer CYCLE_INTERVAL_SECONDS wait when the market is closed for
    the rest of the day (weekend/holiday), so LOCKBOT doesn't poll the
    broker every minute for hours with nothing to do. Ported from the
    now-retired auto_runner.py, which handled this the same way.
    """

    market_clock = trading_client.get_clock()

    if market_clock.is_open:
        return None

    same_day_open = (
        market_clock.next_open.date() == market_clock.timestamp.date()
    )

    if same_day_open:
        return PREMARKET_CHECK_SECONDS

    return CYCLE_INTERVAL_SECONDS


def seconds_until_next_cycle(interval_seconds: int) -> int:
    """Calculate the wait until the next aligned cycle."""

    current_epoch = int(time.time())
    remainder = current_epoch % interval_seconds

    return max(interval_seconds - remainder, 1)


def run_controller() -> None:
    """Run the autonomous LockBot controller loop."""

    validate_required_files()

    state = create_state()
    state.controller_running = True
    state.system_status = "STARTING"

    print("=" * 58)
    print(f"       LOCKBOT AUTONOMOUS CONTROLLER v{LOCKBOT_CONTROLLER_VERSION}")
    print("=" * 58)
    print(f"Cycle interval : {CYCLE_INTERVAL_SECONDS // 60} minutes")
    print(
        f"Recovery       : {MAX_COMPONENT_ATTEMPTS} attempts, "
        f"{COMPONENT_RETRY_DELAY_SECONDS}-second delay"
    )
    print(f"Project folder : {PROJECT_FOLDER}")
    print("Stop safely    : Press Ctrl+C")
    print("=" * 58)

    write_log("Controller started.")
    write_log("Starting broker reconciliation.")

    # A FAILED RECONCILIATION NO LONGER KILLS THE CONTROLLER.
    #
    # It used to raise, and on 2026-08-08 that turned a momentary DNS
    # blip into a 64-minute outage: the scheduled task started the
    # controller at 11:16:57, getaddrinfo failed for
    # paper-api.alpaca.markets, and the supervisor died on the spot with
    # nothing to restart it. DNS was fine seconds later.
    #
    # The whole design of this file is "the controller stays up
    # regardless" -- three attempts per component, then self_repair,
    # then an alert, and it keeps cycling. That resilience lived inside
    # the cycle loop while startup sat outside it, so the one step that
    # runs before the loop was the one step that could not survive a
    # network hiccup.
    #
    # So it now comes up DEGRADED instead. Reconciliation is retried on
    # later cycles, and until it succeeds new entries are blocked -- see
    # `reconciled` in the loop below. Starting without reconciling is not
    # free: it exists to catch positions that drifted while the
    # controller was down, so trading before it succeeds could act on a
    # book we have not verified. Blocking entries while still running
    # exits is the compromise LOCKBOT specified.
    reconciled = run_startup_reconciliation()

    if reconciled:
        write_log("Broker reconciliation completed successfully.")
    else:
        write_log(
            "DEGRADED: startup broker reconciliation FAILED. The controller "
            "is up and will keep running exits, but NEW ENTRIES ARE BLOCKED "
            "until reconciliation succeeds on a later cycle."
        )

        send_controller_notification(
            title="LOCKBOT Started DEGRADED",
            message=(
                "Broker reconciliation failed at startup, usually a network "
                "blip.\n\n"
                "The controller is RUNNING. Exits and stops still run.\n"
                "New entries are BLOCKED until reconciliation succeeds.\n\n"
                "It retries every cycle. No action needed unless this "
                "persists."
            ),
        )

    send_controller_notification(
        title="LOCKBOT Controller Online",
        message=(
            "LOCKBOT started successfully.\n\n"
            f"Version: {LOCKBOT_CONTROLLER_VERSION}\n"
            f"Cycle interval: {CYCLE_INTERVAL_SECONDS // 60} minutes\n"
            f"Recovery attempts: {MAX_COMPONENT_ATTEMPTS}\n"
            f"Time: {datetime.now().astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')}"
        ),
    )

    cycle_number = 0
    last_hourly_health_report_at = time.monotonic()
    last_daily_report_date = None

    while True:
        cycle_number += 1

        state.scanner_cycles = cycle_number
        state.system_status = "RUNNING"
        state.controller_running = True

        write_log(f"Cycle {cycle_number} started.")

        try:
            backoff_seconds = seconds_until_market_open_or_backoff(
                get_market_clock_client()
            )

            if backoff_seconds is not None:
                write_log(
                    "Market is closed. Skipping this cycle's component run "
                    f"and checking again in {backoff_seconds} seconds."
                )
                time.sleep(backoff_seconds)
                continue

            # Retry a reconciliation that failed at startup. Until it
            # succeeds the book has not been verified against the broker,
            # so entries stay blocked while exits keep running.
            if not reconciled:
                reconciled = run_startup_reconciliation()

                if reconciled:
                    write_log(
                        "Broker reconciliation succeeded on retry. Entries "
                        "are no longer blocked."
                    )
                    send_controller_notification(
                        title="LOCKBOT Reconciled",
                        message=("Broker reconciliation succeeded. Normal "
                                 "operation resumed; entries unblocked."),
                    )
                else:
                    write_log(
                        "Still DEGRADED: reconciliation failed again. "
                        "Entries remain blocked; exits continue."
                    )

            # Equities run exits BEFORE entries too, for the same reason
            # the options pair does below: a position already held has a
            # stronger claim on the cycle than one not yet opened.
            #
            # It matters more than it looks. This is what flattens a
            # day-horizon position before the close, and the controller
            # only wakes every SCAN_INTERVAL_SECONDS — running it after
            # the scanner would spend part of that window on a scan and
            # could push the flatten past the bell.
            equity_time_stop_ok = True

            if getattr(config, "EQUITY_TIME_STOP_ENABLED", True):
                equity_time_stop_ok = run_component_with_recovery(
                    script_path=EQUITY_TIME_STOP_FILE,
                    component_name="Equity Time Stop",
                )

            # The two ENTRY paths, gated on reconciliation. Everything
            # above and below this block is an exit, a stop or a report,
            # and those run regardless -- a book we cannot verify is a
            # reason not to add to it, never a reason to stop protecting
            # what is already open.
            scanner_ok = True

            if reconciled:
                scanner_ok = run_component_with_recovery(
                    script_path=SCANNER_FILE,
                    component_name="Market Scanner",
                    health_check=display_scanner_state,
                )
            else:
                write_log(
                    "Market Scanner SKIPPED — unreconciled book, entries "
                    "blocked."
                )

            manager_ok = run_component_with_recovery(
                script_path=TRADE_MANAGER_FILE,
                component_name="Trade Manager",
            )

            position_monitor_ok = run_component_with_recovery(
                script_path=POSITION_MONITOR_FILE,
                component_name="Position Monitor",
            )

            # Options run exits BEFORE entries. Alpaca offers no bracket
            # order for options, so Options Manager is the only stop loss
            # those positions have — it must get its cycle before any new
            # premium is committed.
            options_manager_ok = True
            options_scanner_ok = True

            if getattr(config, "OPTIONS_ENABLED", False):
                options_manager_ok = run_component_with_recovery(
                    script_path=OPTIONS_MANAGER_FILE,
                    component_name="Options Manager",
                )

                # The other entry path. Options Manager above is the
                # only stop loss a contract has and runs regardless;
                # this one commits new premium and does not.
                if reconciled:
                    options_scanner_ok = run_component_with_recovery(
                        script_path=OPTIONS_SCANNER_FILE,
                        component_name="Options Scanner",
                    )
                else:
                    write_log(
                        "Options Scanner SKIPPED — unreconciled book, "
                        "entries blocked."
                    )

            health_ok = run_component_with_recovery(
                script_path=HEALTH_MONITOR_FILE,
                component_name="Health Monitor",
            )

            cycle_healthy = (
                scanner_ok
                and manager_ok
                and position_monitor_ok
                and equity_time_stop_ok
                and options_manager_ok
                and options_scanner_ok
                and health_ok
            )

            current_monotonic_time = time.monotonic()
            current_local_time = datetime.now().astimezone()
            current_local_date = current_local_time.date()

            scanner_snapshot = load_state() or {}
            symbol_snapshot = scanner_snapshot.get("symbols", {})

            active_positions = sum(
                1
                for symbol_data in symbol_snapshot.values()
                if symbol_data.get("position_size", 0) != 0
            )

            if (
                current_monotonic_time - last_hourly_health_report_at
                >= HOURLY_HEALTH_REPORT_SECONDS
            ):
                hourly_status = "HEALTHY" if cycle_healthy else "ERROR"

                send_controller_notification(
                    title="LOCKBOT Hourly Health Report",
                    message=(
                        f"Overall Status: {hourly_status}\n\n"
                        f"Market Scanner: {'HEALTHY' if scanner_ok else 'ERROR'}\n"
                        f"Trade Manager: {'HEALTHY' if manager_ok else 'ERROR'}\n"
                        f"Position Monitor: {'HEALTHY' if position_monitor_ok else 'ERROR'}\n"
                        f"Options Manager: {'HEALTHY' if options_manager_ok else 'ERROR'}\n"
                        f"Options Scanner: {'HEALTHY' if options_scanner_ok else 'ERROR'}\n"
                        f"Health Monitor: {'HEALTHY' if health_ok else 'ERROR'}\n\n"
                        f"Market: {'OPEN' if scanner_snapshot.get('market_open', False) else 'CLOSED'}\n"
                        f"Equity: ${scanner_snapshot.get('account_equity', 0.0):,.2f}\n"
                        f"Daily P&L: ${scanner_snapshot.get('daily_pnl', 0.0):,.2f}\n"
                        f"Active Positions: {active_positions}\n"
                        f"Cycle: {cycle_number}\n"
                        f"Time: {datetime.now().astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')}"
                    ),
                )

                write_log("Hourly health report sent successfully.")
                last_hourly_health_report_at = current_monotonic_time

            daily_report_time_reached = (
                current_local_time.hour > DAILY_REPORT_HOUR
                or (
                    current_local_time.hour == DAILY_REPORT_HOUR
                    and current_local_time.minute >= DAILY_REPORT_MINUTE
                )
            )

            if daily_report_time_reached and last_daily_report_date != current_local_date:
                write_log("Daily report time reached. Generating LOCKBOT daily report.")

                daily_report_sent = send_daily_report(
                    report_date=current_local_date,
                    force=True,
                )

                if daily_report_sent:
                    write_log("Daily report sent successfully.")
                    last_daily_report_date = current_local_date
                else:
                    write_log("Daily report was not sent. LOCKBOT will retry during the next cycle.")

            if cycle_healthy:
                state.system_status = "HEALTHY"
                state.last_scan_time = datetime.now().astimezone()

                wait_seconds = seconds_until_next_cycle(CYCLE_INTERVAL_SECONDS)

                write_log(
                    f"Cycle {cycle_number} complete. System status: {state.system_status}. "
                    f"Next cycle in approximately {wait_seconds} seconds."
                )

            else:
                state.system_status = "ERROR"
                wait_seconds = CYCLE_ERROR_RETRY_DELAY_SECONDS

                write_log(
                    f"Cycle {cycle_number} had an error. Market Scanner: {scanner_ok}. "
                    f"Trade Manager: {manager_ok}. Position Monitor: {position_monitor_ok}. "
                    f"Options Manager: {options_manager_ok}. "
                    f"Options Scanner: {options_scanner_ok}. "
                    f"Health Monitor: {health_ok}. Starting another cycle in {wait_seconds} seconds."
                )

        except Exception as cycle_error:
            state.system_status = "ERROR"
            wait_seconds = CYCLE_ERROR_RETRY_DELAY_SECONDS

            error_type = type(cycle_error).__name__
            error_message = str(cycle_error) or "No error details were provided."

            write_log(
                f"Cycle {cycle_number} crashed unexpectedly: {error_type}: {error_message}. "
                f"Controller remains online and will retry in {wait_seconds} seconds."
            )

            send_controller_notification(
                title="LOCKBOT Cycle Recovery Activated",
                message=(
                    f"Controller cycle {cycle_number} encountered an unexpected error.\n\n"
                    f"Error type: {error_type}\n"
                    f"Details: {error_message}\n"
                    f"Retry delay: {wait_seconds} seconds\n"
                    f"Time: {datetime.now().astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')}\n\n"
                    "The controller is still running."
                ),
            )

        time.sleep(wait_seconds)


if __name__ == "__main__":
    try:
        if not acquire_controller_mutex():
            print()
            print("=" * 58)
            print("LOCKBOT CONTROLLER START BLOCKED")
            print("=" * 58)
            print("Another LOCKBOT controller is already running.")
            print("This duplicate instance will now close safely.")
            print("=" * 58)

            sys.exit(0)

        run_controller()

    except KeyboardInterrupt:
        write_log("Controller stopped safely by the user.")
        print()
        print("LockBot controller stopped.")

    except Exception as error:
        error_type = type(error).__name__
        error_message = str(error) or "No error details were provided."
        crash_time = datetime.now().astimezone()

        write_log(f"Controller stopped unexpectedly: {error_type}: {error_message}")

        send_controller_notification(
            title="URGENT: LOCKBOT Controller Crashed",
            message=(
                "LOCKBOT stopped unexpectedly.\n\n"
                f"Time: {crash_time.strftime('%Y-%m-%d %I:%M:%S %p %Z')}\n"
                f"Error type: {error_type}\n"
                f"Details: {error_message}\n\n"
                "Check the computer and lockbot_controller.log."
            ),
        )

        raise

    finally:
        release_controller_mutex()
