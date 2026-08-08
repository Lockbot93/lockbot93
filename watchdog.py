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

import json
import sys
from datetime import datetime, timedelta, timezone
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

        # Say EQUITY, every time. This check filters options out three
        # lines above and then reported "No open broker positions" while
        # three option legs were open at the broker -- true of what it
        # examined, misleading about what it sounds like. A watchdog line
        # read at 2am is exactly where that costs something.
        if not positions:
            return True, "No open equity positions (options not in scope here)."

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

        return True, f"{len(positions)} open equity position(s), all tracked."

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


def check_telegram_bot(market_open: bool | None) -> tuple[bool, str]:
    """Check that the phone can still reach LOCKBOT.

    The bot is not safety-critical -- it places no trades and its absence
    risks no capital. What it costs is knowing. It is the channel you
    would use to find out whether anything is wrong, so a dead one is
    invisible precisely when it matters, and you discover it by messaging
    a bot that never answers.

    Unlike the controller, the bot is started from the Startup folder and
    so needs a logged-in session. Logging out kills it while the
    controller keeps trading as a scheduled task. That is the case worth
    reporting, and it is also why this is gated on market hours: a bot
    that is down overnight is expected, and alerting on it would repeat
    the mistake health_monitor made by reporting CRITICAL every weekend.
    """

    if not getattr(config, "TELEGRAM_WATCHDOG_ENABLED", True):
        return True, "Telegram check disabled."

    import os

    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        return True, "No Telegram token configured, so no bot is expected."

    try:
        from lockbot_process import find_processes

        running = find_processes("lockbot_telegram.py")
    except Exception as error:
        return True, (
            f"Could not enumerate processes ({type(error).__name__}); "
            "not treating that as the bot being down."
        )

    if running:
        return True, f"Telegram bot running (pid {running[0]['pid']})."

    if market_open is False:
        return True, (
            "Telegram bot is not running, but the market is closed. "
            "It starts with the next login."
        )

    return False, (
        "The Telegram bot is not running, so the phone cannot reach "
        "LOCKBOT while it is trading. It is launched from the Startup "
        "folder, so a logout would explain it. Restart with "
        "'python lockbot_process.py --start-telegram'."
    )


# How many restarts the watchdog may attempt before it stops trying and
# escalates instead.
#
# The failure this guards against is a crash LOOP: the network is down
# for an hour, the controller dies the same way on every start, and an
# unattended scheduled task restarts it every 20 minutes forever while
# the phone fills with identical alerts. Three attempts is enough to ride
# out a transient fault and few enough that a persistent one is obvious.
MAX_RESTARTS = 3
RESTART_WINDOW_HOURS = 6

RESTART_STATE_FILE = PROJECT_FOLDER / "watchdog_restarts.json"

# Written by lockbot_process.stop_controller(), cleared on a deliberate
# start. Its whole purpose is that the watchdog must never undo a human
# decision -- someone stopping the controller for maintenance should not
# have to fight a scheduled task that keeps starting it again.
STOP_MARKER_FILE = PROJECT_FOLDER / "controller_stopped_deliberately"


def _restart_history() -> list[str]:
    try:
        return json.loads(RESTART_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []


def _recent_restarts() -> list[str]:
    """Restart timestamps inside the rolling window."""

    cutoff = datetime.now(timezone.utc) - timedelta(hours=RESTART_WINDOW_HOURS)
    recent = []

    for stamp in _restart_history():
        try:
            when = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            continue

        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)

        if when >= cutoff:
            recent.append(stamp)

    return recent


def _record_restart() -> None:
    history = _recent_restarts()
    history.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))

    try:
        RESTART_STATE_FILE.write_text(json.dumps(history), encoding="utf-8")
    except OSError:
        pass


def stopped_deliberately() -> bool:
    return STOP_MARKER_FILE.exists()


