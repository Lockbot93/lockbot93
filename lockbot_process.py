"""
lockbot_process.py  --  start, stop and inspect LOCKBOT itself  (v1.0)

WHAT THIS IS
    Control over the processes rather than the trades. Is the controller
    up? How long has it been up? Start it, stop it, restart it, run one
    component by hand, and clear out sessions that never exited.

THE ONE THING THAT MAKES THIS DANGEROUS
    Stopping the controller stops options_manager.py, and for options
    that IS the stop loss. Alpaca offers no broker-side bracket for
    contracts, so the only thing standing between an open option and an
    unbounded loss is that module running every cycle.

    Equities are different — their brackets live at the broker and
    survive anything happening on this machine.

    So stop_controller() checks for open option positions first and
    refuses unless told explicitly to proceed. Killing the controller
    with options open is not "pausing the bot", it is removing the stop
    loss, and those deserve different words.

WHY THE SELF-TEST CANNOT TOUCH A REAL PROCESS
    On 2026-07-29 a self-test in lockbot_telegram.py called a live
    function and cancelled the brackets on two open positions. The same
    trap exists here in a worse form, so the same two defences apply: the
    killer is injectable, and EXECUTION_DISABLED refuses outright while
    tests run.

USAGE
    python lockbot_process.py                 what is running
    python lockbot_process.py --start         start the controller
    python lockbot_process.py --stop          stop it (asks about options)
    python lockbot_process.py --restart
    python lockbot_process.py --cleanup       kill stale brain/HUD sessions
    python lockbot_process.py --run scanner   run one component now
    python lockbot_process.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_FOLDER = Path(__file__).resolve().parent
CONTROLLER = "lockbot_controller.py"
LOG_FILE = PROJECT_FOLDER / "lockbot_controller.log"

# Same interlock as lockbot_telegram.py, for the same reason.
EXECUTION_DISABLED = False

COMPONENTS = {
    "scanner": "market_scanner.py",
    "manager": "trade_manager.py",
    "monitor": "position_monitor.py",
    "health": "health_monitor.py",
    "options": "options_manager.py",
    "options_scan": "options_scanner.py",
    "universe": "universe.py",
    "volatility": "universe_volatility.py",
    "shadow": "shadow_trades.py",
    "rearm": "rearm_brackets.py",
    "timestop": "equity_time_stop.py",
}

# Sessions that are safe to clear — none of them trade or hold a stop.
SESSION_SCRIPTS = ("lockbot_brain.py", "lockbot_hud.py", "lockbot_telegram.py")


def _powershell(script: str, timeout: int = 60) -> str:
    """Run a PowerShell snippet and return stdout."""

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout

    except Exception:
        return ""


def find_processes(needle: str) -> list[dict]:
    """
    Every python process whose command line mentions `needle`.

    Uses CIM rather than a PID file: a PID file goes stale the moment
    something dies unexpectedly, which is exactly when this matters.
    """

    script = (
        "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
        "Where-Object { $_.CommandLine -like '*" + needle + "*' } | "
        "ForEach-Object { "
        "  [pscustomobject]@{ pid=$_.ProcessId; "
        "  started=$_.CreationDate.ToString('o'); cmd=$_.CommandLine } "
        "} | ConvertTo-Json -Compress"
    )

    raw = _powershell(script).strip()

    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []

    if isinstance(parsed, dict):
        parsed = [parsed]

    return [p for p in parsed if isinstance(p, dict) and p.get("pid")]


def _uptime(started: str) -> str:
    try:
        begin = datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return "unknown"

    seconds = (datetime.now(begin.tzinfo) - begin).total_seconds()

    if seconds < 3600:
        return f"{seconds / 60:.0f}m"

    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"

    return f"{seconds / 86400:.1f}d"


def log_age_minutes() -> float | None:
    """Minutes since the controller last wrote a line."""

    try:
        return (time.time() - LOG_FILE.stat().st_mtime) / 60
    except OSError:
        return None


def open_option_count() -> int | None:
    """
    How many option positions are open. None when the broker is unreachable.

    None must never be treated as zero — that is the difference between
    "no options at risk" and "no idea whether options are at risk".
    """

    try:
        from lockbot_brain import _trading_client
        from position_filters import option_positions

        return len(option_positions(_trading_client().get_all_positions()))

    except Exception:
        return None


def status() -> dict:
    """Everything worth knowing about what is running."""

    controller = find_processes(CONTROLLER)
    sessions = []

    for script in SESSION_SCRIPTS:
        for process in find_processes(script):
            sessions.append({**process, "script": script})

    age = log_age_minutes()

    return {
        "controller_running": bool(controller),
        "controller": [
            {"pid": p["pid"], "uptime": _uptime(p.get("started", ""))}
            for p in controller
        ],
        "log_age_minutes": None if age is None else round(age, 1),
        "log_fresh": age is not None and age < 10,
        "sessions": sessions,
    }


def print_status() -> int:
    """Human-readable status."""

    state = status()

    print("=" * 58)
    print("LOCKBOT PROCESSES")
    print("=" * 58)

    if state["controller_running"]:
        for entry in state["controller"]:
            print(f"  CONTROLLER   running   pid {entry['pid']}   up {entry['uptime']}")
    else:
        print("  CONTROLLER   NOT RUNNING")

    age = state["log_age_minutes"]

    if age is None:
        print("  LOG          missing")
    elif state["log_fresh"]:
        print(f"  LOG          fresh, written {age:.1f}m ago")
    else:
        print(f"  LOG          STALE, {age:.1f}m since last write")

    if state["sessions"]:
        print(f"\n  Other sessions ({len(state['sessions'])}):")

        for session in state["sessions"]:
            print(
                f"    {session['script']:<22} pid {session['pid']:<7} "
                f"up {_uptime(session.get('started', ''))}"
            )

        print("\n  --cleanup ends these. They hold no positions and no stops.")
    else:
        print("\n  No other sessions.")

    print("=" * 58)

    return 0


def _kill(pid: int) -> bool:
    """Terminate one process. Injectable so tests never reach it."""

    if EXECUTION_DISABLED:
        return False

    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=30,
        )
        return result.returncode == 0

    except Exception:
        return False


def _launch_detached(
    script: Path, args: list[str] | None = None
) -> tuple[bool, Exception | None]:
    """Start a script in its own window, outliving whatever launched it.

    Extracted from start_controller so anything else that must survive
    the shell can reuse it rather than reimplement it. The Telegram bot
    is the first: it is started from an agent session or a terminal, and
    a copy that dies with its launcher is worse than one that never
    started, because nothing reports it missing.

    `args` exists because the controller takes none and lockbot_telegram
    requires --run. Without it the bot printed its help text into a new
    console and exited, which looks exactly like a crash from out here:
    launch reported success, no process appeared, no error anywhere.

    Returns (launched, error) rather than a message, so callers can word
    their own.
    """

    python = PROJECT_FOLDER / ".venv" / "Scripts" / "python.exe"
    interpreter = str(python) if python.exists() else sys.executable

    new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)

    attempts = [new_console | breakaway, new_console] if breakaway else [new_console]

    last_error: Exception | None = None

    for creation_flags in attempts:
        try:
            subprocess.Popen(
                [interpreter, "-u", str(script), *(args or [])],
                cwd=str(PROJECT_FOLDER),
                creationflags=creation_flags,
                close_fds=True,
            )
            return True, None
        except OSError as error:
            # Raised when the parent job forbids breakaway. Fall through
            # to the plain new-console attempt rather than giving up.
            last_error = error

    return False, last_error


def start_telegram() -> str:
    """Start the Telegram bot so it survives the session that started it.

    Deliberately NOT registered as a scheduled task: that needs an
    elevated shell and has failed with Access Denied here. This is the
    thing that works without admin. It does mean the bot does not come
    back by itself after a reboot -- watchdog.py does not watch it
    either -- so a missing bot is silent. Worth knowing before relying
    on it for anything time-critical.
    """

    if EXECUTION_DISABLED:
        return "Execution is disabled in this process."

    existing = find_processes("lockbot_telegram.py")

    if existing:
        pids = ", ".join(str(p["pid"]) for p in existing)
        return (
            f"Already running (pid {pids}). Two copies long-polling the "
            "same token would steal each other's updates, so this will "
            "not start a second."
        )

    script = PROJECT_FOLDER / "lockbot_telegram.py"

    if not script.exists():
        return "lockbot_telegram.py not found."

    launched, error = _launch_detached(script, ["--run"])

    if not launched:
        return f"Launch failed: {type(error).__name__}: {error}"

    for _ in range(8):
        time.sleep(1.5)
        started = find_processes("lockbot_telegram.py")

        if started:
            return f"Started. pid {started[0]['pid']}."

    return (
        "Launch issued but no Telegram process appeared after 12s. "
        "Check the new console window for a startup error."
    )


# Set when somebody stops the controller on purpose; cleared when
# somebody starts it. The watchdog reads it and declines to restart while
# it exists, so an unattended scheduled task can never overrule a person.
STOP_MARKER_FILE = PROJECT_FOLDER / "controller_stopped_deliberately"


def _mark_deliberate_stop() -> None:
    try:
        STOP_MARKER_FILE.write_text(
            f"stopped {datetime.now().astimezone().isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _clear_deliberate_stop() -> None:
    try:
        STOP_MARKER_FILE.unlink()
    except OSError:
        pass


def start_controller(killer=None) -> str:
    """Start the controller in its own window."""

    if EXECUTION_DISABLED:
        return "Execution is disabled in this process."

    # Starting it is consent for the watchdog to keep it alive again.
    _clear_deliberate_stop()

    existing = find_processes(CONTROLLER)

    if existing:
        pids = ", ".join(str(p["pid"]) for p in existing)
        return (
            f"Already running (pid {pids}). The controller holds a Windows "
            "mutex, so a second copy would exit immediately anyway."
        )

    script = PROJECT_FOLDER / CONTROLLER

    if not script.exists():
        return f"{CONTROLLER} not found."

    # Launch the interpreter directly rather than going through
    # `cmd /c start`. That was the original approach and it silently
    # failed: `start` only treats its first argument as a window title
    # when the title is QUOTED, so an unquoted `start LOCKBOT <path>`
    # tried to execute a program named LOCKBOT and reported "The system
    # cannot find the file LOCKBOT" — into a console window that closed
    # immediately. The controller stayed down and the stop had already
    # succeeded.
    #
    # CREATE_NEW_CONSOLE keeps its own window, so its output stays
    # visible. On its own it is NOT enough to keep the controller alive.
    #
    # A new console does not leave the launcher's JOB OBJECT. When this
    # module is run from a shell that owns a job with
    # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE — which is how most task
    # runners, remote shells and CI harnesses invoke things — every
    # descendant is killed the moment the launching command returns.
    #
    # That is exactly what happened on 2026-08-03. A controller started
    # at 00:16:25 logged "Cycle 1 started", then nothing. The watchdog
    # noticed it was down and restarted it at 01:21:15, which logged
    # "Cycle 1 started", then nothing. Each copy lived about a minute --
    # precisely as long as the process that launched it. The watchdog was
    # working perfectly and could not win, because its own restart
    # inherited the same doomed parentage. Two option positions sat
    # without a software stop through both gaps.
    #
    # CREATE_BREAKAWAY_FROM_JOB detaches from that job. DETACHED_PROCESS
    # is deliberately NOT used alongside it: it suppresses the console
    # entirely, and a controller with no window is one nobody can see has
    # died. Some jobs forbid breakaway, so the flag is dropped and
    # retried rather than allowed to fail the launch outright.
    launched, last_error = _launch_detached(script)

    if not launched:
        return f"Launch failed: {type(last_error).__name__}: {last_error}"

    # Startup runs broker reconciliation before the first cycle, so give
    # it room before deciding it did not come up.
    for _ in range(10):
        time.sleep(1.5)
        started = find_processes(CONTROLLER)

        if started:
            return f"Started. pid {started[0]['pid']}."

    return (
        "Launch issued but no controller process appeared after 15s. "
        "Check the new console window for a startup error."
    )


def stop_controller(force: bool = False, killer=None) -> str:
    """
    Stop the controller.

    Refuses while option positions are open unless force=True, because
    for options this module IS the stop loss.
    """

    killer = killer or _kill
    running = find_processes(CONTROLLER)

    if not running:
        return "Controller is not running."

    options = open_option_count()

    if not force:
        if options is None:
            return (
                "REFUSED: could not reach the broker to check for open option "
                "positions. Options have no broker-side stop — options_manager.py "
                "is their only one, and it dies with the controller. Re-run with "
                "force to stop anyway."
            )

        if options > 0:
            return (
                f"REFUSED: {options} open option position(s). Alpaca provides no "
                "bracket for options, so options_manager.py running every cycle "
                "IS their stop loss. Stopping the controller removes it entirely. "
                "Close the options first, or re-run with force if you accept that."
            )

    stopped = [pid for pid in (p["pid"] for p in running) if killer(pid)]

    if not stopped:
        return "Could not stop the controller. Try an elevated shell."

    # Tell the watchdog this was on purpose.
    #
    # The watchdog restarts a controller it finds down, which is what
    # rescues a crash. Without this marker it would also undo a
    # deliberate stop -- somebody halting the system for maintenance
    # would find a scheduled task starting it again twenty minutes
    # later, and would have no way to win that argument. Cleared by
    # start_controller().
    _mark_deliberate_stop()

    note = ""

    if options:
        note = (
            f"\nWARNING: {options} option position(s) are now without any stop "
            "loss. Nothing is watching them."
        )

    return (f"Stopped pid(s) {', '.join(map(str, stopped))}.{note}\n"
            "The watchdog will NOT restart it — this was recorded as a "
            "deliberate stop. Start it normally to hand control back.")


def restart_controller(force: bool = False, killer=None) -> str:
    """Stop then start."""

    result = stop_controller(force=force, killer=killer)

    if result.startswith("REFUSED"):
        return result

    time.sleep(3)

    return f"{result}\n{start_controller()}"


def cleanup_sessions(killer=None) -> str:
    """
    End brain, HUD and Telegram sessions that never exited.

    Safe by construction: none of these hold a position, a stop, or an
    order. The controller is deliberately not in SESSION_SCRIPTS.
    """

    killer = killer or _kill

    # Never kill the caller. lockbot_brain.py is a session script, so a
    # brain session asking to clean up sessions would otherwise terminate
    # itself mid-answer — and the reply would never arrive to say so.
    protected = {os.getpid(), os.getppid()}

    victims = []

    for script in SESSION_SCRIPTS:
        for process in find_processes(script):
            if int(process.get("pid", 0)) in protected:
                continue

            victims.append({**process, "script": script})

    if not victims:
        return "No stale sessions (the one you are talking to does not count)."

    stopped = [v for v in victims if killer(v["pid"])]

    lines = [f"Ended {len(stopped)} of {len(victims)} session(s):"]

    for victim in stopped:
        lines.append(f"  {victim['script']} pid {victim['pid']}")

    return "\n".join(lines)


def run_component(name: str) -> str:
    """Run one component once, now, and report how it went."""

    if EXECUTION_DISABLED:
        return "Execution is disabled in this process."

    script = COMPONENTS.get(name.strip().lower())

    if not script:
        return f"Unknown component. Choose from: {', '.join(sorted(COMPONENTS))}"

    path = PROJECT_FOLDER / script

    if not path.exists():
        return f"{script} not found."

    print(f"Running {script}...\n")

    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(PROJECT_FOLDER),
            capture_output=True,
            text=True,
            timeout=900,
        )

    except subprocess.TimeoutExpired:
        return f"{script} ran past 15 minutes and was stopped."

    tail = "\n".join((result.stdout or "").strip().splitlines()[-25:])
    verdict = "completed" if result.returncode == 0 else f"exited {result.returncode}"

    return f"{tail}\n\n{script} {verdict}."


def _self_test() -> int:
    """Offline checks. Enumeration is real; nothing is ever killed."""

    global EXECUTION_DISABLED

    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    EXECUTION_DISABLED = True

    killed: list[int] = []

    def fake_killer(pid: int) -> bool:
        killed.append(pid)
        return True

    print("Interlock")

    check("execution disabled during tests", EXECUTION_DISABLED)
    check("the real killer refuses", _kill(999999) is False)
    check("start refuses", "disabled" in start_controller())
    check("run_component refuses", "disabled" in run_component("scanner"))

    print()
    print("Status")

    state = status()

    for key in ("controller_running", "controller", "log_age_minutes", "sessions"):
        check(f"status has {key}", key in state)

    check("controller_running is a bool", isinstance(state["controller_running"], bool))
    check("sessions is a list", isinstance(state["sessions"], list))

    print()
    print("Component names")

    check("unknown component is rejected", "Unknown component" in run_component("nope")
          or "disabled" in run_component("nope"))
    check("scanner is mapped", COMPONENTS["scanner"] == "market_scanner.py")
    check("options manager is mapped", COMPONENTS["options"] == "options_manager.py")
    check("rearm is reachable", "rearm" in COMPONENTS)

    print()
    print("The options interlock")

    # sys.modules[__name__], not `import lockbot_process`. Run as a
    # script this file IS __main__, and importing it by name builds a
    # SECOND module object whose globals the live functions never read —
    # so the patches would land somewhere nothing looks, the real
    # enumeration would run, and the test would report on the actual
    # controller. It did exactly that before this line changed.
    module = sys.modules[__name__]

    original = module.open_option_count
    original_find = module.find_processes

    module.find_processes = lambda needle: (
        [{"pid": 1234, "started": ""}] if CONTROLLER in needle else []
    )

    module.open_option_count = lambda: 2
    killed.clear()
    result = stop_controller(killer=fake_killer)
    check("refuses to stop with options open", result.startswith("REFUSED"), result[:60])
    check("and kills nothing", killed == [])
    check("and explains why", "stop loss" in result.lower())

    module.open_option_count = lambda: None
    killed.clear()
    result = stop_controller(killer=fake_killer)
    check("refuses when the broker is unreachable", result.startswith("REFUSED"))
    check("unknown is not treated as zero", killed == [])

    module.open_option_count = lambda: 0
    killed.clear()
    result = stop_controller(killer=fake_killer)
    check("stops when no options are open", killed == [1234], str(killed))

    module.open_option_count = lambda: 2
    killed.clear()
    result = stop_controller(force=True, killer=fake_killer)
    check("force overrides the refusal", killed == [1234], str(killed))
    check("but warns loudly", "WARNING" in result, result[:80])

    module.open_option_count = original
    module.find_processes = original_find

    print()
    print("Cleanup safety")

    check("controller is not a session script", CONTROLLER not in SESSION_SCRIPTS)
    check("brain is", "lockbot_brain.py" in SESSION_SCRIPTS)

    module.find_processes = lambda needle: (
        [{"pid": 555, "started": ""}] if "brain" in needle else []
    )
    killed.clear()
    result = cleanup_sessions(killer=fake_killer)
    check("cleanup ends sessions", killed == [555], str(killed))
    module.find_processes = original_find

    EXECUTION_DISABLED = False

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All process checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Control the LOCKBOT processes.")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--run", metavar="COMPONENT")
    parser.add_argument("--start-telegram", action="store_true",
                        help="start the Telegram bot, detached")
    parser.add_argument("--force", action="store_true",
                        help="stop even with open option positions")
    parser.add_argument("--self-test", action="store_true")

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.start:
        print(start_controller())
        return 0

    if args.start_telegram:
        print(start_telegram())
        return 0

    if args.stop:
        print(stop_controller(force=args.force))
        return 0

    if args.restart:
        print(restart_controller(force=args.force))
        return 0

    if args.cleanup:
        print(cleanup_sessions())
        return 0

    if args.run:
        print(run_component(args.run))
        return 0

    return print_status()


if __name__ == "__main__":
    sys.exit(main())
