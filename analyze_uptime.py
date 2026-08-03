"""
LOCKBOT Uptime Gap Analyzer v1.0

Reads lockbot_controller.log and finds every gap between consecutive
log lines longer than GAP_THRESHOLD_MINUTES. During normal operation,
gaps between lines should never exceed about 5 minutes (the longest
backoff LOCKBOT ever deliberately sleeps for) — anything much longer
than that means the controller wasn't running, whatever the reason
(crash, sleep, laptop off, etc.).

This is read-only. It does not change LOCKBOT, the log, or anything
else — it only reads and reports.

Usage:
    python analyze_uptime.py
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

LOG_FILE = Path(__file__).with_name("lockbot_controller.log")

# Longer than the longest normal backoff (5 minutes), with some margin
# for a slow cycle, so this only flags genuine downtime.
GAP_THRESHOLD_MINUTES = 15.0

TIMESTAMP_PATTERN = re.compile(r"^\[([\d\-T:+]+)\]")

WEEKDAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]


def parse_log_timestamps() -> list[datetime]:
    if not LOG_FILE.exists():
        print(f"{LOG_FILE.name} does not exist.")
        return []

    timestamps: list[datetime] = []

    with LOG_FILE.open(mode="r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            match = TIMESTAMP_PATTERN.match(line)
            if not match:
                continue

            try:
                timestamps.append(datetime.fromisoformat(match.group(1)))
            except ValueError:
                continue

    return timestamps


def is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5  # Monday=0 ... Friday=4


def format_duration(minutes: float) -> str:
    hours, remaining_minutes = divmod(minutes, 60)
    if hours >= 1:
        return f"{int(hours)}h {remaining_minutes:.0f}m"
    return f"{minutes:.0f}m"


def main() -> None:
    print("=" * 65)
    print("       LOCKBOT UPTIME GAP ANALYZER v1.0")
    print("=" * 65)

    timestamps = parse_log_timestamps()

    if len(timestamps) < 2:
        print("Not enough log entries to analyze gaps.")
        return

    print(f"Log entries analyzed : {len(timestamps)}")
    print(f"Log spans            : {timestamps[0]} to {timestamps[-1]}")
    print(f"Gap threshold         : {GAP_THRESHOLD_MINUTES:.0f} minutes")

    gaps: list[tuple[datetime, datetime, float]] = []

    for earlier, later in zip(timestamps, timestamps[1:]):
        gap_minutes = (later - earlier).total_seconds() / 60
        if gap_minutes >= GAP_THRESHOLD_MINUTES:
            gaps.append((earlier, later, gap_minutes))

    print()
    if not gaps:
        print("No gaps found above the threshold. Looks continuously up.")
        print("=" * 65)
        return

    print(f"Found {len(gaps)} gap(s) of {GAP_THRESHOLD_MINUTES:.0f}+ minutes:")
    print("-" * 65)

    weekday_downtime_minutes = 0.0
    weekend_downtime_minutes = 0.0

    for start, end, minutes in gaps:
        start_weekday = WEEKDAY_NAMES[start.weekday()]
        end_weekday = WEEKDAY_NAMES[end.weekday()]
        tag = "WEEKDAY" if is_weekday(start) else "WEEKEND"

        if is_weekday(start):
            weekday_downtime_minutes += minutes
        else:
            weekend_downtime_minutes += minutes

        print(
            f"  [{tag:<7}] {start} ({start_weekday}) "
            f"-> {end} ({end_weekday})  "
            f"gap: {format_duration(minutes)}"
        )

    print("-" * 65)
    print(f"Total downtime during WEEKDAYS (Mon-Fri) : {format_duration(weekday_downtime_minutes)}")
    print(f"Total downtime during WEEKENDS           : {format_duration(weekend_downtime_minutes)}")

    print()
    if weekday_downtime_minutes > 0:
        print(
            "There IS at least some gap time on a weekday — worth looking "
            "at the specific weekday gap(s) listed above."
        )
    else:
        print(
            "All gaps found were on weekends — consistent with the laptop "
            "being off/asleep on weekends only, not a weekday problem."
        )

    print("=" * 65)


if __name__ == "__main__":
    main()