def restart_controller_if_down(is_running=None, starter=None) -> tuple[bool, str]:
    """Bring the controller back, unless it was stopped on purpose.

    Returns (attempted, detail).

    `is_running` and `starter` are injectable so the guards below can be
    tested without a live controller. The first version had no seams and
    two of its own acceptance checks passed vacuously, because the
    running controller short-circuited them before the branch under test
    was reached.

    Detecting an outage and only reporting it is what turned a DNS blip
    into a 64-minute gap on 2026-08-08: the alert fired correctly at
    12:20 and would have fired every 20 minutes all afternoon while
    nothing changed. This module runs OUTSIDE the controller on its own
    schedule precisely so it can catch the controller process dying, and
    then it did nothing about it.

    Starting the controller is restorative and cannot place an order by
    itself -- the scanner decides that, and it will not run at all until
    reconciliation succeeds.
    """

    if is_running is None or starter is None:
        try:
            from lockbot_process import (
                CONTROLLER, find_processes, start_controller,
            )
        except Exception as error:
            return False, (
                f"cannot reach the process module ({type(error).__name__})"
            )

        is_running = is_running or (lambda: bool(find_processes(CONTROLLER)))
        starter = starter or start_controller

    if is_running():
        return False, "controller is running"

    if stopped_deliberately():
        return False, (
            "controller is down because somebody STOPPED it on purpose "
            f"({STOP_MARKER_FILE.name} exists). Not restarting. Delete that "
            "file, or start it normally, to hand control back to the watchdog."
        )

    recent = _recent_restarts()

    if len(recent) >= MAX_RESTARTS:
        return False, (
            f"ESCALATION: {len(recent)} restarts already in the last "
            f"{RESTART_WINDOW_HOURS} hours and it is down again. NOT "
            "restarting a fourth time — something is wrong that a restart "
            "does not fix. This needs a person."
        )

    _record_restart()
    attempt = len(recent) + 1
    result = starter()

    return True, (
        f"controller was down; restart attempt {attempt} of {MAX_RESTARTS} "
        f"in the last {RESTART_WINDOW_HOURS}h — {result}"
    )


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
        ("Telegram reachability", check_telegram_bot(market_open)),
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

        print("Status: PROBLEM DETECTED.")

        # Act, then report what was done. An alert that only says "the
        # controller may need to be restarted manually" is a message
        # nobody can act on at 2am, and this process can restart it.
        attempted, restart_detail = restart_controller_if_down()
        print(f"Recovery                    : {restart_detail}")

        if attempted:
            action = f"\n\nACTION TAKEN: {restart_detail}"
        elif "ESCALATION" in restart_detail:
            action = f"\n\n⚠ {restart_detail}"
        elif "on purpose" in restart_detail:
            action = f"\n\nNOT restarting: {restart_detail}"
        else:
            action = ""

        send_smart_notification(
            symbol="SYSTEM",
            event_type="WATCHDOG_ALERT",
            title="\U0001F6A8 LOCKBOT Watchdog Alert",
            reason="WATCHDOG_CHECK_FAILED",
            message=(
                "The external watchdog detected a problem with LOCKBOT:\n\n"
                f"{problem_text}{action}"
            ),
            force=True,
        )

        return False

    print("Status: HEALTHY — no alert sent.")
    return True


