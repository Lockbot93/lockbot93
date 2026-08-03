import csv
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from risk_engine import check_daily_loss_limit
from risk_manager import (
    TradeRiskRequest,
    evaluate_trade_request,
    get_trades_submitted_today,
    print_risk_decision,
    record_trade_submission,
)
import numpy as np
import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
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
from retry_utils import with_retries
from trade_journal import record_event
from indicators import add_indicators
from market_regime import get_market_regime
from regime_filter import check_regime_approval
from notifications import send_smart_notification
from scanner_state import save_state
from trade_manager import register_bracket_trade
from system_heartbeat import (
    mark_module_critical,
    mark_module_degraded,
    mark_module_healthy,
    mark_module_starting,
)
import lockbot_config as config


load_dotenv()

PROJECT_FOLDER = Path(__file__).resolve().parent
SIGNALS_FILE = PROJECT_FOLDER / "signals.csv"

LOCKBOT_VERSION = config.LOCKBOT_PROJECT_VERSION

# All shared constants below come from lockbot_config.py — the single
# source of truth. Do not redefine these locally; edit lockbot_config.py
# instead so every module stays in sync.
SYMBOLS = config.SYMBOLS

MIN_CONFIDENCE_SCORE = config.MIN_SIGNAL_CONFIDENCE
MIN_VOLUME_RATIO = config.MIN_VOLUME_RATIO

MAX_RISK_PER_TRADE = config.MAX_RISK_PER_TRADE_PERCENT
MAX_OPEN_TRADES = config.MAX_OPEN_POSITIONS
STOP_LOSS_PERCENT = config.BRACKET_STOP_LOSS_PERCENT
TAKE_PROFIT_PERCENT = config.BRACKET_TAKE_PROFIT_PERCENT
MAX_POSITION_PERCENT = config.MAX_POSITION_VALUE_PERCENT

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

api_key = os.getenv("ALPACA_API_KEY")
secret_key = os.getenv("ALPACA_SECRET_KEY")

if not api_key or not secret_key:
    raise RuntimeError(
        "Alpaca API keys were not found in the .env file."
    )


def ensure_signals_file():
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


def calculate_position_size(
    account_equity,
    buying_power,
    entry_price,
):
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


def bars_to_dataframe(bars):
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


def get_trend(latest_row):
    if latest_row["ema_9"] > latest_row["ema_21"]:
        return "BULLISH"

    if latest_row["ema_9"] < latest_row["ema_21"]:
        return "BEARISH"

    return "NEUTRAL"


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


def make_bars_request(
    timeframe,
    start,
    end,
):
    return StockBarsRequest(
        symbol_or_symbols=SYMBOLS,
        timeframe=timeframe,
        start=start,
        end=end,
        limit=1000,
        feed=DataFeed.IEX,
    )


