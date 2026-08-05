"""
runtime_settings.py — settings LOCKBOT may change while it is running.

WHY THIS EXISTS

Remote control needs a way to say "stop trading options" or "tighten the
spread gate" from a phone. The obvious implementation — let the assistant
edit lockbot_config.py — is the wrong one. It puts a language model in a
position to rewrite the file every risk control is read from, on a
machine holding live broker credentials, driven by a Telegram bot token
that is a bearer credential anyone could hold.

So nothing edits code. Changes land in a small JSON file that
lockbot_config.py applies over its own defaults at import, and only for
names on the allowlist below. Every component is spawned fresh each cycle,
so a change takes effect on the next cycle with no restart.

WHAT IS DELIBERATELY NOT CHANGEABLE

PAPER_TRADING and LIVE_TRADING_ENABLED are absent, permanently. They are
the boundary between fake money and real money, and no remote channel
should be able to cross it -- not because the assistant would, but
because a leaked Telegram token must not be able to either. Moving to
live trading should require someone at the keyboard editing the file.

Credentials are absent for the same reason. So is anything that would let
this file name an arbitrary attribute.

BOUNDS, NOT JUST TYPES

Each setting carries a range. "Set the stop to 900%" is a typo, not an
instruction, and a config that accepts it would pass validation and then
never stop out. The bounds are what make a natural-language channel safe
to point at a risk system.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

OVERRIDES_FILE = Path(__file__).resolve().parent / "runtime_overrides.json"
AUDIT_FILE = Path(__file__).resolve().parent / "runtime_overrides.log"


# name -> (type, minimum, maximum, description)
#
# Ranges are deliberately tight. Widening one is a code change, which is
# the point: it needs a person, a diff and a reason.
ALLOWED: dict[str, tuple] = {
    "OPTIONS_ENABLED": (
        bool, None, None, "Whether options trade at all."),
    "EQUITY_ENTRIES_ENABLED": (
        bool, None, None, "Whether share orders are submitted."),
    "OPTIONS_SHADOW_MODE": (
        bool, None, None, "Log option decisions without sending orders."),

    "OPTIONS_MAX_OPEN_POSITIONS": (
        int, 0, 5, "Option positions held at once."),
    "OPTIONS_MAX_TRADES_PER_DAY": (
        int, 0, 10, "Option entries opened per session."),
    "OPTIONS_STOP_CONFIRM_CYCLES": (
        int, 1, 5, "Cycles a stop must hold before it fires."),

    "OPTIONS_MAX_SPREAD_PERCENT": (
        float, 0.01, 0.25, "Widest quoted spread an entry may pay."),
    "OPTIONS_MAX_RISK_PER_TRADE_PERCENT": (
        float, 0.01, 0.15, "Risk budget per option trade."),
    "OPTIONS_TAKE_PROFIT_PERCENT": (
        float, 0.10, 2.00, "Profit target, as a fraction of premium."),
    "OPTIONS_STOP_LOSS_PERCENT": (
        float, 0.10, 0.90, "Stop, as a fraction of premium."),
    "MAX_DAILY_LOSS_PERCENT": (
        float, 0.005, 0.10, "Daily loss budget before entries stop."),

    # Added 2026-08-04 alongside the cost gate. Registered here because
    # they are exactly the kind of threshold worth tuning from evidence,
    # and a setting the learning loop can recommend but nothing can apply
    # is a dead end -- which is how this omission was found.
    "OPTIONS_MAX_IV_PREMIUM": (
        float, 1.00, 3.00,
        "Implied vol as a multiple of realised. 1.0 is fairly priced."),
    "OPTIONS_MAX_DAILY_THETA": (
        float, 0.010, 0.100,
        "Time decay per day as a fraction of premium."),

    # Added 2026-08-04 with the event-risk gate. Registered at the same
    # time as the gate itself rather than afterwards -- the IV settings
    # above were added without it and the omission was only found later
    # by the recommendation tests.
    "OPTIONS_EVENT_RISK_ENABLED": (
        bool, None, None,
        "Refuse entries when an event is priced in before expiry."),
    "OPTIONS_MAX_TERM_INVERSION": (
        float, 1.00, 2.00,
        "Near-dated IV as a multiple of far-dated. Above 1.0 is inverted."),
}


def load_overrides(path: Path | None = None) -> dict[str, Any]:
    """Read the overrides, dropping anything not allowlisted.

    An unreadable file yields no overrides rather than raising. This is
    imported by lockbot_config, so a syntax error here would stop every
    component from starting -- including the exit engine.
    """

    source = Path(path or OVERRIDES_FILE)

    if not source.exists():
        return {}

    try:
        raw = json.loads(source.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(raw, dict):
        return {}

    clean: dict[str, Any] = {}

    for name, value in raw.items():
        ok, _ = validate(name, value)

        if ok:
            clean[name] = coerce(name, value)

    return clean


def coerce(name: str, value: Any) -> Any:
    """Cast a value to the type the setting expects."""

    kind = ALLOWED[name][0]

    if kind is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "on", "1"}
        return bool(value)

    return kind(value)


def validate(name: str, value: Any) -> tuple[bool, str]:
    """Whether this setting may be set to this value. Returns (ok, why)."""

    if name not in ALLOWED:
        return False, (
            f"{name} is not remotely changeable. Allowed: "
            + ", ".join(sorted(ALLOWED))
        )

    kind, low, high, _ = ALLOWED[name]

    try:
        cast = coerce(name, value)
    except (TypeError, ValueError):
        return False, f"{name} expects {kind.__name__}, got {value!r}."

    if kind is bool:
        return True, ""

    if low is not None and cast < low:
        return False, f"{name} must be at least {low}. Got {cast}."

    if high is not None and cast > high:
        return False, (
            f"{name} must be at most {high}. Got {cast}. Raising the "
            "ceiling is a code change, on purpose."
        )

    return True, ""


def set_override(name: str, value: Any, *, who: str = "unknown") -> tuple[bool, str]:
    """Record a setting change. Returns (ok, message)."""

    ok, why = validate(name, value)

    if not ok:
        return False, why

    cast = coerce(name, value)

    current = load_overrides()
    previous = current.get(name, "(default)")
    current[name] = cast

    OVERRIDES_FILE.write_text(json.dumps(current, indent=4), encoding="utf-8")

    # Append-only audit. A remote channel that can change risk settings
    # must leave a trail that is not itself remotely editable.
    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {who}  "
                f"{name}: {previous} -> {cast}\n"
            )
    except OSError:
        pass

    return True, (
        f"{name} set to {cast} (was {previous}). Takes effect next cycle; "
        "components are spawned fresh."
    )


def clear_override(name: str, *, who: str = "unknown") -> tuple[bool, str]:
    """Drop an override so the value in lockbot_config.py applies again."""

    current = load_overrides()

    if name not in current:
        return False, f"{name} has no override; it is already at its default."

    previous = current.pop(name)
    OVERRIDES_FILE.write_text(json.dumps(current, indent=4), encoding="utf-8")

    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {who}  "
                f"{name}: {previous} -> cleared\n"
            )
    except OSError:
        pass

    return True, f"{name} override cleared (was {previous})."


def describe() -> str:
    """What can be changed, and what is currently overridden."""

    active = load_overrides()
    lines = ["Remotely changeable settings:"]

    for name in sorted(ALLOWED):
        kind, low, high, note = ALLOWED[name]
        bounds = "true/false" if kind is bool else f"{low} to {high}"
        mark = f"  [OVERRIDDEN -> {active[name]}]" if name in active else ""
        lines.append(f"  {name}  ({bounds}){mark}")
        lines.append(f"      {note}")

    lines.append("")
    lines.append(
        "PAPER_TRADING and LIVE_TRADING_ENABLED are not on this list and "
        "cannot be changed remotely, by design."
    )

    return "\n".join(lines)


def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    print("The boundary that must never move")

    for forbidden in ("PAPER_TRADING", "LIVE_TRADING_ENABLED",
                      "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
                      "TELEGRAM_BOT_TOKEN"):
        ok, why = validate(forbidden, True)
        check(f"{forbidden} is refused", ok is False, why)

    check(
        "and it is not merely absent from the file but from the allowlist",
        "PAPER_TRADING" not in ALLOWED and "LIVE_TRADING_ENABLED" not in ALLOWED,
    )

    print()
    print("Bounds reject typos, not just wrong types")

    ok, why = validate("OPTIONS_STOP_LOSS_PERCENT", 9.0)
    check("a 900% stop is refused", ok is False, why)
    ok, _ = validate("OPTIONS_STOP_LOSS_PERCENT", 0.35)
    check("a sane stop is accepted", ok is True)
    ok, why = validate("OPTIONS_MAX_OPEN_POSITIONS", 500)
    check("500 positions is refused", ok is False, why)
    ok, why = validate("MAX_DAILY_LOSS_PERCENT", 0.90)
    check("a 90% daily loss budget is refused", ok is False, why)
    ok, _ = validate("OPTIONS_MAX_SPREAD_PERCENT", 0.05)
    check("a sane spread gate is accepted", ok is True)
    ok, why = validate("NOT_A_SETTING", 1)
    check("an unknown name is refused", ok is False, why)
    check("and the error lists what IS allowed", "Allowed:" in why)

    print()
    print("Round trip")

    global OVERRIDES_FILE, AUDIT_FILE
    real_o, real_a = OVERRIDES_FILE, AUDIT_FILE
    folder = Path(tempfile.mkdtemp())
    OVERRIDES_FILE = folder / "runtime_overrides.json"
    AUDIT_FILE = folder / "runtime_overrides.log"

    try:
        ok, message = set_override("OPTIONS_ENABLED", False, who="test")
        check("a setting can be set", ok is True, message)
        check("and reads back", load_overrides().get("OPTIONS_ENABLED") is False)
        check("the audit records who and what",
              "test" in AUDIT_FILE.read_text(encoding="utf-8"))

        ok, _ = set_override("OPTIONS_MAX_SPREAD_PERCENT", 0.08, who="test")
        check("floats survive the round trip",
              abs(load_overrides()["OPTIONS_MAX_SPREAD_PERCENT"] - 0.08) < 1e-9)

        ok, _ = clear_override("OPTIONS_ENABLED", who="test")
        check("an override can be cleared",
              "OPTIONS_ENABLED" not in load_overrides())

        # A hand-edited file containing something forbidden must not apply.
        OVERRIDES_FILE.write_text(
            json.dumps({"LIVE_TRADING_ENABLED": True,
                        "OPTIONS_MAX_SPREAD_PERCENT": 0.06}),
            encoding="utf-8",
        )
        loaded = load_overrides()
        check("a forbidden name in the file is ignored",
              "LIVE_TRADING_ENABLED" not in loaded, str(loaded))
        check("while valid neighbours still apply",
              "OPTIONS_MAX_SPREAD_PERCENT" in loaded)

        OVERRIDES_FILE.write_text("{ not json", encoding="utf-8")
        check("a corrupt file yields no overrides", load_overrides() == {})

        OVERRIDES_FILE.unlink()
        check("a missing file yields no overrides", load_overrides() == {})

    finally:
        OVERRIDES_FILE, AUDIT_FILE = real_o, real_a

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All runtime-settings checks passed.")
    return 0


if __name__ == "__main__":
    import sys

    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(describe())
