"""
LOCKBOT External Watchdog v1.2

Runs INDEPENDENTLY of lockbot_controller.py — scheduled separately
(see the accompanying setup instructions) so it can detect the
controller process itself dying, not just individual components
failing inside it. health_monitor.py can't do this job: if the whole
controller process crashes or the machine loses power, nothing inside
LOCKBOT is left running to notice or alert on it. This script is the
outside check.

What it checks:
- Is the heartbeat file being updated at all, recently?
- Is the controller log file being updated at all, recently?
- Does any module report CRITICAL?
- Are there broker positions LOCKBOT doesn't know about?
- Is universe.csv still being rebuilt each morning?

It only reads files and sends a notification. It never starts,
stops, or modifies anything, and never touches broker orders.

v1.2 adds the universe-file freshness check. market_scanner.py now
takes its symbol list from universe.csv, and if the morning rebuild
stops running the scanner keeps trading an increasingly outdated
list without ever raising an error.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import lockbot_config as config
from notifications import send_smart_notification
from system_heartbeat import load_heartbeat_state
from trade_manager import build_paper_trading_client
from retry_utils import with_retries


WATCHDOG_VERSION = "1.2"

# How stale the controller log can be before we consider the whole
# process possibly dead. This check applies regardless of market
# hours, since the controller logs on every cycle even while backing
# off for a closed market.
MAX_LOG_AGE_MINUTES = 15

# How stale the heartbeat file can be — but only enforced while the
# market is open. While the market is closed, lockbot_controller.py
# deliberately skips running every component (see its market-hours
# backoff logic), so the heartbeat file going stale for hours
# overnight or over a weekend is expected, not a failure.
MAX_HEARTBEAT_AGE_MINUTES = 20

PROJECT_FOLDER = Path(__file__).resolve().parent
CONTROLLER_LOG_FILE = PROJECT_FOLDER / "lockbot_controller.log"


def minutes_since_file_modified(path: Path) -> float | None:
    """Return minutes since a file was last modified, or None if missing."""

    if not path.exists():
        return None

    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    elapsed = datetime.now(timezone.utc) - modified_at

    return max(elapsed.total_seconds() / 60, 0)


def check_market_open() -> bool | None:
    """
    Return whether the market is currently open.

    Returns None (unknown) if the check itself fails — callers should
    treat that conservatively, since we can't tell whether a stale
    heartbeat is expected or not without knowing market hours.
    """

    try:
        trading_client = build_paper_trading_client()
        return bool(with_retries(trading_client.get_clock)().is_open)

    except Exception as error:
        print(f"Could not check market clock: {type(error).__name__}: {error}")
        return None


def check_controller_log() -> tuple[bool, str]:
    """Check whether the controller log has been updated recently."""

    age_minutes = minutes_since_file_modified(CONTROLLER_LOG_FILE)

    if age_minutes is None:
        return False, (
            f"{CONTROLLER_LOG_FILE.name} does not exist. "
            "The controller may have never been started."
        )

    if age_minutes > MAX_LOG_AGE_MINUTES:
        return False, (
            f"{CONTROLLER_LOG_FILE.name} was last updated "
            f"{age_minutes:.1f} minutes ago (limit: {MAX_LOG_AGE_MINUTES}). "
            "The controller process may have stopped or crashed."
        )

    return True, f"Controller log updated {age_minutes:.1f} minutes ago."


def check_heartbeat_file(market_open: bool | None) -> tuple[bool, str]:
    """
    Check whether the heartbeat file has been updated recently.

    Only enforced while the market is open (or unknown, out of an
    abundance of caution) — a stale heartbeat while the market is
    confirmed closed is expected behavior, not a problem.
    """

    age_minutes = minutes_since_file_modified(config.HEARTBEAT_FILE)

    if age_minutes is None:
        return False, (
            f"{config.HEARTBEAT_FILE.name} does not exist. "
            "No component has ever reported a heartbeat."
        )

    if market_open is False and age_minutes > MAX_HEARTBEAT_AGE_MINUTES:
        return True, (
            f"Heartbeat file is {age_minutes:.1f} minutes old, but the "
            "market is closed, so components aren't expected to be "
            "running right now. This is expected, not a problem."
        )

    if age_minutes > MAX_HEARTBEAT_AGE_MINUTES:
        return False, (
            f"{config.HEARTBEAT_FILE.name} was last updated "
            f"{age_minutes:.1f} minutes ago (limit: {MAX_HEARTBEAT_AGE_MINUTES}) "
            "while the market is open or its status is unknown. "
            "A component may not be running correctly."
        )

    return True, f"Heartbeat file updated {age_minutes:.1f} minutes ago."


def check_for_critical_modules() -> tuple[bool, str]:
    """Check whether any module currently reports CRITICAL."""

    state = load_heartbeat_state()
    modules = state.get("modules", {})

    critical_modules = [
        name
        for name, data in modules.items()
        if str(data.get("status", "")).upper() == "CRITICAL"
    ]

    if critical_modules:
        return False, f"Module(s) reporting CRITICAL: {', '.join(sorted(critical_modules))}."

    return True, "No modules currently report CRITICAL."


def check_orphaned_positions() -> tuple[bool, str]:
    """
    Check for broker positions LOCKBOT doesn't know about.

    This is the check that would have caught the untracked SPY
    position from earlier. Back then LOCKBOT allowed only one open
    position, so a single orphan blocked the entire pipeline. With
    several slots now allowed, an orphan is less catastrophic but
    still costly: market_scanner.py's open-position check counts
    EVERY broker position, tracked or not, so each orphan quietly
    eats a slot and is never journaled when it closes.
    """

    try:
        from trade_manager import build_paper_trading_client, _read_pending_trades

        from position_filters import equity_positions

        trading_client = build_paper_trading_client()

        # Equity only. Option positions are tracked in
        # options_position_state.json, not in the pending-trades registry
        # this check compares against, so including them here would report
        # every one of LOCKBOT's own option positions as orphaned.
        positions = equity_positions(trading_client.get_all_positions())

        if not positions:
            return True, "No open broker positions."

        pending_rows = _read_pending_trades()
        tracked_symbols = {
            str(row.get("symbol", "")).strip().upper() for row in pending_rows
        }

        orphaned_symbols = [
            str(position.symbol).upper()
            for position in positions
            if str(position.symbol).upper() not in tracked_symbols
        ]

        if orphaned_symbols:
            max_positions = getattr(config, "MAX_OPEN_POSITIONS", 1)

            return False, (
                f"Broker position(s) exist with no matching entry in "
                f"lockbot_pending_trades.csv: {', '.join(sorted(set(orphaned_symbols)))}. "
                f"Each one consumes one of LOCKBOT's {max_positions} "
                "open-position slots (the open-position check counts ALL "
                "broker positions) and won't be journaled when it closes."
            )

        return True, f"{len(positions)} open position(s), all tracked."

    except Exception as error:
        return False, f"Could not check for orphaned positions: {type(error).__name__}: {error}"


def check_universe_freshness(market_open: bool | None) -> tuple[bool, str]:
    """
    Check that universe.csv is being rebuilt.

    market_scanner.py takes its symbol list from this file. If the
    morning rebuild stops running, the scanner keeps trading an
    increasingly outdated list of symbols and never errors — it just
    logs a warning nobody reads. Only enforced while the market is
    open (or unknown), the same way the heartbeat check is: a stale
    universe file overnight or over a weekend is expected.
    """

    if not getattr(config, "USE_UNIVERSE_FILE", False):
        return True, "Universe file not in use (scanning config.SYMBOLS)."

    universe_file = Path(
        getattr(config, "UNIVERSE_FILE", PROJECT_FOLDER / "universe.csv")
    )

    age_minutes = minutes_since_file_modified(universe_file)

    if age_minutes is None:
        return False, (
            f"{universe_file.name} does not exist, so the scanner is "
            "falling back to config.SYMBOLS. Run 'python universe.py'."
        )

    stale_hours = float(getattr(config, "UNIVERSE_STALE_HOURS", 30))
    age_hours = age_minutes / 60

    if market_open is False and age_hours > stale_hours:
        return True, (
            f"Universe file is {age_hours:.1f} hours old, but the market "
            "is closed, so a rebuild isn't due yet."
        )

    if age_hours > stale_hours:
        return False, (
            f"{universe_file.name} was last rebuilt {age_hours:.1f} hours "
            f"ago (limit: {stale_hours:.0f}). The morning "
            "'python universe.py' run may have stopped happening — the "
            "scanner is trading an outdated symbol list."
        )

    return True, f"Universe file rebuilt {age_hours:.1f} hours ago."


def run_watchdog_check() -> bool:
    """
    Run all watchdog checks. Returns True if everything looks healthy.

    Sends a Pushover alert (forced, bypassing dedup) only when a
    problem is found — a healthy watchdog run is silent by design, so
    it doesn't add notification noise on top of LOCKBOT's own alerts.
    """

    print("=" * 60)
    print(f"          LOCKBOT EXTERNAL WATCHDOG v{WATCHDOG_VERSION}")
    print("=" * 60)

    market_open = check_market_open()

    market_status_label = (
        "OPEN" if market_open is True
        else "CLOSED" if market_open is False
        else "UNKNOWN"
    )
    print(f"Market status               : {market_status_label}")

    checks = [
        ("Controller log freshness", check_controller_log()),
        ("Heartbeat file freshness", check_heartbeat_file(market_open)),
        ("Critical module check", check_for_critical_modules()),
        ("Orphaned position check", check_orphaned_positions()),
        ("Universe file freshness", check_universe_freshness(market_open)),
    ]

    problems: list[str] = []

    for check_name, (ok, detail) in checks:
        status_label = "PASS" if ok else "FAIL"
        print(f"{check_name:<28}: {status_label} — {detail}")

        if not ok:
            problems.append(f"{check_name}: {detail}")

    print("=" * 60)

    if problems:
        problem_text = "\n".join(f"- {problem}" for problem in problems)

        print("Status: PROBLEM DETECTED — sending alert.")

        send_smart_notification(
            symbol="SYSTEM",
            event_type="WATCHDOG_ALERT",
            title="\U0001F6A8 LOCKBOT Watchdog Alert",
            reason="WATCHDOG_CHECK_FAILED",
            message=(
                "The external watchdog detected a problem with LOCKBOT:\n\n"
                f"{problem_text}\n\n"
                "Check the machine — the controller may need to be "
                "restarted manually."
            ),
            force=True,
        )

        return False

    print("Status: HEALTHY — no alert sent.")
    return True


def main() -> None:
    healthy = run_watchdog_check()
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()