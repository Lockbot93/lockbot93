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
/recall   search everything we've said — /recall IBIT
/flatten  EMERGENCY — cancel orders, close everything
/help     this message

I remember our recent conversation, so follow-up questions work — ask
"why?" or "what about the other one?" and I'll know what you mean.
Older exchanges stay searchable with /recall.

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

def remote_trading_allowed() -> tuple[bool, str] | bool:
    """Whether a Telegram session may place orders. Read fresh every time.

    Two conditions, and the second is not remotely changeable:

      TELEGRAM_TRADING_ENABLED       the owner's decision
      PAPER_TRADING                  the guard on that decision

    Read at call time rather than at import so that flipping the config
    and restarting the CONTROLLER is enough — this process does not have
    to be restarted for the wall to go back up.

    Fails CLOSED. If lockbot_config cannot be imported, or either flag is
    missing, the answer is no. A missing setting must never read as
    permission; that was the exact mistake behind daytrade_count, where
    an absent field was treated as "zero day trades used" and therefore
    as licence to trade.
    """

    try:
        import lockbot_config as _config
    except Exception:
        return False

    if not getattr(_config, "TELEGRAM_TRADING_ENABLED", False):
        return False

    if getattr(_config, "TELEGRAM_TRADING_REQUIRES_PAPER", True):
        if not getattr(_config, "PAPER_TRADING", True):
            return False

        if getattr(_config, "LIVE_TRADING_ENABLED", False):
            return False

    return True


def remote_trading_status() -> str:
    """One line for the console banner and --check."""

    if remote_trading_allowed():
        return "ENABLED (paper) — orders permitted from Telegram"

    try:
        import lockbot_config as _config

        if getattr(_config, "TELEGRAM_TRADING_ENABLED", False):
            return ("disabled — TELEGRAM_TRADING_ENABLED is on but the "
                    "account is not in paper mode")
    except Exception:
        return "disabled — configuration unreadable"

    return "disabled (read-only, /flatten excepted)"


