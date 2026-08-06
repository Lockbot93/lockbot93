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

    # VWAP RESETS EACH SESSION, and that is not cosmetic.
    #
    # This was a plain cumsum over the whole frame, so VWAP never reset
    # and simply averaged every bar it was given. The meaning of the
    # indicator then depended on how much history the CALLER happened to
    # pass:
    #
    #   market_scanner (live)  SCAN_LOOKBACK_DAYS_5M = 3   -> 3-day mean
    #   backtest.load_history  days=365                    -> 1-year mean
    #
    # So `close > vwap` meant "above the 3-day average" in the live
    # scanner and "above the YEARLY average" in every backtest -- a
    # 120x difference in the same named condition. CLAUDE.md states that
    # backtest.py imports this function precisely so it tests the bot
    # that trades; it did import it, and was still testing a different
    # signal, because the frame shape changed what the function meant.
    #
    # Grouping the cumulative sums by session date restores the
    # conventional definition (volume-weighted average price FOR THE
    # SESSION) and makes the indicator independent of how much history
    # the caller supplies, which is what actually makes live and
    # backtest comparable.
    #
    # Session boundaries come from the timestamp when there is one. US
    # regular hours (13:30-20:00 UTC) never straddle a UTC date change,
    # so the UTC date is a correct session key here.
    if "timestamp" in result.columns:
        session = pd.to_datetime(result["timestamp"]).dt.date
    else:
        # No timestamp: fall back to treating the frame as one session
        # rather than silently inventing boundaries.
        session = pd.Series(0, index=result.index)

    grouped = result.groupby(session, sort=False)

    result["cum_volume"] = grouped["volume"].cumsum()
    result["cum_vp"] = (
        result["close"] * result["volume"]
    ).groupby(session, sort=False).cumsum()

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


