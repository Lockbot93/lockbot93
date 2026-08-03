import os

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce


# Safety switch: leave False for the first test.
ENABLE_TEST_ORDER = True
TEST_SYMBOL = "SPY"
TEST_QUANTITY = 1


load_dotenv()

api_key = os.getenv("ALPACA_API_KEY")
secret_key = os.getenv("ALPACA_SECRET_KEY")

if not api_key or not secret_key:
    raise RuntimeError("Alpaca API keys were not found in the .env file.")


trading_client = TradingClient(
    api_key,
    secret_key,
    paper=True,
)


account = trading_client.get_account()
market_clock = trading_client.get_clock()
positions = trading_client.get_all_positions()

already_owned = any(
    position.symbol == TEST_SYMBOL
    for position in positions
)


print("=" * 45)
print("MEDLOCKBOT PAPER ORDER TEST")
print("=" * 45)
print(f"Account Status : {account.status}")
print(f"Buying Power   : ${float(account.buying_power):,.2f}")
print(f"Market Open    : {market_clock.is_open}")
print(f"Test Symbol    : {TEST_SYMBOL}")
print(f"Quantity       : {TEST_QUANTITY}")
print(f"Already Owned  : {already_owned}")
print(f"Orders Enabled : {ENABLE_TEST_ORDER}")


if not ENABLE_TEST_ORDER:
    print("\nSAFE TEST COMPLETE: No order was submitted.")

elif not market_clock.is_open:
    print("\nORDER BLOCKED: The stock market is closed.")

elif already_owned:
    print(f"\nORDER BLOCKED: A {TEST_SYMBOL} position already exists.")

else:
    order_request = MarketOrderRequest(
        symbol=TEST_SYMBOL,
        qty=TEST_QUANTITY,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )

    submitted_order = trading_client.submit_order(
        order_data=order_request
    )

    print("\nPAPER ORDER SUBMITTED")
    print(f"Order ID : {submitted_order.id}")
    print(f"Symbol   : {submitted_order.symbol}")
    print(f"Status   : {submitted_order.status}")