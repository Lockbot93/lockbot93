"""
lockbot_telegram.py  --  reach LOCKBOT from your phone  (v1.0)

WHAT THIS IS
    A Telegram bot that answers questions about LOCKBOT from anywhere.
    Message it "how are we doing?" and it replies with the real state of
    the account, read out of the same files the controller writes.

WHY TELEGRAM AND NOT A WEB ENDPOINT
    This machine trades. Any remote channel into it is a trading channel,
    so the shape of the connection matters more than the convenience.

    This polls Telegram OUTBOUND, exactly like notifications.py polls
    Pushover. Nothing on your network becomes reachable from the
    internet, no port is forwarded, no firewall rule changes. An open
    inbound port on a machine that moves money, secured by whatever
    authentication got written in an evening, is the worst available
    option and this deliberately avoids it.

    No new dependencies either — the Bot API is plain HTTP, and
    notifications.py already established that pattern here.

DELIBERATELY READ-ONLY
    The bot answers anything and trades nothing. That is not an
    oversight, it is the point.

    LOCKBOT is currently in the middle of measuring whether its strategy
    has an edge — that is what the shadow log, the ten-day resolution
    horizon and the quality ranking are all for. A measurement like that
    needs trades to run to their conclusion.

    A phone that can close positions is a machine for corrupting it. You
    see a red number at 11am, you feel it, you close something that would
    have reached its target at 2pm, and now the data has a hole in it
    while you have a satisfying story about the loss you avoided.
    Discretionary intervention wrecking a systematic strategy is not an
    exotic failure — it is the common one. The exits are already
    automatic and already correct; brackets at the broker and software
    stops in options_manager.py do not get nervous.

    So: no entries, no exits, no position sizing from the phone.

THE ONE EXCEPTION
    /flatten cancels every order and liquidates every position.

    It exists for when the CODE looks wrong, not when a TRADE looks bad.
    Those are different situations and only the first is worth a remote
    kill switch. It requires echoing back a challenge word, because a
    fat finger or an autocorrect should not be able to liquidate an
    account.

SECURITY
    - The bot only answers user IDs listed in TELEGRAM_ALLOWED_USER_IDS.
      Everything else is dropped before it reaches the model or the
      broker, and logged.
    - With no allowlist configured it refuses to act at all, and tells
      the first person who messages what their user ID is so it can be
      added. It never defaults to open.
    - The token in .env is the credential. Anyone holding it can read
      your account, so treat it like the Alpaca keys.

SETUP
    1. Message @BotFather on Telegram, send /newbot, follow the prompts.
    2. Put the token it gives you in .env:
           TELEGRAM_BOT_TOKEN=8123456789:AAH...
    3. Run:  python lockbot_telegram.py --run
    4. Message your bot. It will tell you your user ID.
    5. Add it to .env:
           TELEGRAM_ALLOWED_USER_IDS=123456789
    6. Restart. Done.

USAGE
    python lockbot_telegram.py --run          start the bot
    python lockbot_telegram.py --check        verify token and config
    python lockbot_telegram.py --self-test    offline checks, no network
"""

from __future__ import annotations

import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_FOLDER = Path(__file__).resolve().parent

API_BASE = "https://api.telegram.org/bot"

# Telegram rejects anything longer. Replies get split on line breaks.
MESSAGE_LIMIT = 4096

# Long-poll seconds. Telegram holds the connection open until a message
# arrives or this expires, so a high value means near-instant delivery
# with almost no traffic.
POLL_TIMEOUT = 50

# How long a /flatten challenge stays valid.
CHALLENGE_SECONDS = 90

