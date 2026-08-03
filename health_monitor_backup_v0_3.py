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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

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

    api_keys_ok = bool(
        api_key
        and secret_key
    )

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
        client = TradingClient(
            api_key,
            secret_key,
            paper=True,
        )

        account = client.get_account()
        clock = client.get_clock()

        account_status = getattr(
            account.status,
            "value",
            str(account.status),
        )

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


def parse_timestamp(
    timestamp: str | None,
) -> datetime | None:
    """Convert an ISO timestamp into a UTC-aware datetime."""

    if not timestamp:
        return None

    try:
        parsed = datetime.fromisoformat(
            timestamp
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc,
            )

        return parsed.astimezone(
            timezone.utc
        )

    except (
        TypeError,
        ValueError,
    ):
        return None


def minutes_since(
    timestamp: str | None,
) -> float | None:
    """Return the number of minutes since an ISO timestamp."""

    parsed = parse_timestamp(
        timestamp
    )

    if parsed is None:
        return None

    elapsed = (
        datetime.now(timezone.utc)
        - parsed
    )

    return max(
        elapsed.total_seconds() / 60,
        0,
    )


def calculate_runtime_seconds(
    module_data: dict[str, Any],
) -> float | None:
    """Calculate module runtime from its saved timestamps."""

    started_at = parse_timestamp(
        module_data.get(
            "started_at_utc"
        )
    )

    if started_at is None:
        return None

    recorded_status = str(
        module_data.get(
            "status",
            "UNKNOWN",
        )
    ).upper()

    if recorded_status == "STOPPED":
        ending_time = parse_timestamp(
            module_data.get(
                "last_heartbeat_at_utc"
            )
        )
    else:
        ending_time = datetime.now(
            timezone.utc
        )

    if ending_time is None:
        return None

    runtime = (
        ending_time
        - started_at
    ).total_seconds()

    return max(
        runtime,
        0,
    )


def is_expected_shutdown(
    module_data: dict[str, Any],
    market_open: str,
) -> bool:
    """
    Determine whether a STOPPED module represents
    a normal and intentional shutdown.
    """

    message = str(
        module_data.get(
            "message",
            "",
        )
    ).lower()

    expected_phrases = (
        "market closed",
        "stopped for the day",
        "normal shutdown",
        "intentional shutdown",
        "completed successfully",
        "shutdown complete",
    )

    if any(
        phrase in message
        for phrase in expected_phrases
    ):
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

    recorded_status = str(
        module_data.get(
            "status",
            "UNKNOWN",
        )
    ).upper()

    message = str(
        module_data.get(
            "message",
            "",
        )
    ).strip()

    error = str(
        module_data.get(
            "error",
            "",
        )
    ).strip()

    age_minutes = minutes_since(
        module_data.get(
            "last_heartbeat_at_utc"
        )
    )

    runtime_seconds = calculate_runtime_seconds(
        module_data
    )

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
        if is_expected_shutdown(
            module_data,
            market_open,
        ):
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

    if age_minutes >= HEARTBEAT_CRITICAL_MINUTES:
        return ModuleHealth(
            module_name=module_name,
            recorded_status=recorded_status,
            evaluated_status="CRITICAL",
            age_minutes=age_minutes,
            runtime_seconds=runtime_seconds,
            message=message,
            error=error,
            reason=(
                "No heartbeat received for "
                f"{age_minutes:.1f} minutes."
            ),
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
            reason=(
                "Heartbeat is "
                f"{age_minutes:.1f} minutes old."
            ),
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
            reason=(
                "Heartbeat received "
                f"{age_minutes:.1f} minutes ago."
            ),
        )

    return ModuleHealth(
        module_name=module_name,
        recorded_status=recorded_status,
        evaluated_status="DEGRADED",
        age_minutes=age_minutes,
        runtime_seconds=runtime_seconds,
        message=message,
        error=error,
        reason=(
            "Unknown recorded status: "
            f"{recorded_status}."
        ),
    )


def format_money(
    value: str,
) -> str:
    """Format a numeric account value as currency."""

    try:
        return f"${float(value):,.2f}"

    except (
        TypeError,
        ValueError,
    ):
        return "N/A"


def format_age(
    age_minutes: float | None,
) -> str:
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


def format_runtime(
    runtime_seconds: float | None,
) -> str:
    """Format module runtime as a readable duration."""

    if runtime_seconds is None:
        return "--"

    total_seconds = int(
        runtime_seconds
    )

    days, remainder = divmod(
        total_seconds,
        86_400,
    )

    hours, remainder = divmod(
        remainder,
        3_600,
    )

    minutes, seconds = divmod(
        remainder,
        60,
    )

    if days:
        return (
            f"{days}d "
            f"{hours}h "
            f"{minutes}m"
        )

    if hours:
        return (
            f"{hours}h "
            f"{minutes}m"
        )

    if minutes:
        return (
            f"{minutes}m "
            f"{seconds}s"
        )

    return f"{seconds}s"


def compact_message(
    message: str,
    max_length: int = 42,
) -> str:
    """Shorten a module message for table display."""

    cleaned = " ".join(
        message.split()
    )

    if not cleaned:
        return "--"

    if len(cleaned) <= max_length:
        return cleaned

    return (
        cleaned[: max_length - 3]
        + "..."
    )


def display_status(
    status: str,
) -> str:
    """Return the user-facing module status label."""

    labels = {
        "HEALTHY": "PASS",
        "STARTING": "START",
        "DEGRADED": "WARN",
        "CRITICAL": "FAIL",
        "STOPPED": "STOPPED",
    }

    return labels.get(
        status,
        status,
    )


