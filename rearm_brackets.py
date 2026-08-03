"""
rearm_brackets.py  --  put stops back on unprotected positions  (v1.0)

WHY THIS EXISTS
    LOCKBOT's safety model is that every equity position carries a
    broker-side bracket: a stop and a target submitted at entry, enforced
    by Alpaca whether or not anything of LOCKBOT's is still running.

    Positions can lose that bracket. close_all.py cancels orders before
    liquidating, and if the liquidation does not complete — the market is
    closed, or the call is interrupted — the position survives with its
    protection already cancelled. A careless script can do the same. On
    2026-07-29 a self-test did exactly that to NVO and LVS.

    An unprotected position is the one state LOCKBOT is built to never be
    in, and until now there was no tool to get out of it. Closing by hand
    was the only option, which turns a recoverable mistake into a forced
    realised loss.

WHAT IT DOES
    Finds equity positions with no live protective order and submits an
    OCO (one-cancels-other) pair: a stop and a limit, so exactly one can
    fill and the position cannot be sold twice.

    Levels come from lockbot_pending_trades.csv — the adaptive stop and
    target LOCKBOT chose at entry. It does not invent levels. A position
    with no registered entry is reported and skipped, because guessing a
    stop is worse than telling you it needs one.

A NOTE ON pending_cancel
    A cancelled order does not release its shares immediately. It sits in
    pending_cancel — still holding the quantity — and while the market is
    closed it can stay there for hours. During that window a replacement
    bracket is rejected with "insufficient qty available".

    So pending_cancel is NOT protection: the order will not fill, but it
    blocks the fix. This script reports that state plainly and exits
    without pretending it succeeded. Run it again after the open.

USAGE
    python rearm_brackets.py            report what is unprotected
    python rearm_brackets.py --arm      submit the brackets
    python rearm_brackets.py --self-test
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

PROJECT_FOLDER = Path(__file__).resolve().parent

# An order in one of these states is on its way out. It does not protect
# the position, but it does still hold the shares.
DYING_STATES = {"pending_cancel", "pending_replace", "pending_new"}

# States where an order is genuinely working and protecting the position.
LIVE_STATES = {"new", "accepted", "held", "partially_filled", "replaced"}


def _text(value) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def load_entry_levels(path: Path | None = None) -> dict[str, dict]:
    """
    Read each registered position's entry price and bracket percentages.

    Returns {symbol: {entry, stop, target}} using the same adaptive
    percentages the original order used.
    """

    path = path or (PROJECT_FOLDER / "lockbot_pending_trades.csv")

    if not path.exists():
        return {}

    levels: dict[str, dict] = {}

    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return {}

    for row in rows:
        symbol = str(row.get("symbol", "")).strip().upper()

        try:
            equity = float(row["account_equity_at_entry"])
            value = float(row["position_value"])
            stop_percent = float(row["stop_loss_percent"])
            target_percent = float(row["take_profit_percent"])
            risk = float(row["initial_risk_dollars"])
        except (KeyError, TypeError, ValueError):
            continue

        if not symbol or stop_percent <= 0 or target_percent <= 0:
            continue

        levels[symbol] = {
            "stop_percent": stop_percent,
            "target_percent": target_percent,
            "position_value": value,
            "equity_at_entry": equity,
            "risk_dollars": risk,
        }

    return levels


def flatten_orders(orders) -> list:
    """
    Return every order AND every leg as one flat list.

    An OCO or bracket parent carries its stop as a LEG, and a plain
    get_orders() call returns the parent with legs=0 — the stop is simply
    not in the response. Classifying from that flat list reports a
    correctly protected position as unprotected, and would then try to
    stack a second bracket on top of the first.

    Callers must fetch with nested=True for the legs to be present; this
    function is what makes them visible to the checks below.
    """

    flat = []

    for order in orders or []:
        flat.append(order)
        flat.extend(getattr(order, "legs", None) or [])

    return flat


def classify_orders(orders, symbol: str) -> dict:
    """
    Work out what protection a symbol actually has.

    Distinguishes orders that are working from orders that are dying but
    still holding shares — the difference between "protected" and
    "blocked from being protected".
    """

    relevant = [
        o for o in flatten_orders(orders)
        if str(o.symbol).upper() == symbol.upper()
    ]

    live = [o for o in relevant if _text(o.status) in LIVE_STATES]
    dying = [o for o in relevant if _text(o.status) in DYING_STATES]

    has_stop = any(
        "stop" in _text(o.order_type) or o.stop_price is not None for o in live
    )

    return {
        "live": live,
        "dying": dying,
        "has_stop": has_stop,
        "shares_blocked": bool(dying),
    }


def build_bracket(entry_price: float, levels: dict) -> tuple[float, float]:
    """Return (stop_price, target_price) for a long position."""

    stop = round(entry_price * (1 - levels["stop_percent"]), 2)
    target = round(entry_price * (1 + levels["target_percent"]), 2)

    return stop, target


def open_orders_with_legs(client):
    """
    Fetch open orders with their legs rolled up.

    nested=True is required. Without it an OCO parent comes back with
    legs=0 and its stop is invisible.
    """

    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    return client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)
    )


def _client():
    from alpaca.trading.client import TradingClient
    from dotenv import load_dotenv

    import lockbot_config as config

    load_dotenv(PROJECT_FOLDER / ".env")

    return TradingClient(
        os.getenv(config.ALPACA_API_KEY_ENV),
        os.getenv(config.ALPACA_SECRET_KEY_ENV),
        paper=config.PAPER_TRADING,
    )


def run(arm: bool = False) -> int:
    """Report, and optionally repair, unprotected positions."""

    from position_filters import equity_positions

    client = _client()
    positions = equity_positions(client.get_all_positions())
    orders = open_orders_with_legs(client)
    levels_by_symbol = load_entry_levels()

    print("=" * 62)
    print("BRACKET RE-ARM" + ("" if arm else "  (report only — pass --arm to fix)"))
    print("=" * 62)

    if not positions:
        print("\nNo equity positions. Nothing to protect.")
        return 0

    unprotected: list = []
    blocked: list = []

    for position in positions:
        symbol = str(position.symbol).upper()
        state = classify_orders(orders, symbol)
        entry = float(position.avg_entry_price)

        print(f"\n{symbol}  qty={position.qty}  entry=${entry:.2f}")

        for order in state["live"]:
            print(f"  live  : {_text(order.order_type)} {_text(order.status)}")

        for order in state["dying"]:
            print(
                f"  dying : {_text(order.order_type)} {_text(order.status)}"
                "  (holds the shares, does not protect)"
            )

        if state["has_stop"]:
            print("  STATUS: protected")
            continue

        if state["shares_blocked"]:
            print("  STATUS: UNPROTECTED, and shares are held by a dying order")
            blocked.append(symbol)
            continue

        print("  STATUS: UNPROTECTED")
        unprotected.append((position, symbol, entry))

    if blocked:
        print(
            f"\n{len(blocked)} position(s) cannot be re-armed yet: "
            f"{', '.join(blocked)}"
        )
        print(
            "Their shares are held by orders stuck in pending_cancel, which "
            "does not clear while the market is closed. Run this again after "
            "the open."
        )

    if not unprotected:
        if not blocked:
            print("\nEvery position has a working stop.")
        return 1 if blocked else 0

    if not arm:
        print(f"\n{len(unprotected)} position(s) need a bracket. Pass --arm to submit.")
        return 1

    from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
    )

    print("\nSubmitting brackets:")
    failures = 0

    for position, symbol, entry in unprotected:
        levels = levels_by_symbol.get(symbol)

        if not levels:
            print(
                f"  {symbol}: no registered entry in lockbot_pending_trades.csv. "
                "Skipped — a guessed stop is worse than none."
            )
            failures += 1
            continue

        stop, target = build_bracket(entry, levels)
        quantity = abs(int(float(position.qty)))

        try:
            order = client.submit_order(
                order_data=LimitOrderRequest(
                    symbol=symbol,
                    qty=quantity,
                    side=OrderSide.SELL,
                    type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.OCO,
                    take_profit=TakeProfitRequest(limit_price=target),
                    stop_loss=StopLossRequest(stop_price=stop),
                )
            )

            print(
                f"  {symbol}: armed. stop ${stop:.2f} / target ${target:.2f}  "
                f"order {order.id}"
            )

        except Exception as error:
            failures += 1
            print(f"  {symbol}: FAILED — {type(error).__name__}: {error}")

    return 1 if failures or blocked else 0


def _self_test() -> int:
    """Offline checks. No network, no broker."""

    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    class FakeOrder:
        def __init__(self, symbol, order_type, status, stop_price=None):
            self.symbol = symbol
            self.order_type = order_type
            self.status = status
            self.stop_price = stop_price

    print("Order classification")

    working = [FakeOrder("NVO", "stop", "held", stop_price=49.58)]
    state = classify_orders(working, "NVO")
    check("a held stop counts as protection", state["has_stop"])
    check("and does not block", not state["shares_blocked"])

    dying = [FakeOrder("NVO", "limit", "pending_cancel")]
    state = classify_orders(dying, "NVO")
    check("pending_cancel is not protection", not state["has_stop"])
    check("pending_cancel blocks the shares", state["shares_blocked"])

    limit_only = [FakeOrder("NVO", "limit", "new")]
    state = classify_orders(limit_only, "NVO")
    check("a take-profit alone is not protection", not state["has_stop"])

    # The bug this caught in production: an OCO parent carries its stop
    # as a LEG. Classifying the parent alone reported a correctly
    # protected position as unprotected, and --arm would then have
    # stacked a second bracket on top of the working one.
    class FakeParent(FakeOrder):
        def __init__(self, symbol, legs):
            super().__init__(symbol, "limit", "new")
            self.legs = legs

    oco = [FakeParent("NVO", [FakeOrder("NVO", "stop", "held", stop_price=49.58)])]
    state = classify_orders(oco, "NVO")
    check("an OCO stop leg counts as protection", state["has_stop"])
    check("and the parent does not block", not state["shares_blocked"])

    check(
        "flatten_orders surfaces legs",
        len(flatten_orders(oco)) == 2,
        str(len(flatten_orders(oco))),
    )
    check("flatten_orders handles no legs", len(flatten_orders(limit_only)) == 1)
    check("flatten_orders handles empty", flatten_orders([]) == [])
    check("flatten_orders handles None", flatten_orders(None) == [])

    check(
        "another symbol's orders are ignored",
        not classify_orders([FakeOrder("LVS", "stop", "held", 1.0)], "NVO")["has_stop"],
    )

    check("no orders means no protection", not classify_orders([], "NVO")["has_stop"])

    print()
    print("Bracket maths")

    stop, target = build_bracket(51.36, {"stop_percent": 0.034725, "target_percent": 0.06945})
    check("NVO stop", stop == 49.58, str(stop))
    check("NVO target", target == 54.93, str(target))

    stop, target = build_bracket(48.49, {"stop_percent": 0.03897, "target_percent": 0.07794})
    check("LVS stop", stop == 46.60, str(stop))
    check("LVS target", target == 52.27, str(target))

    check("stop is below entry", build_bracket(100.0, {"stop_percent": 0.02, "target_percent": 0.04})[0] < 100.0)
    check("target is above entry", build_bracket(100.0, {"stop_percent": 0.02, "target_percent": 0.04})[1] > 100.0)

    print()
    print("Entry levels")

    levels = load_entry_levels()
    check("reads the pending file", isinstance(levels, dict))

    for symbol, data in levels.items():
        check(
            f"{symbol} has usable percentages",
            data["stop_percent"] > 0 and data["target_percent"] > 0,
            str(data),
        )

    check(
        "a missing file returns empty",
        load_entry_levels(PROJECT_FOLDER / "does_not_exist.csv") == {},
    )

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All re-arm checks passed.")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Re-arm brackets on unprotected positions.")
    parser.add_argument("--arm", action="store_true", help="submit the brackets")
    parser.add_argument("--self-test", action="store_true", help="offline checks")

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    return run(arm=args.arm)


if __name__ == "__main__":
    sys.exit(main())
