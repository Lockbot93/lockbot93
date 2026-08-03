"""
Market regime classification for LockBot.

This module uses ADX, ATR, Positive DI, and Negative DI
to determine the current market environment.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


REQUIRED_REGIME_COLUMNS = {
    "close",
    "atr",
    "adx",
    "plus_di",
    "minus_di",
}

STRONG_TREND_ADX = 25.0
WEAK_TREND_ADX = 18.0
HIGH_VOLATILITY_ATR_PERCENT = 0.50


def validate_regime_data(
    df: pd.DataFrame,
) -> None:
    """
    Confirm that the DataFrame contains the values
    needed to classify the market regime.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            "Market regime input must be a pandas DataFrame."
        )

    if df.empty:
        raise ValueError(
            "Market regime input DataFrame is empty."
        )

    missing_columns = (
        REQUIRED_REGIME_COLUMNS.difference(
            df.columns
        )
    )

    if missing_columns:
        missing_text = ", ".join(
            sorted(missing_columns)
        )

        raise ValueError(
            "Market regime data is missing required columns: "
            f"{missing_text}"
        )


def is_finite_number(
    value: Any,
) -> bool:
    """
    Check whether a value can safely be treated
    as a finite number.
    """
    try:
        return math.isfinite(
            float(value)
        )

    except (
        TypeError,
        ValueError,
    ):
        return False


def calculate_atr_percent(
    close_price: float,
    atr_value: float,
) -> float:
    """
    Convert ATR into a percentage of the current price.
    """
    if close_price <= 0:
        raise ValueError(
            "Close price must be greater than zero."
        )

    if atr_value < 0:
        raise ValueError(
            "ATR cannot be negative."
        )

    return (
        atr_value / close_price
    ) * 100.0


def classify_market_regime(
    latest_row: pd.Series,
) -> dict[str, Any]:
    """
    Classify one completed market bar.

    Possible regimes:

    - STRONG_UPTREND
    - STRONG_DOWNTREND
    - WEAK_UPTREND
    - WEAK_DOWNTREND
    - RANGING
    - HIGH_VOLATILITY
    - UNKNOWN
    """
    required_values = {
        "close": latest_row.get("close"),
        "atr": latest_row.get("atr"),
        "adx": latest_row.get("adx"),
        "plus_di": latest_row.get("plus_di"),
        "minus_di": latest_row.get("minus_di"),
    }

    values_are_valid = all(
        is_finite_number(value)
        for value in required_values.values()
    )

    if not values_are_valid:
        return {
            "regime": "UNKNOWN",
            "trend_direction": "UNKNOWN",
            "trend_strength": "UNKNOWN",
            "volatility_state": "UNKNOWN",
            "atr_percent": float("nan"),
            "adx": required_values["adx"],
            "plus_di": required_values["plus_di"],
            "minus_di": required_values["minus_di"],
        }

    close_price = float(
        required_values["close"]
    )

    atr_value = float(
        required_values["atr"]
    )

    adx_value = float(
        required_values["adx"]
    )

    plus_di = float(
        required_values["plus_di"]
    )

    minus_di = float(
        required_values["minus_di"]
    )

    atr_percent = calculate_atr_percent(
        close_price=close_price,
        atr_value=atr_value,
    )

    if plus_di > minus_di:
        trend_direction = "UP"

    elif minus_di > plus_di:
        trend_direction = "DOWN"

    else:
        trend_direction = "NEUTRAL"

    if adx_value >= STRONG_TREND_ADX:
        trend_strength = "STRONG"

    elif adx_value >= WEAK_TREND_ADX:
        trend_strength = "WEAK"

    else:
        trend_strength = "RANGING"

    if (
        atr_percent
        >= HIGH_VOLATILITY_ATR_PERCENT
    ):
        volatility_state = "HIGH"

    else:
        volatility_state = "NORMAL"

    if volatility_state == "HIGH":
        regime = "HIGH_VOLATILITY"

    elif (
        trend_strength == "STRONG"
        and trend_direction == "UP"
    ):
        regime = "STRONG_UPTREND"

    elif (
        trend_strength == "STRONG"
        and trend_direction == "DOWN"
    ):
        regime = "STRONG_DOWNTREND"

    elif (
        trend_strength == "WEAK"
        and trend_direction == "UP"
    ):
        regime = "WEAK_UPTREND"

    elif (
        trend_strength == "WEAK"
        and trend_direction == "DOWN"
    ):
        regime = "WEAK_DOWNTREND"

    else:
        regime = "RANGING"

    return {
        "regime": regime,
        "trend_direction": trend_direction,
        "trend_strength": trend_strength,
        "volatility_state": volatility_state,
        "atr_percent": atr_percent,
        "adx": adx_value,
        "plus_di": plus_di,
        "minus_di": minus_di,
    }


def get_market_regime(
    df: pd.DataFrame,
) -> dict[str, Any]:
    """
    Classify the latest row in a complete
    LockBot indicator DataFrame.
    """
    validate_regime_data(df)

    latest_row = df.iloc[-1]

    return classify_market_regime(
        latest_row=latest_row
    )