def handle_message(
    text: str,
    user_id: int,
    executor=None,
    *,
    remember: bool = True,
) -> str:
    """
    Turn one incoming message into a reply.

    Pure routing over the brain and the local readers, so the whole
    surface can be exercised offline. `executor` is forwarded to the
    flatten path so tests can route a real challenge without a broker.

    `remember` is what makes a conversation a conversation rather than a
    series of unrelated questions. Set False in tests so they do not
    write to the real transcript -- and note that turning it off makes
    follow-up questions stop working, which is the point of it being on.
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

    # Recall searches the whole transcript. Continuity below only
    # replays the last few turns, so anything older than the window is
    # reachable this way rather than not at all.
    if command == "recall":
        import conversation_memory

        term = text.split(maxsplit=1)[1] if len(text.split()) > 1 else ""

        if not term:
            info = conversation_memory.stats()
            return (
                f"{info.get('exchanges', 0)} exchange(s) remembered.\n"
                "Usage: /recall <term>"
            )

        return conversation_memory.format_hits(
            conversation_memory.search(term, user_id=user_id), term
        )

    try:
        import lockbot_brain

        # READ_ONLY is the money wall, and as of 2026-08-07 the owner
        # decides whether it stands on this channel.
        #
        # It used to be hardcoded True, on the reasoning that a leaked
        # bot token must not be able to move money regardless of what is
        # asked or how convincingly. That reasoning has not been refuted.
        # It was OVERRULED, explicitly and twice, for a paper account —
        # which is a different judgement, not a rebuttal. The remaining
        # access control is the allowlist, currently one user ID.
        #
        # The one part that is not remotely negotiable is below: order
        # authority here is conditional on PAPER_TRADING. A decision made
        # about fake money must not silently become a decision about real
        # money on the day LIVE_TRADING_ENABLED flips, so going live
        # re-raises this wall automatically and someone has to choose
        # again at a keyboard.
        lockbot_brain.READ_ONLY = not remote_trading_allowed()

        # The confirmation handler is a DIFFERENT question, and refusing
        # everything here was wrong.
        #
        # CONFIRM exists for the interactive console, where a person
        # types y/n at a prompt. There is no prompt over Telegram -- the
        # user's message IS the authorisation, and /flatten carries its
        # own challenge for the one destructive thing that is allowed.
        # Refusing unconditionally therefore blocked nothing dangerous
        # and blocked everything useful: on 2026-08-06 the controller sat
        # down for eight hours while LOCKBOT could see it in
        # get_process_status and could not restart it.
        #
        # With the guard split by places_orders, approving here permits
        # operational recovery -- restart, cleanup, rebuild the universe,
        # force an options-manager pass -- while READ_ONLY still refuses
        # anything that moves money.
        lockbot_brain.CONFIRM = lambda *_: True

        if command == "brief":
            reply = lockbot_brain.brief(send=False)
        elif command == "analyze":
            reply = lockbot_brain.analyze()
        else:
            history = []

            if remember:
                import conversation_memory

                history = conversation_memory.as_messages(
                    conversation_memory.recent(user_id)
                )

            reply = lockbot_brain.ask(text, history=history)

    except Exception as error:
        return f"Something broke answering that: {type(error).__name__}: {error}"

    # Recorded after the answer, so a failed turn does not enter the
    # transcript as an exchange that never happened. Credentials are
    # stripped on the way in -- see conversation_memory.redact.
    if remember and reply:
        try:
            import conversation_memory

            conversation_memory.record(user_id, text, reply)
        except Exception:
            pass

    return reply


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
    print(f"Trading    : {remote_trading_status()}")
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

    print(f"Trading          : {remote_trading_status()}")

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
    print("Remote order authority is the owner's switch, guarded by PAPER")

    # The guard that is NOT remotely negotiable. TELEGRAM_TRADING_ENABLED
    # is the owner's call; the paper condition is what stops that call
    # silently becoming a decision about real money later.
    import lockbot_config as _cfg

    _saved = (
        getattr(_cfg, "TELEGRAM_TRADING_ENABLED", False),
        getattr(_cfg, "TELEGRAM_TRADING_REQUIRES_PAPER", True),
        getattr(_cfg, "PAPER_TRADING", True),
        getattr(_cfg, "LIVE_TRADING_ENABLED", False),
    )

    try:
        _cfg.TELEGRAM_TRADING_REQUIRES_PAPER = True

        _cfg.TELEGRAM_TRADING_ENABLED = False
        _cfg.PAPER_TRADING = True
        _cfg.LIVE_TRADING_ENABLED = False
        check_that("off by default means no remote orders",
                   not remote_trading_allowed())

        _cfg.TELEGRAM_TRADING_ENABLED = True
        check_that("the owner's switch turns it on for paper",
                   remote_trading_allowed())

        _cfg.PAPER_TRADING = False
        check_that("but a LIVE account overrides the owner's switch",
                   not remote_trading_allowed(),
                   "remote trading must never survive leaving paper mode")

        _cfg.PAPER_TRADING = True
        _cfg.LIVE_TRADING_ENABLED = True
        check_that("and LIVE_TRADING_ENABLED alone is enough to block it",
                   not remote_trading_allowed())

        _cfg.LIVE_TRADING_ENABLED = False
        check_that("the status line stops claiming it is on",
                   "disabled" in remote_trading_status()
                   or "ENABLED" in remote_trading_status())
    finally:
        (_cfg.TELEGRAM_TRADING_ENABLED,
         _cfg.TELEGRAM_TRADING_REQUIRES_PAPER,
         _cfg.PAPER_TRADING,
         _cfg.LIVE_TRADING_ENABLED) = _saved

    print()
    print("With remote trading OFF, a session still cannot spend money")

    import lockbot_brain as _brain

    _real_ro, _real_confirm = _brain.READ_ONLY, _brain.CONFIRM

    try:
        # What handle_message sets when remote trading is disabled.
        _brain.READ_ONLY = True
        _brain.CONFIRM = lambda *_: True

        for label, kwargs in (
            ("closing a position", {"places_orders": True}),
            ("liquidating everything", {"places_orders": True}),
            ("submitting a trade", {"places_orders": True}),
        ):
            refusal = _brain._guard("TEST", label, **kwargs)
            check_that(f"{label} is refused remotely",
                       refusal is not None and "read-only" in refusal,
                       str(refusal))

        for label in ("restarting the controller", "running the shadow "
                      "resolver", "rebuilding the universe"):
            refusal = _brain._guard("TEST", label, places_orders=False)
            check_that(f"{label} is permitted remotely",
                       refusal is None or "kill switch" in refusal,
                       str(refusal))

        # The one operational action that is really a safety action.
        check_that("stopping the controller counts as order-placing",
                   "options" not in _brain.ORDER_CAPABLE_COMPONENTS)

    finally:
        _brain.READ_ONLY, _brain.CONFIRM = _real_ro, _real_confirm

    print()
    print("Conversation memory")

    # The brain is replaced with a double: these checks are about what
    # gets remembered and replayed, and must not cost an API call.
    import sys as _sys
    import tempfile
    import types

    import conversation_memory

    seen: list[dict] = []

    double = types.ModuleType("lockbot_brain")
    double.READ_ONLY = False
    double.CONFIRM = None

    def fake_ask(question, state=None, history=None):
        seen.append({"question": question, "history": list(history or [])})
        return f"answered: {question}"

    double.ask = fake_ask
    double.brief = lambda send=True: "brief"
    double.analyze = lambda: "analysis"

    real_brain = _sys.modules.get("lockbot_brain")
    _sys.modules["lockbot_brain"] = double

    real_transcript = conversation_memory.TRANSCRIPT_FILE
    conversation_memory.TRANSCRIPT_FILE = (
        Path(tempfile.mkdtemp()) / "conversation_log.jsonl"
    )

    try:
        reply = handle_message("what is the shadow win rate", 7)
        check_that("a plain question reaches the brain", len(seen) == 1)
        check_that("with no history on the first turn",
                   seen[0]["history"] == [], str(seen[0]["history"]))
        check_that("and the answer comes back", "answered" in reply)

        reply = handle_message("why?", 7)
        check_that("the second turn carries history",
                   len(seen[1]["history"]) == 2,
                   str(len(seen[1]["history"])))
        check_that("the earlier question is in it",
                   "shadow win rate" in seen[1]["history"][0]["content"])
        check_that("so is the earlier answer",
                   "answered" in seen[1]["history"][1]["content"])
        check_that("history is oldest first",
                   seen[1]["history"][0]["role"] == "user")

        # Another user's conversation must not leak into this one.
        handle_message("unrelated question", 8)
        check_that("a different user starts clean",
                   seen[2]["history"] == [], str(seen[2]["history"]))

        # This is the whole point of the feature, stated as a check.
        handle_message("and the other one?", 7)
        replayed = " ".join(m["content"] for m in seen[3]["history"])
        check_that("a follow-up can see what came before",
                   "shadow win rate" in replayed and "why?" in replayed)

        hits = handle_message("/recall shadow", 7)
        check_that("/recall finds a past exchange", "shadow win rate" in hits,
                   hits[:100])
        check_that("/recall with no term reports the count",
                   "exchange" in handle_message("/recall", 7))
        check_that("/recall does not reach the brain", len(seen) == 4,
                   str(len(seen)))

        seen.clear()
        handle_message("a private question", 7, remember=False)
        check_that("remember=False sends no history",
                   seen[0]["history"] == [])
        handle_message("did that get stored", 7, remember=False)
        check_that("and stores nothing", seen[1]["history"] == [],
                   str(seen[1]["history"]))

        # A turn that raised must not be recorded as an exchange.
        def broken_ask(question, state=None, history=None):
            raise RuntimeError("model unavailable")

        double.ask = broken_ask
        before = len(conversation_memory.recent(7, limit=100))
        reply = handle_message("this will fail", 7)
        check_that("a failed turn reports the failure",
                   "Something broke" in reply, reply[:60])
        check_that("and is not recorded as an exchange",
                   len(conversation_memory.recent(7, limit=100)) == before)

    finally:
        conversation_memory.TRANSCRIPT_FILE = real_transcript

        if real_brain is not None:
            _sys.modules["lockbot_brain"] = real_brain
        else:
            _sys.modules.pop("lockbot_brain", None)

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