def _self_test() -> int:
    """LOCKBOT's four acceptance tests for the 2026-08-08 outage."""

    import tempfile

    global RESTART_STATE_FILE, STOP_MARKER_FILE

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))
            failures.append(name)

    real_state, real_marker = RESTART_STATE_FILE, STOP_MARKER_FILE

    with tempfile.TemporaryDirectory() as folder:
        RESTART_STATE_FILE = Path(folder) / "restarts.json"
        STOP_MARKER_FILE = Path(folder) / "stopped"

        print("\nTHE LOG CHECK MUST NOT BE SILENCED BY A CLOSED MARKET")
        # The fix that was NOT made, asserted so nobody makes it later.
        # The closed-market backoff is 300s, well inside the 15-minute
        # limit, so a stale log means the controller is really gone.
        check("no market-closed exemption exists on the log check",
              "market" not in check_controller_log.__doc__.lower(),
              "a market-closed exemption here would have hidden a "
              "64-minute outage on 2026-08-08")
        check("and the limit is still tighter than the closed backoff",
              MAX_LOG_AGE_MINUTES * 60 > 300,
              f"{MAX_LOG_AGE_MINUTES}min vs a 300s backoff")

        # The controller is DOWN for these, injected rather than real.
        started: list[int] = []
        down = lambda: False            # noqa: E731
        up = lambda: True               # noqa: E731

        def starter():
            started.append(1)
            return "Started. pid 999."

        print("\nA RUNNING CONTROLLER IS LEFT ALONE")
        attempted, detail = restart_controller_if_down(up, starter)
        check("nothing is done when it is already up", not attempted, detail)
        check("and nothing was started", not started)

        print("\nA DELIBERATE STOP IS NEVER UNDONE")
        STOP_MARKER_FILE.write_text("stopped", encoding="utf-8")
        check("the marker is seen", stopped_deliberately())

        attempted, detail = restart_controller_if_down(down, starter)
        check("and no restart is attempted", not attempted, detail)
        check("the reason says it was on purpose", "on purpose" in detail,
              detail)
        check("nothing was started", not started)

        STOP_MARKER_FILE.unlink()
        check("clearing the marker hands control back",
              not stopped_deliberately())

        print("\nA DOWN CONTROLLER IS ACTUALLY RESTARTED")
        attempted, detail = restart_controller_if_down(down, starter)
        check("the restart happens", attempted, detail)
        check("the starter really ran", len(started) == 1)
        check("and the alert names the attempt count",
              "attempt 1 of" in detail, detail)

        print("\nTHE CRASH-LOOP GUARD")
        while len(_recent_restarts()) < MAX_RESTARTS:
            _record_restart()

        check(f"{MAX_RESTARTS} restarts are remembered",
              len(_recent_restarts()) == MAX_RESTARTS)

        before = len(started)
        attempted, detail = restart_controller_if_down(down, starter)
        check("a fourth attempt is refused", not attempted, detail)
        check("and it escalates rather than going quiet",
              "ESCALATION" in detail, detail)
        check("nothing was started on the fourth", len(started) == before)

        print("\nOLD ATTEMPTS AGE OUT OF THE WINDOW")
        stale = (datetime.now(timezone.utc)
                 - timedelta(hours=RESTART_WINDOW_HOURS + 1))
        RESTART_STATE_FILE.write_text(
            json.dumps([stale.isoformat(timespec="seconds")] * 5),
            encoding="utf-8")
        check("attempts older than the window are forgotten",
              not _recent_restarts())

        print("\nIT NEVER BREAKS ON BAD STATE")
        RESTART_STATE_FILE.write_text("not json", encoding="utf-8")
        check("a corrupt state file reads as empty", _recent_restarts() == [])
        RESTART_STATE_FILE.unlink()
        check("a missing state file reads as empty", _recent_restarts() == [])

    RESTART_STATE_FILE, STOP_MARKER_FILE = real_state, real_marker

    print("\nTHE CONTROLLER SURVIVES AN UNREACHABLE BROKER")
    source = (PROJECT_FOLDER / "lockbot_controller.py").read_text(
        encoding="utf-8")
    check("a failed reconciliation no longer raises",
          'raise RuntimeError("Startup broker reconciliation failed.")'
          not in source,
          "a DNS blip would kill the controller again")
    check("it comes up degraded instead", "DEGRADED" in source)
    check("and blocks the equity entry path",
          "Market Scanner SKIPPED" in source)
    check("and the options entry path",
          "Options Scanner SKIPPED" in source)
    check("while still running the options stop loss",
          source.index("Options Manager")
          < source.index("Options Scanner SKIPPED"),
          "exits must not be gated on reconciliation")

    print()

    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All watchdog checks passed.")
    return 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="LOCKBOT external watchdog")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(_self_test())

    healthy = run_watchdog_check()
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()