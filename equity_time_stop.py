"""The time-based exit for equity positions. Owns it alone.

WHY THIS MODULE EXISTS AT ALL

Owner directive 2026-08-07: LOCKBOT must be able to take day trades as
well as swings and overnight holds (agent_channel a002c6a0). A day trade
is defined by being flat at the close, and nothing in LOCKBOT could make
that happen — the broker-side bracket exits on price, never on time.

So a horizon needs a second exit path, and a second exit path is exactly
what invariant 3 of CLAUDE.md forbids: one exit mechanism per instrument,
one owner each. position_monitor.py once ran its own exits at tighter
thresholds than the bracket, two mechanisms raced on one position, and it
was cut back to monitoring permanently.

That invariant is not weakened here, it is honoured the same way the
options side honours it. options_manager.py is the sole options exit
because options have no broker stop; this module is the sole equity TIME
exit because brackets have no clock. One rule, one owner, one file. It is
the only module that may close an equity position, trade_manager keeps
its "does not submit, modify, replace or cancel orders" guarantee, and
ENABLE_PAPER_EXITS stays False — a horizon exit is not a paper exit.

THE SEQUENCE, AND WHY IT IS NOT THE OBVIOUS ONE

LOCKBOT's original acceptance test asked for a day position to be
flattened "without touching its price bracket". That is not safely
implementable and it ruled so when the conflict was put to it.

A bracket is a broker-side OCO with a stop leg and a target leg both
working. Send a market sell while they are live and two exits are racing
for the same shares: the sell fills, the stop fills moments later, and
the account is SHORT a name it never chose to short. This profile forbids
shorting under $2,000 of equity, so that is not a state anything here can
unwind.

The order is therefore: CANCEL the legs, CONFIRM they are gone, and only
then close. Never overlapping.

AND THE WINDOW THAT SEQUENCE OPENS

Between a successful cancel and a successful close, the position has NO
exit of any kind. If the close then fails, walking away would leave an
unprotected position overnight — strictly worse than never having started.

So a failed close re-arms the bracket immediately, alerts, and leaves the
position protected. The rule is: this module never ends a cycle having
removed protection without replacing it.

WHAT IT WILL NOT TOUCH

Reserved portfolio symbols. equity_positions() hides them by default and
that default is correct here, because this IS the trading engine. The ETF
sleeve is buy-and-hold and has no horizon; flattening SCHD at 3:45pm
because it had no tag would be a serious bug, and reading positions the
default way makes it impossible rather than merely unlikely.

Positions tagged "unknown" are also left alone. Every trade taken before
2026-08-07 carries that tag, and the engine must not act on a horizon it
cannot read.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lockbot_config as config
import trade_horizon

PROJECT_FOLDER = Path(__file__).resolve().parent

MODULE_NAME = "EQUITY_TIME_STOP"
VERSION = "0.1"

ENABLED = getattr(config, "EQUITY_TIME_STOP_ENABLED", True)
FLATTEN_MINUTES = getattr(
    config, "DAY_HORIZON_FLATTEN_MINUTES_BEFORE_CLOSE", 15
)

# Order states that still hold shares, borrowed from rearm_brackets so
# both modules agree on what "live" means.
try:
    from rearm_brackets import DYING_STATES, LIVE_STATES, _text
except Exception:  # pragma: no cover
    LIVE_STATES = {"new", "accepted", "held", "partially_filled", "replaced"}
    DYING_STATES = {"pending_cancel", "pending_replace", "pending_new"}

    def _text(value: Any) -> str:
        return str(getattr(value, "value", value) or "").strip().lower()


# Why a position was closed. Journalled so the reason survives.
TIME_STOP_SESSION = "TIME_STOP_SESSION_END"
TIME_STOP_MAX_HOLD = "TIME_STOP_MAX_HOLD_DAYS"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def horizons_by_symbol() -> dict[str, dict[str, Any]]:
    """What horizon each open trade was entered for, keyed by symbol.

    Read from the pending registry, which is where market_scanner records
    the tag at submission. A symbol absent from it has no horizon LOCKBOT
    knows about and is therefore never acted on.
    """

    try:
        from trade_manager import _read_pending_trades

        rows = _read_pending_trades()
    except Exception:
        return {}

    out: dict[str, dict[str, Any]] = {}

    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()

        if not symbol:
            continue

        out[symbol] = {
            "horizon": trade_horizon.normalise(row.get("horizon")),
            "registered_at": row.get("registered_at") or "",
            "side": str(row.get("side") or "LONG").strip().upper(),
        }

    return out


def _held_days(registered_at: str, *, now: datetime | None = None) -> float:
    """Calendar days a position has been held. -1 when unknown."""

    if not registered_at:
        return -1.0

    try:
        entered = datetime.fromisoformat(str(registered_at))
    except (TypeError, ValueError):
        return -1.0

    if entered.tzinfo is None:
        entered = entered.replace(tzinfo=timezone.utc)

    return ((now or _now()) - entered).total_seconds() / 86400.0


def minutes_to_close(clock: Any, *, now: datetime | None = None) -> float:
    """Minutes until the session closes. Large when it cannot be read.

    A failure returns something far in the future rather than something
    imminent, so an unreadable clock can never trigger a flatten. Closing
    a position because the time could not be determined would be the
    worst possible reading of an error.
    """

    try:
        close = clock.next_close

        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)

        return ((close - (now or _now())).total_seconds()) / 60.0
    except Exception:
        return 1e9


def due_for_exit(
    position: Any,
    tracked: dict[str, Any],
    *,
    minutes_left: float,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Whether this position must be closed now, and why.

    Pure, so the decision can be tested without a broker.
    """

    horizon = trade_horizon.normalise(tracked.get("horizon"))

    # An untagged position is one the engine cannot reason about. Every
    # trade taken before horizons existed is in this state.
    if horizon == trade_horizon.UNKNOWN:
        return False, ""

    if (
        trade_horizon.flattens_before_close(horizon)
        and minutes_left <= FLATTEN_MINUTES
    ):
        return True, TIME_STOP_SESSION

    limit = trade_horizon.max_hold_days(horizon)
    held = _held_days(tracked.get("registered_at", ""), now=now)

    # held < 0 means the entry time could not be read. Not a reason to
    # close; a position with an unreadable entry time still has its
    # bracket, and guessing would exit trades at random.
    if limit > 0 and held >= 0 and held >= limit:
        return True, TIME_STOP_MAX_HOLD

    return False, ""


