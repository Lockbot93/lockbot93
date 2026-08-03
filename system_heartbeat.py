"""LOCKBOT system heartbeat manager v0.2.

Stores health and activity information for LOCKBOT modules in a
persistent JSON file.

This module does not submit, modify, replace, or cancel broker orders.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HEARTBEAT_VERSION = "0.2"

HEARTBEAT_FILE = Path(__file__).with_name(
    "lockbot_heartbeat.json"
)

VALID_STATUSES = {
    "STARTING",
    "HEALTHY",
    "DEGRADED",
    "CRITICAL",
    "STOPPED",
}


def _utc_now() -> str:
    """Return the current UTC timestamp."""

    return datetime.now(
        timezone.utc
    ).isoformat()


def _new_heartbeat_state() -> dict[str, Any]:
    """Create a clean heartbeat-state structure."""

    current_time = _utc_now()

    return {
        "heartbeat_version": HEARTBEAT_VERSION,
        "system_started_at_utc": current_time,
        "last_updated_at_utc": current_time,
        "modules": {},
    }


def load_heartbeat_state() -> dict[str, Any]:
    """Load LOCKBOT's saved heartbeat state."""

    if not HEARTBEAT_FILE.exists():
        return _new_heartbeat_state()

    try:
        with HEARTBEAT_FILE.open(
            mode="r",
            encoding="utf-8",
        ) as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError(
                "Heartbeat state must be a dictionary."
            )

        modules = state.get(
            "modules"
        )

        if not isinstance(
            modules,
            dict,
        ):
            state["modules"] = {}

        if not state.get(
            "system_started_at_utc"
        ):
            state["system_started_at_utc"] = (
                _utc_now()
            )

        if not state.get(
            "last_updated_at_utc"
        ):
            state["last_updated_at_utc"] = (
                _utc_now()
            )

        state["heartbeat_version"] = (
            HEARTBEAT_VERSION
        )

        return state

    except (
        json.JSONDecodeError,
        OSError,
        ValueError,
    ) as error:
        print(
            "Could not read heartbeat state: "
            f"{type(error).__name__}: {error}"
        )

        return _new_heartbeat_state()


def save_heartbeat_state(
    state: dict[str, Any],
) -> bool:
    """Safely save LOCKBOT's heartbeat state."""

    temporary_file = (
        HEARTBEAT_FILE.with_suffix(
            ".tmp"
        )
    )

    state["heartbeat_version"] = (
        HEARTBEAT_VERSION
    )

    state["last_updated_at_utc"] = (
        _utc_now()
    )

    try:
        with temporary_file.open(
            mode="w",
            encoding="utf-8",
        ) as file:
            json.dump(
                state,
                file,
                indent=4,
                default=str,
            )

        temporary_file.replace(
            HEARTBEAT_FILE
        )

        return True

    except OSError as error:
        print(
            "Could not save heartbeat state: "
            f"{type(error).__name__}: {error}"
        )

        return False


def update_module_heartbeat(
    module_name: str,
    *,
    status: str,
    message: str = "",
    error: str = "",
    details: dict[str, Any] | None = None,
) -> bool:
    """Update one LOCKBOT module's heartbeat."""

    normalized_module = (
        module_name.strip().upper()
    )

    normalized_status = (
        status.strip().upper()
    )

    if not normalized_module:
        raise ValueError(
            "module_name cannot be blank."
        )

    if normalized_status not in VALID_STATUSES:
        raise ValueError(
            "Invalid heartbeat status: "
            f"{normalized_status}. "
            "Expected one of: "
            f"{', '.join(sorted(VALID_STATUSES))}"
        )

    if details is not None and not isinstance(
        details,
        dict,
    ):
        raise TypeError(
            "details must be a dictionary or None."
        )

    state = load_heartbeat_state()

    modules = state.setdefault(
        "modules",
        {},
    )

    previous_data = modules.get(
        normalized_module,
        {},
    )

    current_time = _utc_now()

    started_at_utc = previous_data.get(
        "started_at_utc"
    )

    previous_status = previous_data.get(
        "status"
    )

    if (
        not started_at_utc
        or (
            normalized_status == "STARTING"
            and previous_status
            in {
                "STOPPED",
                "CRITICAL",
            }
        )
    ):
        started_at_utc = current_time

    heartbeat_count = int(
        previous_data.get(
            "heartbeat_count",
            0,
        )
    ) + 1

    modules[normalized_module] = {
        "module_name": normalized_module,
        "status": normalized_status,
        "message": message.strip(),
        "error": error.strip(),
        "started_at_utc": started_at_utc,
        "last_heartbeat_at_utc": current_time,
        "heartbeat_count": heartbeat_count,
        "details": details or {},
    }

    return save_heartbeat_state(
        state
    )


