"""
Technical indicator calculations for LockBot.

This module calculates and adds:

- EMA 9
- EMA 21
- EMA 12
- EMA 26
- MACD
- MACD signal line
- VWAP
- 20-bar average volume
- RSI 14
- True Range
- ATR 14
- ADX 14
- Positive Directional Indicator
- Negative Directional Indicator
"""

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
}


def validate_market_data(df: pd.DataFrame) -> None:
    """
    Verify that the supplied market data can be used
    by LockBot's indicator calculations.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            "Indicator input must be a pandas DataFrame."
        )

    if df.empty:
        raise ValueError(
            "Market data DataFrame is empty."
        )

    missing_columns = REQUIRED_COLUMNS.difference(
        df.columns
    )

    if missing_columns:
        missing_text = ", ".join(
            sorted(missing_columns)
        )

        raise ValueError(
            "Market data is missing required columns: "
            f"{missing_text}"
        )


def calculate_rsi(
    close_prices: pd.Series,
    period: int = 14,
) -> pd.Series:
    """
    Calculate the Relative Strength Index.
    """
    price_change = close_prices.diff()

    gains = price_change.clip(lower=0)
    losses = -price_change.clip(upper=0)

    average_gain = gains.rolling(
        window=period,
        min_periods=period,
    ).mean()

    average_loss = losses.rolling(
        window=period,
        min_periods=period,
    ).mean()

    safe_average_loss = average_loss.replace(
        0,
        np.nan,
    )

    relative_strength = (
        average_gain / safe_average_loss
    )

    rsi = 100 - (
        100 / (1 + relative_strength)
    )

    only_gains = (
        (average_gain > 0)
        & (average_loss == 0)
    )

    no_price_change = (
        (average_gain == 0)
        & (average_loss == 0)
    )

    rsi = rsi.mask(
        only_gains,
        100.0,
    )

    rsi = rsi.mask(
        no_price_change,
        50.0,
    )

    return rsi


def calculate_true_range(
    df: pd.DataFrame,
) -> pd.Series:
    """
    Calculate True Range for every market bar.
    """
    previous_close = df["close"].shift(1)

    high_low_range = (
        df["high"] - df["low"]
    ).abs()

    high_previous_close_range = (
        df["high"] - previous_close
    ).abs()

    low_previous_close_range = (
        df["low"] - previous_close
    ).abs()

    range_values = pd.concat(
        [
            high_low_range,
            high_previous_close_range,
            low_previous_close_range,
        ],
        axis=1,
    )

    true_range = range_values.max(
        axis=1
    )

    return true_range


def calculate_atr(
    df: pd.DataFrame,
    period: int = 14,
) -> pd.Series:
    """
    Calculate Average True Range using
    Wilder-style exponential smoothing.
    """
    true_range = calculate_true_range(df)

    atr = true_range.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    return atr


def calculate_adx(
    df: pd.DataFrame,
    period: int = 14,
) -> tuple[
    pd.Series,
    pd.Series,
    pd.Series,
]:
    """
    Calculate:

    - ADX
    - Positive Directional Indicator
    - Negative Directional Indicator
    """
    upward_move = df["high"].diff()

    downward_move = -df["low"].diff()

    plus_directional_movement = pd.Series(
        np.where(
            (
                upward_move
                > downward_move
            )
            & (
                upward_move > 0
            ),
            upward_move,
            0.0,
        ),
        index=df.index,
        dtype="float64",
    )

    minus_directional_movement = pd.Series(
        np.where(
            (
                downward_move
                > upward_move
            )
            & (
                downward_move > 0
            ),
            downward_move,
            0.0,
        ),
        index=df.index,
        dtype="float64",
    )

    true_range = calculate_true_range(df)

    smoothed_true_range = true_range.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    smoothed_plus_dm = (
        plus_directional_movement.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()
    )

    smoothed_minus_dm = (
        minus_directional_movement.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()
    )

    safe_true_range = (
        smoothed_true_range.replace(
            0,
            np.nan,
        )
    )

    plus_di = (
        100
        * smoothed_plus_dm
        / safe_true_range
    )

    minus_di = (
        100
        * smoothed_minus_dm
        / safe_true_range
    )

    directional_sum = (
        plus_di + minus_di
    ).replace(
        0,
        np.nan,
    )

    directional_difference = (
        plus_di - minus_di
    ).abs()

    directional_index = (
        100
        * directional_difference
        / directional_sum
    )

    adx = directional_index.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    return (
        adx,
        plus_di,
        minus_di,
    )


def add_indicators(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Add all technical indicators currently used
    by LockBot to a copy of the supplied DataFrame.
    """
    validate_market_data(df)

    result = df.copy()

    numeric_columns = [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]

    for column in numeric_columns:
        result[column] = pd.to_numeric(
            result[column],
            errors="coerce",
        )

    invalid_data_exists = (
        result[numeric_columns]
        .isna()
        .any()
        .any()
    )

    if invalid_data_exists:
        raise ValueError(
            "Market data contains missing or invalid "
            "numeric values."
        )

    result["ema_9"] = (
        result["close"]
        .ewm(
            span=9,
            adjust=False,
        )
        .mean()
    )

    result["ema_21"] = (
        result["close"]
        .ewm(
            span=21,
            adjust=False,
        )
        .mean()
    )

    result["ema_12"] = (
        result["close"]
        .ewm(
            span=12,
            adjust=False,
        )
        .mean()
    )

    result["ema_26"] = (
        result["close"]
        .ewm(
            span=26,
            adjust=False,
        )
        .mean()
    )

    result["macd"] = (
        result["ema_12"]
        - result["ema_26"]
    )

    result["macd_signal"] = (
        result["macd"]
        .ewm(
            span=9,
            adjust=False,
        )
        .mean()
    )

    result["cum_volume"] = (
        result["volume"].cumsum()
    )

    result["cum_vp"] = (
        result["close"]
        * result["volume"]
    ).cumsum()

    result["vwap"] = (
        result["cum_vp"]
        / result["cum_volume"].replace(
            0,
            np.nan,
        )
    )

    result["volume_avg_20"] = (
        result["volume"]
        .shift(1)
        .rolling(
            window=20,
            min_periods=20,
        )
        .mean()
    )

    result["rsi"] = calculate_rsi(
        close_prices=result["close"],
        period=14,
    )

    result["true_range"] = (
        calculate_true_range(result)
    )

    result["atr"] = calculate_atr(
        df=result,
        period=14,
    )

    (
        result["adx"],
        result["plus_di"],
        result["minus_di"],
    ) = calculate_adx(
        df=result,
        period=14,
    )

    return result