"""
test_execution.py — prove LOCKBOT's order path works, before the open.

WHAT THIS DOES
    Builds the exact same bracket order market_scanner.py builds, sends it
    to your Alpaca PAPER account, confirms the broker accepted it, then
    immediately cancels it. Once for a long, once for a short.

WHAT THIS PROVES
    - Credentials and permissions work for placing orders, not just reading
    - Alpaca accepts LOCKBOT's bracket format (entry + stop + target)
    - Shorting is actually enabled on the account
    - Orders can be cancelled cleanly

WHAT THIS DOES NOT TOUCH
    Nothing in LOCKBOT. No journal entries, no pending-trades rows, no risk
    state, no daily trade counter. It talks straight to Alpaca and cleans up
    after itself.

THE ONE RISK
    The market is closed, so the order queues rather than filling. It gets
    cancelled seconds later. If a cancel somehow failed, that order would
    fill at tomorrow's open — one share, paper money — and become an
    untracked position that uses a slot. The script checks every
    cancellation and shouts if one didn't take.

USAGE
    python test_execution.py              # long and short test, 1 share each
    python test_execution.py --symbol F   # pick a cheaper symbol
    python test_execution.py --long-only
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass,
    OrderSide,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

import lockbot_config as config

load_dotenv()

PROJECT_FOLDER = Path(__file__).resolve().parent
UNIVERSE_FILE = getattr(config, "UNIVERSE_FILE", PROJECT_FOLDER / "universe.csv")

STOP_LOSS_PERCENT = config.BRACKET_STOP_LOSS_PERCENT
TAKE_PROFIT_PERCENT = config.BRACKET_TAKE_PROFIT_PERCENT

FAILURES: list[str] = []
UNCANCELLED: list[str] = []


def report(label: str, ok: bool, detail: str = "") -> bool:
    if not ok:
        FAILURES.append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    return ok


def pick_test_symbol(preferred: str | None) -> tuple[str, float]:
    """Choose a cheap, shortable name from the universe to keep the test small."""

    rows = []

    if Path(UNIVERSE_FILE).exists():
        with open(UNIVERSE_FILE, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

    if preferred:
        for row in rows:
            if row["symbol"].upper() == preferred.upper():
                return row["symbol"], float(row["last_close"])
        return preferred.upper(), 0.0

    shortable = [
        row for row in rows
        if str(row.get("shortable", "")).lower() == "true"
        and float(row.get("last_close", 0)) > 5
    ]

    if not shortable:
        return "SPY", 0.0

    cheapest = min(shortable, key=lambda row: float(row["last_close"]))
    return cheapest["symbol"], float(cheapest["last_close"])


def build_bracket(symbol: str, price: float, quantity: int, is_long: bool):
    """Identical construction to market_scanner.py."""

    if is_long:
        stop_price = round(price * (1 - STOP_LOSS_PERCENT), 2)
        target_price = round(price * (1 + TAKE_PROFIT_PERCENT), 2)
    else:
        stop_price = round(price * (1 + STOP_LOSS_PERCENT), 2)
        target_price = round(price * (1 - TAKE_PROFIT_PERCENT), 2)

    request = MarketOrderRequest(
        symbol=symbol,
        qty=quantity,
        side=OrderSide.BUY if is_long else OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=target_price),
        stop_loss=StopLossRequest(stop_price=stop_price),
    )

    return request, stop_price, target_price


def run_side_test(client, symbol: str, price: float, quantity: int, is_long: bool):
    side_label = "LONG" if is_long else "SHORT"

    print(f"\n{side_label} bracket test — {quantity} share(s) of {symbol}")

    request, stop_price, target_price = build_bracket(symbol, price, quantity, is_long)

    print(f"  reference price ${price:.2f} | stop ${stop_price:.2f} | target ${target_price:.2f}")

    try:
        order = client.submit_order(order_data=request)
    except Exception as error:
        report(f"{side_label} order accepted by Alpaca", False,
               f"{type(error).__name__}: {error}")
        return

    report(f"{side_label} order accepted by Alpaca", True,
           f"id {str(order.id)[:8]}… status {order.status}")

    legs = getattr(order, "legs", None) or []
    report(f"{side_label} bracket has both exit legs attached", len(legs) >= 2,
           f"{len(legs)} leg(s) returned")

    time.sleep(1)

    try:
        client.cancel_order_by_id(order.id)
        time.sleep(2)
        refreshed = client.get_order_by_id(order.id)
        status = str(refreshed.status).lower()
        cancelled = "cancel" in status or "expired" in status

        if not cancelled:
            UNCANCELLED.append(f"{symbol} {side_label} (order {order.id}, status {status})")

        report(f"{side_label} order cancelled", cancelled, f"status {refreshed.status}")

    except Exception as error:
        UNCANCELLED.append(f"{symbol} {side_label} (order {order.id})")
        report(f"{side_label} order cancelled", False,
               f"{type(error).__name__}: {error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Test LOCKBOT's order path")
    parser.add_argument("--symbol", default=None, help="symbol to test with")
    parser.add_argument("--qty", type=int, default=1, help="shares per test order")
    parser.add_argument("--long-only", action="store_true", help="skip the short test")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        print("Alpaca API keys not found in .env")
        return 1

    client = TradingClient(api_key, secret_key, paper=True)

    account = client.get_account()

    print("=" * 60)
    print("        LOCKBOT ORDER PATH TEST")
    print("=" * 60)
    print(f"Account   : {account.status}")
    print(f"Equity    : ${float(account.equity):,.2f}")

    # The client is constructed with paper=True above, which points it at
    # Alpaca's paper endpoint. That is what makes this safe — there is no
    # account field worth testing for it.
    print("Endpoint  : PAPER (paper=True)")

    clock = client.get_clock()
    print(f"Market    : {'OPEN' if clock.is_open else 'CLOSED'}")

    if clock.is_open:
        print("\nThe market is OPEN. A test order could fill instantly.")
        print("Run this while the market is closed, or accept the fill.")
        if not args.yes:
            if input("Type YES to continue anyway: ").strip() != "YES":
                print("Cancelled.")
                return 0

    symbol, price = pick_test_symbol(args.symbol)

    if price <= 0:
        print(f"\nNo cached price for {symbol}. Run 'python universe.py' first, "
              "or pass --symbol for a name that's in universe.csv.")
        return 1

    print(f"Test with : {args.qty} share(s) of {symbol} at ~${price:.2f}")

    existing = [p for p in client.get_all_positions() if p.symbol == symbol.upper()]
    if existing:
        print(f"\n{symbol} is already held. Pick a different symbol with --symbol.")
        return 1

    if not args.yes:
        print("\nThis sends real orders to your PAPER account and cancels them "
              "seconds later. No real money is involved.")
        if input("Type YES to run the test: ").strip() != "YES":
            print("Cancelled.")
            return 0

    run_side_test(client, symbol, price, args.qty, is_long=True)

    if not args.long_only:
        run_side_test(client, symbol, price, args.qty, is_long=False)

    # Final sweep: make sure nothing was left behind.
    print("\nCleanup check")

    open_orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    leftover = [o for o in open_orders if o.symbol == symbol.upper()]

    report("no leftover open orders for the test symbol", not leftover,
           f"{len(leftover)} still open" if leftover else "")

    positions = [p for p in client.get_all_positions() if p.symbol == symbol.upper()]
    report("no position was created", not positions,
           f"{len(positions)} position(s)" if positions else "")

    print("\n" + "=" * 60)

    if UNCANCELLED:
        print("ACTION NEEDED — these orders may not have cancelled:")
        for item in UNCANCELLED:
            print(f"  - {item}")
        print("Cancel them in the Alpaca dashboard before the market opens.")
        return 1

    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        print("LOCKBOT's order path is NOT confirmed working.")
        return 1

    print("Order path confirmed. LOCKBOT can submit and cancel bracket")
    print("orders, long and short, and nothing was left behind.")
    return 0


if __name__ == "__main__":
    sys.exit(main())