def mark_module_starting(
    module_name: str,
    message: str = "",
    details: dict[str, Any] | None = None,
) -> bool:
    """Mark a module as starting."""

    return update_module_heartbeat(
        module_name,
        status="STARTING",
        message=message,
        details=details,
    )


def mark_module_healthy(
    module_name: str,
    message: str = "",
    details: dict[str, Any] | None = None,
) -> bool:
    """Mark a module as healthy."""

    return update_module_heartbeat(
        module_name,
        status="HEALTHY",
        message=message,
        details=details,
    )


def mark_module_degraded(
    module_name: str,
    message: str = "",
    error: str = "",
    details: dict[str, Any] | None = None,
) -> bool:
    """Mark a module as degraded."""

    return update_module_heartbeat(
        module_name,
        status="DEGRADED",
        message=message,
        error=error,
        details=details,
    )


def mark_module_critical(
    module_name: str,
    message: str = "",
    error: str = "",
    details: dict[str, Any] | None = None,
) -> bool:
    """Mark a module as critical."""

    return update_module_heartbeat(
        module_name,
        status="CRITICAL",
        message=message,
        error=error,
        details=details,
    )


def mark_module_stopped(
    module_name: str,
    message: str = "",
    details: dict[str, Any] | None = None,
) -> bool:
    """Mark a module as intentionally stopped."""

    return update_module_heartbeat(
        module_name,
        status="STOPPED",
        message=message,
        details=details,
    )


def get_module_heartbeat(
    module_name: str,
) -> dict[str, Any] | None:
    """Return one module's saved heartbeat."""

    normalized_module = (
        module_name.strip().upper()
    )

    state = load_heartbeat_state()

    return state.get(
        "modules",
        {},
    ).get(
        normalized_module
    )


def remove_module_heartbeat(
    module_name: str,
) -> bool:
    """Remove one module from the heartbeat state."""

    normalized_module = (
        module_name.strip().upper()
    )

    if not normalized_module:
        raise ValueError(
            "module_name cannot be blank."
        )

    state = load_heartbeat_state()

    modules = state.setdefault(
        "modules",
        {},
    )

    if normalized_module not in modules:
        return True

    del modules[normalized_module]

    return save_heartbeat_state(
        state
    )


def clear_test_heartbeat() -> bool:
    """Remove the obsolete HEARTBEAT_TEST record."""

    return remove_module_heartbeat(
        "HEARTBEAT_TEST"
    )


def print_heartbeat_state() -> None:
    """Print all saved module heartbeat records."""

    state = load_heartbeat_state()

    print("=" * 60)
    print(
        "          LOCKBOT SYSTEM HEARTBEATS "
        f"v{HEARTBEAT_VERSION}"
    )
    print("=" * 60)

    print(
        "System Started : "
        f"{state.get(
            'system_started_at_utc',
            'UNKNOWN',
        )}"
    )

    print(
        "Last Updated   : "
        f"{state.get(
            'last_updated_at_utc',
            'UNKNOWN',
        )}"
    )

    print("-" * 60)

    modules = state.get(
        "modules",
        {},
    )

    if not modules:
        print(
            "No module heartbeats have been recorded."
        )

    for module_name in sorted(
        modules
    ):
        module_data = modules[
            module_name
        ]

        print(
            f"{module_name:<22}: "
            f"{module_data.get(
                'status',
                'UNKNOWN',
            )}"
        )

        print(
            "  Started              : "
            f"{module_data.get(
                'started_at_utc',
                'UNKNOWN',
            )}"
        )

        print(
            "  Last heartbeat       : "
            f"{module_data.get(
                'last_heartbeat_at_utc',
                'UNKNOWN',
            )}"
        )

        print(
            "  Heartbeat count      : "
            f"{module_data.get(
                'heartbeat_count',
                0,
            )}"
        )

        message = module_data.get(
            "message",
            "",
        )

        if message:
            print(
                f"  Message              : "
                f"{message}"
            )

        error = module_data.get(
            "error",
            "",
        )

        if error:
            print(
                f"  Error                : "
                f"{error}"
            )

        details = module_data.get(
            "details",
            {},
        )

        if details:
            print(
                "  Details              : "
                f"{details}"
            )

        print("-" * 60)

    print("Status           : READY")


def main() -> None:
    """Print the current heartbeat state."""

    clear_test_heartbeat()
    print_heartbeat_state()


if __name__ == "__main__":
    main()