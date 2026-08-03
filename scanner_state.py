"""
LockBot Shared Scanner State

Provides helper functions for safely saving and loading
scanner results between independent processes.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


PROJECT_FOLDER = Path(__file__).resolve().parent

STATE_FILE = PROJECT_FOLDER / "scanner_state.json"

SAVE_RETRY_ATTEMPTS = 5
SAVE_RETRY_DELAY_SECONDS = 0.5


def save_state(data: dict) -> None:
    """Save scanner state atomically with file-lock recovery."""

    temporary_file = STATE_FILE.with_suffix(".json.tmp")

    last_error: Exception | None = None

    for attempt in range(1, SAVE_RETRY_ATTEMPTS + 1):
        try:
            with temporary_file.open(
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    data,
                    file,
                    indent=4,
                    default=str,
                )

                file.flush()
                os.fsync(file.fileno())

            os.replace(
                temporary_file,
                STATE_FILE,
            )

            return

        except PermissionError as error:
            last_error = error

            print(
                "Scanner state file is temporarily locked. "
                f"Retrying save attempt "
                f"{attempt}/{SAVE_RETRY_ATTEMPTS}..."
            )

            time.sleep(
                SAVE_RETRY_DELAY_SECONDS * attempt
            )

        finally:
            try:
                if temporary_file.exists():
                    temporary_file.unlink()
            except PermissionError:
                pass

    raise RuntimeError(
        "Unable to save scanner state after "
        f"{SAVE_RETRY_ATTEMPTS} attempts."
    ) from last_error


def load_state() -> dict:
    """Load scanner state safely."""

    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)

    except json.JSONDecodeError as error:
        print(
            "Scanner state file contains invalid JSON. "
            f"Returning empty state: {error}"
        )
        return {}

    except PermissionError as error:
        print(
            "Scanner state file is temporarily locked. "
            f"Returning empty state: {error}"
        )
        return {}