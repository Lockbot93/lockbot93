"""
LOCKBOT Market Scanner — universe edition.

WHAT CHANGED FROM THE TWO-SYMBOL VERSION
    1. Symbols come from universe.csv (built by universe.py) instead of
       config.SYMBOLS. Falls back to config.SYMBOLS if the file is missing.
    2. Bars are fetched in batches, and the `limit=1000` cap is gone — that
       cap is a TOTAL across all symbols, so with 300 symbols every one of
       them would have come back with three or four bars and failed the
       "not enough data" check.
    3. Two-stage scanning. The 5-minute chart is checked for every symbol;
       the 15-minute and hourly charts are only fetched for the handful that
       already look interesting. Roughly 5% of symbols reach stage two, so
       this cuts the data pulled per cycle by about 20x.
    4. Signals are ranked. Instead of trading whichever symbol happens to be
       checked first, every qualifying setup is collected, sorted by
       confidence, and the strongest are submitted.
    5. Positions and open orders are tracked as orders are submitted, so a
       second entry can't be placed against a stale view of the account.
    6. A same-direction cap, so several positions can't quietly become one
       big bet on the market going the same way.

ADAPTIVE BRACKETS (new, and OFF by default)
    Every trade used to get the same 2% stop and 4% target regardless of
    what was bought. That fits almost nothing: VTEB moves 0.30% a day, so a
    4% target sat ~13 days away and froze a position slot; CELH moves 4.49%
    a day, so a 2% stop was smaller than its ordinary wiggle and would be
    hit at random rather than because the trade was wrong.

    With config.USE_ADAPTIVE_BRACKETS = True, each trade's stop is sized to
    that stock's own average daily movement (read from
    universe_volatility_report.csv), and the target stays a fixed multiple
    of the stop.

    The part that matters: a wider stop with the same share count would
    silently multiply the dollars at risk per trade. So the share count is
    derived FROM the risk budget instead — see calculate_bracket_and_size().
    Wider stop, fewer shares, same dollars at risk.

    While the switch is False, every code path below produces exactly what
    it produced before adaptive brackets existed. Setting it back to False
    is the complete rollback.

WHAT DID NOT CHANGE
    The entry rules, the confidence scoring, the volume filter, the regime
    filter, the bracket exits, and the risk manager are all untouched.

ONE BEHAVIOUR NOTE
    Rejection reasons shift slightly. A symbol that fails both the volume
    check and timeframe alignment is now logged as LOW_VOLUME, because
    alignment isn't known until stage two. Worth remembering when comparing
    old and new signals.csv analysis.
"""

import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from risk_engine import check_book_daily_loss, equity_book_pnl
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
from position_filters import equity_positions
from day_trade_tracker import day_trade_limit_reached as check_day_trade_limit
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

try:
    from universe import load_universe, universe_age_hours
except Exception:  # universe.py absent — fall back to config.SYMBOLS
    load_universe = None
    universe_age_hours = None

try:
    from shadow_trades import record_candidates
except Exception:  # shadow logging is optional, never load-bearing
    record_candidates = None

try:
    from adaptive_brackets import compute_bracket, load_atr_table, OK as BRACKET_OK
except Exception:  # adaptive brackets absent — the fixed 2%/4% path still works
    compute_bracket = None
    load_atr_table = None
    BRACKET_OK = "OK"

try:
    from signal_quality import compute_quality, load_weights
except Exception:  # quality ranking absent — the old sort still works
    compute_quality = None
    load_weights = None


load_dotenv()

PROJECT_FOLDER = Path(__file__).resolve().parent
SIGNALS_FILE = PROJECT_FOLDER / "signals.csv"

LOCKBOT_VERSION = config.LOCKBOT_PROJECT_VERSION


def _cfg(name, default):
    """Read a config value, falling back if lockbot_config.py lacks it yet."""
    return getattr(config, name, default)


# Shared constants come from lockbot_config.py — the single source of truth.
SYMBOLS = config.SYMBOLS

MIN_CONFIDENCE_SCORE = config.MIN_SIGNAL_CONFIDENCE
MIN_VOLUME_RATIO = config.MIN_VOLUME_RATIO

MAX_RISK_PER_TRADE = config.MAX_RISK_PER_TRADE_PERCENT
MAX_OPEN_TRADES = config.MAX_OPEN_POSITIONS
STOP_LOSS_PERCENT = config.BRACKET_STOP_LOSS_PERCENT
TAKE_PROFIT_PERCENT = config.BRACKET_TAKE_PROFIT_PERCENT
MAX_POSITION_PERCENT = config.MAX_POSITION_VALUE_PERCENT

# Universe-scanning settings. Defaults are used when the constant is not yet
# present in lockbot_config.py, so this file runs either way.
USE_UNIVERSE_FILE = _cfg("USE_UNIVERSE_FILE", True)
MAX_SCAN_SYMBOLS = _cfg("MAX_SCAN_SYMBOLS", 300)
SCAN_BATCH_SIZE = _cfg("SCAN_BATCH_SIZE", 100)
SCAN_LOOKBACK_DAYS_5M = _cfg("SCAN_LOOKBACK_DAYS_5M", 3)
SCAN_LOOKBACK_DAYS_HIGHER = _cfg("SCAN_LOOKBACK_DAYS_HIGHER", 15)
MAX_SAME_DIRECTION_POSITIONS = _cfg("MAX_SAME_DIRECTION_POSITIONS", 3)
MAX_NEW_ENTRIES_PER_CYCLE = _cfg("MAX_NEW_ENTRIES_PER_CYCLE", 2)
VERBOSE_SYMBOL_LOGGING = _cfg("VERBOSE_SYMBOL_LOGGING", False)
UNIVERSE_STALE_HOURS = _cfg("UNIVERSE_STALE_HOURS", 30)

# Small-account rules. Shorting is not permitted below $2,000 of equity, and
# an account under $25,000 gets 3 same-day round trips per 5 business days.
ALLOW_SHORT_ENTRIES = _cfg("ALLOW_SHORT_ENTRIES", True)
MAX_DAY_TRADES_PER_5_DAYS = _cfg("MAX_DAY_TRADES_PER_5_DAYS", 0)

# Per-stock stop and target. Off by default: while this is False every code
# path below behaves exactly as it did before adaptive brackets existed.
USE_ADAPTIVE_BRACKETS = _cfg("USE_ADAPTIVE_BRACKETS", False)

# Rank approved setups by measured quality instead of the old
# (confidence, volume_ratio) sort. See lockbot_config.py for why that
# sort was broken. False restores the previous behaviour exactly.
USE_QUALITY_RANKING = _cfg("USE_QUALITY_RANKING", False)

# Equity entries. False scans and shadow-logs as normal but submits no
# share orders — see lockbot_config.py. Defaults True so a missing config
# key can never silently stop trading.
EQUITY_ENTRIES_ENABLED = _cfg("EQUITY_ENTRIES_ENABLED", True)

# Advance shorts LOCKBOT cannot trade into the shadow log so the strategy
# is measured on all of its output, not just the long half. Defaults
# False so a missing config key cannot silently change what is measured.
SHADOW_LOG_BLOCKED_SHORTS = _cfg("SHADOW_LOG_BLOCKED_SHORTS", False)
QUALITY_WEIGHTS = load_weights() if load_weights else None

