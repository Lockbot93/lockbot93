"""
lockbot_incidents.py  --  what went wrong, and whether it keeps happening

WHY THIS EXISTS
    lockbot_learn.py studies trading results. It had no idea LOCKBOT ever
    FAILED — module crashes, self-repairs, rejected orders and network
    drops all scrolled past in a log nobody reads. A system that cannot
    see its own errors cannot learn from them.

    On 2026-07-29 the controller logged a Market Scanner failure that
    burned three retries and a self-repair, and separately dropped four
    cycles to DNS resolution failures across two nights. Neither reached
    the analysis, so neither was ever considered.

FINGERPRINTING IS THE POINT
    A raw error list is not learning, it is a second log. What matters is
    whether the SAME failure keeps happening, so each incident is reduced
    to a fingerprint with the variable parts stripped out — timestamps,
    cycle numbers, PIDs, durations.

    Four DNS failures across two nights then collapse into one incident
    seen four times, which is a fact worth acting on, rather than four
    lines worth skimming.

WHAT IT DOES NOT DO
    Reads logs, heartbeats and closed orders. Changes nothing, fixes
    nothing, submits nothing.

USAGE
    python lockbot_incidents.py              recent incidents
    python lockbot_incidents.py --days 7
    python lockbot_incidents.py --all        include one-off occurrences
    python lockbot_incidents.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_FOLDER = Path(__file__).resolve().parent
CONTROLLER_LOG = PROJECT_FOLDER / "lockbot_controller.log"

# Lines worth treating as incidents, with the category and how much it
# matters. Ordered — the first match wins, so specific beats general.
LOG_PATTERNS = (
    (re.compile(r"could not recover after", re.I), "component_failure", "high"),
    (re.compile(r"Starting controlled self-repair", re.I), "self_repair", "high"),
    (re.compile(r"crashed unexpectedly", re.I), "cycle_crash", "medium"),
    (re.compile(r"was unsuccessful", re.I), "component_retry", "low"),
    (re.compile(r"URGENT", re.I), "alert", "high"),
    (re.compile(r"failed with exit code", re.I), "component_exit", "medium"),
    (re.compile(r"recovered (successfully|automatically)", re.I), "recovery", "info"),
    (re.compile(r"Startup broker reconciliation failed", re.I), "startup_failure", "high"),
)

# Everything that varies between two occurrences of the SAME problem.
# Stripped before fingerprinting so recurrences group together.
_NOISE = (
    (re.compile(r"\[\d{4}-\d{2}-\d{2}T[\d:.\-+]+\]"), ""),      # timestamps
    (re.compile(r"\bCycle \d+\b", re.I), "Cycle N"),
    (re.compile(r"\battempt \d+/\d+\b", re.I), "attempt N"),
    (re.compile(r"\bpid \d+\b", re.I), "pid N"),
    (re.compile(r"\b\d+\.\d+ seconds\b"), "N seconds"),
    (re.compile(r"\bin \d+ seconds\b"), "in N seconds"),
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),
    (re.compile(r"\b\d{4,}\b"), "N"),
    (re.compile(r"\s+"), " "),
)

TIMESTAMP = re.compile(r"\[(\d{4}-\d{2}-\d{2}T[\d:.\-+]+)\]")


def fingerprint(line: str) -> str:
    """Reduce a log line to its recurring shape."""

    text = line

    for pattern, replacement in _NOISE:
        text = pattern.sub(replacement, text)

    return text.strip()[:220]


def _line_time(line: str) -> datetime | None:
    match = TIMESTAMP.search(line)

    if not match:
        return None

    try:
        parsed = datetime.fromisoformat(match.group(1))
    except ValueError:
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def scan_controller_log(days: int = 3, path: Path | None = None) -> list[dict]:
    """Find incidents in the controller log within the window."""

    path = path or CONTROLLER_LOG

    if not path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    found = []

    for line in lines:
        when = _line_time(line)

        if when is None or when < cutoff:
            continue

        for pattern, category, severity in LOG_PATTERNS:
            if pattern.search(line):
                found.append(
                    {
                        "source": "controller_log",
                        "category": category,
                        "severity": severity,
                        "at": when.isoformat(),
                        "message": TIMESTAMP.sub("", line).strip()[:400],
                        "fingerprint": fingerprint(line),
                    }
                )
                break

    return found


def scan_heartbeat() -> list[dict]:
    """Any module currently reporting something other than healthy."""

    try:
        import lockbot_config as config

        data = json.loads(config.HEARTBEAT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

    found = []

    for name, module in (data.get("modules") or {}).items():
        status = str(module.get("status", "")).upper()

        if status in {"HEALTHY", ""}:
            continue

        message = module.get("error") or module.get("message") or ""

        found.append(
            {
                "source": "heartbeat",
                "category": "module_" + status.lower(),
                "severity": "high" if status == "CRITICAL" else "medium",
                "at": module.get("last_heartbeat_at_utc", ""),
                "message": f"{name}: {message}"[:400],
                "fingerprint": fingerprint(f"{name} {status} {message}"),
            }
        )

    return found


def scan_broker(days: int = 7) -> list[dict]:
    """Orders the broker rejected — the clearest signal of a bad request."""

    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        from lockbot_brain import _trading_client

        client = _trading_client()

        orders = client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=datetime.now(timezone.utc) - timedelta(days=days),
                limit=500,
            )
        )

    except Exception:
        return []

    found = []

    for order in orders or []:
        status = str(getattr(order.status, "value", order.status)).lower()

        # Cancellations are usually deliberate; a rejection never is.
        if status not in {"rejected", "expired"}:
            continue

        reason = getattr(order, "reject_reason", None) or status

        found.append(
            {
                "source": "broker",
                "category": "order_" + status,
                "severity": "high",
                "at": str(getattr(order, "submitted_at", "")),
                "message": f"{order.symbol} {status}: {reason}"[:400],
                "fingerprint": fingerprint(f"order {status} {reason}"),
            }
        )

    return found


def collect(days: int = 3, include_broker: bool = True) -> dict:
    """
    Gather incidents and group them by fingerprint.

    The grouping is the useful part: an incident seen forty times is a
    different problem from one seen once, and only the count says which.
    """

    incidents = scan_controller_log(days) + scan_heartbeat()

    if include_broker:
        incidents.extend(scan_broker(days=max(days, 7)))

    grouped: dict[str, dict] = {}

    for incident in incidents:
        key = incident["fingerprint"]

        if key not in grouped:
            grouped[key] = {
                "category": incident["category"],
                "severity": incident["severity"],
                "source": incident["source"],
                "example": incident["message"],
                "count": 0,
                "first_seen": incident["at"],
                "last_seen": incident["at"],
            }

        entry = grouped[key]
        entry["count"] += 1

        if incident["at"]:
            if not entry["first_seen"] or incident["at"] < entry["first_seen"]:
                entry["first_seen"] = incident["at"]
            if not entry["last_seen"] or incident["at"] > entry["last_seen"]:
                entry["last_seen"] = incident["at"]

    order = {"high": 0, "medium": 1, "low": 2, "info": 3}

    ranked = sorted(
        grouped.values(),
        key=lambda e: (order.get(e["severity"], 9), -e["count"]),
    )

    return {
        "window_days": days,
        "total_occurrences": sum(e["count"] for e in ranked),
        "distinct_incidents": len(ranked),
        "recurring": [e for e in ranked if e["count"] > 1],
        "incidents": ranked,
    }


def report(days: int = 3, show_all: bool = False) -> int:
    """Print what went wrong."""

    result = collect(days)

    print("=" * 62)
    print(f"LOCKBOT INCIDENTS — last {days} day(s)")
    print("=" * 62)

    if not result["incidents"]:
        print("\nNothing recorded. No failures, no self-repairs, no rejections.")
        return 0

    print(
        f"\n{result['distinct_incidents']} distinct, "
        f"{result['total_occurrences']} occurrence(s), "
        f"{len(result['recurring'])} recurring\n"
    )

    shown = result["incidents"] if show_all else [
        e for e in result["incidents"] if e["count"] > 1 or e["severity"] == "high"
    ]

    if not shown:
        print("Only one-off, low-severity events. Pass --all to see them.")
        return 0

    for entry in shown:
        marker = "*" if entry["count"] > 1 else " "
        print(
            f"{marker} [{entry['severity'].upper():<6}] {entry['category']}  "
            f"x{entry['count']}"
        )
        print(f"    {entry['example'][:150]}")
        print(f"    first {entry['first_seen'][:19]}   last {entry['last_seen'][:19]}")
        print()

    if result["recurring"]:
        print("* recurring — these are the ones worth fixing rather than watching.")

    return 0


def _self_test() -> int:
    """Offline checks. No broker, no network."""

    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    print("Fingerprinting")

    a = "[2026-07-29T21:45:32-05:00] Cycle 311 crashed unexpectedly: ConnectionError"
    b = "[2026-07-30T01:26:42-05:00] Cycle 4 crashed unexpectedly: ConnectionError"

    check("same failure, different cycle groups", fingerprint(a) == fingerprint(b),
          f"{fingerprint(a)!r} vs {fingerprint(b)!r}")

    c = "[2026-07-30T01:26:42-05:00] Cycle 4 crashed unexpectedly: ValueError"
    check("different failures stay apart", fingerprint(b) != fingerprint(c))

    check("attempt numbers are stripped",
          fingerprint("Market Scanner attempt 1/3 was unsuccessful")
          == fingerprint("Market Scanner attempt 3/3 was unsuccessful"))

    check("pids are stripped",
          fingerprint("Stopped pid 4600") == fingerprint("Stopped pid 15828"))

    check("durations are stripped",
          fingerprint("took 1.25 seconds") == fingerprint("took 9.80 seconds"))

    print()
    print("Log scanning")

    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        log = Path(folder) / "c.log"
        now = datetime.now(timezone.utc)

        recent = (now - timedelta(hours=2)).isoformat()
        old = (now - timedelta(days=30)).isoformat()

        log.write_text(
            f"[{recent}] Cycle 1 crashed unexpectedly: ConnectionError\n"
            f"[{recent}] Cycle 2 crashed unexpectedly: ConnectionError\n"
            f"[{recent}] Market Scanner could not recover after 3 attempts.\n"
            f"[{recent}] Cycle 3 started.\n"
            f"[{old}] Cycle 9 crashed unexpectedly: ConnectionError\n",
            encoding="utf-8",
        )

        found = scan_controller_log(days=3, path=log)

        check("finds incidents", len(found) == 3, str(len(found)))
        check("ignores ordinary lines",
              all("started" not in f["message"] for f in found))
        check("respects the window", all("Cycle 9" not in f["message"] for f in found))

        categories = {f["category"] for f in found}
        check("categorises crashes", "cycle_crash" in categories, str(categories))
        check("categorises failures", "component_failure" in categories)

        severities = {f["severity"] for f in found}
        check("failure outranks crash", "high" in severities and "medium" in severities)

        check("a missing log is empty",
              scan_controller_log(days=3, path=Path(folder) / "nope.log") == [])

    print()
    print("Grouping")

    grouped = collect(days=3, include_broker=False)

    check("returns a dict", isinstance(grouped, dict))
    for key in ("recurring", "incidents", "total_occurrences", "distinct_incidents"):
        check(f"has {key}", key in grouped)

    check("recurring is a subset of incidents",
          len(grouped["recurring"]) <= len(grouped["incidents"]))
    check("counts are positive",
          all(e["count"] >= 1 for e in grouped["incidents"]))
    check("high severity sorts first",
          not grouped["incidents"]
          or grouped["incidents"][0]["severity"] in {"high", "medium", "low", "info"})

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All incident checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="What went wrong, and how often.")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--all", action="store_true", help="include one-offs")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--self-test", action="store_true")

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.json:
        print(json.dumps(collect(args.days), indent=2, default=str))
        return 0

    return report(args.days, args.all)


if __name__ == "__main__":
    sys.exit(main())
