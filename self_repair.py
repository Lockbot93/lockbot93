"""LOCKBOT controlled self-repair service v0.2."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class RepairResult:
    """Describe the result of one controlled repair attempt."""

    successful: bool
    component_name: str
    action_taken: str
    details: str


COMPONENT_STATE_FILES: dict[str, tuple[str, ...]] = {
    "Market Scanner": (
        "scanner_state.json",
        "lockbot_heartbeat.json",
    ),
    "Trade Manager": (
        "lockbot_heartbeat.json",
    ),
    "Position Monitor": (
        "position_state.json",
        "lockbot_heartbeat.json",
    ),
    "Health Monitor": (
        "lockbot_heartbeat.json",
    ),
}


def verify_python_script(script_path: Path) -> tuple[bool, str]:
    """Verify that a Python component exists and compiles."""

    if not script_path.exists():
        return (
            False,
            f"Required script does not exist: {script_path.name}",
        )

    if not script_path.is_file():
        return (
            False,
            f"Component path is not a file: {script_path.name}",
        )

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "py_compile",
                str(script_path),
            ],
            cwd=str(script_path.parent),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    except subprocess.TimeoutExpired:
        return (
            False,
            "Python compilation verification timed out.",
        )

    except Exception as error:
        return (
            False,
            (
                "Python compilation verification crashed: "
                f"{type(error).__name__}: {error}"
            ),
        )

    if result.returncode == 0:
        return (
            True,
            f"{script_path.name} passed compilation verification.",
        )

    error_output = (
        result.stderr.strip()
        or result.stdout.strip()
        or "No compiler error details were provided."
    )

    return (
        False,
        (
            f"{script_path.name} failed compilation verification: "
            f"{error_output}"
        ),
    )


def create_corrupt_backup(state_path: Path) -> Path:
    """Preserve a damaged state file before replacing it."""

    timestamp = datetime.now().astimezone().strftime(
        "%Y%m%d_%H%M%S"
    )

    backup_path = state_path.with_name(
        f"{state_path.stem}.corrupt_{timestamp}{state_path.suffix}.bak"
    )

    shutil.copy2(state_path, backup_path)

    return backup_path


def write_safe_empty_state(state_path: Path) -> None:
    """Create a valid empty JSON object using an atomic replace."""

    temporary_path = state_path.with_suffix(
        state_path.suffix + ".repair.tmp"
    )

    temporary_path.write_text(
        "{}\n",
        encoding="utf-8",
    )

    temporary_path.replace(state_path)


def verify_and_repair_json_state(
    state_path: Path,
) -> tuple[bool, str]:
    """
    Verify one JSON state file and repair only safe failures.

    Missing files are recreated as empty JSON objects. Corrupt files
    are backed up before being replaced. Valid files are never changed.
    """

    if not state_path.exists():
        try:
            write_safe_empty_state(state_path)
        except Exception as error:
            return (
                False,
                (
                    f"Could not recreate missing state file "
                    f"{state_path.name}: "
                    f"{type(error).__name__}: {error}"
                ),
            )

        return (
            True,
            f"Recreated missing state file: {state_path.name}",
        )

    if not state_path.is_file():
        return (
            False,
            f"State path is not a file: {state_path.name}",
        )

    try:
        raw_text = state_path.read_text(encoding="utf-8")
        loaded_state = json.loads(raw_text)

    except (OSError, UnicodeError) as error:
        return (
            False,
            (
                f"Could not read state file {state_path.name}: "
                f"{type(error).__name__}: {error}"
            ),
        )

    except json.JSONDecodeError as error:
        try:
            backup_path = create_corrupt_backup(state_path)
            write_safe_empty_state(state_path)

        except Exception as repair_error:
            return (
                False,
                (
                    f"Detected corrupt JSON in {state_path.name}, "
                    f"but repair failed: "
                    f"{type(repair_error).__name__}: "
                    f"{repair_error}"
                ),
            )

        return (
            True,
            (
                f"Repaired corrupt JSON state file "
                f"{state_path.name}. Backup saved as "
                f"{backup_path.name}. Original JSON error: "
                f"line {error.lineno}, column {error.colno}."
            ),
        )

    if not isinstance(loaded_state, dict):
        try:
            backup_path = create_corrupt_backup(state_path)
            write_safe_empty_state(state_path)

        except Exception as repair_error:
            return (
                False,
                (
                    f"State file {state_path.name} did not contain "
                    f"a JSON object, and repair failed: "
                    f"{type(repair_error).__name__}: "
                    f"{repair_error}"
                ),
            )

        return (
            True,
            (
                f"Repaired invalid state structure in "
                f"{state_path.name}. Backup saved as "
                f"{backup_path.name}."
            ),
        )

    return (
        True,
        f"{state_path.name} passed JSON integrity verification.",
    )


def verify_component_state_files(
    project_folder: Path,
    component_name: str,
) -> tuple[bool, list[str]]:
    """Verify and safely repair state files used by a component."""

    state_file_names = COMPONENT_STATE_FILES.get(
        component_name,
        (),
    )

    if not state_file_names:
        return (
            True,
            [
                (
                    f"No protected JSON state files are registered "
                    f"for {component_name}."
                )
            ],
        )

    all_files_ok = True
    details: list[str] = []

    for state_file_name in state_file_names:
        state_path = project_folder / state_file_name

        state_ok, state_details = verify_and_repair_json_state(
            state_path
        )

        details.append(state_details)

        if not state_ok:
            all_files_ok = False

    return all_files_ok, details


def attempt_component_repair(
    script_path: Path,
    component_name: str,
) -> RepairResult:
    """
    Perform a controlled component and state-file repair.

    This repair process:
    - verifies that the component exists and compiles;
    - verifies registered JSON state files;
    - recreates missing state files as empty JSON objects;
    - backs up and replaces corrupt JSON files.

    It does not modify source code, broker orders, positions,
    credentials, strategy settings, or valid state files.
    """

    script_is_valid, script_details = verify_python_script(
        script_path
    )

    if not script_is_valid:
        return RepairResult(
            successful=False,
            component_name=component_name,
            action_taken="COMPONENT_VERIFICATION_FAILED",
            details=script_details,
        )

    state_files_ok, state_details = verify_component_state_files(
        project_folder=script_path.parent,
        component_name=component_name,
    )

    combined_details = " | ".join(
        [script_details, *state_details]
    )

    if not state_files_ok:
        return RepairResult(
            successful=False,
            component_name=component_name,
            action_taken="STATE_FILE_REPAIR_FAILED",
            details=combined_details,
        )

    return RepairResult(
        successful=True,
        component_name=component_name,
        action_taken="COMPONENT_AND_STATE_VERIFIED_FOR_FINAL_RETRY",
        details=combined_details,
    )