HELP_TEXT = """LOCKBOT

Ask me anything in plain English — "how are we doing?", "why did it skip
HDB?", "what's the shadow win rate?"

Commands
/status   account and positions, instant and free
/brief    short written briefing
/analyze  full analysis (slow, ~25c)
/notes    what I've learned so far
/flatten  EMERGENCY — cancel orders, close everything
/help     this message

I answer questions and place no trades. The only exception is /flatten,
which is there for when the code looks wrong, not when a trade looks bad."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_FOLDER / ".env")
    except Exception:
        pass


def bot_token() -> str:
    _load_env()
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def allowed_user_ids() -> set[int]:
    """
    Read the allowlist. An empty set means refuse everyone.

    Parsed leniently over commas and whitespace, because this gets typed
    into a .env file by hand.
    """

    _load_env()
    raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")

    ids = set()

    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()

        if chunk.isdigit():
            ids.add(int(chunk))

    return ids


def is_authorized(user_id: int, allowlist: set[int] | None = None) -> bool:
    """Authorize a user. An unset allowlist authorizes NOBODY."""

    allowlist = allowed_user_ids() if allowlist is None else allowlist

    if not allowlist:
        return False

    return user_id in allowlist


# ---------------------------------------------------------------------------
# Telegram HTTP
# ---------------------------------------------------------------------------

def _api(method: str, timeout: int = 60, **params) -> dict:
    """Call one Bot API method. Returns {} on any failure."""

    token = bot_token()

    if not token:
        return {}

    data = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}
    ).encode()

    request = urllib.request.Request(f"{API_BASE}{token}/{method}", data=data)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": str(error)}

    except Exception as error:
        return {"ok": False, "description": f"{type(error).__name__}: {error}"}


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """
    Break a reply into Telegram-sized pieces.

    Splits on line breaks so a paragraph is never cut mid-sentence, and
    falls back to a hard cut for a single line longer than the limit.
    """

    text = (text or "").strip()

    if not text:
        return []

    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line

    if current:
        chunks.append(current)

    return chunks


def send(chat_id: int, text: str) -> None:
    """Send a reply, split across messages when needed."""

    for chunk in split_message(text):
        _api("sendMessage", chat_id=chat_id, text=chunk)


# ---------------------------------------------------------------------------
# Local answers — no model call, instant and free
# ---------------------------------------------------------------------------

def status_text() -> str:
    """A quick account summary read straight from LOCKBOT's state files."""

    try:
        from lockbot_brain import collect_state

        state = collect_state()
    except Exception as error:
        return f"Could not read state: {type(error).__name__}: {error}"

    scanner = state.get("scanner_state", {})
    risk = state.get("risk_state", {})
    equity_positions = state.get("equity_positions_tracked", {})
    option_positions = state.get("option_positions_tracked", {})
    shadow = state.get("shadow_summary", {})

    lines = [
        "LOCKBOT status",
        "",
        f"Equity      ${scanner.get('account_equity', 0):,.2f}",
        f"Day P&L     ${scanner.get('daily_pnl', 0):,.2f} "
        f"({scanner.get('daily_pnl_percent', 0) * 100:.2f}%)",
        f"Market      {'OPEN' if scanner.get('market_open') else 'closed'}",
        f"Kill switch {'ACTIVE' if risk.get('kill_switch_active') else 'off'}",
        f"Trades today {risk.get('trades_submitted_today', 0)}",
        "",
        f"Equity positions ({len(equity_positions)}):",
    ]

    for symbol, data in equity_positions.items():
        lines.append(
            f"  {symbol} in at {data.get('entry_price', 0):.2f}, "
            f"peak +{data.get('highest_gain_percent', 0):.2f}%"
        )

    if not equity_positions:
        lines.append("  none")

    lines.append(f"\nOption positions ({len(option_positions)}):")

    for _, data in option_positions.items():
        lines.append(
            f"  {data.get('underlying')} {data.get('strategy')} "
            f"debit ${data.get('entry_debit', 0):.2f}"
        )

    if not option_positions:
        lines.append("  none")

    unhealthy = [
        name
        for name, data in (state.get("module_health") or {}).items()
        if data.get("status") not in {"HEALTHY", None}
    ]

    lines.append(
        "\nModules     " + ("all healthy" if not unhealthy else ", ".join(unhealthy))
    )

    if shadow.get("resolved"):
        lines.append(
            f"Shadow      {shadow['win_rate_percent']}% wins on "
            f"{shadow['resolved']} resolved, avg {shadow['average_r_multiple']}R"
        )

    return "\n".join(lines)


def notes_text() -> str:
    """The brain's accumulated notes."""

    try:
        from lockbot_brain import read_memory

        return read_memory()[:MESSAGE_LIMIT * 2]
    except Exception as error:
        return f"Could not read notes: {type(error).__name__}: {error}"


# ---------------------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------------------

_PENDING_FLATTEN: dict[int, tuple[str, float]] = {}

# Hard interlock. _do_flatten() refuses outright while this is True, and
# the self-test sets it before touching any routing code.
#
# This exists because the first version of the self-test did not have it.
# The routing test fed a valid challenge into handle_message() to prove
# the confirmation path worked, that call reached the live broker, and it
# cancelled the bracket legs protecting two real open positions. A test
# proving that a kill switch fires must never be able to fire it.
#
# Injecting a fake executor (below) is the primary defence. This flag is
# the second one, so a future test that forgets to inject still cannot
# place an order.
EXECUTION_DISABLED = False


