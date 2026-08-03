"""LOCKBOT smart notification service v0.2.

Provides Pushover delivery, duplicate suppression, cooldown protection,
persistent notification state, and explicit delivery-result statuses.

This module does not submit, modify, replace, or cancel broker orders.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY")
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN")

STATE_FILE = Path(__file__).with_name(
    "notification_state.json"
)


class NotificationStatus(str, Enum):
    """Possible outcomes from a notification attempt."""

    SENT = "SENT"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    SKIPPED_COOLDOWN = "SKIPPED_COOLDOWN"
    SKIPPED_MUTED = "SKIPPED_MUTED"
    FAILED = "FAILED"

    def __bool__(self) -> bool:
        """Preserve compatibility with older Boolean checks."""

        return self is NotificationStatus.SENT


def _config():
    """lockbot_config, imported lazily so a config error cannot stop alerts.

    Notifications are how LOCKBOT reports that something is wrong,
    including a dead options stop loss. A module-level import here would
    mean a broken config file silences the very channel that would have
    told you about it.
    """

    try:
        import lockbot_config

        return lockbot_config
    except Exception:
        return None


def _config_tuple(name: str) -> frozenset[str]:
    """Read an upper-cased set of event types from config.

    An unreadable or missing setting yields an empty set, so the failure
    mode is "everything still notifies" rather than silence.
    """

    module = _config()

    if module is None:
        return frozenset()

    values = getattr(module, name, ()) or ()

    try:
        return frozenset(str(value).upper().strip() for value in values)
    except TypeError:
        return frozenset()


def load_notification_state() -> dict:
    """Load LOCKBOT's notification history."""

    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open(
            mode="r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if isinstance(data, dict):
            return data

        print(
            "Notification state was invalid. "
            "Starting with empty state."
        )

        return {}

    except (json.JSONDecodeError, OSError) as error:
        print(
            "Could not read notification state: "
            f"{error}"
        )

        return {}


def save_notification_state(
    state: dict,
) -> bool:
    """Safely save LOCKBOT's notification history."""

    temporary_file = STATE_FILE.with_suffix(
        ".tmp"
    )

    try:
        with temporary_file.open(
            mode="w",
            encoding="utf-8",
        ) as file:
            json.dump(
                state,
                file,
                indent=4,
            )

        temporary_file.replace(
            STATE_FILE
        )

        return True

    except OSError as error:
        print(
            "Could not save notification state: "
            f"{error}"
        )

        return False


def create_notification_signature(
    event_type: str,
    reason: str,
    message: str,
) -> str:
    """Create a unique fingerprint for a notification."""

    signature_text = (
        f"{event_type}|{reason}|{message}"
    )

    return hashlib.sha256(
        signature_text.encode("utf-8")
    ).hexdigest()


def send_notification(
    title: str,
    message: str,
) -> bool:
    """Send a standard Pushover notification."""

    if (
        not PUSHOVER_USER_KEY
        or not PUSHOVER_APP_TOKEN
    ):
        print(
            "Pushover notification skipped: "
            "credentials are missing."
        )

        return False

    payload = urllib.parse.urlencode(
        {
            "token": PUSHOVER_APP_TOKEN,
            "user": PUSHOVER_USER_KEY,
            "title": title,
            "message": message,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=payload,
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=10,
        ) as response:
            response_status = response.status

        success = response_status == 200

        if success:
            print(
                "Pushover notification sent."
            )
        else:
            print(
                "Pushover returned status: "
                f"{response_status}"
            )

        return success

    except Exception as error:
        print(
            "Pushover notification failed: "
            f"{type(error).__name__}: {error}"
        )

        return False


def send_smart_notification(
    symbol: str,
    event_type: str,
    title: str,
    message: str,
    reason: str = "",
    force: bool = False,
    cooldown_minutes: int = 0,
) -> NotificationStatus:
    """Send an alert only when the event is new or changed."""

    normalized_symbol = (
        symbol.upper().strip()
    )

    normalized_event_type = (
        event_type.upper().strip()
    )

    normalized_reason = (
        reason.upper().strip()
    )

    # ---- muted types never reach the phone
    #
    # `force` does not override this. A caller that hardcodes force=True
    # would otherwise be able to ignore the user's own mute list, which
    # defeats the point of having one.
    muted = _config_tuple("NOTIFY_MUTED_EVENT_TYPES")

    if normalized_event_type in muted:
        print(
            "Notification muted by config: "
            f"{normalized_symbol} {normalized_event_type}"
        )

        return NotificationStatus.SKIPPED_MUTED

    # ---- ongoing conditions repeat; events do not
    #
    # The watchdog embeds a live age in its text, so its signature
    # changes on every run and duplicate suppression never catches it.
    # Applying a cooldown to these types throttles the repeat without
    # delaying the first alert, which still goes out immediately.
    if cooldown_minutes <= 0:
        throttled = _config_tuple("NOTIFY_THROTTLED_EVENT_TYPES")

        if normalized_event_type in throttled:
            cooldown_minutes = getattr(
                _config(), "NOTIFY_REPEAT_COOLDOWN_MINUTES", 0
            ) or 0

    state_key = (
        f"{normalized_symbol}:"
        f"{normalized_event_type}"
    )

    signature = create_notification_signature(
        event_type=normalized_event_type,
        reason=normalized_reason,
        message=message,
    )

    state = load_notification_state()

    previous_event = state.get(
        state_key,
        {},
    )

    previous_signature = previous_event.get(
        "signature"
    )

    previous_sent_at = previous_event.get(
        "sent_at_utc"
    )

    if (
        not force
        and previous_signature == signature
    ):
        print(
            "Duplicate notification skipped: "
            f"{normalized_symbol} "
            f"{normalized_event_type}"
        )

        return (
            NotificationStatus.SKIPPED_DUPLICATE
        )

    if (
        not force
        and cooldown_minutes > 0
        and previous_sent_at
    ):
        try:
            previous_time = datetime.fromisoformat(
                previous_sent_at
            )

            elapsed_minutes = (
                datetime.now(timezone.utc)
                - previous_time
            ).total_seconds() / 60

            if elapsed_minutes < cooldown_minutes:
                remaining_minutes = (
                    cooldown_minutes
                    - elapsed_minutes
                )

                print(
                    "Notification cooldown active: "
                    f"{normalized_symbol} "
                    f"{normalized_event_type} "
                    f"({remaining_minutes:.1f} "
                    "minutes remaining)"
                )

                return (
                    NotificationStatus
                    .SKIPPED_COOLDOWN
                )

        except ValueError:
            print(
                "Saved notification time was "
                "invalid. Cooldown check skipped."
            )

    notification_sent = send_notification(
        title=title,
        message=message,
    )

    if not notification_sent:
        return NotificationStatus.FAILED

    state[state_key] = {
        "symbol": normalized_symbol,
        "event_type": normalized_event_type,
        "reason": normalized_reason,
        "signature": signature,
        "title": title,
        "message": message,
        "sent_at_utc": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
    }

    state_saved = save_notification_state(
        state
    )

    if not state_saved:
        print(
            "Warning: notification was sent, "
            "but its duplicate-protection state "
            "could not be saved."
        )

    return NotificationStatus.SENT


def clear_notification_state(
    symbol: str | None = None,
    event_type: str | None = None,
) -> bool:
    """Clear some or all saved notification memory."""

    state = load_notification_state()

    if (
        symbol is None
        and event_type is None
    ):
        return save_notification_state({})

    normalized_symbol = (
        symbol.upper().strip()
        if symbol
        else None
    )

    normalized_event_type = (
        event_type.upper().strip()
        if event_type
        else None
    )

    keys_to_remove: list[str] = []

    for state_key, event_data in state.items():
        saved_symbol = event_data.get(
            "symbol"
        )

        saved_event_type = event_data.get(
            "event_type"
        )

        symbol_matches = (
            normalized_symbol is None
            or saved_symbol
            == normalized_symbol
        )

        event_matches = (
            normalized_event_type is None
            or saved_event_type
            == normalized_event_type
        )

        if symbol_matches and event_matches:
            keys_to_remove.append(
                state_key
            )

    for state_key in keys_to_remove:
        state.pop(
            state_key,
            None,
        )

    return save_notification_state(
        state
    )


def main() -> None:
    """Verify that the module imports correctly."""

    print("=" * 50)
    print(
        "      LOCKBOT NOTIFICATIONS v0.2"
    )
    print("=" * 50)
    print(
        "Pushover delivery     : READY"
    )
    print(
        "Duplicate protection  : READY"
    )
    print(
        "Cooldown protection   : READY"
    )
    print(
        "Explicit status output: READY"
    )
    print(
        "Status                : READY"
    )


def _self_test() -> int:
    """Offline checks for the filtering added on 2026-08-02.

    This module decides what reaches the phone, so both failure modes are
    dangerous and they pull in opposite directions. Too loud and the real
    alert is lost in noise -- the watchdog embeds a live age in its text,
    which changed its signature every run and defeated duplicate
    suppression entirely, producing four pushes an hour for one unchanged
    problem. Too quiet and a dead options stop loss goes unreported.

    So these checks assert both directions: muted types never send, and
    genuine first-time alerts always do.

    Delivery is stubbed. An earlier version of this test patched a
    function name that did not exist, the stub silently did nothing, and
    five real notifications were sent. The stub is now asserted before
    anything is dispatched.
    """

    import tempfile

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    global STATE_FILE, send_notification

    real_state_file = STATE_FILE
    real_send = send_notification
    sent: list = []

    STATE_FILE = Path(tempfile.mkdtemp()) / "notification_state.json"
    send_notification = lambda *a, **k: sent.append(k or a) or True  # noqa: E731

    if send_notification is real_send:
        print("  FAIL  stub did not apply; refusing to send real pushes")
        return 1

    try:
        print("Muting")

        result = send_smart_notification(
            symbol="SYSTEM", event_type="SYSTEM_TEST",
            title="t", message="m",
        )
        check("a muted type does not send",
              result == NotificationStatus.SKIPPED_MUTED, str(result))
        check("and nothing was dispatched", not sent)

        result = send_smart_notification(
            symbol="SYSTEM", event_type="SYSTEM_TEST",
            title="t", message="m", force=True,
        )
        check("force cannot override the user's mute list",
              result == NotificationStatus.SKIPPED_MUTED, str(result))

        print()
        print("Emergencies are never delayed")

        result = send_smart_notification(
            symbol="SYSTEM", event_type="WATCHDOG_ALERT", title="a",
            message="Heartbeat file is 62.3 minutes old",
            reason="WATCHDOG_CHECK_FAILED",
        )
        check("the first watchdog alert sends immediately",
              result == NotificationStatus.SENT, str(result))
        check("and was dispatched", len(sent) == 1, str(len(sent)))

        # The exact noise mechanism: same problem, moving number.
        result = send_smart_notification(
            symbol="SYSTEM", event_type="WATCHDOG_ALERT", title="a",
            message="Heartbeat file is 77.8 minutes old",
            reason="WATCHDOG_CHECK_FAILED",
        )
        check("a repeat with a changed number is throttled",
              result == NotificationStatus.SKIPPED_COOLDOWN, str(result))
        check("and was not dispatched", len(sent) == 1, str(len(sent)))

        print()
        print("Discrete events are never throttled")

        before = len(sent)

        for symbol in ("EWZ", "PCG", "PBR"):
            result = send_smart_notification(
                symbol=symbol, event_type="TRADE_COMPLETED",
                title="closed", message=f"{symbol} closed",
            )
            check(f"{symbol} trade close sends",
                  result == NotificationStatus.SENT, str(result))

        result = send_smart_notification(
            symbol="EWZ", event_type="OPTIONS_ORDER_SUBMITTED",
            title="entry", message="bought a call",
        )
        check("an options entry sends",
              result == NotificationStatus.SENT, str(result))
        check("all four were dispatched",
              len(sent) - before == 4, str(len(sent) - before))

        print()
        print("A broken config must not cause silence")

        module = _config()
        real_muted = getattr(module, "NOTIFY_MUTED_EVENT_TYPES", ())

        try:
            module.NOTIFY_MUTED_EVENT_TYPES = None
            result = send_smart_notification(
                symbol="Q", event_type="WATCHDOG_ALERT", title="t",
                message="a message never used elsewhere", reason="R",
            )
            check("an unreadable mute list still lets alerts through",
                  result != NotificationStatus.SKIPPED_MUTED, str(result))
        finally:
            module.NOTIFY_MUTED_EVENT_TYPES = real_muted

    finally:
        STATE_FILE = real_state_file
        send_notification = real_send

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All notification checks passed.")
    return 0


if __name__ == "__main__":
    import sys

    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    main()