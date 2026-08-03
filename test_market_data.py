import os

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("ALPACA_API_KEY")
secret_key = os.getenv("ALPACA_SECRET_KEY")

if not api_key or not secret_key:
    raise RuntimeError("Alpaca API keys were not found in the .env file.")

print("Connecting to Alpaca market data...")

try:
    data_client = StockHistoricalDataClient(api_key, secret_key)

    request = StockLatestTradeRequest(symbol_or_symbols="SPY")
    latest_trades = data_client.get_stock_latest_trade(request)

    spy_trade = latest_trades["SPY"]

    print("Market data connection successful.")
    print("Symbol       : SPY")
    print(f"Latest Price : ${spy_trade.price}")
    print(f"Trade Time   : {spy_trade.timestamp}")

except Exception as error:
    print("Market data connection failed.")
    print(error)