def _live_exit_legs(client: Any, symbol: str) -> list:
    """Working orders that would exit this symbol."""

    try:
        from rearm_brackets import flatten_orders, open_orders_with_legs

        orders = flatten_orders(open_orders_with_legs(client))
    except Exception:
        return []

    return [
        order for order in orders
        if str(getattr(order, "symbol", "")).upper() == symbol.upper()
        and _text(getattr(order, "status", "")) in (LIVE_STATES | DYING_STATES)
    ]


def cancel_and_confirm(client: Any, symbol: str) -> tuple[bool, str]:
    """Cancel every working order on a symbol and verify none survive.

    Returns (clear, detail). `clear` is True only when a re-read shows
    nothing live. A cancel request that was accepted is not evidence: an
    order in pending_cancel still holds the shares, and closing against
    one is the race this whole module is arranged to avoid.
    """

    legs = _live_exit_legs(client, symbol)

    if not legs:
        return True, "no working orders"

    cancelled = 0

    for order in legs:
        try:
            client.cancel_order_by_id(order.id)
            cancelled += 1
        except Exception as error:
            return False, f"cancel failed on {order.id}: {type(error).__name__}"

    remaining = _live_exit_legs(client, symbol)

    if remaining:
        return False, (
            f"cancelled {cancelled}, but {len(remaining)} still working — "
            "not closing while an exit leg is live"
        )

    return True, f"cancelled {cancelled} order(s)"


def close_position(client: Any, symbol: str, quantity: float, side: str) -> tuple[bool, str]:
    """Market-close a position. Assumes the bracket is already gone."""

    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    closing_side = OrderSide.SELL if side.upper() == "LONG" else OrderSide.BUY

    try:
        order = client.submit_order(order_data=MarketOrderRequest(
            symbol=symbol,
            qty=abs(float(quantity)),
            side=closing_side,
            time_in_force=TimeInForce.DAY,
        ))

        return True, f"closed via order {getattr(order, 'id', '?')}"
    except Exception as error:
        return False, f"close failed: {type(error).__name__}: {error}"


