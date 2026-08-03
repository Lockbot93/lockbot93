import csv
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderSide,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
)
from dotenv import load_dotenv

from notifications import send_smart_notification
from risk_engine import check_daily_loss_limit


load_dotenv()


# --------------------------------------------------
# PROJECT FILES
# --------------------------------------------------

PROJECT_FOLDER = Path(__file__).resolve().parent
SIGNALS_FILE = PROJECT_FOLDER / "signals.csv"


# --------------------------------------------------
# SCANNER SETTINGS
# --------------------------------------------------

SYMBOLS = ["SPY", "QQQ"]

MIN_CONFIDENCE_SCORE = 80
MIN_VOLUME_RATIO = 1.10

MAX_RISK_PER_TRADE = 0.01
MAX_OPEN_TRADES = 1

STOP_LOSS_PERCENT = 0.02
TAKE_PROFIT_PERCENT = 0.04
MAX_POSITION_PERCENT = 0.10


# --------------------------------------------------
# SIGNAL FILE COLUMNS
# --------------------------------------------------

SIGNAL_COLUMNS = [
    "timestamp",
    "symbol",
    "latest_close",
    "ema_9",
    "ema_21",
    "vwap",
    "rsi",
    "macd",
    "macd_signal",
    "latest_volume",
    "volume_avg_20",
    "volume_ratio",
    "volume_confirmed",
    "trend_5m",
    "trend_15m",
    "trend_1h",
    "timeframes_aligned",
    "signal_reason",
    "data_age_minutes",
    "signal",
    "confidence_score",
    "trade_approved",
    "approval_reason",
]


# --------------------------------------------------
# ALPACA CREDENTIALS
# --------------------------------------------------

api_key = os.getenv("ALPACA_API_KEY")
secret_key = os.getenv("ALPACA_SECRET_KEY")

if not api_key or not secret_key:
    raise RuntimeError(
        "Alpaca API keys were not found in the .env file."
    )


# --------------------------------------------------
# SIGNAL FILE MANAGEMENT
# --------------------------------------------------

def ensure_signals_file():
    """Create or validate signals.csv."""

    expected_header = ",".join(SIGNAL_COLUMNS)

    if not SIGNALS_FILE.exists() or SIGNALS_FILE.stat().st_size == 0:
        SIGNALS_FILE.write_text(
            expected_header + "\n",
            encoding="utf-8",
        )
        return

    with SIGNALS_FILE.open(
        mode="r",
        encoding="utf-8-sig",
        newline="",
    ) as csv_file:
        rows = list(csv.reader(csv_file))

    current_header = ",".join(rows[0]) if rows else ""

    if current_header == expected_header:
        return

    if len(rows) <= 1:
        SIGNALS_FILE.write_text(
            expected_header + "\n",
            encoding="utf-8",
        )

        print(
            "signals.csv header was updated for "
            "higher-timeframe and volume data."
        )
        return

    raise RuntimeError(
        "signals.csv contains data under an old header. "
        "Clear the file and run LockBot again."
    )


# --------------------------------------------------
# POSITION SIZING
# --------------------------------------------------

def calculate_position_size(
    account_equity,
    buying_power,
    entry_price,
):
    """Calculate a risk-controlled share quantity."""

    if account_equity <= 0:
        return 0

    if buying_power <= 0:
        return 0

    if entry_price <= 0:
        return 0

    max_dollar_risk = (
        account_equity * MAX_RISK_PER_TRADE
    )

    stop_loss_distance = (
        entry_price * STOP_LOSS_PERCENT
    )

    if stop_loss_distance <= 0:
        return 0

    risk_based_shares = int(
        max_dollar_risk / stop_loss_distance
    )

    max_position_value = (
        account_equity * MAX_POSITION_PERCENT
    )

    position_cap_shares = int(
        max_position_value / entry_price
    )

    affordable_shares = int(
        buying_power / entry_price
    )

    position_size = min(
        risk_based_shares,
        position_cap_shares,
        affordable_shares,
    )

    return max(position_size, 0)


# --------------------------------------------------
# MARKET DATA HELPERS
# --------------------------------------------------