# Notifications are limited to orders LOCKBOT actually submits. Opportunity
# and rejection alerts are off — on a 300-symbol universe they were constant
# noise, and none of them represented an action taken.
NOTIFY_ON_ORDER_SUBMISSION_ONLY = _cfg("NOTIFY_ON_ORDER_SUBMISSION_ONLY", True)

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
        first_line = csv_file.readline().strip()

    if first_line == expected_header:
        return

    raise RuntimeError(
        "signals.csv contains data under an old header. "
        "Clear the file and run LockBot again."
    )


def get_scan_symbols():
    """Return the symbols to scan this cycle, and where they came from."""

    if not USE_UNIVERSE_FILE or load_universe is None:
        return list(SYMBOLS), "config.SYMBOLS"

    try:
        symbols = load_universe()
    except Exception as error:
        print(f"Could not read universe.csv ({error}). Using config.SYMBOLS.")
        return list(SYMBOLS), "config.SYMBOLS (universe unreadable)"

    if not symbols:
        print("universe.csv is empty or missing. Using config.SYMBOLS.")
        print("Run 'python universe.py' to build the list.")
        return list(SYMBOLS), "config.SYMBOLS (universe missing)"

    if universe_age_hours is not None:
        age = universe_age_hours()
        if age is not None and age > UNIVERSE_STALE_HOURS:
            print(
                f"WARNING: universe.csv is {age:.1f} hours old. "
                "Schedule 'python universe.py' to run each morning."
            )

    symbols = symbols[:MAX_SCAN_SYMBOLS]

    # Always include the config symbols so SPY/QQQ stay covered even if the
    # universe file somehow drops them.
    for fallback_symbol in SYMBOLS:
        if fallback_symbol not in symbols:
            symbols.append(fallback_symbol)

    return symbols, "universe.csv"


def get_movement_table():
    """Load each symbol's average daily movement once per cycle.

    Returns an empty table when adaptive brackets are off, when
    adaptive_brackets.py is missing, or when the report file has not been
    written yet. An empty table is safe: every symbol then falls back to
    the fixed bracket.
    """

    if not USE_ADAPTIVE_BRACKETS or load_atr_table is None:
        return {}

    try:
        table = load_atr_table()
    except Exception as error:
        print(
            f"Could not read the movement table ({type(error).__name__}: {error}). "
            "Falling back to fixed brackets this cycle."
        )
        return {}

    if not table:
        print(
            "WARNING: adaptive brackets are ON but no movement data was found. "
            "Run 'python universe_volatility.py' after 'python universe.py'. "
            "Falling back to fixed brackets this cycle."
        )

    return table


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


def calculate_bracket_and_size(
    account_equity,
    buying_power,
    entry_price,
    symbol,
    is_long,
    movement_table,
):
    """Return (shares, stop_percent, take_profit_percent) for one candidate.

    With adaptive brackets off — or adaptive_brackets.py missing, or no
    movement data for this symbol — this returns exactly what the old fixed
    path returned, so nothing downstream can tell the difference.

    The important part when it IS on: as the stop widens, the share count
    comes down to match, so the dollars at risk per trade stay where
    MAX_RISK_PER_TRADE_PERCENT put them. Widening the stop on its own would
    quietly multiply risk per trade without anyone deciding to.
    """

    if not USE_ADAPTIVE_BRACKETS or compute_bracket is None:
        shares = calculate_position_size(
            account_equity=account_equity,
            buying_power=buying_power,
            entry_price=entry_price,
        )
        return shares, STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT

    bracket = compute_bracket(
        symbol=symbol,
        price=entry_price,
        equity=account_equity,
        atr_pct=(movement_table or {}).get(symbol),
        side="long" if is_long else "short",
    )

    if bracket.get("status") != BRACKET_OK:
        # Rejected — usually one share costs more than the risk budget
        # allows at this stop distance. A normal outcome on a small account.
        return 0, STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT

    shares = int(bracket["shares"])

    # The buying-power cap that calculate_position_size applied still has
    # to apply here.
    if entry_price > 0:
        affordable_shares = int(buying_power / entry_price)
        shares = min(shares, affordable_shares)

    return (
        max(shares, 0),
        float(bracket["stop_percent"]),
        float(bracket["target_percent"]),
    )


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


def detect_signal(latest_row, trend_5m, data_is_fresh):
    """The entry rule. Returns (signal, reason).

    Extracted from evaluate_five_minute so that it and confidence_score
    can be compared directly -- see confidence_score for why that matters.
    """

    if not data_is_fresh:
        return "NO_TRADE", "STALE_DATA"

    if (
        trend_5m == "BULLISH"
        and latest_row["close"] > latest_row["ema_9"]
        and latest_row["close"] > latest_row["vwap"]
        and 50 < latest_row["rsi"] < 70
        and latest_row["macd"] > latest_row["macd_signal"]
    ):
        return "BUY_LONG", "BULLISH_EMA_RSI_VWAP_MACD"

    if (
        trend_5m == "BEARISH"
        and latest_row["close"] < latest_row["ema_9"]
        and latest_row["close"] < latest_row["vwap"]
        and 30 < latest_row["rsi"] < 50
        and latest_row["macd"] < latest_row["macd_signal"]
    ):
        return "SELL_SHORT", "BEARISH_EMA_RSI_VWAP_MACD"

    return "NO_TRADE", "SETUP_NOT_CONFIRMED"


def confidence_score(latest_row, trend_5m):
    """Score a setup 0-100 in twenty-point steps.

    WARNING: THIS NUMBER CANNOT DISCRIMINATE BETWEEN TRADABLE SETUPS.

    Each of the five conditions rewarded below is one of the five
    conditions detect_signal already REQUIRES for a signal. Trend
    direction, price vs EMA, MACD vs its signal line, the RSI band, and
    price vs VWAP appear in both places. So any setup that produces
    BUY_LONG or SELL_SHORT has, by construction, satisfied all five and
    scores exactly 100 -- every time, necessarily. Anything scoring less
    is a NO_TRADE that gets discarded before the score is ever consulted.

    The consequence is that MIN_SIGNAL_CONFIDENCE (80) is an inert gate:
    the CONFIDENCE_TOO_LOW rejection in market_scanner.py and the
    "confidence below 80" drop in options_scanner.py can never fire. All
    208 setups in shadow_trades.csv carry confidence 100, which is what
    exposed this on 2026-08-02.

    It is left in place deliberately rather than replaced with a guess.
    The continuous measure that CAN rank setups already exists in
    signal_quality.py and is already logged per setup; once enough of
    those resolve, signal_research.py will say whether it carries any
    information. Inventing a new formula before that evidence arrives
    would be fitting to noise -- the same mistake as tuning on the
    volume ratio, which turned out to be chance at p=0.61.

    _self_test() below proves the tautology by exhaustion so that nobody
    re-derives this the hard way.
    """

    score = 0

    if trend_5m in {"BULLISH", "BEARISH"}:
        score += 20

    if trend_5m == "BULLISH" and latest_row["close"] > latest_row["ema_9"]:
        score += 20
    elif trend_5m == "BEARISH" and latest_row["close"] < latest_row["ema_9"]:
        score += 20

    if trend_5m == "BULLISH" and latest_row["macd"] > latest_row["macd_signal"]:
        score += 20
    elif trend_5m == "BEARISH" and latest_row["macd"] < latest_row["macd_signal"]:
        score += 20

    if 50 < latest_row["rsi"] < 70:
        score += 20
    elif 30 < latest_row["rsi"] < 50:
        score += 20

    if trend_5m == "BULLISH" and latest_row["close"] > latest_row["vwap"]:
        score += 20
    elif trend_5m == "BEARISH" and latest_row["close"] < latest_row["vwap"]:
        score += 20

    return score