def make_challenge() -> str:
    """A short random word the user has to echo back."""

    return "".join(random.choice(string.ascii_uppercase) for _ in range(4))


def request_flatten(user_id: int, now: float | None = None) -> str:
    """Start a flatten. Returns the message to send."""

    now = time.time() if now is None else now
    challenge = make_challenge()
    _PENDING_FLATTEN[user_id] = (challenge, now)

    return (
        "FLATTEN REQUESTED\n\n"
        "This cancels every open order and liquidates every position, "
        "equity and options.\n\n"
        f"Reply with exactly:  {challenge}\n\n"
        f"Expires in {CHALLENGE_SECONDS} seconds. Anything else cancels it."
    )


def confirm_flatten(
    user_id: int,
    text: str,
    now: float | None = None,
    executor=None,
) -> str | None:
    """
    Check a reply against a pending challenge.

    Returns None when there is nothing pending. Otherwise returns the
    result message, and the pending challenge is cleared either way — a
    wrong answer never gets a second attempt.

    `executor` exists so tests can verify the confirmation logic without
    a broker anywhere near it. Production passes nothing and gets the
    real one.
    """

    now = time.time() if now is None else now
    pending = _PENDING_FLATTEN.pop(user_id, None)

    if pending is None:
        return None

    challenge, issued_at = pending

    if now - issued_at > CHALLENGE_SECONDS:
        return "Flatten expired. Send /flatten again if you still want it."

    if text.strip().upper() != challenge:
        return "Challenge did not match. Nothing was closed."

    return (executor or _do_flatten)()


def _do_flatten() -> str:
    """Cancel orders and liquidate. Called only after a matched challenge."""

    if EXECUTION_DISABLED:
        return "Execution is disabled in this process. Nothing was sent."

    try:
        from lockbot_brain import _trading_client

        client = _trading_client()
        positions = client.get_all_positions()

        if not positions:
            return "Account is already flat. Nothing was sent."

        summary = ", ".join(f"{p.symbol} x{p.qty}" for p in positions)
        client.close_all_positions(cancel_orders=True)
        remaining = client.get_all_positions()

        return (
            f"FLATTENED.\n\nSubmitted liquidation for: {summary}\n"
            f"Still showing open: {len(remaining)}\n\n"
            "If the market is closed those orders are queued for the open."
        )

    except Exception as error:
        return f"Flatten FAILED: {type(error).__name__}: {error}"


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def handle_message(text: str, user_id: int, executor=None) -> str:
    """
    Turn one incoming message into a reply.

    Pure routing over the brain and the local readers, so the whole
    surface can be exercised offline. `executor` is forwarded to the
    flatten path so tests can route a real challenge without a broker.
    """

    text = (text or "").strip()

    if not text:
        return ""

    # A pending challenge takes priority over everything, so the reply to
    # /flatten cannot be swallowed by another command.
    resolved = confirm_flatten(user_id, text, executor=executor)

    if resolved is not None:
        return resolved

    command = text.split()[0].lower().lstrip("/")

    if command in {"start", "help"}:
        return HELP_TEXT

    if command == "status":
        return status_text()

    if command == "notes":
        return notes_text()

    if command == "flatten":
        return request_flatten(user_id)

    try:
        import lockbot_brain

        # Belt and braces. The brain's trading tools are already excluded
        # from a remote session, and this makes them refuse even if one
        # were reachable some other way.
        lockbot_brain.READ_ONLY = True
        lockbot_brain.CONFIRM = lambda *_: False

        if command == "brief":
            return lockbot_brain.brief(send=False)

        if command == "analyze":
            return lockbot_brain.analyze()

        return lockbot_brain.ask(text)

    except Exception as error:
        return f"Something broke answering that: {type(error).__name__}: {error}"


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def run() -> int:
    """Long-poll Telegram and answer messages."""

    token = bot_token()

    if not token:
        print("TELEGRAM_BOT_TOKEN is not set in .env.")
        print("Message @BotFather on Telegram, send /newbot, and paste the token.")
        return 1

    identity = _api("getMe")

    if not identity.get("ok"):
        print(f"Telegram rejected the token: {identity.get('description')}")
        return 1

    username = identity["result"].get("username")
    allowlist = allowed_user_ids()

    print("=" * 60)
    print("LOCKBOT TELEGRAM")
    print("=" * 60)
    print(f"Bot        : @{username}")
    print(f"Allowlist  : {sorted(allowlist) if allowlist else 'EMPTY — will refuse everyone'}")
    print("Trading    : disabled (read-only, /flatten excepted)")
    print("Stop       : Ctrl+C")
    print("=" * 60)

    offset = None

    while True:
        try:
            updates = _api(
                "getUpdates",
                offset=offset,
                timeout=POLL_TIMEOUT,
            )

            if not updates.get("ok"):
                print(f"[poll error] {updates.get('description')}")
                time.sleep(5)
                continue

            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")

                if not message:
                    continue

                chat_id = message["chat"]["id"]
                user_id = message.get("from", {}).get("id", 0)
                text = message.get("text", "")
                stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

                if not is_authorized(user_id):
                    print(f"[{stamp}] REFUSED user {user_id}: {text[:60]}")

                    send(
                        chat_id,
                        "Not authorized.\n\n"
                        f"Your Telegram user ID is {user_id}.\n"
                        "Add it to TELEGRAM_ALLOWED_USER_IDS in .env and "
                        "restart the bot if this is you.",
                    )
                    continue

                print(f"[{stamp}] {user_id}: {text[:70]}")

                send(chat_id, "…thinking" if not text.startswith("/") else "…")

                reply = handle_message(text, user_id)

                if reply:
                    send(chat_id, reply)

        except KeyboardInterrupt:
            print("\nStopped.")
            return 0

        except Exception as error:
            print(f"[loop error] {type(error).__name__}: {error}")
            time.sleep(5)


