"""
Standalone test for LockBot's indicators module.

This test uses artificial market data.
It does not connect to Alpaca.
It cannot submit an order.
"""

import numpy as np
import pandas as pd

from indicators import add_indicators


NUMBER_OF_BARS = 100

bar_numbers = np.arange(
    NUMBER_OF_BARS,
    dtype="float64",
)

close_prices = (
    500.0
    + (bar_numbers * 0.25)
    + np.sin(bar_numbers / 4.0)
)

test_data = pd.DataFrame(
    {
        "timestamp": pd.date_range(
            start="2026-01-01 09:30:00",
            periods=NUMBER_OF_BARS,
            freq="5min",
            tz="America/New_York",
        ),
        "open": close_prices - 0.15,
        "high": close_prices + 0.60,
        "low": close_prices - 0.60,
        "close": close_prices,
        "volume": (
            1_000_000
            + (bar_numbers * 2_500)
        ),
    }
)

result = add_indicators(test_data)

latest = result.iloc[-1]

required_indicator_columns = [
    "ema_9",
    "ema_21",
    "ema_12",
    "ema_26",
    "macd",
    "macd_signal",
    "vwap",
    "volume_avg_20",
    "rsi",
    "true_range",
    "atr",
    "adx",
    "plus_di",
    "minus_di",
]

missing_columns = [
    column
    for column in required_indicator_columns
    if column not in result.columns
]

if missing_columns:
    raise RuntimeError(
        "Indicator test failed. Missing columns: "
        + ", ".join(missing_columns)
    )

latest_values = latest[
    required_indicator_columns
]

if latest_values.isna().any():
    invalid_columns = latest_values[
        latest_values.isna()
    ].index.tolist()

    raise RuntimeError(
        "Indicator test failed. Latest row contains "
        "empty values for: "
        + ", ".join(invalid_columns)
    )

print("=" * 50)
print("       LOCKBOT INDICATOR MODULE TEST")
print("=" * 50)

print("\nLatest Test Bar")
print(
    f"Close         : "
    f"${latest['close']:.2f}"
)

print("\nTrend Indicators")
print(
    f"EMA 9         : "
    f"${latest['ema_9']:.2f}"
)
print(
    f"EMA 21        : "
    f"${latest['ema_21']:.2f}"
)
print(
    f"MACD          : "
    f"{latest['macd']:.4f}"
)
print(
    f"MACD Signal   : "
    f"{latest['macd_signal']:.4f}"
)

print("\nMomentum and Price")
print(
    f"VWAP          : "
    f"${latest['vwap']:.2f}"
)
print(
    f"RSI           : "
    f"{latest['rsi']:.2f}"
)

print("\nVolatility and Trend Strength")
print(
    f"True Range    : "
    f"{latest['true_range']:.4f}"
)
print(
    f"ATR           : "
    f"{latest['atr']:.4f}"
)
print(
    f"ADX           : "
    f"{latest['adx']:.2f}"
)
print(
    f"Positive DI   : "
    f"{latest['plus_di']:.2f}"
)
print(
    f"Negative DI   : "
    f"{latest['minus_di']:.2f}"
)

print("\nVolume")
print(
    f"20-Bar Average: "
    f"{latest['volume_avg_20']:,.0f}"
)

print("\nPASS: All indicator columns were created.")
print("PASS: Latest indicator values are usable.")
print("PASS: indicators.py is working correctly.")