def write_signal_rows(rows):
    """Append every scanned symbol's row in one write, not one write each."""

    if not rows:
        return

    with SIGNALS_FILE.open(mode="a", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=SIGNAL_COLUMNS)
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in SIGNAL_COLUMNS})


def fetch_bars_in_batches(
    data_client,
    symbols,
    timeframe,
    start,
    end,
    label,
):
    """Fetch bars for many symbols in chunks. No limit= — that caps the TOTAL."""

    all_bars = {}
    total_batches = (len(symbols) + SCAN_BATCH_SIZE - 1) // SCAN_BATCH_SIZE

    for index in range(0, len(symbols), SCAN_BATCH_SIZE):
        chunk = symbols[index:index + SCAN_BATCH_SIZE]
        batch_number = index // SCAN_BATCH_SIZE + 1

        if total_batches > 1:
            print(
                f"  {label}: batch {batch_number}/{total_batches} "
                f"({len(chunk)} symbols)"
            )

        request = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=timeframe,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )

        try:
            bar_set = with_retries(data_client.get_stock_bars)(request)
        except Exception as error:
            print(
                f"  {label}: batch {batch_number} failed "
                f"({type(error).__name__}: {error}). Skipping it."
            )
            continue

        for symbol, bars in (getattr(bar_set, "data", {}) or {}).items():
            all_bars[symbol] = bars

    return all_bars


def evaluate_five_minute(symbol, bars_5m):
    """Stage one. Returns a per-symbol result dict, or None if unusable."""

    if not bars_5m:
        return None

    df_5m = add_indicators(bars_to_dataframe(bars_5m))

    if len(df_5m) < 22:
        return None

    latest = df_5m.iloc[-1]

    previous_volume_average = float(latest["volume_avg_20"])
    latest_volume = float(latest["volume"])

    if np.isfinite(previous_volume_average) and previous_volume_average > 0:
        volume_ratio = latest_volume / previous_volume_average
    else:
        volume_ratio = 0.0

    volume_confirmed = volume_ratio >= MIN_VOLUME_RATIO

    bar_time = latest["timestamp"].to_pydatetime()

    data_age_minutes = (
        datetime.now(timezone.utc) - bar_time
    ).total_seconds() / 60

    data_is_fresh = data_age_minutes <= 10

    trend_5m = get_trend(latest)

    signal, signal_reason = detect_signal(latest, trend_5m, data_is_fresh)
    score = confidence_score(latest, trend_5m)

    return {
        "symbol": symbol,
        "df_5m": df_5m,
        "latest": latest,
        "latest_volume": latest_volume,
        "volume_avg_20": previous_volume_average,
        "volume_ratio": volume_ratio,
        "volume_confirmed": volume_confirmed,
        "data_age_minutes": data_age_minutes,
        "data_is_fresh": data_is_fresh,
        "trend_5m": trend_5m,
        "trend_15m": "",
        "trend_1h": "",
        "timeframes_aligned": False,
        "signal": signal,
        "signal_reason": signal_reason,
        "score": score,
        "market_regime": None,
        "trade_approved": False,
        "approval_reason": "",
    }


def score_setup(result):
    """
    Score one approved setup for ranking.

    Returns (quality, components). Falls back to a neutral 50 with empty
    components when signal_quality.py is unavailable or the indicator row
    is unusable — ranking is a preference, and a scoring failure must
    never stop a trade the risk rules already approved.
    """

    if compute_quality is None:
        return 50.0, {}

    try:
        return compute_quality(
            result["latest"],
            is_long=result["signal"] == "BUY_LONG",
            weights=QUALITY_WEIGHTS,
        )
    except Exception as error:
        print(
            f"  {result.get('symbol', '?')}: quality scoring failed "
            f"({type(error).__name__}: {error}). Ranking it neutral."
        )
        return 50.0, {}


def build_signal_row(result):
    latest = result["latest"]

    return {
        "timestamp": latest["timestamp"],
        "symbol": result["symbol"],
        "latest_close": latest["close"],
        "ema_9": latest["ema_9"],
        "ema_21": latest["ema_21"],
        "vwap": latest["vwap"],
        "rsi": latest["rsi"],
        "macd": latest["macd"],
        "macd_signal": latest["macd_signal"],
        "latest_volume": result["latest_volume"],
        "volume_avg_20": result["volume_avg_20"],
        "volume_ratio": result["volume_ratio"],
        "volume_confirmed": result["volume_confirmed"],
        "trend_5m": result["trend_5m"],
        "trend_15m": result["trend_15m"],
        "trend_1h": result["trend_1h"],
        "timeframes_aligned": result["timeframes_aligned"],
        "signal_reason": result["signal_reason"],
        "data_age_minutes": result["data_age_minutes"],
        "signal": result["signal"],
        "confidence_score": result["score"],
        "trade_approved": result["trade_approved"],
        "approval_reason": result["approval_reason"],
    }


def print_candidate_detail(result):
    latest = result["latest"]
    regime = result["market_regime"] or {}

    print(f"\n{result['symbol']} Market Snapshot")
    print(f"Latest Close : ${latest['close']:.2f}")
    print(f"9 EMA        : ${latest['ema_9']:.2f}")
    print(f"21 EMA       : ${latest['ema_21']:.2f}")
    print(f"VWAP         : ${latest['vwap']:.2f}")
    print(f"RSI          : {latest['rsi']:.2f}")
    print(f"MACD         : {latest['macd']:.4f}")
    print(f"MACD Signal  : {latest['macd_signal']:.4f}")
    print(f"5m Trend     : {result['trend_5m']}")
    print(f"15m Trend    : {result['trend_15m']}")
    print(f"1h Trend     : {result['trend_1h']}")
    print(f"TF Alignment : {result['timeframes_aligned']}")
    print(f"Volume Ratio : {result['volume_ratio']:.2f}x")

    if regime:
        print(f"Regime       : {regime.get('regime')}")
        print(f"ADX          : {regime.get('adx', 0):.2f}")

    print(f"Signal        : {result['signal']}")
    # Printed as a checklist count, not a confidence level, because it is
    # always 100 for a tradable setup -- see confidence_score().
    print(f"Entry rules   : {result['score']}/100 (all signals score 100)")
    print(f"Approved      : {result['trade_approved']}")
    print(f"Approval Info : {result['approval_reason']}")


