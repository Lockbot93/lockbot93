"""
close_all.py — cancel every open order, then close every open position.

Run this ONLY when you want a clean slate. It liquidates everything.

Why not just close the positions? Each position has a stop leg and a
take-profit leg holding its shares. Closing underneath them either gets
rejected for insufficient quantity, or leaves an orphaned stop that can
later fire against no shares. cancel_orders=True cancels first, then
liquidates, in that order.

    python close_all.py            # shows what you have, asks first
    python close_all.py --yes      # skips the confirmation
"""

import sys
import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()

client = TradingClient(
    os.getenv("ALPACA_API_KEY"),
    os.getenv("ALPACA_SECRET_KEY"),
    paper=True,
)

account = client.get_account()
positions = client.get_all_positions()
orders = client.get_orders()
clock = client.get_clock()

print(f"Account equity : ${float(account.equity):,.2f}")
print(f"Buying power   : ${float(account.buying_power):,.2f}")
print(f"Market open    : {clock.is_open}")
print(f"Open positions : {len(positions)}")

for p in positions:
    print(f"  {p.symbol:<6} {p.qty} shares, "
          f"value ${float(p.market_value):,.2f}, "
          f"P/L ${float(p.unrealized_pl):,.2f}")

print(f"Open orders    : {len(orders)}")
for o in orders:
    print(f"  {o.symbol:<6} {o.side} {o.order_type} qty {o.qty}")

if not positions and not orders:
    print("\nNothing to close. Account is already flat.")
    sys.exit(0)

if not clock.is_open:
    print("\nNOTE: the market is CLOSED. The closing orders will queue and "
          "fill at the next open, not right now.")

if "--yes" not in sys.argv:
    answer = input("\nCancel all orders and close all positions? (yes/no): ")
    if answer.strip().lower() not in {"yes", "y"}:
        print("Cancelled. Nothing was changed.")
        sys.exit(0)

print("\nCancelling orders, then liquidating positions...")
client.close_all_positions(cancel_orders=True)

# Re-read from the broker rather than trusting the call above.
positions_after = client.get_all_positions()
orders_after = client.get_orders()

print(f"\nPositions remaining : {len(positions_after)}")
print(f"Orders remaining    : {len(orders_after)}")

for p in positions_after:
    print(f"  still open: {p.symbol} {p.qty}")
for o in orders_after:
    print(f"  still pending: {o.symbol} {o.side} {o.status}")

if positions_after or orders_after:
    print("\nNot fully flat yet. If the market is closed this is normal — "
          "the orders are queued for the open. Re-run this after the open "
          "to confirm.")
else:
    print("\nAccount is flat. Nothing open.")