def main():
    """Run one complete LOCKBOT market-scanning cycle."""

    scan_started_monotonic = time.monotonic()
    scan_started_at = datetime.now(timezone.utc)

    metrics = {
        "symbols_requested": len(SYMBOLS),
        "symbols_completed": 0,
        "symbols_skipped": 0,
        "signals_generated": 0,
        "trades_approved": 0,
        "orders_submitted": 0,
    }

    mark_module_starting(
        "MARKET_SCANNER",
        f"LOCKBOT Market Scanner v{LOCKBOT_VERSION} is starting.",
        details={
            "version": LOCKBOT_VERSION,
            "symbols": SYMBOLS,
            "started_at": scan_started_at.isoformat(),
        },
    )

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

    print("=" * 50)
    print(f"         LOCKBOT MARKET SCANNER v{LOCKBOT_VERSION}")
    print("=" * 50)

    try:
        bar_set_5m = with_retries(data_client.get_stock_bars)(request_5m)
        bar_set_15m = with_retries(data_client.get_stock_bars)(request_15m)
        bar_set_1h = with_retries(data_client.get_stock_bars)(request_1h)

        account = with_retries(trading_client.get_account)()
        market_clock = with_retries(trading_client.get_clock)()

        account_equity = float(account.equity)
        previous_close_equity = float(account.last_equity)
        buying_power = float(account.buying_power)

        (
            daily_loss_limit_reached,
            daily_pnl,
            daily_pnl_percent,
            daily_loss_reason,
        ) = check_daily_loss_limit(
            current_equity=account_equity,
            previous_close_equity=previous_close_equity,
        )

        print("\nPaper Account Check")
        print(f"Account Status : {account.status}")
        print(f"Equity         : ${account_equity:,.2f}")
        print(f"Prior Equity   : ${previous_close_equity:,.2f}")
        print(f"Daily P&L      : ${daily_pnl:,.2f}")
        print(f"Daily P&L %    : {daily_pnl_percent:.2%}")
        print(f"Loss Limit Hit : {daily_loss_limit_reached}")
        print(f"Buying Power   : ${buying_power:,.2f}")
        print(f"Market Open    : {market_clock.is_open}")
        save_state(
            {
                "scan_time": datetime.now(timezone.utc).isoformat(),
                "market_open": market_clock.is_open,
                "account_equity": account_equity,
                "buying_power": buying_power,
                "daily_pnl": daily_pnl,
                "daily_pnl_percent": daily_pnl_percent,
                "daily_loss_limit_hit": daily_loss_limit_reached,
            }
        )
        symbol_results = {}

        for symbol in SYMBOLS:

            bars_5m = bar_set_5m.data.get(symbol, [])
            bars_15m = bar_set_15m.data.get(symbol, [])
            bars_1h = bar_set_1h.data.get(symbol, [])

            if not bars_5m:
                print(f"\nNo 5-minute bar data returned for {symbol}.")
                metrics["symbols_skipped"] += 1
                continue

            if not bars_15m:
                print(f"\nNo 15-minute bar data returned for {symbol}.")
                metrics["symbols_skipped"] += 1
                continue

            if not bars_1h:
                print(f"\nNo 1-hour bar data returned for {symbol}.")
                metrics["symbols_skipped"] += 1
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
                metrics["symbols_skipped"] += 1
                continue

            if len(df_15m) < 21:
                print(
                    f"\nNot enough 15-minute data for {symbol}."
                )
                metrics["symbols_skipped"] += 1
                continue

            if len(df_1h) < 21:
                print(
                    f"\nNot enough 1-hour data for {symbol}."
                )
                metrics["symbols_skipped"] += 1
                continue

            market_regime = get_market_regime(df_5m)

            latest = df_5m.iloc[-1]
            latest_15m = df_15m.iloc[-1]
            latest_1h = df_1h.iloc[-1]

            previous_volume_average = float(
                latest["volume_avg_20"]
            )

            latest_volume = float(latest["volume"])

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
                signal = "BUY_LONG"
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
                signal = "SELL_SHORT"
                signal_reason = (
                    "BEARISH_EMA_RSI_VWAP_MACD"
                )

            else:
                signal = "NO_TRADE"
                signal_reason = "SETUP_NOT_CONFIRMED"

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

            if not market_clock.is_open:
                trade_approved = False
                approval_reason = "MARKET_IS_CLOSED"

            elif daily_loss_limit_reached:
                trade_approved = False
                approval_reason = daily_loss_reason

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

            else:
                regime_approved, regime_reason = (
                    check_regime_approval(
                        signal=signal,
                        market_regime=market_regime,
                    )
                )

                if not regime_approved:
                    trade_approved = False
                    approval_reason = regime_reason

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
            print("\nMarket Regime")
            print(f"Regime       : {market_regime['regime']}")
            print(f"ADX          : {market_regime['adx']:.2f}")
            print(f"ATR %        : {market_regime['atr_percent']:.2f}%")
            print(f"Direction    : {market_regime['trend_direction']}")
            print(f"Volatility   : {market_regime['volatility_state']}")

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
            metrics["symbols_completed"] += 1
            if signal != "NO_TRADE":
                metrics["signals_generated"] += 1
            if trade_approved:
                metrics["trades_approved"] += 1
            symbol_results[symbol] = {
                "signal": signal,
                "confidence": score,
                "approved": trade_approved,
                "approval_reason": approval_reason,
                "trend_5m": trend_5m,
                "trend_15m": trend_15m,
                "trend_1h": trend_1h,
                "market_regime": market_regime["regime"],
                "latest_price": round(float(latest["close"]), 2),
                "position_size": position_size,
            }

            if not trade_approved:
                important_rejection_reasons = {
                    "LOW_VOLUME",
                    "TIMEFRAMES_NOT_ALIGNED",
                    "CONFIDENCE_TOO_LOW",
                    "DAILY_LOSS_LIMIT_REACHED",
                    "REGIME_BLOCKED_RANGING",
                    "REGIME_BLOCKED_HIGH_VOLATILITY",
                    "REGIME_BLOCKED_UNKNOWN",
                    # FIXED: regime_filter.check_regime_approval() actually
                    # returns REGIME_OPPOSES_LONG / REGIME_OPPOSES_SHORT.
                    # The old CALL/PUT strings here never matched anything,
                    # so this whole notification case silently never fired.
                    "REGIME_OPPOSES_LONG",
                    "REGIME_OPPOSES_SHORT",
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
                            f"Daily P&L: ${daily_pnl:,.2f} "
                            f"({daily_pnl_percent:.2%})\n"
                            f"Volume ratio: {volume_ratio:.2f}x\n"
                            f"Regime: {market_regime['regime']}\n"
                            f"ADX: {market_regime['adx']:.2f}\n"
                            f"5m: {trend_5m}\n"
                            f"15m: {trend_15m}\n"
                            f"1h: {trend_1h}"
                        ),
                    )

                continue

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
                    f"Regime: {market_regime['regime']}\n"
                    f"ADX: {market_regime['adx']:.2f}\n"
                    f"5m: {trend_5m}\n"
                    f"15m: {trend_15m}\n"
                    f"1h: {trend_1h}\n\n"
                    f"Suggested size: {position_size} shares"
                ),
            )

            positions = trading_client.get_all_positions()

            if len(positions) >= MAX_OPEN_TRADES:
                print(
                    "ORDER BLOCKED: Maximum number of "
                    "open positions has been reached."
                )
                continue

            already_has_position = any(
                position.symbol == symbol
                for position in positions
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

            if signal not in {"BUY_LONG", "SELL_SHORT"}:
                print(
                    f"ORDER BLOCKED: Unsupported signal "
                    f"{signal}."
                )
                continue

            is_long = signal == "BUY_LONG"
            trade_side_label = "LONG" if is_long else "SHORT"
            order_side = OrderSide.BUY if is_long else OrderSide.SELL

            print("\n==============================")
            print(f"PAPER BRACKET TRADE ({trade_side_label})")
            print("==============================")

            reference_entry_price = float(latest["close"])

            if is_long:
                stop_loss_price = round(
                    reference_entry_price
                    * (1 - STOP_LOSS_PERCENT),
                    2,
                )

                take_profit_price = round(
                    reference_entry_price
                    * (1 + TAKE_PROFIT_PERCENT),
                    2,
                )

                if stop_loss_price <= 0:
                    print(
                        "ORDER BLOCKED: Invalid stop-loss price."
                    )
                    continue

                if take_profit_price <= reference_entry_price:
                    print(
                        "ORDER BLOCKED: Invalid take-profit price."
                    )
                    continue

            else:
                # SHORT: the stop-loss protects against the price
                # rising (so it sits ABOVE entry), and the take-profit
                # targets the price falling (so it sits BELOW entry) —
                # the mirror image of the long case above.
                stop_loss_price = round(
                    reference_entry_price
                    * (1 + STOP_LOSS_PERCENT),
                    2,
                )

                take_profit_price = round(
                    reference_entry_price
                    * (1 - TAKE_PROFIT_PERCENT),
                    2,
                )

                if take_profit_price <= 0:
                    print(
                        "ORDER BLOCKED: Invalid take-profit price."
                    )
                    continue

                if stop_loss_price <= reference_entry_price:
                    print(
                        "ORDER BLOCKED: Invalid stop-loss price."
                    )
                    continue

            order_request = MarketOrderRequest(
                symbol=symbol,
                qty=position_size,
                side=order_side,
                time_in_force=TimeInForce.GTC,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(
                    limit_price=take_profit_price,
                ),
                stop_loss=StopLossRequest(
                    stop_price=stop_loss_price,
                ),
            )
            risk_request = TradeRiskRequest(
                symbol=symbol,
                side=trade_side_label,
                quantity=position_size,
                estimated_entry_price=reference_entry_price,
                estimated_stop_price=stop_loss_price,
                account_equity=account_equity,
                daily_profit_loss=daily_pnl,
                trades_today=get_trades_submitted_today(),
                open_position_count=len(positions),
                total_open_exposure=sum(
                    abs(float(position.market_value))
                    for position in positions
                ),
                existing_position_quantity=next(
                    (
                        float(position.qty)
                        for position in positions
                        if position.symbol == symbol
                    ),
                    0.0,
                ),
                duplicate_open_order_exists=already_has_open_order,
            )

            risk_decision = evaluate_trade_request(
                risk_request
            )

            print_risk_decision(risk_decision)

            record_event(
                event_type=(
                    "TRADE_APPROVED"
                    if risk_decision.approved
                    else "TRADE_REJECTED"
                ),
                symbol=symbol,
                action=trade_side_label,
                status=str(risk_decision.decision),
                reason=risk_decision.reason,
                quantity=position_size,
                entry_price=reference_entry_price,
                confidence=score,
                market_regime=market_regime["regime"],
                estimated_risk_dollars=(
                    risk_decision.estimated_risk_dollars
                ),
                estimated_risk_percent=(
                    risk_decision.estimated_risk_percent
                ),
            )

            if not risk_decision.approved:
                print(
                    f"ORDER BLOCKED BY RISK MANAGER: "
                    f"{risk_decision.reason}"
                )
                continue

            submitted_order = trading_client.submit_order(
                order_data=order_request
            )

            record_event(
                event_type="ORDER_SUBMITTED",
                symbol=symbol,
                action=trade_side_label,
                status=str(submitted_order.status),
                reason="Paper bracket order submitted.",
                quantity=position_size,
                entry_price=reference_entry_price,
                confidence=score,
                market_regime=market_regime["regime"],
                estimated_risk_dollars=(
                    risk_decision.estimated_risk_dollars
                ),
                estimated_risk_percent=(
                    risk_decision.estimated_risk_percent
                ),
                order_id=str(submitted_order.id),
            )

            trades_submitted_today = record_trade_submission()

            print(
                f"Daily Trade Count : "
                f"{trades_submitted_today}"
            )

            metrics["orders_submitted"] += 1

            trade_registered = register_bracket_trade(
                parent_order_id=str(submitted_order.id),
                strategy_version=LOCKBOT_VERSION,
                symbol=symbol,
                side=trade_side_label,
                market_regime=market_regime["regime"],
                confidence=score,
                position_value=estimated_position_value,
                account_equity_at_entry=account_equity,
                initial_risk_dollars=estimated_dollar_risk,
                stop_loss_percent=STOP_LOSS_PERCENT,
                take_profit_percent=TAKE_PROFIT_PERCENT,
                paper_trade=True,
            )

            print(
                f"Submitted PAPER bracket order for {symbol}"
            )
            print(f"Quantity       : {position_size}")
            print(
                f"Reference Entry: "
                f"${reference_entry_price:.2f}"
            )
            print(
                f"Stop Loss      : "
                f"${stop_loss_price:.2f}"
            )
            print(
                f"Take Profit    : "
                f"${take_profit_price:.2f}"
            )
            print(f"Order ID       : {submitted_order.id}")
            print(
                f"Order Status   : "
                f"{submitted_order.status}"
            )
            print(
                f"Trade Registered: {trade_registered}"
            )
            print(
                f"Strategy Version: v{LOCKBOT_VERSION}"
            )

            send_smart_notification(
                symbol=symbol,
                event_type=(
                    "BUY_ORDER_SUBMITTED"
                    if is_long
                    else "SHORT_ORDER_SUBMITTED"
                ),
                title=(
                    "🟢 LockBot BUY Order Submitted"
                    if is_long
                    else "🔴 LockBot SHORT Order Submitted"
                ),
                reason=signal,
                message=(
                    f"{symbol} {trade_side_label} paper bracket order submitted\n\n"
                    f"Shares: {position_size}\n"
                    f"Reference entry: "
                    f"${reference_entry_price:.2f}\n"
                    f"Stop loss: ${stop_loss_price:.2f}\n"
                    f"Take profit: ${take_profit_price:.2f}\n"
                    f"Estimated position: "
                    f"${estimated_position_value:,.2f}\n"
                    f"Estimated risk: "
                    f"${estimated_dollar_risk:,.2f}\n"
                    f"Confidence: {score}/100\n"
                    f"Regime: {market_regime['regime']}\n"
                    f"Daily P&L: ${daily_pnl:,.2f} "
                    f"({daily_pnl_percent:.2%})\n"
                    f"Account equity: "
                    f"${account_equity:,.2f}\n"
                    f"Order status: {submitted_order.status}\n"
                    f"Registered: {trade_registered}\n"
                    f"Version: v{LOCKBOT_VERSION}"
                ),
            )
        save_state(
            {
                "scan_time": datetime.now(timezone.utc).isoformat(),
                "market_open": market_clock.is_open,
                "account_equity": account_equity,
                "buying_power": buying_power,
                "daily_pnl": daily_pnl,
                "daily_pnl_percent": daily_pnl_percent,
                "daily_loss_limit_hit": daily_loss_limit_reached,
                "symbols": symbol_results,
            }
        )

        scan_completed_at = datetime.now(timezone.utc)
        scan_duration_seconds = round(
            time.monotonic() - scan_started_monotonic,
            2,
        )

        heartbeat_details = {
            **metrics,
            "version": LOCKBOT_VERSION,
            "symbols": SYMBOLS,
            "market_open": bool(market_clock.is_open),
            "account_equity": account_equity,
            "buying_power": buying_power,
            "daily_pnl": daily_pnl,
            "daily_pnl_percent": daily_pnl_percent,
            "daily_loss_limit_hit": daily_loss_limit_reached,
            "started_at": scan_started_at.isoformat(),
            "completed_at": scan_completed_at.isoformat(),
            "duration_seconds": scan_duration_seconds,
        }

        if metrics["symbols_completed"] == len(SYMBOLS):
            mark_module_healthy(
                "MARKET_SCANNER",
                (
                    f"Market scan completed successfully for "
                    f"{metrics['symbols_completed']} symbols."
                ),
                details=heartbeat_details,
            )
            heartbeat_status = "HEALTHY"
        else:
            mark_module_degraded(
                "MARKET_SCANNER",
                (
                    "Market scan completed with one or more symbols "
                    "skipped because usable market data was unavailable."
                ),
                details=heartbeat_details,
            )
            heartbeat_status = "DEGRADED"

        print("\nScan Summary")
        print("-" * 50)
        print(f"Symbols Requested : {metrics['symbols_requested']}")
        print(f"Symbols Completed : {metrics['symbols_completed']}")
        print(f"Symbols Skipped   : {metrics['symbols_skipped']}")
        print(f"Signals Generated : {metrics['signals_generated']}")
        print(f"Trades Approved   : {metrics['trades_approved']}")
        print(f"Orders Submitted  : {metrics['orders_submitted']}")
        print(f"Duration          : {scan_duration_seconds:.2f} seconds")
        print(f"Heartbeat         : {heartbeat_status}")

    except Exception as error:
        error_type = type(error).__name__
        scan_duration_seconds = round(
            time.monotonic() - scan_started_monotonic,
            2,
        )

        print("\nMarket scanner failed.")
        print(f"{error_type}: {error}")

        mark_module_critical(
            "MARKET_SCANNER",
            "LOCKBOT Market Scanner encountered an unhandled error.",
            error=f"{error_type}: {error}",
            details={
                **metrics,
                "version": LOCKBOT_VERSION,
                "symbols": SYMBOLS,
                "started_at": scan_started_at.isoformat(),
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": scan_duration_seconds,
            },
        )

        try:
            send_smart_notification(
                symbol="SYSTEM",
                event_type="SCANNER_ERROR",
                title="⚠️ LockBot Scanner Error",
                reason=error_type,
                cooldown_minutes=30,
                message=(
                    f"LockBot encountered an error.\n\n"
                    f"Type: {error_type}\n"
                    f"Details: {error}"
                ),
            )
        except Exception as notification_error:
            print(
                "Scanner error notification also failed: "
                f"{type(notification_error).__name__}: "
                f"{notification_error}"
            )

        raise


if __name__ == "__main__":
    main()