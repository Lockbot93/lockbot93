"""LOCKBOT controlled self-repair service v0.1."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepairResult:
    """Describe the result of one controlled repair attempt."""

    successful: bool
    component_name: str
    action_taken: str
    details: str


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


def attempt_component_repair(
    script_path: Path,
    component_name: str,
) -> RepairResult:
    """
    Perform a safe controlled repair assessment.

    Phase 1 does not alter source code, broker orders, positions,
    configuration files, or trading state. It verifies that the
    failed component still exists and contains valid Python syntax.
    """

    script_is_valid, verification_details = verify_python_script(
        script_path
    )

    if not script_is_valid:
        return RepairResult(
            successful=False,
            component_name=component_name,
            action_taken="COMPONENT_VERIFICATION_FAILED",
            details=verification_details,
        )

    return RepairResult(
        successful=True,
        component_name=component_name,
        action_taken="COMPONENT_VERIFIED_FOR_FINAL_RETRY",
        details=verification_details,
    )