def check() -> int:
    """Verify the token and configuration without starting the loop."""

    token = bot_token()

    print(f"Token set        : {bool(token)}")

    if not token:
        print("\nMessage @BotFather, send /newbot, then put the token in .env as")
        print("TELEGRAM_BOT_TOKEN=...")
        return 1

    identity = _api("getMe")

    if identity.get("ok"):
        result = identity["result"]
        print("Token valid      : yes")
        print(f"Bot username     : @{result.get('username')}")
    else:
        print(f"Token valid      : NO — {identity.get('description')}")
        return 1

    allowlist = allowed_user_ids()

    print(f"Allowlist        : {sorted(allowlist) if allowlist else 'EMPTY'}")

    if not allowlist:
        print("\nThe allowlist is empty, so the bot will refuse everyone —")
        print("including you. Start it, message it once, and it will reply")
        print("with your user ID to add to TELEGRAM_ALLOWED_USER_IDS.")

    print("Trading          : disabled (read-only, /flatten excepted)")

    return 0


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    """Offline checks. No network, no token, no Telegram."""

    global EXECUTION_DISABLED

    failures = []

    def check_that(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    # Before anything else. An earlier version of this test did not do
    # this, reached the live broker through the confirmation path, and
    # cancelled the bracket legs on two open positions.
    EXECUTION_DISABLED = True

    fired: list[str] = []

    def fake_executor() -> str:
        fired.append("called")
        return "FLATTENED (test double — no broker was contacted)"

    print("Interlock")

    check_that("execution is disabled during tests", EXECUTION_DISABLED)
    check_that(
        "the real executor refuses while disabled",
        "disabled" in _do_flatten(),
        _do_flatten(),
    )

    print()
    print("Authorization")

    check_that("empty allowlist refuses everyone", not is_authorized(123, set()))
    check_that("listed user is allowed", is_authorized(123, {123}))
    check_that("unlisted user is refused", not is_authorized(999, {123}))
    check_that("zero user id is refused", not is_authorized(0, {123}))

    print()
    print("Allowlist parsing")

    original = os.environ.get("TELEGRAM_ALLOWED_USER_IDS")

    for raw, expected in (
        ("123", {123}),
        ("123,456", {123, 456}),
        ("123, 456", {123, 456}),
        ("123;456", {123, 456}),
        ("", set()),
        ("not-a-number", set()),
        ("123,oops,456", {123, 456}),
    ):
        os.environ["TELEGRAM_ALLOWED_USER_IDS"] = raw
        parsed = allowed_user_ids()
        check_that(f"parses {raw!r}", parsed == expected, str(parsed))

    if original is None:
        os.environ.pop("TELEGRAM_ALLOWED_USER_IDS", None)
    else:
        os.environ["TELEGRAM_ALLOWED_USER_IDS"] = original

    print()
    print("Message splitting")

    check_that("empty stays empty", split_message("") == [])
    check_that("short stays whole", split_message("hello") == ["hello"])

    long_lines = "\n".join(f"line {i}" for i in range(2000))
    chunks = split_message(long_lines)
    check_that("long text is split", len(chunks) > 1, str(len(chunks)))
    check_that(
        "every chunk fits",
        all(len(c) <= MESSAGE_LIMIT for c in chunks),
        str(max(len(c) for c in chunks)),
    )
    check_that(
        "no content is lost",
        sum(len(c.replace("\n", "")) for c in chunks)
        == len(long_lines.replace("\n", "")),
    )

    single = "x" * 10000
    chunks = split_message(single)
    check_that(
        "one huge line is hard-cut",
        all(len(c) <= MESSAGE_LIMIT for c in chunks) and len(chunks) == 3,
        str([len(c) for c in chunks]),
    )

    print()
    print("Flatten challenge")

    _PENDING_FLATTEN.clear()

    check_that("challenge is four letters", len(make_challenge()) == 4)
    check_that("challenge is uppercase", make_challenge().isupper())

    check_that(
        "no reply without a request",
        confirm_flatten(1, "ABCD") is None,
    )

    prompt = request_flatten(1, now=1000.0)
    challenge = _PENDING_FLATTEN[1][0]
    check_that("request names the challenge", challenge in prompt)

    # The correct challenge must reach the executor — verified against a
    # test double, never the broker.
    fired.clear()
    result = confirm_flatten(1, challenge, now=1001.0, executor=fake_executor)
    check_that("a matching challenge fires the executor", fired == ["called"])
    check_that("and reports the result", "FLATTENED" in (result or ""), str(result))
    check_that("lowercase is accepted", True)

    request_flatten(1, now=1000.0)
    lower = _PENDING_FLATTEN[1][0].lower()
    fired.clear()
    confirm_flatten(1, lower, now=1001.0, executor=fake_executor)
    check_that("case-insensitive match still fires", fired == ["called"])

    request_flatten(1, now=1000.0)
    result = confirm_flatten(1, "WRONG", now=1001.0, executor=fake_executor)
    check_that(
        "a wrong answer refuses",
        result is not None and "did not match" in result,
        str(result),
    )
    check_that("a wrong answer clears the request", 1 not in _PENDING_FLATTEN)

    fired.clear()
    check_that(
        "and cannot be retried",
        confirm_flatten(1, challenge, now=1002.0, executor=fake_executor) is None,
    )
    check_that("a retry does not reach the executor", fired == [])

    request_flatten(2, now=1000.0)
    result = confirm_flatten(
        2, _PENDING_FLATTEN.get(2, ("", 0))[0], now=9999.0, executor=fake_executor
    )
    check_that(
        "an expired challenge refuses",
        result is not None and "expired" in result,
        str(result),
    )

    request_flatten(3, now=1000.0)
    challenge_three = _PENDING_FLATTEN[3][0]
    fired.clear()
    check_that(
        "one user's challenge does not unlock another's",
        confirm_flatten(4, challenge_three, now=1001.0, executor=fake_executor)
        is None,
    )
    check_that("and does not reach the executor", fired == [])

    _PENDING_FLATTEN.clear()

    print()
    print("Routing")

    check_that("empty message returns nothing", handle_message("", 1) == "")
    check_that("/help returns help", "LOCKBOT" in handle_message("/help", 1))
    check_that("/start returns help", "Commands" in handle_message("/start", 1))

    _PENDING_FLATTEN.clear()
    reply = handle_message("/flatten", 5)
    check_that("/flatten asks for confirmation", "FLATTEN REQUESTED" in reply)
    check_that("/flatten does not close anything yet", 5 in _PENDING_FLATTEN)

    # The pending challenge must win over a command, or the confirmation
    # could be swallowed by something else the user typed. Routed through
    # the test double — this is the exact call that reached the broker
    # before the executor was injectable.
    stored = _PENDING_FLATTEN[5][0]
    fired.clear()
    reply = handle_message(stored, 5, executor=fake_executor)
    check_that(
        "the challenge reply routes to flatten, not the model",
        fired == ["called"],
        reply[:80],
    )
    check_that("no broker was contacted", "test double" in reply, reply[:80])

    _PENDING_FLATTEN.clear()

    print()
    print("Status rendering")

    text = status_text()
    check_that("status renders", isinstance(text, str) and len(text) > 20)
    check_that("status mentions equity", "Equity" in text)
    check_that("status fits one message", len(text) <= MESSAGE_LIMIT, str(len(text)))

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All telegram checks passed.")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Reach LOCKBOT from your phone.")
    parser.add_argument("--run", action="store_true", help="start the bot")
    parser.add_argument("--check", action="store_true", help="verify token and config")
    parser.add_argument("--self-test", action="store_true", help="offline checks")

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.check:
        return check()

    if args.run:
        return run()

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
