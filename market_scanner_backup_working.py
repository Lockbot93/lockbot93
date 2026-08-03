import os
import pandas as pd 
import numpy as np
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, QueryOrderStatus
from alpaca.trading.enums import TimeInForce

from dotenv import load_dotenv

load_dotenv()
MIN_CONFIDENCE_SCORE = 80

api_key = os.getenv("ALPACA_API_KEY")
secret_key = os.getenv("ALPACA_SECRET_KEY")

if not api_key or not secret_key:
    raise RuntimeError("Alpaca API keys were not found in the .env file.")

data_client = StockHistoricalDataClient(api_key, secret_key)
trading_client = TradingClient(
    api_key,
    secret_key,
    paper=True
)

end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(days=5)

request = StockBarsRequest(
    symbol_or_symbols=["SPY", "QQQ"],
    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
    start=start_time,
    end=end_time,
    limit=1000,
    feed=DataFeed.IEX,
)

print("=" * 50)
print("         MEDLOCKBOT MARKET SCANNER v0.1")
print("=" * 50)

try:
    bar_set = data_client.get_stock_bars(request)
    for symbol in ["SPY", "QQQ"]:
        bars = bar_set.data.get(symbol, [])

        if not bars:
            print(f"\nNo bar data returned for {symbol}.")
            continue

        data = []

        for bar in bars:
            data.append({
                "timestamp": bar.timestamp,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume
            })

        df = pd.DataFrame(data)

        df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema_21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["ema_12"] = df["close"].ewm(span=12, adjust=False).mean()
        df["ema_26"] = df["close"].ewm(span=26, adjust=False).mean()

        df["macd"] = df["ema_12"] - df["ema_26"]
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()        
        df["cum_volume"] = df["volume"].cumsum()

        df["cum_vp"] = (df["close"] * df["volume"]).cumsum()

        df["vwap"] = df["cum_vp"] / df["cum_volume"]
        delta = df["close"].diff()

        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)

        avg_gain = pd.Series(gain).rolling(14).mean()
        avg_loss = pd.Series(loss).rolling(14).mean()

        rs = avg_gain / avg_loss

        df["rsi"] = 100 - (100 / (1 + rs))

        latest = df.iloc[-1]
        bar_time = latest["timestamp"].to_pydatetime()
        data_age_minutes = (
            datetime.now(timezone.utc) - bar_time
        ).total_seconds() / 60

        data_is_fresh = data_age_minutes <= 10
        print(f"\n{symbol} Market Snapshot")
        print(f"Latest Close : ${latest['close']:.2f}")
        print(f"9 EMA        : ${latest['ema_9']:.2f}")
        print(f"21 EMA       : ${latest['ema_21']:.2f}")
        print(f"VWAP       : ${latest['vwap']:.2f}")
        print(f"RSI        : {latest['rsi']:.2f}")
        print(f"MACD       : {latest['macd']:.4f}")
        print(f"MACD Signal: {latest['macd_signal']:.4f}")
        if latest["ema_9"] > latest["ema_21"]:
            trend = "BULLISH"
        elif latest["ema_9"] < latest["ema_21"]:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"

        print(f"Trend        : {trend}")
        print("\nTrade Decision")

        if not data_is_fresh:
            signal = "NO_TRADE"
            signal_reason = "STALE_DATA"

        elif (
            trend == "BULLISH"
            and latest["close"] > latest["ema_9"]
            and latest["close"] > latest["vwap"]
            and 50 < latest["rsi"] < 70
            and latest["macd"] > latest["macd_signal"]
        ):
            signal = "BUY_CALL"
            signal_reason = "BULLISH_EMA_RSI_VWAP_MACD"

        elif (
            trend == "BEARISH"
            and latest["close"] < latest["ema_9"]
            and latest["close"] < latest["vwap"]
            and 30 < latest["rsi"] < 50
            and latest["macd"] < latest["macd_signal"]
        ):
            signal = "BUY_PUT"
            signal_reason = "BEARISH_EMA_RSI_VWAP_MACD"

        else:
            signal = "NO_TRADE"
            signal_reason = "SETUP_NOT_CONFIRMED" 
        score = 0

        # Trend
        if trend == "BULLISH":
            score += 20
        elif trend == "BEARISH":
            score += 20

        # EMA confirmation
        if latest["close"] > latest["ema_9"] and trend == "BULLISH":
            score += 20

        if latest["close"] < latest["ema_9"] and trend == "BEARISH":
            score += 20
            # MACD confirmation
        if latest["macd"] > latest["macd_signal"] and trend == "BULLISH":
            score += 20

        if latest["macd"] < latest["macd_signal"] and trend == "BEARISH":
            score += 20

        # RSI confirmation
        if 50 < latest["rsi"] < 70:
            score += 20

        if 30 < latest["rsi"] < 50:
            score += 20
        # VWAP confirmation
        if latest["close"] > latest["vwap"] and trend == "BULLISH":
            score += 20

        if latest["close"] < latest["vwap"] and trend == "BEARISH":
            score += 20

        # Trade approval gate
        market_clock = trading_client.get_clock()

        if not market_clock.is_open:
            trade_approved = False
            approval_reason = "MARKET_IS_CLOSED"

        elif not data_is_fresh:
            trade_approved = False
            approval_reason = "DATA_IS_STALE"

        elif signal == "NO_TRADE":
            trade_approved = False
            approval_reason = "NO_VALID_SIGNAL"

        elif score < MIN_CONFIDENCE_SCORE:
            trade_approved = False
            approval_reason = "CONFIDENCE_TOO_LOW"

        else:
            trade_approved = True
            approval_reason = "APPROVED_FOR_PAPER_EXECUTION"
                         
        print(f"Signal        : {signal}")
        print(f"Reason        : {signal_reason}")
        print(f"Data Age      : {data_age_minutes:.1f} minutes")        
        print(f"Confidence    : {score}/100")
        print(f"Approved      : {trade_approved}")
        print(f"Approval Info : {approval_reason}")
        pd.DataFrame([{
            "timestamp": latest["timestamp"],
            "symbol": symbol,
            "latest_close": latest["close"],
            "ema_9": latest["ema_9"],
            "ema_21": latest["ema_21"],
            "vwap": latest["vwap"],
            "rsi": latest["rsi"],
            "macd": latest["macd"],
            "macd_signal": latest["macd_signal"],
            "signal_reason": signal_reason,
            "data_age_minutes": data_age_minutes,
            "trend": trend,
            "signal": signal,
            "confidence_score": score,
            "trade_approved": trade_approved,
            "approval_reason": approval_reason,
        }]).to_csv(
            "signals.csv",
            mode="a",
            header=not os.path.exists("signals.csv") or os.path.getsize("signals.csv") == 0,
            index=False,
        )

        print("Signal saved to signals.csv")

                # Verify Alpaca paper account connection
        account = trading_client.get_account()

        print("\nPaper Account Check")
        print(f"Account Status : {account.status}")
        print(f"Buying Power   : ${float(account.buying_power):,.2f}")
        print(f"Market Open    : {market_clock.is_open}")
        # ==========================================