def rearm(symbol: str) -> str:
    """Put a bracket back on a position whose close failed.

    Delegated to rearm_brackets, which owns bracket construction. This
    module must never end a cycle having removed protection without
    replacing it, and re-implementing the levels here would be a second
    definition of the same bracket.
    """

    try:
        import rearm_brackets

        rearm_brackets.run(arm=True)

        return "bracket re-armed"
    except Exception as error:
        return f"RE-ARM FAILED ({type(error).__name__}) — POSITION UNPROTECTED"


def _alert(title: str, message: str) -> None:
    try:
        from notifications import send_smart_notification

        send_smart_notification(
            symbol="LOCKBOT",
            event_type=title,
            message=message,
        )
    except Exception:
        pass


def run(*, client: Any = None, now: datetime | None = None,
        dry_run: bool = False) -> dict[str, Any]:
    """One pass. Returns what it found and did."""

    summary: dict[str, Any] = {
        "checked": 0, "due": 0, "closed": 0, "failed": 0,
        "skipped_no_horizon": 0, "actions": [],
    }

    if not ENABLED:
        summary["actions"].append("disabled in config")
        return summary

    if client is None:
        from rearm_brackets import _client

        client = _client()

    try:
        clock = client.get_clock()
    except Exception as error:
        summary["actions"].append(
            f"could not read the market clock ({type(error).__name__}) — "
            "doing nothing"
        )
        return summary

    if not getattr(clock, "is_open", False):
        summary["actions"].append("market closed; nothing to do")
        return summary

    left = minutes_to_close(clock, now=now)
    tracked = horizons_by_symbol()

    try:
        from position_filters import equity_positions

        # Default filtering, deliberately: reserved ETF-sleeve symbols
        # must be invisible here. See the module docstring.
        positions = equity_positions(client.get_all_positions())
    except Exception as error:
        summary["actions"].append(f"could not read positions ({type(error).__name__})")
        return summary

    for position in positions:
        symbol = str(position.symbol).upper()
        summary["checked"] += 1

        info = tracked.get(symbol)

        if not info:
            summary["skipped_no_horizon"] += 1
            continue

        due, reason = due_for_exit(position, info, minutes_left=left, now=now)

        if not due:
            continue

        summary["due"] += 1

        if dry_run:
            summary["actions"].append(f"{symbol}: WOULD close ({reason})")
            continue

        clear, detail = cancel_and_confirm(client, symbol)

        if not clear:
            # The bracket is still live, so the position is still
            # protected. Leaving it is the safe outcome.
            summary["failed"] += 1
            summary["actions"].append(f"{symbol}: not closed — {detail}")
            _alert("EQUITY_TIME_STOP_BLOCKED",
                   f"{symbol} {reason}: {detail}. Position keeps its bracket.")
            continue

        closed, close_detail = close_position(
            client, symbol, position.qty, info.get("side", "LONG")
        )

        if closed:
            summary["closed"] += 1
            summary["actions"].append(f"{symbol}: {reason} — {close_detail}")
            continue

        # The dangerous branch: protection is gone and the close failed.
        summary["failed"] += 1
        rearm_detail = rearm(symbol)
        summary["actions"].append(
            f"{symbol}: {close_detail}; {rearm_detail}"
        )
        _alert(
            "EQUITY_TIME_STOP_FAILED",
            f"{symbol}: bracket cancelled but the close failed "
            f"({close_detail}). {rearm_detail}.",
        )

    return summary