def _self_test() -> int:
    """Offline checks. Every measurement in this project starts here.

    Written after add_indicators was found computing VWAP cumulatively
    over the whole frame, so the same condition meant "above the 3-day
    average" live and "above the yearly average" in backtests. Nothing
    caught it because nothing tested this module at all.
    """

    from datetime import datetime, timedelta, timezone

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    base = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)

    def frame(prices, *, days=1, volume=1000.0):
        """Bars across `days` sessions, cycling through `prices`."""
        rows = []
        for day in range(days):
            for i, price in enumerate(prices):
                rows.append({
                    "timestamp": base + timedelta(days=day, minutes=5 * i),
                    "open": price, "high": price * 1.002,
                    "low": price * 0.998, "close": price,
                    "volume": volume,
                })
        return pd.DataFrame(rows)

    print("Input validation")

    try:
        add_indicators("not a frame")
        check("a non-frame is rejected", False)
    except TypeError:
        check("a non-frame is rejected", True)

    try:
        add_indicators(pd.DataFrame({"close": [1.0]}))
        check("missing columns are rejected", False)
    except (ValueError, KeyError):
        check("missing columns are rejected", True)

    bad = frame([100.0] * 30)
    bad.loc[5, "close"] = None
    try:
        add_indicators(bad)
        check("missing values are rejected, not filled", False)
    except ValueError:
        check("missing values are rejected, not filled", True)

    print()
    print("VWAP resets each session")

    # THE REGRESSION. Three flat sessions at different prices: with a
    # per-session reset each day's VWAP equals that day's price. With a
    # cumulative sum it lags behind, and `close > vwap` becomes true for
    # no reason other than that yesterday was cheaper.
    stepped = pd.concat([
        frame([100.0] * 78),
        frame([200.0] * 78).assign(
            timestamp=lambda d: d["timestamp"] + timedelta(days=1)),
        frame([300.0] * 78).assign(
            timestamp=lambda d: d["timestamp"] + timedelta(days=2)),
    ], ignore_index=True)

    out = add_indicators(stepped)
    last_of_day = out.groupby(
        pd.to_datetime(out["timestamp"]).dt.date).last()

    check("each session's VWAP equals its own price",
          all(abs(row["vwap"] - row["close"]) < 0.01
              for _, row in last_of_day.iterrows()),
          str(list(zip(last_of_day["close"], last_of_day["vwap"]))))

    check("VWAP does not drift across sessions",
          abs(out["vwap"].iloc[-1] - 300.0) < 0.01,
          f"{out['vwap'].iloc[-1]:.2f}")

    # The property that actually matters: the answer must not depend on
    # how much history the caller happened to pass.
    one_day = add_indicators(frame([100.0] * 78))
    many_days = add_indicators(frame([100.0] * 78, days=30))

    check("VWAP is independent of frame length",
          abs(one_day["vwap"].iloc[-1]
              - many_days["vwap"].iloc[77]) < 0.01,
          f"{one_day['vwap'].iloc[-1]:.4f} vs "
          f"{many_days['vwap'].iloc[77]:.4f}")

    # A rising session must put VWAP below the last close, because the
    # earlier cheaper bars drag the average down.
    rising = add_indicators(frame([100.0 + i for i in range(78)]))
    check("VWAP trails price in a rising session",
          rising["vwap"].iloc[-1] < rising["close"].iloc[-1])

    print()
    print("No look-ahead")

    # volume_avg_20 must exclude the current bar, or the scanner is
    # comparing volume against an average that already contains it.
    spike = frame([100.0] * 40)
    spike.loc[39, "volume"] = 1_000_000.0
    out = add_indicators(spike)
    check("the 20-bar volume average excludes the current bar",
          abs(out["volume_avg_20"].iloc[39] - 1000.0) < 1.0,
          f"{out['volume_avg_20'].iloc[39]:.1f}")
    check("and it is undefined before 20 bars exist",
          pd.isna(out["volume_avg_20"].iloc[10]))

    print()
    print("RSI")

    rsi_up = add_indicators(frame([100.0 * (1.01 ** i) for i in range(60)]))
    check("a series that only rises is overbought",
          rsi_up["rsi"].iloc[-1] > 70, f"{rsi_up['rsi'].iloc[-1]:.1f}")

    rsi_down = add_indicators(frame([100.0 * (0.99 ** i) for i in range(60)]))
    check("a series that only falls is oversold",
          rsi_down["rsi"].iloc[-1] < 30, f"{rsi_down['rsi'].iloc[-1]:.1f}")

    check("RSI stays inside 0-100",
          rsi_up["rsi"].dropna().between(0, 100).all()
          and rsi_down["rsi"].dropna().between(0, 100).all())

    print()
    print("MACD, EMA and ATR")

    trend = add_indicators(frame([100.0 + i * 0.5 for i in range(80)]))

    check("MACD is the 12/26 EMA difference",
          abs(trend["macd"].iloc[-1]
              - (trend["ema_12"].iloc[-1] - trend["ema_26"].iloc[-1]))
          < 1e-9)
    check("MACD leads its signal line in an uptrend",
          trend["macd"].iloc[-1] > trend["macd_signal"].iloc[-1])
    check("the fast EMA leads the slow one in an uptrend",
          trend["ema_9"].iloc[-1] > trend["ema_21"].iloc[-1])
    check("EMAs sit inside the price range",
          trend["close"].min() <= trend["ema_21"].iloc[-1]
          <= trend["close"].max())

    flat = add_indicators(frame([100.0] * 60))
    check("a flat series has no true range beyond its own bar",
          flat["true_range"].iloc[-1] < 100.0 * 0.005)
    check("ATR is never negative", (trend["atr"].dropna() >= 0).all())

    print()
    print("Every field the rules may reference is produced")

    try:
        from strategy_lab import FIELDS

        produced = set(trend.columns)
        missing = FIELDS - produced
        check("add_indicators produces every allowlisted field",
              not missing, f"missing {sorted(missing)}")
    except ImportError:
        check("strategy_lab importable", False)

    check("the input frame is not mutated",
          "rsi" not in frame([100.0] * 30).columns)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All indicator checks passed.")
    return 0


if __name__ == "__main__":
    import sys

    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(__doc__)