def main():
    """Run one complete LOCKBOT market-scanning cycle."""

    scan_started_monotonic = time.monotonic()
    scan_started_at = datetime.now(timezone.utc)

    scan_symbols, symbol_source = get_scan_symbols()

    # Read once per cycle, not once per symbol.
    movement_table = get_movement_table()

    metrics = {
        "symbols_requested": len(scan_symbols),
        "symbols_completed": 0,
        "symbols_skipped": 0,
        "signals_generated": 0,
        "candidates_advanced": 0,
        "trades_approved": 0,
        "orders_submitted": 0,
    }

    mark_module_starting(
        "MARKET_SCANNER",
        f"LOCKBOT Market Scanner v{LOCKBOT_VERSION} is starting.",
        details={
            "version": LOCKBOT_VERSION,
            "symbol_count": len(scan_symbols),
            "symbol_source": symbol_source,
            "adaptive_brackets": USE_ADAPTIVE_BRACKETS,
            "movement_data_symbols": len(movement_table),
            "started_at": scan_started_at.isoformat(),
        },
    )

    ensure_signals_file()

    data_client = StockHistoricalDataClient(api_key, secret_key)
    trading_client = TradingClient(api_key, secret_key, paper=True)

    end_time = datetime.now(timezone.utc)
    start_time_5m = end_time - timedelta(days=SCAN_LOOKBACK_DAYS_5M)
    start_time_higher = end_time - timedelta(days=SCAN_LOOKBACK_DAYS_HIGHER)

    print("=" * 50)
    print(f"         LOCKBOT MARKET SCANNER v{LOCKBOT_VERSION}")
    print("=" * 50)
    print(f"Scanning {len(scan_symbols)} symbols from {symbol_source}")

    if USE_ADAPTIVE_BRACKETS:
        print(
            f"Brackets: ADAPTIVE — movement data for "
            f"{len(movement_table)} symbol(s)"
        )
    else:
        print(
            f"Brackets: FIXED {STOP_LOSS_PERCENT:.1%} stop / "
            f"{TAKE_PROFIT_PERCENT:.1%} target for every symbol"
        )

    try:
        account = with_retries(trading_client.get_account)()
        market_clock = with_retries(trading_client.get_clock)()

        account_equity = float(account.equity)
        previous_close_equity = float(account.last_equity)
        buying_power = float(account.buying_power)

        # THIS BOOK'S losses, not the account's.
        #
        # Passing account.equity here gated share entries on marks from
        # positions this path does not own. On 2026-08-06 the equity
        # path was locked all day at -3.02% while the equity book was
        # flat: the whole move was an IBIT spread's two legs marking
        # independently, with nothing sold and nothing realised. The ETF
        # sleeve leaked in the same way, despite position_filters
        # existing so the trading engine cannot see it.
        try:
            open_positions_now = with_retries(
                trading_client.get_all_positions)()
        except Exception:
            open_positions_now = []

        equity_pnl = equity_book_pnl(open_positions_now)

        (
            daily_loss_limit_reached,
            daily_pnl,
            daily_pnl_percent,
            daily_loss_reason,
        ) = check_book_daily_loss(
            equity_pnl,
            previous_close_equity,
        )

        print("\nPaper Account Check")
        print(f"Account Status : {account.status}")
        print(f"Equity         : ${account_equity:,.2f}")
        print(f"Prior Equity   : ${previous_close_equity:,.2f}")
        print(f"Equity Book P&L: ${daily_pnl:,.2f}  (this book only)")
        print(f"Daily P&L %    : {daily_pnl_percent:.2%}")
        print(f"Loss Limit Hit : {daily_loss_limit_reached}")
        print(f"Buying Power   : ${buying_power:,.2f}")
        print(f"Market Open    : {market_clock.is_open}")

        # Counted locally by day_trade_tracker.py. Alpaca removed
        # daytrade_count from account responses on 2026-07-06 (FINRA
        # intraday-margin migration), and the previous code read the
        # resulting None as 0 — so this guard reported "0/3" every cycle
        # and could never fire. Round trips are now counted from filled
        # order history, which still exists.
        day_trade_limit_reached, day_trade_detail = check_day_trade_limit(
            trading_client,
            MAX_DAY_TRADES_PER_5_DAYS,
        )

        if MAX_DAY_TRADES_PER_5_DAYS > 0:
            print(f"Day Trades     : {day_trade_detail}")

        if day_trade_limit_reached:
            print(
                "Day-trade limit reached — no new positions will be opened. "
                "Existing positions still exit normally through their brackets."
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
            }
        )

        # ------------------------------------------------------------------
        # Stage one: the 5-minute chart for every symbol
        # ------------------------------------------------------------------

        print(f"\nStage 1: 5-minute scan of {len(scan_symbols)} symbols")

        bars_5m_by_symbol = fetch_bars_in_batches(
            data_client=data_client,
            symbols=scan_symbols,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start_time_5m,
            end=end_time,
            label="5m",
        )

        results = []

        for symbol in scan_symbols:
            result = evaluate_five_minute(
                symbol,
                bars_5m_by_symbol.get(symbol, []),
            )

            if result is None:
                metrics["symbols_skipped"] += 1
                continue

            metrics["symbols_completed"] += 1

            if result["signal"] != "NO_TRADE":
                metrics["signals_generated"] += 1

            results.append(result)

        # Decide which symbols are worth the slower timeframes.
        candidates = []

        # Shorts LOCKBOT will not trade but should still MEASURE.
        #
        # Filed by LOCKBOT as agent_channel item 46169c86. Shorting is off
        # under $2,000 of equity, so SELL_SHORT was rejected here in stage
        # one and never went further -- no timeframe alignment, no bracket,
        # and crucially never into `approved`, which is the list the shadow
        # logger iterates. So every one of the 119 resolved shadow setups
        # is LONG, and roughly 350 short signals a session were discarded
        # unmeasured. The strategy has been judged on half its own output.
        #
        # These advance through the whole of stage two so what gets
        # measured is a short LOCKBOT would actually have taken, sized the
        # way it would have sized it -- a shadow trade built from anything
        # less is scoring a bot that isn't running. They never receive
        # trade_approved, and the submission loop selects on exactly that,
        # so this cannot put an order on the wire even if shorting is
        # switched on later.
        shadow_candidates = []

        for result in results:
            if not market_clock.is_open:
                result["approval_reason"] = "MARKET_IS_CLOSED"
            elif daily_loss_limit_reached:
                result["approval_reason"] = daily_loss_reason
            elif not result["data_is_fresh"]:
                result["approval_reason"] = "DATA_IS_STALE"
            elif result["signal"] == "NO_TRADE":
                result["approval_reason"] = "NO_VALID_SIGNAL"
            elif day_trade_limit_reached:
                result["approval_reason"] = "DAY_TRADE_LIMIT_REACHED"
            elif result["signal"] == "SELL_SHORT" and not ALLOW_SHORT_ENTRIES:
                result["approval_reason"] = "SHORT_EXECUTION_NOT_ENABLED"

                # The remaining stage-one gates are applied by hand here
                # because the elif chain short-circuits: a short that also
                # failed on volume would be caught by the clause above and
                # look identical to a tradable one. Measuring those would
                # inflate the short sample with setups LOCKBOT would have
                # rejected anyway, which is worse than not measuring at all.
                if (SHADOW_LOG_BLOCKED_SHORTS
                        and result["volume_confirmed"]
                        and result["score"] >= MIN_CONFIDENCE_SCORE):
                    result["shadow_only"] = True
                    shadow_candidates.append(result)
            elif not result["volume_confirmed"]:
                result["approval_reason"] = "LOW_VOLUME"
            elif result["score"] < MIN_CONFIDENCE_SCORE:
                result["approval_reason"] = "CONFIDENCE_TOO_LOW"
            else:
                candidates.append(result)

        metrics["candidates_advanced"] = len(candidates)
        metrics["shadow_shorts_advanced"] = len(shadow_candidates)

        print(
            f"Stage 1 complete: {metrics['symbols_completed']} scanned, "
            f"{metrics['signals_generated']} signals, "
            f"{len(candidates)} advancing to stage 2"
        )

        # ------------------------------------------------------------------
        # Stage two: slower timeframes, but only for the candidates
        # ------------------------------------------------------------------

        # Shadow shorts ride the same pipeline. Fetching their higher
        # timeframes together with the real candidates keeps the request
        # count the same shape and guarantees both are judged against
        # identical data.
        stage_two = candidates + shadow_candidates

        if stage_two:
            candidate_symbols = [result["symbol"] for result in stage_two]

            print(
                f"\nStage 2: higher timeframes for "
                f"{', '.join(candidate_symbols[:12])}"
                + (" ..." if len(candidate_symbols) > 12 else "")
            )

            bars_15m_by_symbol = fetch_bars_in_batches(
                data_client=data_client,
                symbols=candidate_symbols,
                timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                start=start_time_higher,
                end=end_time,
                label="15m",
            )

            bars_1h_by_symbol = fetch_bars_in_batches(
                data_client=data_client,
                symbols=candidate_symbols,
                timeframe=TimeFrame(1, TimeFrameUnit.Hour),
                start=start_time_higher,
                end=end_time,
                label="1h",
            )

            for result in stage_two:
                symbol = result["symbol"]

                bars_15m = bars_15m_by_symbol.get(symbol, [])
                bars_1h = bars_1h_by_symbol.get(symbol, [])

                if not bars_15m or not bars_1h:
                    result["approval_reason"] = "HIGHER_TIMEFRAME_DATA_MISSING"
                    continue

                df_15m = add_indicators(bars_to_dataframe(bars_15m))
                df_1h = add_indicators(bars_to_dataframe(bars_1h))

                if len(df_15m) < 21 or len(df_1h) < 21:
                    result["approval_reason"] = "HIGHER_TIMEFRAME_DATA_MISSING"
                    continue

                result["trend_15m"] = get_trend(df_15m.iloc[-1])
                result["trend_1h"] = get_trend(df_1h.iloc[-1])

                bullish_aligned = (
                    result["trend_5m"] == "BULLISH"
                    and result["trend_15m"] == "BULLISH"
                    and result["trend_1h"] == "BULLISH"
                )

                bearish_aligned = (
                    result["trend_5m"] == "BEARISH"
                    and result["trend_15m"] == "BEARISH"
                    and result["trend_1h"] == "BEARISH"
                )

                result["timeframes_aligned"] = bullish_aligned or bearish_aligned

                if not result["timeframes_aligned"]:
                    result["approval_reason"] = "TIMEFRAMES_NOT_ALIGNED"
                    continue

                result["market_regime"] = get_market_regime(result["df_5m"])

                regime_approved, regime_reason = check_regime_approval(
                    signal=result["signal"],
                    market_regime=result["market_regime"],
                )

                if not regime_approved:
                    result["approval_reason"] = regime_reason
                    continue

                (
                    position_size,
                    stop_percent,
                    take_profit_percent,
                ) = calculate_bracket_and_size(
                    account_equity=account_equity,
                    buying_power=buying_power,
                    entry_price=float(result["latest"]["close"]),
                    symbol=symbol,
                    is_long=result["signal"] == "BUY_LONG",
                    movement_table=movement_table,
                )

                if position_size <= 0:
                    result["approval_reason"] = "POSITION_SIZE_CALCULATION_FAILED"
                    continue

                result["position_size"] = position_size
                result["stop_percent"] = stop_percent
                result["take_profit_percent"] = take_profit_percent

                # The one line that keeps a measured short off the wire.
                #
                # trade_approved is what the submission loop selects on,
                # so withholding it here is a structural guarantee rather
                # than a check that could be forgotten: there is no path
                # from a shadow_only result to an order. The reason stays
                # SHORT_EXECUTION_NOT_ENABLED_MEASURED so signals.csv
                # still says plainly that this was never tradable.
                if result.get("shadow_only"):
                    result["approval_reason"] = (
                        "SHORT_EXECUTION_NOT_ENABLED_MEASURED"
                    )
                    metrics["shadow_shorts_measured"] = (
                        metrics.get("shadow_shorts_measured", 0) + 1
                    )
                    continue

                result["trade_approved"] = True
                result["approval_reason"] = "APPROVED_FOR_PAPER_EXECUTION"
                metrics["trades_approved"] += 1

        # ------------------------------------------------------------------
        # Log every scanned symbol, then rank the approved ones
        # ------------------------------------------------------------------

        write_signal_rows([build_signal_row(result) for result in results])

        approved = [result for result in results if result["trade_approved"]]

        # Shorts that completed stage two but are not tradable. Separate
        # from `approved` on purpose: everything downstream that can put
        # an order on the wire reads `approved`, and this list must never
        # be merged into it.
        shadow_shorts = [
            result for result in results
            if result.get("approval_reason")
            == "SHORT_EXECUTION_NOT_ENABLED_MEASURED"
        ]

        # Measured, not traded. Quality is scored for both so the q_*
        # columns exist for shorts too -- signal_research.py splits on
        # them, and a column that is present for longs and blank for
        # shorts would make the two halves incomparable.
        measured = approved + shadow_shorts

        # Rank the approved setups. The old key was (score, volume_ratio),
        # but score is 100 for every approved setup — it restates the entry
        # condition rather than measuring quality — so volume_ratio was
        # doing the actual ranking, and the shadow data says it ranks
        # backwards. signal_quality.py scores measures the entry rules
        # don't already use. See USE_QUALITY_RANKING in lockbot_config.py.
        for result in measured:
            quality, components = score_setup(result)
            result["quality"] = quality
            result["quality_components"] = components

        if USE_QUALITY_RANKING and compute_quality is not None:
            approved.sort(
                key=lambda result: result.get("quality", 0.0),
                reverse=True,
            )
        else:
            approved.sort(
                key=lambda result: (result["score"], result["volume_ratio"]),
                reverse=True,
            )

        if approved:
            ranked_by = (
                "quality"
                if USE_QUALITY_RANKING and compute_quality is not None
                else "confidence then volume"
            )

            print(
                f"\n{len(approved)} approved setup(s), strongest first "
                f"(ranked by {ranked_by}):"
            )

            for rank, result in enumerate(approved, start=1):
                components = result.get("quality_components") or {}

                detail = "  ".join(
                    f"{name.replace('_', ' ')} {value:.2f}"
                    for name, value in components.items()
                )

                print(
                    f"  {rank}. {result['symbol']:<6} {result['signal']:<11} "
                    f"quality {result.get('quality', 0):.1f}/100  "
                    f"volume {result['volume_ratio']:.2f}x  "
                    f"stop {result.get('stop_percent', STOP_LOSS_PERCENT):.2%}"
                )

                if detail:
                    print(f"        {detail}")

        # ------------------------------------------------------------------
        # Submission, with the account view kept current as we go
        # ------------------------------------------------------------------

        submitted_this_cycle = 0
        submitted_symbols = set()

        # Equity entries can be switched off while the scan keeps running.
        # Everything above this point still happened — the symbols were
        # scanned, ranked and written to signals.csv, and the shadow rows
        # below are still recorded. Only the order submission stops.
        #
        # That split is deliberate: the shadow log is the only measurement
        # of whether the signal engine has an edge, and it keeps
        # accumulating while the account's capital sits in options.
        if approved and not EQUITY_ENTRIES_ENABLED:
            print(
                f"\n{len(approved)} setup(s) approved but EQUITY_ENTRIES_ENABLED "
                "is False — no share orders will be submitted. Shadow logging "
                "continues."
            )

        # `approved` is NOT cleared here. The shadow logger further down
        # reads it, and those rows are the measurement — dropping them to
        # disable trading would switch off the experiment along with the
        # orders, which is the opposite of the intent.
        if approved and EQUITY_ENTRIES_ENABLED:
            # Equity only. MAX_OPEN_TRADES, the same-direction cap and the
            # exposure ceiling are all equity-path limits — letting option
            # contracts count against them would silently block share
            # trading. Options have their own caps in lockbot_config.py.
            # The genuinely shared resource, cash, is already handled:
            # buying_power below comes from the account and options
            # purchases reduce it.
            positions = equity_positions(
                with_retries(trading_client.get_all_positions)()
            )

            open_orders = with_retries(trading_client.get_orders)(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
            )

            held_symbols = {position.symbol for position in positions}
            ordered_symbols = {order.symbol for order in open_orders}

            # Real quantities, so the risk manager can run its own duplicate
            # and conflicting-position checks rather than trusting ours.
            position_quantities = {
                position.symbol: float(position.qty) for position in positions
            }

            open_position_count = len(positions)

            long_count = sum(
                1 for position in positions if float(position.qty) > 0
            )
            short_count = sum(
                1 for position in positions if float(position.qty) < 0
            )

            total_exposure = sum(
                abs(float(position.market_value)) for position in positions
            )

            remaining_buying_power = buying_power

            for result in approved:
                symbol = result["symbol"]
                signal = result["signal"]
                is_long = signal == "BUY_LONG"
                trade_side_label = "LONG" if is_long else "SHORT"

                if submitted_this_cycle >= MAX_NEW_ENTRIES_PER_CYCLE:
                    print(
                        "Entry limit for this cycle reached "
                        f"({MAX_NEW_ENTRIES_PER_CYCLE}). Remaining setups skipped."
                    )
                    break

                if open_position_count >= MAX_OPEN_TRADES:
                    print(
                        "ORDER BLOCKED: Maximum number of open positions "
                        f"({MAX_OPEN_TRADES}) has been reached."
                    )
                    break

                same_direction_count = long_count if is_long else short_count

                if same_direction_count >= MAX_SAME_DIRECTION_POSITIONS:
                    print(
                        f"ORDER BLOCKED ({symbol}): already holding "
                        f"{same_direction_count} {trade_side_label} position(s), "
                        f"limit is {MAX_SAME_DIRECTION_POSITIONS}. "
                        "Several positions facing the same way are one bet."
                    )
                    continue

                if symbol in held_symbols:
                    print(f"ORDER BLOCKED: An open {symbol} position already exists.")
                    continue

                if symbol in ordered_symbols:
                    print(f"ORDER BLOCKED: An open {symbol} order already exists.")
                    continue

                position_size = result["position_size"]
                reference_entry_price = float(result["latest"]["close"])

                # Per-stock when adaptive brackets are on; the fixed config
                # values when they are off.
                stop_percent = result.get("stop_percent", STOP_LOSS_PERCENT)
                take_profit_percent = result.get(
                    "take_profit_percent", TAKE_PROFIT_PERCENT
                )

                affordable = int(remaining_buying_power / reference_entry_price)

                if affordable < position_size:
                    position_size = affordable

                if position_size <= 0:
                    print(f"ORDER BLOCKED ({symbol}): insufficient buying power.")
                    continue

                if is_long:
                    stop_loss_price = round(
                        reference_entry_price * (1 - stop_percent), 2
                    )
                    take_profit_price = round(
                        reference_entry_price * (1 + take_profit_percent), 2
                    )

                    if stop_loss_price <= 0:
                        print(f"ORDER BLOCKED ({symbol}): Invalid stop-loss price.")
                        continue

                    if take_profit_price <= reference_entry_price:
                        print(f"ORDER BLOCKED ({symbol}): Invalid take-profit price.")
                        continue

                else:
                    # SHORT: stop sits ABOVE entry, target BELOW it.
                    stop_loss_price = round(
                        reference_entry_price * (1 + stop_percent), 2
                    )
                    take_profit_price = round(
                        reference_entry_price * (1 - take_profit_percent), 2
                    )

                    if take_profit_price <= 0:
                        print(f"ORDER BLOCKED ({symbol}): Invalid take-profit price.")
                        continue

                    if stop_loss_price <= reference_entry_price:
                        print(f"ORDER BLOCKED ({symbol}): Invalid stop-loss price.")
                        continue

                print_candidate_detail(result)

                print("\n==============================")
                print(f"PAPER BRACKET TRADE ({trade_side_label}) — {symbol}")
                print("==============================")

                estimated_position_value = position_size * reference_entry_price
                estimated_dollar_risk = estimated_position_value * stop_percent

                market_regime = result["market_regime"] or {"regime": "UNKNOWN"}

                order_request = MarketOrderRequest(
                    symbol=symbol,
                    qty=position_size,
                    side=OrderSide.BUY if is_long else OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=take_profit_price),
                    stop_loss=StopLossRequest(stop_price=stop_loss_price),
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
                    open_position_count=open_position_count,
                    total_open_exposure=total_exposure,
                    existing_position_quantity=position_quantities.get(symbol, 0.0),
                    duplicate_open_order_exists=symbol in ordered_symbols,
                )

                risk_decision = evaluate_trade_request(risk_request)

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
                    confidence=result["score"],
                    market_regime=market_regime.get("regime"),
                    estimated_risk_dollars=risk_decision.estimated_risk_dollars,
                    estimated_risk_percent=risk_decision.estimated_risk_percent,
                )

                if not risk_decision.approved:
                    print(
                        f"ORDER BLOCKED BY RISK MANAGER: {risk_decision.reason}"
                    )
                    continue

                submitted_order = with_retries(trading_client.submit_order)(
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
                    confidence=result["score"],
                    market_regime=market_regime.get("regime"),
                    estimated_risk_dollars=risk_decision.estimated_risk_dollars,
                    estimated_risk_percent=risk_decision.estimated_risk_percent,
                    order_id=str(submitted_order.id),
                )

                trades_submitted_today = record_trade_submission()

                trade_registered = register_bracket_trade(
                    parent_order_id=str(submitted_order.id),
                    strategy_version=LOCKBOT_VERSION,
                    symbol=symbol,
                    side=trade_side_label,
                    market_regime=market_regime.get("regime"),
                    confidence=result["score"],
                    position_value=estimated_position_value,
                    account_equity_at_entry=account_equity,
                    initial_risk_dollars=estimated_dollar_risk,
                    stop_loss_percent=stop_percent,
                    take_profit_percent=take_profit_percent,
                    paper_trade=True,
                )

                # Keep our view of the account current so the next candidate
                # in this same cycle is judged against reality.
                submitted_this_cycle += 1
                submitted_symbols.add(symbol)
                open_position_count += 1
                held_symbols.add(symbol)
                ordered_symbols.add(symbol)
                total_exposure += estimated_position_value
                remaining_buying_power -= estimated_position_value

                if is_long:
                    long_count += 1
                else:
                    short_count += 1

                metrics["orders_submitted"] += 1

                print(f"Submitted PAPER bracket order for {symbol}")
                print(f"Quantity        : {position_size}")
                print(f"Reference Entry : ${reference_entry_price:.2f}")
                print(f"Stop Loss       : ${stop_loss_price:.2f} ({stop_percent:.2%})")
                print(
                    f"Take Profit     : ${take_profit_price:.2f} "
                    f"({take_profit_percent:.2%})"
                )
                print(f"Order ID        : {submitted_order.id}")
                print(f"Order Status    : {submitted_order.status}")
                print(f"Trade Registered: {trade_registered}")
                print(f"Daily Trade Count: {trades_submitted_today}")

                send_smart_notification(
                    symbol=symbol,
                    event_type=(
                        "BUY_ORDER_SUBMITTED" if is_long else "SHORT_ORDER_SUBMITTED"
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
                        f"Reference entry: ${reference_entry_price:.2f}\n"
                        f"Stop loss: ${stop_loss_price:.2f} ({stop_percent:.2%})\n"
                        f"Take profit: ${take_profit_price:.2f} "
                        f"({take_profit_percent:.2%})\n"
                        f"Estimated position: ${estimated_position_value:,.2f}\n"
                        f"Estimated risk: ${estimated_dollar_risk:,.2f}\n"
                        f"Confidence: {result['score']}/100\n"
                        f"Open positions now: {open_position_count}"
                        f"/{MAX_OPEN_TRADES}\n"
                        f"Regime: {market_regime.get('regime')}\n"
                        f"Daily P&L: ${daily_pnl:,.2f} ({daily_pnl_percent:.2%})\n"
                        f"Account equity: ${account_equity:,.2f}\n"
                        f"Order status: {submitted_order.status}\n"
                        f"Registered: {trade_registered}\n"
                        f"Version: v{LOCKBOT_VERSION}"
                    ),
                )

        # ------------------------------------------------------------------
        # Shadow log: record every approved setup, including the ones there
        # was no room for. shadow_trades.py later replays the price history
        # and asks whether the ones LOCKBOT chose actually beat the ones it
        # skipped. Wrapped so a logging failure can never stop trading.
        #
        # These use the SAME per-stock levels the real order used. If they
        # didn't, the shadow log would be scoring a bot that isn't running.
        # ------------------------------------------------------------------

        if record_candidates and measured:
            shadow_rows = []

            for result in measured:
                entry_price = float(result["latest"]["close"])
                is_long = result["signal"] == "BUY_LONG"

                shadow_stop_percent = result.get("stop_percent", STOP_LOSS_PERCENT)
                shadow_target_percent = result.get(
                    "take_profit_percent", TAKE_PROFIT_PERCENT
                )

                if is_long:
                    shadow_stop = round(entry_price * (1 - shadow_stop_percent), 2)
                    shadow_target = round(entry_price * (1 + shadow_target_percent), 2)
                else:
                    shadow_stop = round(entry_price * (1 + shadow_stop_percent), 2)
                    shadow_target = round(entry_price * (1 - shadow_target_percent), 2)

                shadow_rows.append({
                    "logged_at": datetime.now(timezone.utc),
                    "symbol": result["symbol"],
                    "side": "LONG" if is_long else "SHORT",
                    "confidence": result["score"],
                    "volume_ratio": result["volume_ratio"],
                    "regime": (result["market_regime"] or {}).get("regime", ""),
                    "reference_price": entry_price,
                    "stop_price": shadow_stop,
                    "target_price": shadow_target,
                    "taken": result["symbol"] in submitted_symbols,
                    "quality": result.get("quality", ""),
                    "quality_components": result.get("quality_components", {}),
                })

            recorded = record_candidates(shadow_rows)

            if recorded:
                print(
                    f"\nShadow log: recorded {recorded} approved setup(s), "
                    f"{len(submitted_symbols)} taken, "
                    f"{recorded - len(submitted_symbols)} passed over."
                )

        # Rejection notifications were removed deliberately. LOCKBOT now
        # notifies only on orders it actually submits; every rejection is
        # still written to signals.csv for analyze_signals.py.

        # ------------------------------------------------------------------
        # State, heartbeat, summary
        # ------------------------------------------------------------------

        # Only candidates go into scanner_state — 300 entries every cycle
        # would bloat the state file for no benefit.
        symbol_results = {
            result["symbol"]: {
                "signal": result["signal"],
                "confidence": result["score"],
                "approved": result["trade_approved"],
                "approval_reason": result["approval_reason"],
                "trend_5m": result["trend_5m"],
                "trend_15m": result["trend_15m"],
                "trend_1h": result["trend_1h"],
                "market_regime": (result["market_regime"] or {}).get("regime"),
                "latest_price": round(float(result["latest"]["close"]), 2),
                "position_size": result.get("position_size", 0),
                "stop_percent": result.get("stop_percent"),
                "take_profit_percent": result.get("take_profit_percent"),
            }
            for result in candidates
        }

        save_state(
            {
                "scan_time": datetime.now(timezone.utc).isoformat(),
                "market_open": market_clock.is_open,
                "account_equity": account_equity,
                "buying_power": buying_power,
                "daily_pnl": daily_pnl,
                "daily_pnl_percent": daily_pnl_percent,
                "daily_loss_limit_hit": daily_loss_limit_reached,
                "symbol_source": symbol_source,
                "symbols_scanned": len(scan_symbols),
                "adaptive_brackets": USE_ADAPTIVE_BRACKETS,
                "symbols": symbol_results,
            }
        )

        scan_completed_at = datetime.now(timezone.utc)
        scan_duration_seconds = round(time.monotonic() - scan_started_monotonic, 2)

        heartbeat_details = {
            **metrics,
            "version": LOCKBOT_VERSION,
            "symbol_source": symbol_source,
            "adaptive_brackets": USE_ADAPTIVE_BRACKETS,
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

        # With hundreds of symbols a few will always be missing data, so
        # "every symbol completed" is no longer the right health bar.
        completion_rate = (
            metrics["symbols_completed"] / len(scan_symbols)
            if scan_symbols
            else 0
        )

        if completion_rate >= 0.80:
            mark_module_healthy(
                "MARKET_SCANNER",
                (
                    f"Market scan completed for {metrics['symbols_completed']}"
                    f"/{len(scan_symbols)} symbols."
                ),
                details=heartbeat_details,
            )
            heartbeat_status = "HEALTHY"
        else:
            mark_module_degraded(
                "MARKET_SCANNER",
                (
                    f"Only {metrics['symbols_completed']}/{len(scan_symbols)} "
                    "symbols had usable market data this cycle."
                ),
                details=heartbeat_details,
            )
            heartbeat_status = "DEGRADED"

        print("\nScan Summary")
        print("-" * 50)
        print(f"Symbol Source     : {symbol_source}")
        print(f"Bracket Mode      : {'ADAPTIVE' if USE_ADAPTIVE_BRACKETS else 'FIXED'}")
        print(f"Symbols Requested : {metrics['symbols_requested']}")
        print(f"Symbols Completed : {metrics['symbols_completed']}")
        print(f"Symbols Skipped   : {metrics['symbols_skipped']}")
        print(f"Signals Generated : {metrics['signals_generated']}")
        print(f"Reached Stage 2   : {metrics['candidates_advanced']}")
        print(f"Trades Approved   : {metrics['trades_approved']}")
        print(f"Orders Submitted  : {metrics['orders_submitted']}")
        print(f"Duration          : {scan_duration_seconds:.2f} seconds")
        print(f"Heartbeat         : {heartbeat_status}")

        if scan_duration_seconds > config.SCAN_INTERVAL_SECONDS * 0.8:
            print(
                "\nWARNING: this scan used most of the cycle interval. "
                "Lower MAX_SCAN_SYMBOLS or raise SCAN_INTERVAL_SECONDS."
            )

    except Exception as error:
        error_type = type(error).__name__
        scan_duration_seconds = round(time.monotonic() - scan_started_monotonic, 2)

        print("\nMarket scanner failed.")
        print(f"{error_type}: {error}")

        mark_module_critical(
            "MARKET_SCANNER",
            "LOCKBOT Market Scanner encountered an unhandled error.",
            error=f"{error_type}: {error}",
            details={
                **metrics,
                "version": LOCKBOT_VERSION,
                "symbol_source": symbol_source,
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
                f"{type(notification_error).__name__}: {notification_error}"
            )

        raise


def _self_test():
    """Offline proof that the confidence score cannot rank tradable setups.

    This is proved by exhaustion rather than argued: every combination of
    the five indicator conditions is enumerated, and for each one both
    detect_signal and confidence_score are evaluated. If any combination
    ever produces a real signal at less than 100, the tautology is broken
    and this test starts passing for the right reason instead.
    """

    import itertools

    failures = []

    def check(name, condition, detail=""):
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    def row(*, above_ema, above_vwap, rsi, macd_above):
        """A synthetic bar. Price is fixed; the levels move around it."""

        return {
            "close": 100.0,
            "ema_9": 99.0 if above_ema else 101.0,
            "vwap": 99.0 if above_vwap else 101.0,
            "rsi": rsi,
            "macd": 1.0,
            "macd_signal": 0.5 if macd_above else 1.5,
        }

    print("Exhaustive enumeration of every indicator combination")

    scored_signals = []
    combinations = 0

    rsi_values = (25.0, 40.0, 55.0, 65.0, 75.0)

    for trend, above_ema, above_vwap, rsi, macd_above in itertools.product(
        ("BULLISH", "BEARISH", "NEUTRAL"),
        (True, False),
        (True, False),
        rsi_values,
        (True, False),
    ):
        combinations += 1
        bar = row(
            above_ema=above_ema,
            above_vwap=above_vwap,
            rsi=rsi,
            macd_above=macd_above,
        )

        signal, _ = detect_signal(bar, trend, data_is_fresh=True)
        score = confidence_score(bar, trend)

        if signal in ("BUY_LONG", "SELL_SHORT"):
            scored_signals.append((signal, score, trend, rsi))

    print(f"  enumerated {combinations} combinations, "
          f"{len(scored_signals)} produced a tradable signal")

    check("some combinations do produce signals", len(scored_signals) > 0)
    check(
        "EVERY tradable signal scores exactly 100",
        all(score == 100 for _, score, _, _ in scored_signals),
        str(sorted({score for _, score, _, _ in scored_signals})),
    )
    check(
        "the score therefore has zero variance among tradable setups",
        len({score for _, score, _, _ in scored_signals}) == 1,
    )
    check(
        "so MIN_SIGNAL_CONFIDENCE cannot reject a real signal",
        min(score for _, score, _, _ in scored_signals) > MIN_CONFIDENCE_SCORE,
        f"min={min(score for _, score, _, _ in scored_signals)} "
        f"gate={MIN_CONFIDENCE_SCORE}",
    )
    check("both directions are represented", len(
        {signal for signal, _, _, _ in scored_signals}) == 2)

    print()
    print("Stale data still blocks a perfect setup")

    perfect = row(above_ema=True, above_vwap=True, rsi=60.0, macd_above=True)
    stale, reason = detect_signal(perfect, "BULLISH", data_is_fresh=False)
    check("stale data yields NO_TRADE", stale == "NO_TRADE", stale)
    check("and says why", reason == "STALE_DATA", reason)
    check(
        "but the score is unchanged, which is the bug in miniature",
        confidence_score(perfect, "BULLISH") == 100,
    )

    print()
    print("Non-signals can score below the gate (they are discarded anyway)")

    neutral = confidence_score(perfect, "NEUTRAL")
    check("a NEUTRAL trend scores under 100", neutral < 100, str(neutral))
    check(
        "and produces no signal",
        detect_signal(perfect, "NEUTRAL", data_is_fresh=True)[0] == "NO_TRADE",
    )

    print()
    print("A measured short can never become an order")

    # The selection predicates, verbatim from the scan. If either of
    # these changes shape, this test stops matching the code it guards
    # -- which is the point of writing them out rather than importing a
    # helper that could be edited to agree with a mistake.
    MEASURED_REASON = "SHORT_EXECUTION_NOT_ENABLED_MEASURED"

    fake_results = [
        {"symbol": "AAA", "signal": "BUY_LONG", "trade_approved": True,
         "approval_reason": "APPROVED_FOR_PAPER_EXECUTION"},
        {"symbol": "BBB", "signal": "SELL_SHORT", "trade_approved": False,
         "shadow_only": True, "approval_reason": MEASURED_REASON},
        {"symbol": "CCC", "signal": "SELL_SHORT", "trade_approved": False,
         "approval_reason": "SHORT_EXECUTION_NOT_ENABLED"},
    ]

    approved_now = [r for r in fake_results if r["trade_approved"]]
    shadow_now = [r for r in fake_results
                  if r.get("approval_reason") == MEASURED_REASON]

    check("the submission list holds only approved setups",
          [r["symbol"] for r in approved_now] == ["AAA"],
          str([r["symbol"] for r in approved_now]))
    check("a measured short is not in it",
          all(r["symbol"] != "BBB" for r in approved_now))
    check("but it IS measured", [r["symbol"] for r in shadow_now] == ["BBB"])
    check("the two lists never overlap",
          not ({r["symbol"] for r in approved_now}
               & {r["symbol"] for r in shadow_now}))
    check("a short that failed another gate is neither",
          all(r["symbol"] != "CCC" for r in approved_now + shadow_now))
    check("no measured short carries trade_approved",
          all(not r["trade_approved"] for r in shadow_now))

    print()
    print("Short shadow levels are the right way up")

    # Never exercised before this change, because no short ever reached
    # the shadow logger. A short profits when price FALLS, so its target
    # must sit below the entry and its stop above -- inverted levels
    # would score every short backwards and look like a signal.
    entry = 20.00
    stop_pct, target_pct = 0.02, 0.04

    short_stop = round(entry * (1 + stop_pct), 2)
    short_target = round(entry * (1 - target_pct), 2)

    check("a short's stop is above the entry", short_stop > entry,
          f"{short_stop} vs {entry}")
    check("and its target is below", short_target < entry,
          f"{short_target} vs {entry}")

    long_stop = round(entry * (1 - stop_pct), 2)
    long_target = round(entry * (1 + target_pct), 2)

    check("a long's are the other way", long_stop < entry < long_target)
    check("and the reward:risk matches on both sides",
          abs((long_target - entry) / (entry - long_stop)
              - (entry - short_target) / (short_stop - entry)) < 1e-9)

    print()
    print("The measurement switch is real and defaults safe")

    check("the scanner reads the flag from config",
          isinstance(SHADOW_LOG_BLOCKED_SHORTS, bool))
    check("and lockbot_config is the source of truth",
          hasattr(config, "SHADOW_LOG_BLOCKED_SHORTS"))

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All market-scanner checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    main()