def _self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))
            failures.append(name)

    from datetime import timedelta

    NOW = datetime(2026, 8, 7, 19, 50, tzinfo=timezone.utc)

    class Order:
        def __init__(self, symbol, status="new", order_id="o1"):
            self.symbol, self.status, self.id = symbol, status, order_id
            self.order_type, self.stop_price, self.legs = "stop", 10.0, []

    class Position:
        def __init__(self, symbol, qty=2):
            self.symbol, self.qty = symbol, qty
            self.asset_class = "us_equity"

    class Clock:
        def __init__(self, is_open=True, minutes=5):
            self.is_open = is_open
            self.next_close = NOW + timedelta(minutes=minutes)

    print("\nWHEN A POSITION IS DUE")

    day = {"horizon": "day", "registered_at": NOW.isoformat(), "side": "LONG"}
    swing = {"horizon": "swing", "registered_at": NOW.isoformat(), "side": "LONG"}
    unknown = {"horizon": "unknown", "registered_at": NOW.isoformat(), "side": "LONG"}

    check("a day trade near the close is due",
          due_for_exit(None, day, minutes_left=5, now=NOW)[0])
    check("and the reason names the session end",
          due_for_exit(None, day, minutes_left=5, now=NOW)[1] == TIME_STOP_SESSION)
    check("a day trade mid-session is not due",
          not due_for_exit(None, day, minutes_left=300, now=NOW)[0])
    check("a swing near the close is NOT due",
          not due_for_exit(None, swing, minutes_left=1, now=NOW)[0])

    check("an UNKNOWN horizon is never due, even at the bell",
          not due_for_exit(None, unknown, minutes_left=0, now=NOW)[0])

    old_swing = {"horizon": "swing", "side": "LONG",
                 "registered_at": (NOW - timedelta(days=11)).isoformat()}
    check("a swing past its max hold is due",
          due_for_exit(None, old_swing, minutes_left=300, now=NOW)[0])
    check("and says so",
          due_for_exit(None, old_swing, minutes_left=300, now=NOW)[1]
          == TIME_STOP_MAX_HOLD)

    check("an unreadable entry time never forces an exit",
          not due_for_exit(None, {"horizon": "swing", "registered_at": "junk",
                                  "side": "LONG"},
                           minutes_left=300, now=NOW)[0])
    check("an unknown horizon has no max hold either",
          not due_for_exit(None, {"horizon": "unknown", "side": "LONG",
                                  "registered_at": (NOW - timedelta(days=99)).isoformat()},
                           minutes_left=300, now=NOW)[0])

    print("\nAN UNREADABLE CLOCK NEVER TRIGGERS A FLATTEN")

    class BadClock:
        @property
        def next_close(self):
            raise RuntimeError("no")

    check("a broken clock reads as far from the close",
          minutes_to_close(BadClock(), now=NOW) > 1000)
    check("so nothing is due under it",
          not due_for_exit(None, day,
                           minutes_left=minutes_to_close(BadClock(), now=NOW),
                           now=NOW)[0])

    print("\nCANCEL MUST BE CONFIRMED, NOT ASSUMED")

    class Client:
        def __init__(self, orders=None, positions=None, clock=None,
                     cancel_works=True, orders_after=None, close_works=True):
            self._orders = orders if orders is not None else []
            self._after = orders_after
            self.positions = positions or []
            self._clock = clock or Clock()
            self.cancel_works = cancel_works
            self.close_works = close_works
            self.cancelled, self.submitted = [], []
            self._reads = 0

        def get_clock(self):
            return self._clock

        def get_all_positions(self):
            return self.positions

        def get_orders(self, filter=None):
            self._reads += 1
            if self._reads > 1 and self._after is not None:
                return list(self._after)
            return list(self._orders)

        def cancel_order_by_id(self, order_id):
            if not self.cancel_works:
                raise RuntimeError("broker said no")
            self.cancelled.append(order_id)

        def submit_order(self, order_data=None):
            if not self.close_works:
                raise RuntimeError("close rejected")
            self.submitted.append(order_data)
            return type("O", (), {"id": "close-1"})()

    clear, detail = cancel_and_confirm(Client(orders=[Order("AAPL")],
                                              orders_after=[]), "AAPL")
    check("a cancel that really clears is confirmed", clear, detail)

    stuck = Client(orders=[Order("AAPL")],
                   orders_after=[Order("AAPL", status="pending_cancel")])
    clear, detail = cancel_and_confirm(stuck, "AAPL")
    check("an order stuck in pending_cancel is NOT clear", not clear, detail)
    check("and the refusal says why", "still working" in detail, detail)

    clear, detail = cancel_and_confirm(
        Client(orders=[Order("AAPL")], cancel_works=False), "AAPL")
    check("a rejected cancel is not clear", not clear, detail)

    clear, _ = cancel_and_confirm(Client(orders=[]), "AAPL")
    check("no orders at all is trivially clear", clear)

    print("\nTHE FULL PASS")

    import trade_manager

    real_reader = trade_manager._read_pending_trades
    trade_manager._read_pending_trades = lambda: [
        {"symbol": "AAPL", "horizon": "day", "side": "LONG",
         "registered_at": NOW.isoformat()},
        {"symbol": "MSFT", "horizon": "swing", "side": "LONG",
         "registered_at": NOW.isoformat()},
        {"symbol": "F", "horizon": "unknown", "side": "LONG",
         "registered_at": NOW.isoformat()},
    ]

    try:
        client = Client(
            orders=[Order("AAPL")], orders_after=[],
            positions=[Position("AAPL"), Position("MSFT"), Position("F")],
            clock=Clock(minutes=5),
        )
        out = run(client=client, now=NOW)

        check("the day trade is closed", out["closed"] == 1, str(out))
        check("the swing is left alone", "MSFT" not in str(out["actions"]))
        check("the untagged position is left alone",
              "F:" not in str(out["actions"]))
        check("the bracket was cancelled before the close",
              len(client.cancelled) == 1 and len(client.submitted) == 1)

        # The dangerous path: cancel succeeds, close fails.
        client = Client(
            orders=[Order("AAPL")], orders_after=[],
            positions=[Position("AAPL")], clock=Clock(minutes=5),
            close_works=False,
        )
        out = run(client=client, now=NOW)

        check("a failed close is counted as a failure", out["failed"] == 1, str(out))
        check("nothing is reported closed", out["closed"] == 0)
        check("and a re-arm was attempted",
              "re-arm" in str(out["actions"]).lower(), str(out["actions"]))

        # A blocked cancel must leave the bracket in place, untouched.
        client = Client(
            orders=[Order("AAPL")],
            orders_after=[Order("AAPL", status="pending_cancel")],
            positions=[Position("AAPL")], clock=Clock(minutes=5),
        )
        out = run(client=client, now=NOW)

        check("a blocked cancel closes nothing",
              out["closed"] == 0 and not client.submitted, str(out))
        check("and says the bracket is kept",
              "not closed" in str(out["actions"]), str(out["actions"]))

        # Mid-session: nothing due at all.
        client = Client(orders=[Order("AAPL")], orders_after=[],
                        positions=[Position("AAPL")], clock=Clock(minutes=300))
        out = run(client=client, now=NOW)
        check("mid-session nothing is due",
              out["due"] == 0 and not client.submitted, str(out))

        # A closed market must never act.
        client = Client(positions=[Position("AAPL")],
                        clock=Clock(is_open=False))
        out = run(client=client, now=NOW)
        check("a closed market does nothing", out["checked"] == 0, str(out))

        # Dry run touches nothing.
        client = Client(orders=[Order("AAPL")], orders_after=[],
                        positions=[Position("AAPL")], clock=Clock(minutes=5))
        out = run(client=client, now=NOW, dry_run=True)
        check("a dry run places no orders",
              not client.submitted and not client.cancelled, str(out))
        check("but still reports what it would do", out["due"] == 1, str(out))
    finally:
        trade_manager._read_pending_trades = real_reader

    print("\nIT NEVER TOUCHES THE PORTFOLIO SLEEVE")

    import position_filters

    reserved = position_filters.reserved_symbols()
    check("reserved symbols exist to be protected", bool(reserved), str(reserved))
    check("and equity_positions hides them by default",
          not position_filters.equity_positions(
              [Position(s) for s in reserved]),
          "reserved symbols leaked into the trading view")

    print()

    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All equity-time-stop checks passed.")

    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    result = run(dry_run=args.dry_run)

    print(f"{MODULE_NAME} v{VERSION}")
    print(f"  checked {result['checked']}, due {result['due']}, "
          f"closed {result['closed']}, failed {result['failed']}, "
          f"untagged {result['skipped_no_horizon']}")

    for action in result["actions"]:
        print(f"  {action}")

    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
