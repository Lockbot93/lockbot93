"""
check_account.py — read-only. Shows what's open. Changes nothing, ever.

There is no code path in this file that submits, cancels, or closes
anything. Safe to run any time, as often as you like.

    python check_account.py
"""

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

print("=" * 56)
print("ACCOUNT CHECK")
print("=" * 56)
print(f"Equity        : ${float(account.equity):,.2f}")
print(f"Prior equity  : ${float(account.last_equity):,.2f}")
print(f"Buying power  : ${float(account.buying_power):,.2f}")
print(f"Day trades    : {getattr(account, 'daytrade_count', 0)} (rolling 5 days)")
print(f"Market open   : {clock.is_open}")
print(f"Next open     : {clock.next_open}")

print(f"\nOPEN POSITIONS : {len(positions)}")
if positions:
    for p in positions:
        print(f"  {p.symbol:<6} {p.qty:>4} shares  "
              f"value ${float(p.market_value):>9,.2f}  "
              f"P/L ${float(p.unrealized_pl):>7,.2f}")
else:
    print("  none")

print(f"\nOPEN ORDERS    : {len(orders)}")
if orders:
    for o in orders:
        print(f"  {o.symbol:<6} {str(o.side):<16} {str(o.order_type):<18} "
              f"qty {o.qty}  status {o.status}")
else:
    print("  none")

print("\n" + "=" * 56)
if not positions and not orders:
    print("FLAT — nothing open. Safe to start the controller.")
else:
    print("NOT FLAT — the items above are still live.")
    if not clock.is_open:
        print("The market is closed, so queued orders won't clear until "
              "the next open. Re-check after the open.")
    else:
        print("Do NOT start the controller until this reads FLAT.")
print("=" * 56)