def count_module_statuses(
    module_results: list[ModuleHealth],
) -> dict[str, int]:
    """Count evaluated module statuses."""

    counts = {
        "HEALTHY": 0,
        "STARTING": 0,
        "DEGRADED": 0,
        "CRITICAL": 0,
        "STOPPED": 0,
    }

    for result in module_results:
        status = result.evaluated_status

        counts[status] = (
            counts.get(
                status,
                0,
            )
            + 1
        )

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

    evaluated_statuses = {
        result.evaluated_status
        for result in module_results
    }

    if "CRITICAL" in evaluated_statuses:
        return "CRITICAL"

    if "DEGRADED" in evaluated_statuses:
        return "DEGRADED"

    if "STARTING" in evaluated_statuses:
        return "STARTING"

    return "HEALTHY"


def print_broker_section(
    broker: BrokerHealth,
) -> None:
    """Print the broker connectivity section."""

    print("BROKER CONNECTION")
    print("-" * 78)

    print(
        f"API Keys              : "
        f"{'PASS' if broker.api_keys else 'FAIL'}"
    )

    print(
        f"Alpaca Login          : "
        f"{'PASS' if broker.alpaca else 'FAIL'}"
    )

    print(
        f"Market Clock          : "
        f"{'PASS' if broker.market_clock else 'FAIL'}"
    )

    print(
        f"Account Status        : "
        f"{broker.account_status}"
    )

    print(
        f"Market Open           : "
        f"{broker.market_open}"
    )

    print(
        f"Buying Power          : "
        f"{format_money(broker.buying_power)}"
    )

    print(
        f"Cash                  : "
        f"{format_money(broker.cash)}"
    )

    if broker.error:
        print(
            f"Broker Error          : "
            f"{broker.error}"
        )


def print_module_table(
    module_results: list[ModuleHealth],
) -> None:
    """Print a compact summary table for all modules."""

    print("MODULE HEARTBEATS")
    print("-" * 78)

    if not module_results:
        print(
            "No module heartbeats have been recorded."
        )
        return

    print(
        f"{'MODULE':<23}"
        f"{'STATUS':<10}"
        f"{'AGE':<9}"
        f"{'RUNTIME':<12}"
        f"MESSAGE"
    )

    print("-" * 78)

    for result in module_results:
        print(
            f"{result.module_name:<23}"
            f"{display_status(result.evaluated_status):<10}"
            f"{format_age(result.age_minutes):<9}"
            f"{format_runtime(result.runtime_seconds):<12}"
            f"{compact_message(result.message)}"
        )


def print_module_details(
    module_results: list[ModuleHealth],
) -> None:
    """Print detailed diagnostic information per module."""

    print("MODULE DETAILS")
    print("-" * 78)

    if not module_results:
        print(
            "No detailed module information is available."
        )
        return

    for result in module_results:
        print(
            f"{result.module_name}"
        )

        print(
            f"  Recorded status     : "
            f"{result.recorded_status}"
        )

        print(
            f"  Health assessment   : "
            f"{result.evaluated_status}"
        )

        print(
            f"  Heartbeat age       : "
            f"{format_age(result.age_minutes)}"
        )

        print(
            f"  Runtime             : "
            f"{format_runtime(result.runtime_seconds)}"
        )

        print(
            f"  Reason              : "
            f"{result.reason}"
        )

        if result.message:
            print(
                f"  Latest message      : "
                f"{result.message}"
            )

        if result.error:
            print(
                f"  Latest error        : "
                f"{result.error}"
            )

        print("-" * 78)


def print_system_assessment(
    broker: BrokerHealth,
    module_results: list[ModuleHealth],
) -> None:
    """Print aggregate LOCKBOT system health information."""

    counts = count_module_statuses(
        module_results
    )

    overall_status = determine_overall_status(
        broker,
        module_results,
    )

    print("SYSTEM ASSESSMENT")
    print("-" * 78)

    print(
        f"Total Modules         : "
        f"{len(module_results)}"
    )

    print(
        f"Healthy               : "
        f"{counts['HEALTHY']}"
    )

    print(
        f"Starting              : "
        f"{counts['STARTING']}"
    )

    print(
        f"Degraded              : "
        f"{counts['DEGRADED']}"
    )

    print(
        f"Critical              : "
        f"{counts['CRITICAL']}"
    )

    print(
        f"Stopped               : "
        f"{counts['STOPPED']}"
    )

    print(
        f"Heartbeat Warning     : "
        f"{HEARTBEAT_WARNING_MINUTES} minutes"
    )

    print(
        f"Heartbeat Critical    : "
        f"{HEARTBEAT_CRITICAL_MINUTES} minutes"
    )

    print(
        f"Overall Status        : "
        f"{overall_status}"
    )


def main() -> None:
    """Run the complete LOCKBOT health inspection."""

    broker = check_alpaca()
    heartbeat_state = load_heartbeat_state()

    saved_modules = heartbeat_state.get(
        "modules",
        {},
    )

    module_results: list[ModuleHealth] = []

    for module_name in sorted(
        saved_modules
    ):
        module_data = saved_modules[
            module_name
        ]

        result = evaluate_module(
            module_name=module_name,
            module_data=module_data,
            market_open=broker.market_open,
        )

        module_results.append(
            result
        )

    print("=" * 78)
    print(
        f"                "
        f"LOCKBOT HEALTH MONITOR "
        f"v{HEALTH_MONITOR_VERSION}"
    )
    print("=" * 78)

    print_broker_section(
        broker
    )

    print()
    print_module_table(
        module_results
    )

    print()
    print_module_details(
        module_results
    )

    print()
    print_system_assessment(
        broker,
        module_results,
    )

    print("=" * 78)


if __name__ == "__main__":
    main()