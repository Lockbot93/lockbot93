"""Controlled one-share Alpaca paper bracket-order test."""

import os
import time

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
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
from dotenv import load_dotenv


TEST_SYMBOL = "SPY"
TEST_QUANTITY = 1
STOP_LOSS_PERCENT = 0.02
TAKE_PROFIT_PERCENT = 0.04
ORDER_CHECK_DELAY_SECONDS = 5


load_dotenv()

api_key = os.getenv("ALPACA_API_KEY")
secret_key = os.getenv("ALPACA_SECRET_KEY")

if not api_key or not secret_key:
    raise RuntimeError(
        "Alpaca API keys were not found in the .env file."
    )


trading_client = TradingClient(
    api_key,
    secret_key,
    paper=True,
)

data_client = StockHistoricalDataClient(
    api_key,
    secret_key,
)


print("=" * 50)
print("       LOCKBOT BRACKET ORDER TEST")
print("=" * 50)
print("Mode           : PAPER TRADING")
print(f"Symbol         : {TEST_SYMBOL}")
print(f"Quantity       : {TEST_QUANTITY} share")


market_clock = trading_client.get_clock()

print(f"Market Open    : {market_clock.is_open}")

if not market_clock.is_open:
    print("\nTEST STOPPED: The stock market is closed.")
    print(
        "Run this file again during regular market hours "
        "so the entry can fill immediately."
    )
    raise SystemExit(0)


positions = trading_client.get_all_positions()

already_has_position = any(
    position.symbol == TEST_SYMBOL
    for position in positions
)

if already_has_position:
    print(
        f"\nTEST STOPPED: A {TEST_SYMBOL} position "
        "already exists in the paper account."
    )
    raise SystemExit(0)


open_orders = trading_client.get_orders(
    filter=GetOrdersRequest(
        status=QueryOrderStatus.OPEN,
    )
)

already_has_open_order = any(
    order.symbol == TEST_SYMBOL
    for order in open_orders
)

if already_has_open_order:
    print(
        f"\nTEST STOPPED: An open {TEST_SYMBOL} "
        "order already exists."
    )
    raise SystemExit(0)


latest_trade_request = StockLatestTradeRequest(
    symbol_or_symbols=TEST_SYMBOL,
    feed=DataFeed.IEX,
)

latest_trades = data_client.get_stock_latest_trade(
    latest_trade_request
)

latest_trade = latest_trades.get(TEST_SYMBOL)

if latest_trade is None:
    raise RuntimeError(
        f"No latest trade was returned for {TEST_SYMBOL}."
    )


reference_price = float(latest_trade.price)

if reference_price <= 0:
    raise RuntimeError(
        "The latest market price is invalid."
    )


stop_loss_price = round(
    reference_price * (1 - STOP_LOSS_PERCENT),
    2,
)

take_profit_price = round(
    reference_price * (1 + TAKE_PROFIT_PERCENT),
    2,
)

if stop_loss_price <= 0:
    raise RuntimeError(
        "The calculated stop-loss price is invalid."
    )

if take_profit_price <= reference_price:
    raise RuntimeError(
        "The calculated take-profit price is invalid."
    )


print(f"Reference Price: ${reference_price:.2f}")
print(f"Stop Loss      : ${stop_loss_price:.2f}")
print(f"Take Profit    : ${take_profit_price:.2f}")


order_request = MarketOrderRequest(
    symbol=TEST_SYMBOL,
    qty=TEST_QUANTITY,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.GTC,
    order_class=OrderClass.BRACKET,
    take_profit=TakeProfitRequest(
        limit_price=take_profit_price,
    ),
    stop_loss=StopLossRequest(
        stop_price=stop_loss_price,
    ),
)

submitted_order = trading_client.submit_order(
    order_data=order_request,
)


print("\nBracket order submitted.")
print(f"Order ID       : {submitted_order.id}")
print(f"Initial Status : {submitted_order.status}")
print(
    f"Waiting {ORDER_CHECK_DELAY_SECONDS} seconds "
    "before checking the linked orders..."
)


time.sleep(ORDER_CHECK_DELAY_SECONDS)


checked_order = trading_client.get_order_by_id(
    submitted_order.id,
    nested=True,
)

print("\nBroker Verification")
print(f"Parent Status  : {checked_order.status}")

legs = checked_order.legs or []

print(f"Linked Legs    : {len(legs)}")

for number, leg in enumerate(legs, start=1):
    print(f"\nLeg {number}")
    print(f"Side           : {leg.side}")
    print(f"Type           : {leg.type}")
    print(f"Status         : {leg.status}")
    print(f"Limit Price    : {leg.limit_price}")
    print(f"Stop Price     : {leg.stop_price}")


if len(legs) == 2:
    print("\nSUCCESS: Alpaca returned both bracket exit legs.")
    print(
        "Check the Alpaca paper dashboard to confirm "
        "the position and linked orders visually."
    )
else:
    print(
        "\nNOTICE: The parent order was accepted, but "
        "both exit legs are not visible yet."
    )
    print(
        "The entry may still be filling. Check the "
        "Alpaca paper dashboard after a few seconds."
    )