# PAPER TRADE EXECUTION
# ==========================================

            # ==========================================
        # PAPER TRADE EXECUTION
        # ==========================================

        if trade_approved:
            positions = trading_client.get_all_positions()

            already_has_position = any(
                position.symbol == symbol
                for position in positions
            )

            open_order_request = GetOrdersRequest(
                status=QueryOrderStatus.OPEN
            )

            open_orders = trading_client.get_orders(
                filter=open_order_request
            )

            already_has_open_order = any(
                order.symbol == symbol
                for order in open_orders
            )

            if already_has_position:
                print(
                    f"ORDER BLOCKED: An open {symbol} position already exists."
                )

            elif already_has_open_order:
                print(
                    f"ORDER BLOCKED: An open {symbol} order already exists."
                )

            else:
                print("\n==============================")
                print("PAPER TRADE")
                print("==============================")

                if signal == "BUY_CALL":
                    stock_side = OrderSide.BUY

                elif signal == "BUY_PUT":
                    print(
                        f"ORDER BLOCKED: {signal} requires an options contract; "
                        f"MedlockBot will not buy {symbol} shares for a bearish signal."
                    )
                    continue

                else:
                    print(f"ORDER BLOCKED: Unsupported signal {signal}.")
                    continue

                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=1,
                    side=stock_side,
                    time_in_force=TimeInForce.DAY,
                )
                submitted_order = trading_client.submit_order(
                    order_data=order
)

                print(f"Submitted PAPER order for {symbol}")
                print(f"Order ID       : {submitted_order.id}")
                print(f"Order Status   : {submitted_order.status}")

except Exception as error:
    print("\nMarket scanner failed.")
    print(error)