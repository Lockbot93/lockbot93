"""LOCKBOT controlled Alpaca paper-execution test."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
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

from notifications import send_smart_notification
from trade_manager import register_bracket_trade


load_dotenv()

PROJECT_FOLDER = Path(__file__).resolve().parent

TEST_SYMBOL = "QQQ"
TEST_QUANTITY = 1

STOP_LOSS_PERCENT = 0.02
TAKE_PROFIT_PERCENT = 0.04

TEST_CONFIRMATION_TEXT = "EXECUTE PAPER TEST"


def stop_test(message: str) -> None:
    """Display why the test was stopped and exit safely."""

    print()
    print("=" * 58)
    print("LOCKBOT PAPER EXECUTION TEST STOPPED")
    print("=" * 58)
    print(message)
    print("=" * 58)

    sys.exit(0)


def main() -> None:
    """Submit one controlled paper bracket order."""

    print("=" * 58)
    print("       LOCKBOT PAPER EXECUTION TEST v0.1")
    print("=" * 58)
    print(f"Symbol          : {TEST_SYMBOL}")
    print(f"Quantity        : {TEST_QUANTITY} share")
    print("Account Mode    : PAPER ONLY")
    print("Order Type      : MARKET BRACKET")
    print(f"Stop Loss       : {STOP_LOSS_PERCENT:.0%}")
    print(f"Take Profit     : {TAKE_PROFIT_PERCENT:.0%}")
    print("=" * 58)

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

    account = trading_client.get_account()
    market_clock = trading_client.get_clock()

    print()
    print("Paper Account Check")
    print(f"Account Status : {account.status}")
    print(f"Market Open    : {market_clock.is_open}")
    print(f"Equity         : ${float(account.equity):,.2f}")
    print(f"Buying Power   : ${float(account.buying_power):,.2f}")

    if not market_clock.is_open:
        stop_test(
            "The market is closed. No test order was submitted."
        )

    positions = trading_client.get_all_positions()

    already_has_position = any(
        position.symbol == TEST_SYMBOL
        for position in positions
    )

    if already_has_position:
        stop_test(
            f"An existing {TEST_SYMBOL} position was found. "
            "No duplicate position was created."
        )

    open_order_request = GetOrdersRequest(
        status=QueryOrderStatus.OPEN
    )

    open_orders = trading_client.get_orders(
        filter=open_order_request
    )

    already_has_open_order = any(
        order.symbol == TEST_SYMBOL
        for order in open_orders
    )

    if already_has_open_order:
        stop_test(
            f"An existing open {TEST_SYMBOL} order was found. "
            "No duplicate order was created."
        )

    quote_request = StockLatestQuoteRequest(
        symbol_or_symbols=TEST_SYMBOL,
        feed=DataFeed.IEX,
    )

    latest_quotes = data_client.get_stock_latest_quote(
        quote_request
    )

    latest_quote = latest_quotes.get(TEST_SYMBOL)

    if latest_quote is None:
        raise RuntimeError(
            f"No latest quote was returned for {TEST_SYMBOL}."
        )

    ask_price = float(latest_quote.ask_price)

    if ask_price <= 0:
        raise RuntimeError(
            f"Invalid ask price returned for {TEST_SYMBOL}: "
            f"{ask_price}"
        )

    stop_loss_price = round(
        ask_price * (1 - STOP_LOSS_PERCENT),
        2,
    )

    take_profit_price = round(
        ask_price * (1 + TAKE_PROFIT_PERCENT),
        2,
    )

    estimated_position_value = (
        ask_price * TEST_QUANTITY
    )

    estimated_risk_dollars = (
        estimated_position_value * STOP_LOSS_PERCENT
    )

    print()
    print("Proposed Test Order")
    print(f"Symbol          : {TEST_SYMBOL}")
    print(f"Quantity        : {TEST_QUANTITY}")
    print(f"Reference Ask   : ${ask_price:.2f}")
    print(f"Stop Loss       : ${stop_loss_price:.2f}")
    print(f"Take Profit     : ${take_profit_price:.2f}")
    print(
        f"Estimated Value : "
        f"${estimated_position_value:,.2f}"
    )
    print(
        f"Estimated Risk  : "
        f"${estimated_risk_dollars:,.2f}"
    )

    print()
    print("This will submit a real order to your")
    print("Alpaca PAPER account only.")
    print()

    confirmation = input(
        f'Type "{TEST_CONFIRMATION_TEXT}" to continue: '
    ).strip()

    if confirmation != TEST_CONFIRMATION_TEXT:
        stop_test(
            "Confirmation text did not match. "
            "No order was submitted."
        )

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
        order_data=order_request
    )

    trade_registered = register_bracket_trade(
        parent_order_id=str(submitted_order.id),
        strategy_version="EXECUTION_TEST_v0.1",
        symbol=TEST_SYMBOL,
        side="LONG",
        market_regime="CONTROLLED_TEST",
        confidence=100,
        position_value=estimated_position_value,
        account_equity_at_entry=float(account.equity),
        initial_risk_dollars=estimated_risk_dollars,
        stop_loss_percent=STOP_LOSS_PERCENT,
        take_profit_percent=TAKE_PROFIT_PERCENT,
        paper_trade=True,
    )

    print()
    print("=" * 58)
    print("LOCKBOT PAPER ORDER SUBMITTED")
    print("=" * 58)
    print(f"Symbol           : {TEST_SYMBOL}")
    print(f"Quantity         : {TEST_QUANTITY}")
    print(f"Order ID         : {submitted_order.id}")
    print(f"Order Status     : {submitted_order.status}")
    print(f"Trade Registered : {trade_registered}")
    print(
        "Submitted At      : "
        f"{datetime.now().astimezone().isoformat(timespec='seconds')}"
    )
    print("=" * 58)

    send_smart_notification(
        symbol=TEST_SYMBOL,
        event_type="PAPER_EXECUTION_TEST",
        title="LOCKBOT Paper Test Submitted",
        reason="CONTROLLED_EXECUTION_TEST",
        message=(
            f"{TEST_SYMBOL} controlled paper test submitted.\n\n"
            f"Quantity: {TEST_QUANTITY}\n"
            f"Reference ask: ${ask_price:.2f}\n"
            f"Stop loss: ${stop_loss_price:.2f}\n"
            f"Take profit: ${take_profit_price:.2f}\n"
            f"Order status: {submitted_order.status}\n"
            f"Trade registered: {trade_registered}"
        ),
    )


if __name__ == "__main__":
    main()