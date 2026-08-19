"""
LOCKBOT Health Monitor v0.3

Central diagnostics dashboard for LOCKBOT.

Checks:
- Alpaca API credentials
- Broker connection
- Account status
- Market clock
- Buying power and cash
- Module heartbeat status
- Heartbeat freshness
- Module runtime
- Overall LOCKBOT system health

This module does not submit, modify, replace,
or cancel brokerage orders.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from alpaca.trading.client import TradingClient
from dotenv import load_dotenv
from retry_utils import with_retries

from system_heartbeat import load_heartbeat_state


load_dotenv()


HEALTH_MONITOR_VERSION = "0.3"

HEARTBEAT_WARNING_MINUTES = 10
HEARTBEAT_CRITICAL_MINUTES = 20


@dataclass
class BrokerHealth:
    """Stores the result of the Alpaca broker health check."""

    api_keys: bool
    alpaca: bool
    market_clock: bool
    buying_power: str
    cash: str
    account_status: str
    market_open: str
    error: str


@dataclass
class ModuleHealth:
    """Stores the evaluated health of one LOCKBOT module."""

    module_name: str
    recorded_status: str
    evaluated_status: str
    age_minutes: float | None
    runtime_seconds: float | None
    message: str
    error: str
    reason: str


def check_alpaca() -> BrokerHealth:
    """Check Alpaca credentials, account access, and market clock."""

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    api_keys_ok = bool(api_key and secret_key)

    if not api_keys_ok:
        return BrokerHealth(
            api_keys=False,
            alpaca=False,
            market_clock=False,
            buying_power="N/A",
            cash="N/A",
            account_status="UNKNOWN",
            market_open="UNKNOWN",
            error="Alpaca API credentials are missing.",
        )

    try:
        client = TradingClient(api_key, secret_key, paper=True)

        account = with_retries(client.get_account)()
        clock = with_retries(client.get_clock)()

        account_status = getattr(account.status, "value", str(account.status))

        return BrokerHealth(
            api_keys=True,
            alpaca=True,
            market_clock=True,
            buying_power=str(account.buying_power),
            cash=str(account.cash),
            account_status=str(account_status).upper(),
            market_open="YES" if clock.is_open else "NO",
            error="",
        )

    except Exception as error:
        return BrokerHealth(
            api_keys=True,
            alpaca=False,
            market_clock=False,
            buying_power="N/A",
            cash="N/A",
            account_status="UNKNOWN",
            market_open="UNKNOWN",
            error=f"{type(error).__name__}: {error}",
        )


def parse_timestamp(timestamp: str | None) -> datetime | None:
    """Convert an ISO timestamp into a UTC-aware datetime."""

    if not timestamp:
        return None

    try:
        parsed = datetime.fromisoformat(timestamp)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (TypeError, ValueError):
        return None


def minutes_since(timestamp: str | None) -> float | None:
    """Return the number of minutes since an ISO timestamp."""

    parsed = parse_timestamp(timestamp)

    if parsed is None:
        return None

    elapsed = datetime.now(timezone.utc) - parsed

    return max(elapsed.total_seconds() / 60, 0)


def calculate_runtime_seconds(module_data: dict[str, Any]) -> float | None:
    """Calculate module runtime from its saved timestamps."""

    started_at = parse_timestamp(module_data.get("started_at_utc"))

    if started_at is None:
        return None

    recorded_status = str(module_data.get("status", "UNKNOWN")).upper()

    if recorded_status == "STOPPED":
        ending_time = parse_timestamp(module_data.get("last_heartbeat_at_utc"))
    else:
        ending_time = datetime.now(timezone.utc)

    if ending_time is None:
        return None

    runtime = (ending_time - started_at).total_seconds()

    return max(runtime, 0)


def is_expected_shutdown(module_data: dict[str, Any], market_open: str) -> bool:
    """
    Determine whether a STOPPED module represents
    a normal and intentional shutdown.
    """

    message = str(module_data.get("message", "")).lower()

    expected_phrases = (
        "market closed",
        "stopped for the day",
        "normal shutdown",
        "intentional shutdown",
        "completed successfully",
        "shutdown complete",
    )

    if any(phrase in message for phrase in expected_phrases):
        return True

    if market_open == "NO":
        return True

    return False


def evaluate_module(
    module_name: str,
    module_data: dict[str, Any],
    market_open: str,
) -> ModuleHealth:
    """Evaluate one saved module heartbeat record."""

    recorded_status = str(module_data.get("status", "UNKNOWN")).upper()
    message = str(module_data.get("message", "")).strip()
    error = str(module_data.get("error", "")).strip()

    age_minutes = minutes_since(module_data.get("last_heartbeat_at_utc"))
    runtime_seconds = calculate_runtime_seconds(module_data)

    if recorded_status == "CRITICAL":
        return ModuleHealth(
            module_name=module_name,
            recorded_status=recorded_status,
            evaluated_status="CRITICAL",
            age_minutes=age_minutes,
            runtime_seconds=runtime_seconds,
            message=message,
            error=error,
            reason="Module reported a critical condition.",
        )

    if recorded_status == "STOPPED":
        if is_expected_shutdown(module_data, market_open):
            return ModuleHealth(
                module_name=module_name,
                recorded_status=recorded_status,
                evaluated_status="STOPPED",
                age_minutes=age_minutes,
                runtime_seconds=runtime_seconds,
                message=message,
                error=error,
                reason="Module completed an expected shutdown.",
            )

        return ModuleHealth(
            module_name=module_name,
            recorded_status=recorded_status,
            evaluated_status="CRITICAL",
            age_minutes=age_minutes,
            runtime_seconds=runtime_seconds,
            message=message,
            error=error,
            reason=(
                "Module is stopped while the market "
                "is open or no expected shutdown reason exists."
            ),
        )

    if age_minutes is None:
        return ModuleHealth(
            module_name=module_name,
            recorded_status=recorded_status,
            evaluated_status="CRITICAL",
            age_minutes=None,
            runtime_seconds=runtime_seconds,
            message=message,
            error=error,
            reason="Heartbeat timestamp is missing or invalid.",
        )

    # A stale heartbeat only means something while the market is open.
    #
    # The controller runs its components on the market clock, so between
    # sessions every module is silent by design. Judging that silence
    # against a fixed 20-minute threshold made LOCKBOT report CRITICAL
    # every night and all weekend -- 5 of 5 modules critical on a Sunday
    # with "Market is closed. No options scan performed." as the last
    # message from each one.
    #
    # That is not a harmless cosmetic bug. A stale OPTIONS_MANAGER
    # heartbeat during market hours means open option positions have no
    # stop loss, which is the single most serious alert this system can
    # raise. An alarm that fires every weekend is one nobody reads on the
    # Tuesday it is real, so silencing the false case protects the true
    # one. Note this branch sits AFTER the recorded-CRITICAL check above:
    # a module that genuinely failed before the close still reports
    # CRITICAL, closed market or not.
    if market_open == "NO":
        return ModuleHealth(
            module_name=module_name,
            recorded_status=recorded_status,
            evaluated_status="IDLE",
            age_minutes=age_minutes,
            runtime_seconds=runtime_seconds,
            message=message,
            error=error,
            reason=(
                "Market is closed; the module is idle between sessions "
                f"({format_age(age_minutes)} since its last run)."
            ),
        )

    if age_minutes >= HEARTBEAT_CRITICAL_MINUTES:
        return ModuleHealth(
            module_name=module_name,
            recorded_status=recorded_status,
            evaluated_status="CRITICAL",
            age_minutes=age_minutes,
            runtime_seconds=runtime_seconds,
            message=message,
            error=error,
            reason=f"No heartbeat received for {age_minutes:.1f} minutes.",
        )

    if recorded_status == "DEGRADED":
        return ModuleHealth(
            module_name=module_name,
            recorded_status=recorded_status,
            evaluated_status="DEGRADED",
            age_minutes=age_minutes,
            runtime_seconds=runtime_seconds,
            message=message,
            error=error,
            reason="Module reported degraded operation.",
        )

    if recorded_status == "STARTING":
        return ModuleHealth(
            module_name=module_name,
            recorded_status=recorded_status,
            evaluated_status="STARTING",
            age_minutes=age_minutes,
            runtime_seconds=runtime_seconds,
            message=message,
            error=error,
            reason="Module is currently starting.",
        )

    if age_minutes >= HEARTBEAT_WARNING_MINUTES:
        return ModuleHealth(
            module_name=module_name,
            recorded_status=recorded_status,
            evaluated_status="DEGRADED",
            age_minutes=age_minutes,
            runtime_seconds=runtime_seconds,
            message=message,
            error=error,
            reason=f"Heartbeat is {age_minutes:.1f} minutes old.",
        )

    if recorded_status == "HEALTHY":
        return ModuleHealth(
            module_name=module_name,
            recorded_status=recorded_status,
            evaluated_status="HEALTHY",
            age_minutes=age_minutes,
            runtime_seconds=runtime_seconds,
            message=message,
            error=error,
            reason=f"Heartbeat received {age_minutes:.1f} minutes ago.",
        )

    return ModuleHealth(
        module_name=module_name,
        recorded_status=recorded_status,
        evaluated_status="DEGRADED",
        age_minutes=age_minutes,
        runtime_seconds=runtime_seconds,
        message=message,
        error=error,
        reason=f"Unknown recorded status: {recorded_status}.",
    )


def format_money(value: str) -> str:
    """Format a numeric account value as currency."""

    try:
        return f"${float(value):,.2f}"

    except (TypeError, ValueError):
        return "N/A"


def format_age(age_minutes: float | None) -> str:
    """Format heartbeat age for compact display."""

    if age_minutes is None:
        return "--"

    if age_minutes < 1:
        return "<1m"

    if age_minutes < 60:
        return f"{age_minutes:.1f}m"

    hours = age_minutes / 60

    if hours < 24:
        return f"{hours:.1f}h"

    days = hours / 24

    return f"{days:.1f}d"


def format_runtime(runtime_seconds: float | None) -> str:
    """Format module runtime as a readable duration."""

    if runtime_seconds is None:
        return "--"

    total_seconds = int(runtime_seconds)

    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)

    if days:
        return f"{days}d {hours}h {minutes}m"

    if hours:
        return f"{hours}h {minutes}m"

    if minutes:
        return f"{minutes}m {seconds}s"

    return f"{seconds}s"


def compact_message(message: str, max_length: int = 42) -> str:
    """Shorten a module message for table display."""

    cleaned = " ".join(message.split())

    if not cleaned:
        return "--"

    if len(cleaned) <= max_length:
        return cleaned

    return cleaned[: max_length - 3] + "..."


def display_status(status: str) -> str:
    """Return the user-facing module status label."""

    labels = {
        "HEALTHY": "PASS",
        "STARTING": "START",
        "DEGRADED": "WARN",
        "CRITICAL": "FAIL",
        "STOPPED": "STOPPED",
        "IDLE": "IDLE",
    }

    return labels.get(status, status)


def count_module_statuses(module_results: list[ModuleHealth]) -> dict[str, int]:
    """Count evaluated module statuses."""

    counts = {
        "HEALTHY": 0,
        "STARTING": 0,
        "DEGRADED": 0,
        "CRITICAL": 0,
        "STOPPED": 0,
        "IDLE": 0,
    }

    for result in module_results:
        status = result.evaluated_status
        counts[status] = counts.get(status, 0) + 1

    return counts


def determine_overall_status(
    broker: BrokerHealth,
    module_results: list[ModuleHealth],
) -> str:
    """Determine the full LOCKBOT system status."""

    if not broker.api_keys:
        return "CRITICAL"

    if not broker.alpaca:
        return "CRITICAL"

    if not broker.market_clock:
        return "CRITICAL"

    evaluated_statuses = {result.evaluated_status for result in module_results}

    if "CRITICAL" in evaluated_statuses:
        return "CRITICAL"

    if "DEGRADED" in evaluated_statuses:
        return "DEGRADED"

    if "STARTING" in evaluated_statuses:
        return "STARTING"

    return "HEALTHY"


def print_broker_section(broker: BrokerHealth) -> None:
    """Print the broker connectivity section."""

    print("BROKER CONNECTION")
    print("-" * 78)
    print(f"API Keys              : {'PASS' if broker.api_keys else 'FAIL'}")
    print(f"Alpaca Login          : {'PASS' if broker.alpaca else 'FAIL'}")
    print(f"Market Clock          : {'PASS' if broker.market_clock else 'FAIL'}")
    print(f"Account Status        : {broker.account_status}")
    print(f"Market Open           : {broker.market_open}")
    print(f"Buying Power          : {format_money(broker.buying_power)}")
    print(f"Cash                  : {format_money(broker.cash)}")

    if broker.error:
        print(f"Broker Error          : {broker.error}")


def print_module_table(module_results: list[ModuleHealth]) -> None:
    """Print a compact summary table for all modules."""

    print("MODULE HEARTBEATS")
    print("-" * 78)

    if not module_results:
        print("No module heartbeats have been recorded.")
        return

    print(f"{'MODULE':<23}{'STATUS':<10}{'AGE':<9}{'RUNTIME':<12}MESSAGE")
    print("-" * 78)

    for result in module_results:
        print(
            f"{result.module_name:<23}"
            f"{display_status(result.evaluated_status):<10}"
            f"{format_age(result.age_minutes):<9}"
            f"{format_runtime(result.runtime_seconds):<12}"
            f"{compact_message(result.message)}"
        )


def print_module_details(module_results: list[ModuleHealth]) -> None:
    """Print detailed diagnostic information per module."""

    print("MODULE DETAILS")
    print("-" * 78)

    if not module_results:
        print("No detailed module information is available.")
        return

    for result in module_results:
        print(f"{result.module_name}")
        print(f"  Recorded status     : {result.recorded_status}")
        print(f"  Health assessment   : {result.evaluated_status}")
        print(f"  Heartbeat age       : {format_age(result.age_minutes)}")
        print(f"  Runtime             : {format_runtime(result.runtime_seconds)}")
        print(f"  Reason              : {result.reason}")

        if result.message:
            print(f"  Latest message      : {result.message}")

        if result.error:
            print(f"  Latest error        : {result.error}")

        print("-" * 78)


def print_system_assessment(
    broker: BrokerHealth,
    module_results: list[ModuleHealth],
) -> None:
    """Print aggregate LOCKBOT system health information."""

    counts = count_module_statuses(module_results)
    overall_status = determine_overall_status(broker, module_results)

    print("SYSTEM ASSESSMENT")
    print("-" * 78)
    print(f"Total Modules         : {len(module_results)}")
    print(f"Healthy               : {counts['HEALTHY']}")
    print(f"Starting              : {counts['STARTING']}")
    print(f"Degraded              : {counts['DEGRADED']}")
    print(f"Critical              : {counts['CRITICAL']}")
    print(f"Stopped               : {counts['STOPPED']}")
    print(f"Idle (market closed)  : {counts['IDLE']}")
    print(f"Heartbeat Warning     : {HEARTBEAT_WARNING_MINUTES} minutes")
    print(f"Heartbeat Critical    : {HEARTBEAT_CRITICAL_MINUTES} minutes")
    print(f"Overall Status        : {overall_status}")


def run_config_sweep() -> None:
    """Once a day, check that no setting configures nothing.

    Lives here rather than in its own scheduled module on LOCKBOT's ruling
    of 2026-08-19 (channel item d8372ec3), and the reasoning is the point:
    an unscheduled checker is the exact defect class it checks for, so it
    must attach to something whose execution there is continuous evidence
    of. health_monitor beats on every controller cycle. A new module or a
    second scheduled task would be one more thing that can silently never
    run -- which is how watchdog.py went unscheduled since the project
    began, and why nothing reported the nine-hour outage of 08-15/16.

    Gated on date change rather than a timer, because the failure is
    introduced at edit time and costs nothing per hour undetected. Reports
    only; deletion stays a human act.

    Never raises. A reporting tool must not be able to take down the
    module that reports on everything else.
    """

    try:
        import config_sweep
    except Exception as error:                       # noqa: BLE001
        print(f"\nConfig sweep unavailable: {type(error).__name__}: {error}")
        return

    try:
        if not config_sweep.should_run_today():
            return

        result = config_sweep.sweep()
        config_sweep.record_run()

        print()
        config_sweep.report(result)

        if result.clean:
            return

        from system_heartbeat import mark_module_degraded

        mark_module_degraded(
            "CONFIG_SWEEP",
            message=result.summary(),
            error="",
        )

        from notifications import send_smart_notification

        # Keyed on the finding itself, so a standing orphan alerts once
        # rather than every day until somebody deletes it.
        send_smart_notification(
            symbol="CONFIG",
            event_type="CONFIG_SWEEP",
            title="LOCKBOT: config that configures nothing",
            message=(
                f"{result.summary()}\n\n"
                "These read as settings and control nothing. Reported, not "
                "deleted -- deletion is a human act."
            ),
            reason=", ".join(result.orphans + result.unreasoned),
            cooldown_minutes=1440,
        )
    except Exception as error:                       # noqa: BLE001
        print(f"\nConfig sweep failed: {type(error).__name__}: {error}")


def main() -> None:
    """Run the complete LOCKBOT health inspection."""

    broker = check_alpaca()
    heartbeat_state = load_heartbeat_state()
    saved_modules = heartbeat_state.get("modules", {})

    module_results: list[ModuleHealth] = []

    for module_name in sorted(saved_modules):
        module_data = saved_modules[module_name]
        result = evaluate_module(
            module_name=module_name,
            module_data=module_data,
            market_open=broker.market_open,
        )
        module_results.append(result)

    print("=" * 78)
    print(f"                LOCKBOT HEALTH MONITOR v{HEALTH_MONITOR_VERSION}")
    print("=" * 78)

    print_broker_section(broker)
    print()
    print_module_table(module_results)
    print()
    print_module_details(module_results)
    print()
    print_system_assessment(broker, module_results)
    print("=" * 78)

    run_config_sweep()


def _self_test() -> int:
    """Offline checks for the heartbeat verdict logic.

    This module decides whether LOCKBOT alerts. The case that matters
    most is a stale OPTIONS_MANAGER heartbeat during market hours, which
    means open option positions have no stop loss -- these checks exist
    so that silencing the weekend false alarm can never silence that.
    """

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    now = datetime.now(timezone.utc)

    def heartbeat(minutes_old: float, status: str = "HEALTHY", message: str = "ok"):
        stamp = now - timedelta(minutes=minutes_old)
        return {
            "status": status,
            "message": message,
            "last_heartbeat_at_utc": stamp.isoformat(),
        }

    print("Market open -- staleness still alerts")

    stale_open = evaluate_module(
        "OPTIONS_MANAGER", heartbeat(3192.0), market_open="YES"
    )
    check(
        "a 2-day-old heartbeat is CRITICAL while the market is open",
        stale_open.evaluated_status == "CRITICAL",
        stale_open.evaluated_status,
    )

    warn_open = evaluate_module(
        "OPTIONS_MANAGER", heartbeat(12.0), market_open="YES"
    )
    check(
        "a 12-minute-old heartbeat is DEGRADED while open",
        warn_open.evaluated_status == "DEGRADED",
        warn_open.evaluated_status,
    )

    fresh_open = evaluate_module(
        "OPTIONS_MANAGER", heartbeat(2.0), market_open="YES"
    )
    check(
        "a fresh heartbeat is HEALTHY while open",
        fresh_open.evaluated_status == "HEALTHY",
        fresh_open.evaluated_status,
    )

    print()
    print("Market closed -- silence is expected")

    stale_closed = evaluate_module(
        "OPTIONS_MANAGER",
        heartbeat(3192.0, message="Market is closed. No options scan performed."),
        market_open="NO",
    )
    check(
        "the same 2-day-old heartbeat is IDLE while closed",
        stale_closed.evaluated_status == "IDLE",
        stale_closed.evaluated_status,
    )
    check(
        "IDLE does not drag the system to CRITICAL",
        determine_overall_status(
            BrokerHealth(
                api_keys=True,
                alpaca=True,
                market_clock=True,
                buying_power="139.19",
                cash="139.19",
                account_status="ACTIVE",
                market_open="NO",
                error="",
            ),
            [stale_closed],
        ) != "CRITICAL",
    )

    print()
    print("A real failure is never silenced by the market being closed")

    broken_closed = evaluate_module(
        "OPTIONS_MANAGER",
        heartbeat(30.0, status="CRITICAL", message="Exit engine crashed."),
        market_open="NO",
    )
    check(
        "a module that reported CRITICAL stays CRITICAL when closed",
        broken_closed.evaluated_status == "CRITICAL",
        broken_closed.evaluated_status,
    )

    missing_stamp = evaluate_module(
        "OPTIONS_MANAGER",
        {"status": "HEALTHY", "message": "ok", "last_heartbeat_at_utc": None},
        market_open="NO",
    )
    check(
        "a missing heartbeat timestamp stays CRITICAL when closed",
        missing_stamp.evaluated_status == "CRITICAL",
        missing_stamp.evaluated_status,
    )

    counts = count_module_statuses([stale_closed, fresh_open, broken_closed])
    check("IDLE is counted", counts["IDLE"] == 1, str(counts))
    check("CRITICAL is still counted", counts["CRITICAL"] == 1, str(counts))

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All health-monitor checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    main()