def bars_to_dataframe(bars):
    """Convert Alpaca bar objects into a DataFrame."""

    data = []

    for bar in bars:
        data.append(
            {
                "timestamp": bar.timestamp,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
        )

    return pd.DataFrame(data)


def add_indicators(df):
    """Add LockBot technical indicators."""

    df = df.copy()

    df["ema_9"] = (
        df["close"]
        .ewm(span=9, adjust=False)
        .mean()
    )

    df["ema_21"] = (
        df["close"]
        .ewm(span=21, adjust=False)
        .mean()
    )

    df["ema_12"] = (
        df["close"]
        .ewm(span=12, adjust=False)
        .mean()
    )

    df["ema_26"] = (
        df["close"]
        .ewm(span=26, adjust=False)
        .mean()
    )

    df["macd"] = (
        df["ema_12"] - df["ema_26"]
    )

    df["macd_signal"] = (
        df["macd"]
        .ewm(span=9, adjust=False)
        .mean()
    )

    df["cum_volume"] = df["volume"].cumsum()

    df["cum_vp"] = (
        df["close"] * df["volume"]
    ).cumsum()

    df["vwap"] = (
        df["cum_vp"] / df["cum_volume"]
    )

    df["volume_avg_20"] = (
        df["volume"]
        .shift(1)
        .rolling(20)
        .mean()
    )

    delta = df["close"].diff()

    gain = pd.Series(
        np.where(delta > 0, delta, 0),
        index=df.index,
    )

    loss = pd.Series(
        np.where(delta < 0, -delta, 0),
        index=df.index,
    )

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    df["rsi"] = 100 - (
        100 / (1 + rs)
    )

    return df


def get_trend(latest_row):
    """Return the EMA-based trend direction."""

    if latest_row["ema_9"] > latest_row["ema_21"]:
        return "BULLISH"

    if latest_row["ema_9"] < latest_row["ema_21"]:
        return "BEARISH"

    return "NEUTRAL"


def make_bars_request(
    timeframe,
    start,
    end,
):
    """Create a stock-bar request for configured symbols."""

    return StockBarsRequest(
        symbol_or_symbols=SYMBOLS,
        timeframe=timeframe,
        start=start,
        end=end,
        limit=1000,
        feed=DataFeed.IEX,
    )


# --------------------------------------------------
# SIGNAL JOURNAL
# --------------------------------------------------

def save_signal(
    symbol,
    latest,
    latest_volume,
    previous_volume_average,
    volume_ratio,
    volume_confirmed,
    trend_5m,
    trend_15m,
    trend_1h,
    timeframes_aligned,
    signal,
    signal_reason,
    data_age_minutes,
    score,
    trade_approved,
    approval_reason,
):
    """Append the latest scanner decision to signals.csv."""

    signal_row = pd.DataFrame(
        [
            {
                "timestamp": latest["timestamp"],
                "symbol": symbol,
                "latest_close": latest["close"],
                "ema_9": latest["ema_9"],
                "ema_21": latest["ema_21"],
                "vwap": latest["vwap"],
                "rsi": latest["rsi"],
                "macd": latest["macd"],
                "macd_signal": latest["macd_signal"],
                "latest_volume": latest_volume,
                "volume_avg_20": previous_volume_average,
                "volume_ratio": volume_ratio,
                "volume_confirmed": volume_confirmed,
                "trend_5m": trend_5m,
                "trend_15m": trend_15m,
                "trend_1h": trend_1h,
                "timeframes_aligned": timeframes_aligned,
                "signal_reason": signal_reason,
                "data_age_minutes": data_age_minutes,
                "signal": signal,
                "confidence_score": score,
                "trade_approved": trade_approved,
                "approval_reason": approval_reason,
            }
        ],
        columns=SIGNAL_COLUMNS,
    )

    signal_row.to_csv(
        SIGNALS_FILE,
        mode="a",
        header=False,
        index=False,
    )


# --------------------------------------------------
# INITIALIZATION
# --------------------------------------------------

ensure_signals_file()

data_client = StockHistoricalDataClient(
    api_key,
    secret_key,
)

trading_client = TradingClient(
    api_key,
    secret_key,
    paper=True,
)


# --------------------------------------------------
# SCANNER
# --------------------------------------------------

print("=" * 50)
print("         LOCKBOT MARKET SCANNER v0.7")
print("=" * 50)

try:
    # --------------------------------------------------
    # DAILY RISK CHECK
    # --------------------------------------------------

    risk_status = check_daily_loss_limit()

    print("\n" + "=" * 50)
    print("LOCKBOT DAILY RISK CHECK")
    print("=" * 50)

    print(
        f"Previous Equity : "
        f"${risk_status['previous_equity']:,.2f}"
    )

    print(
        f"Current Equity  : "
        f"${risk_status['current_equity']:,.2f}"
    )

    print(
        f"Daily P/L       : "
        f"${risk_status['daily_pl_dollars']:,.2f}"
    )

    print(
        f"Daily P/L %     : "
        f"{risk_status['daily_pl_percent']:.2f}%"
    )

    print(
        f"Trading Blocked : "
        f"{risk_status['trading_blocked']}"
    )

    print(
        f"Risk Decision   : "
        f"{risk_status['block_reason']}"
    )

    if risk_status["trading_blocked"]:
        send_smart_notification(
            symbol="SYSTEM",
            event_type="DAILY_LOSS_LIMIT",
            title="LockBot Trading Blocked",
            reason="DAILY_LOSS_LIMIT_REACHED",
            cooldown_minutes=720,
            message=(
                "New LockBot entries have been disabled.\n\n"
                f"Daily P/L: "
                f"${risk_status['daily_pl_dollars']:,.2f}\n"
                f"Daily P/L %: "
                f"{risk_status['daily_pl_percent']:.2f}%\n\n"
                f"Reason: {risk_status['block_reason']}"
            ),
        )

        print(
            "\nNew entries are disabled because the "
            "daily loss limit was reached."
        )

        raise SystemExit(0)

    # --------------------------------------------------
    # MARKET DATA REQUESTS
    # --------------------------------------------------

    end_time = datetime.now(timezone.utc)
    start_time_5m = end_time - timedelta(days=5)
    start_time_higher = end_time - timedelta(days=15)

    request_5m = make_bars_request(
        timeframe=TimeFrame(
            5,
            TimeFrameUnit.Minute,
        ),
        start=start_time_5m,
        end=end_time,
    )

    request_15m = make_bars_request(
        timeframe=TimeFrame(
            15,
            TimeFrameUnit.Minute,
        ),
        start=start_time_higher,
        end=end_time,
    )

    request_1h = make_bars_request(
        timeframe=TimeFrame(
            1,
            TimeFrameUnit.Hour,
        ),
        start=start_time_higher,
        end=end_time,
    )

    bar_set_5m = data_client.get_stock_bars(request_5m)
    bar_set_15m = data_client.get_stock_bars(request_15m)
    bar_set_1h = data_client.get_stock_bars(request_1h)

    account = trading_client.get_account()
    market_clock = trading_client.get_clock()

    account_equity = float(account.equity)
    buying_power = float(account.buying_power)

    print("\nPaper Account Check")
    print(f"Account Status : {account.status}")
    print(f"Equity         : ${account_equity:,.2f}")
    print(f"Buying Power   : ${buying_power:,.2f}")
    print(f"Market Open    : {market_clock.is_open}")

    # --------------------------------------------------
    # SYMBOL SCANNING LOOP
    # --------------------------------------------------

    for symbol in SYMBOLS:
        bars_5m = bar_set_5m.data.get(symbol, [])
        bars_15m = bar_set_15m.data.get(symbol, [])
        bars_1h = bar_set_1h.data.get(symbol, [])

        if not bars_5m:
            print(
                f"\nNo 5-minute bar data returned for {symbol}."
            )
            continue

        if not bars_15m:
            print(
                f"\nNo 15-minute bar data returned for {symbol}."
            )
            continue

        if not bars_1h:
            print(
                f"\nNo 1-hour bar data returned for {symbol}."
            )
            continue

        df_5m = add_indicators(
            bars_to_dataframe(bars_5m)
        )

        df_15m = add_indicators(
            bars_to_dataframe(bars_15m)
        )

        df_1h = add_indicators(
            bars_to_dataframe(bars_1h)
        )

        if len(df_5m) < 22:
            print(
                f"\nNot enough 5-minute data for {symbol}."
            )
            continue

        if len(df_15m) < 21:
            print(
                f"\nNot enough 15-minute data for {symbol}."
            )
            continue

        if len(df_1h) < 21:
            print(
                f"\nNot enough 1-hour data for {symbol}."
            )
            continue

        latest = df_5m.iloc[-1]
        latest_15m = df_15m.iloc[-1]
        latest_1h = df_1h.iloc[-1]

        previous_volume_average = float(
            latest["volume_avg_20"]
        )

        latest_volume = float(
            latest["volume"]
        )

        if (
            np.isfinite(previous_volume_average)
            and previous_volume_average > 0
        ):
            volume_ratio = (
                latest_volume / previous_volume_average
            )

        else:
            volume_ratio = 0.0

        volume_confirmed = (
            volume_ratio >= MIN_VOLUME_RATIO
        )

        bar_time = latest["timestamp"].to_pydatetime()

        data_age_minutes = (
            datetime.now(timezone.utc) - bar_time
        ).total_seconds() / 60

        data_is_fresh = data_age_minutes <= 10

        trend_5m = get_trend(latest)
        trend_15m = get_trend(latest_15m)
        trend_1h = get_trend(latest_1h)

        bullish_timeframes_aligned = (
            trend_5m == "BULLISH"
            and trend_15m == "BULLISH"
            and trend_1h == "BULLISH"
        )

        bearish_timeframes_aligned = (
            trend_5m == "BEARISH"
            and trend_15m == "BEARISH"
            and trend_1h == "BEARISH"
        )

        timeframes_aligned = (
            bullish_timeframes_aligned
            or bearish_timeframes_aligned
        )

        # --------------------------------------------------
        # SIGNAL GENERATION
        # --------------------------------------------------

        if not data_is_fresh:
            signal = "NO_TRADE"
            signal_reason = "STALE_DATA"

        elif (
            trend_5m == "BULLISH"
            and latest["close"] > latest["ema_9"]
            and latest["close"] > latest["vwap"]
            and 50 < latest["rsi"] < 70
            and latest["macd"] > latest["macd_signal"]
        ):
            signal = "BUY_CALL"
            signal_reason = (
                "BULLISH_EMA_RSI_VWAP_MACD"
            )

        elif (
            trend_5m == "BEARISH"
            and latest["close"] < latest["ema_9"]
            and latest["close"] < latest["vwap"]
            and 30 < latest["rsi"] < 50
            and latest["macd"] < latest["macd_signal"]
        ):
            signal = "BUY_PUT"
            signal_reason = (
                "BEARISH_EMA_RSI_VWAP_MACD"
            )

        else:
            signal = "NO_TRADE"
            signal_reason = "SETUP_NOT_CONFIRMED"

        # --------------------------------------------------
        # CONFIDENCE SCORE
        # --------------------------------------------------

        score = 0

        if trend_5m in {"BULLISH", "BEARISH"}:
            score += 20

        if (
            trend_5m == "BULLISH"
            and latest["close"] > latest["ema_9"]
        ):
            score += 20

        elif (
            trend_5m == "BEARISH"
            and latest["close"] < latest["ema_9"]
        ):
            score += 20

        if (
            trend_5m == "BULLISH"
            and latest["macd"] > latest["macd_signal"]
        ):
            score += 20

        elif (
            trend_5m == "BEARISH"
            and latest["macd"] < latest["macd_signal"]
        ):
            score += 20

        if 50 < latest["rsi"] < 70:
            score += 20

        elif 30 < latest["rsi"] < 50:
            score += 20

        if (
            trend_5m == "BULLISH"
            and latest["close"] > latest["vwap"]
        ):
            score += 20

        elif (
            trend_5m == "BEARISH"
            and latest["close"] < latest["vwap"]
        ):
            score += 20

        # --------------------------------------------------
        # TRADE APPROVAL
        # --------------------------------------------------

        if not market_clock.is_open:
            trade_approved = False
            approval_reason = "MARKET_IS_CLOSED"

        elif not data_is_fresh:
            trade_approved = False
            approval_reason = "DATA_IS_STALE"

        elif signal == "NO_TRADE":
            trade_approved = False
            approval_reason = "NO_VALID_SIGNAL"

        elif not timeframes_aligned:
            trade_approved = False
            approval_reason = "TIMEFRAMES_NOT_ALIGNED"

        elif not volume_confirmed:
            trade_approved = False
            approval_reason = "LOW_VOLUME"

        elif signal == "BUY_PUT":
            trade_approved = False
            approval_reason = (
                "OPTIONS_EXECUTION_NOT_ENABLED"
            )

        elif score < MIN_CONFIDENCE_SCORE:
            trade_approved = False
            approval_reason = "CONFIDENCE_TOO_LOW"

        else:
            trade_approved = True
            approval_reason = (
                "APPROVED_FOR_PAPER_EXECUTION"
            )

        position_size = calculate_position_size(
            account_equity=account_equity,
            buying_power=buying_power,
            entry_price=float(latest["close"]),
        )

        if position_size <= 0:
            trade_approved = False
            approval_reason = (
                "POSITION_SIZE_CALCULATION_FAILED"
            )

        # --------------------------------------------------
        # OUTPUT
        # --------------------------------------------------

        print(f"\n{symbol} Market Snapshot")
        print(f"Latest Close : ${latest['close']:.2f}")
        print(f"9 EMA        : ${latest['ema_9']:.2f}")
        print(f"21 EMA       : ${latest['ema_21']:.2f}")
        print(f"VWAP         : ${latest['vwap']:.2f}")
        print(f"RSI          : {latest['rsi']:.2f}")
        print(f"MACD         : {latest['macd']:.4f}")

        print(
            f"MACD Signal  : "
            f"{latest['macd_signal']:.4f}"
        )

        print(f"5m Trend     : {trend_5m}")
        print(f"15m Trend    : {trend_15m}")
        print(f"1h Trend     : {trend_1h}")
        print(f"TF Alignment : {timeframes_aligned}")
        print(f"Latest Volume: {latest_volume:,.0f}")

        print(
            f"20-Bar Avg   : "
            f"{previous_volume_average:,.0f}"
        )

        print(f"Volume Ratio : {volume_ratio:.2f}x")
        print(f"Volume Pass  : {volume_confirmed}")

        print("\nTrade Decision")
        print(f"Signal        : {signal}")
        print(f"Reason        : {signal_reason}")

        print(
            f"Data Age      : "
            f"{data_age_minutes:.1f} minutes"
        )

        print(f"Confidence    : {score}/100")
        print(f"Approved      : {trade_approved}")
        print(f"Approval Info : {approval_reason}")

        print(
            f"Suggested Size: "
            f"{position_size} shares"
        )

        estimated_position_value = (
            position_size * float(latest["close"])
        )

        estimated_dollar_risk = (
            estimated_position_value
            * STOP_LOSS_PERCENT
        )

        print(
            f"Estimated Cost: "
            f"${estimated_position_value:,.2f}"
        )

        print(
            f"Estimated Risk: "
            f"${estimated_dollar_risk:,.2f}"
        )

        # --------------------------------------------------
        # SAVE SIGNAL
        # --------------------------------------------------

        save_signal(
            symbol=symbol,
            latest=latest,
            latest_volume=latest_volume,
            previous_volume_average=(
                previous_volume_average
            ),
            volume_ratio=volume_ratio,
            volume_confirmed=volume_confirmed,
            trend_5m=trend_5m,
            trend_15m=trend_15m,
            trend_1h=trend_1h,
            timeframes_aligned=timeframes_aligned,
            signal=signal,
            signal_reason=signal_reason,
            data_age_minutes=data_age_minutes,
            score=score,
            trade_approved=trade_approved,
            approval_reason=approval_reason,
        )

        print("Signal saved to signals.csv")

        # --------------------------------------------------
        # REJECTED TRADE NOTIFICATIONS
        # --------------------------------------------------

        if not trade_approved:
            important_rejection_reasons = {
                "LOW_VOLUME",
                "TIMEFRAMES_NOT_ALIGNED",
                "CONFIDENCE_TOO_LOW",
                "OPTIONS_EXECUTION_NOT_ENABLED",
            }

            if approval_reason in important_rejection_reasons:
                send_smart_notification(
                    symbol=symbol,
                    event_type="TRADE_REJECTED",
                    title="LockBot Trade Rejected",
                    reason=approval_reason,
                    cooldown_minutes=60,
                    message=(
                        f"{symbol} setup rejected\n\n"
                        f"Signal: {signal}\n"
                        f"Reason: {approval_reason}\n"
                        f"Confidence: {score}/100\n"
                        f"Volume ratio: {volume_ratio:.2f}x\n"
                        f"5m: {trend_5m}\n"
                        f"15m: {trend_15m}\n"
                        f"1h: {trend_1h}"
                    ),
                )

            continue

        # --------------------------------------------------
        # TRADE OPPORTUNITY NOTIFICATION
        # --------------------------------------------------

        send_smart_notification(
            symbol=symbol,
            event_type="TRADE_OPPORTUNITY",
            title="LockBot Trade Opportunity",
            reason=signal,
            message=(
                f"{symbol} {signal} setup detected\n\n"
                f"Confidence: {score}/100\n"
                f"Volume confirmed: YES\n"
                f"Timeframes aligned: YES\n"
                f"5m: {trend_5m}\n"
                f"15m: {trend_15m}\n"
                f"1h: {trend_1h}\n\n"
                f"Suggested size: {position_size} shares"
            ),
        )

        # --------------------------------------------------
        # DUPLICATE AND EXPOSURE CHECKS
        # --------------------------------------------------

        current_positions = trading_client.get_all_positions()

        if len(current_positions) >= MAX_OPEN_TRADES:
            print(
                "ORDER BLOCKED: Maximum number of "
                "open positions has been reached."
            )
            continue

        already_has_position = any(
            position.symbol == symbol
            for position in current_positions
        )

        if already_has_position:
            print(
                f"ORDER BLOCKED: An open {symbol} "
                "position already exists."
            )
            continue

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

        if already_has_open_order:
            print(
                f"ORDER BLOCKED: An open {symbol} "
                "order already exists."
            )
            continue

        if signal != "BUY_CALL":
            print(
                f"ORDER BLOCKED: Unsupported signal "
                f"{signal}."
            )
            continue

        # --------------------------------------------------
        # PAPER ORDER SUBMISSION
        # --------------------------------------------------

        print("\n==============================")
        print("PAPER TRADE")
        print("==============================")

        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=position_size,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )

        submitted_order = trading_client.submit_order(
            order_data=order_request
        )

        print(
            f"Submitted PAPER order for {symbol}"
        )

        print(f"Quantity       : {position_size}")
        print(f"Order ID       : {submitted_order.id}")

        print(
            f"Order Status   : "
            f"{submitted_order.status}"
        )

        send_smart_notification(
            symbol=symbol,
            event_type="PAPER_ORDER_SUBMITTED",
            title="🚀 LockBot Paper Order",
            reason=signal,
            message=(
                f"{symbol}\n\n"
                f"Signal: {signal}\n"
                f"Shares: {position_size}\n"
                f"Entry: ${latest['close']:.2f}\n"
                f"Risk: ${estimated_dollar_risk:,.2f}\n"
                f"Confidence: {score}/100\n\n"
                f"Trend:\n"
                f"5m: {trend_5m}\n"
                f"15m: {trend_15m}\n"
                f"1h: {trend_1h}\n\n"
                f"Status: {submitted_order.status}"
            ),
        )

except Exception as error:
    error_type = type(error).__name__

    print("\nMarket scanner failed.")
    print(f"{error_type}: {error}")

    send_smart_notification(
        symbol="SYSTEM",
        event_type="SCANNER_ERROR",
        title="⚠️ LockBot Scanner Error",
        reason=error_type,
        cooldown_minutes=30,
        message=(
            "LockBot encountered an error.\n\n"
            f"Type: {error_type}\n"
            f"Details: {error}"
        ),
    )