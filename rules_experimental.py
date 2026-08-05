"""
rules_experimental.py — candidate entry rules, for backtesting only.

WHY THESE EXIST

market_scanner.detect_signal is one rule from one family: momentum
confluence. Trend up, price above its EMA, above VWAP, RSI in the upper
half, MACD above its signal — every condition says "this is already
moving up, join it".

Over 119 resolved setups it converts 20.2% into winners against a 33.3%
breakeven, and the internal split points somewhere specific: WEAK_UPTREND
setups win 26.7% while STRONG_UPTREND setups win 13.6%. Buying MORE
strength does WORSE. That is the signature of a rule arriving late — by
the time every momentum condition agrees, the move being joined is often
the one that is ending.

So the useful experiment is not more momentum rules. It is a rule from a
different family, tested on the same bars by the same harness.

NOTHING HERE TRADES. These are passed to backtest.py and nowhere else.
Promoting one into market_scanner would be a separate, deliberate change
made only if the evidence supports it — and five days of data does not.
"""

from __future__ import annotations

from typing import Any


def momentum_baseline(row: dict[str, Any], trend: str) -> str:
    """LOCKBOT's live rule, imported rather than restated.

    detect_signal indexes its row directly and raises on a missing key.
    That is fine in production — evaluate_five_minute builds every row
    from a DataFrame with known columns — but a backtest can feed it a
    gap, and a comparison that dies on one bad bar tells you nothing.
    Guarded here rather than in detect_signal, because loosening the live
    entry rule to make a test convenient is the wrong trade.
    """

    from market_scanner import detect_signal

    try:
        signal, _ = detect_signal(row, trend, data_is_fresh=True)
    except (KeyError, TypeError):
        return "NO_TRADE"

    return signal


def mean_reversion(row: dict[str, Any], trend: str) -> str:
    """Buy weakness inside an uptrend, rather than strength.

    The mirror of the baseline's premise. Same trend filter — this is not
    catching falling knives, the longer-term direction must still be up —
    but it enters when price has pulled BELOW its short EMA and RSI is
    oversold rather than strong.

    If the baseline's problem is arriving late, this should do better on
    the same bars. If both lose, the entry timing is not the issue and
    the problem is further upstream.
    """

    try:
        if trend != "BULLISH":
            return "NO_TRADE"

        if row["close"] >= row["ema_9"]:
            return "NO_TRADE"

        if not 30 < row["rsi"] < 45:
            return "NO_TRADE"

        # Still above the slower average: a pullback, not a breakdown.
        if row["close"] < row["ema_21"] * 0.98:
            return "NO_TRADE"

        return "BUY_LONG"

    except (KeyError, TypeError):
        return "NO_TRADE"


def trend_only(row: dict[str, Any], trend: str) -> str:
    """The trend filter alone, with none of the confluence.

    A deliberate control. If this performs like the baseline, the four
    extra conditions are decoration — they would be filtering setups
    without improving them, which is worth knowing before anyone tunes
    them.
    """

    return "BUY_LONG" if trend == "BULLISH" else "NO_TRADE"


def strong_momentum_only(row: dict[str, Any], trend: str) -> str:
    """The baseline, restricted to the strongest setups.

    Tests the regime finding directly. If STRONG_UPTREND entries really
    are the worse half, this should underperform the baseline rather than
    beat it — the opposite of what "take only the best signals" implies.
    """

    try:
        if momentum_baseline(row, trend) != "BUY_LONG":
            return "NO_TRADE"

        # Well extended above both averages: the strongest end.
        if row["close"] < row["ema_9"] * 1.005:
            return "NO_TRADE"

        if row["rsi"] < 60:
            return "NO_TRADE"

        return "BUY_LONG"

    except (KeyError, TypeError):
        return "NO_TRADE"


CANDIDATES = {
    "momentum (live rule)": momentum_baseline,
    "mean reversion": mean_reversion,
    "trend filter only": trend_only,
    "strongest momentum": strong_momentum_only,
}


def _self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    strong = {
        "close": 103.0, "ema_9": 101.0, "ema_21": 98.0, "vwap": 100.0,
        "rsi": 65.0, "macd": 1.2, "macd_signal": 0.4,
    }
    pullback = {
        "close": 99.5, "ema_9": 101.0, "ema_21": 98.0, "vwap": 100.0,
        "rsi": 38.0, "macd": 1.2, "macd_signal": 0.4,
    }

    print("The rules disagree, which is the point")

    check("momentum takes strength",
          momentum_baseline(strong, "BULLISH") == "BUY_LONG")
    check("momentum passes on a pullback",
          momentum_baseline(pullback, "BULLISH") == "NO_TRADE")
    check("mean reversion takes the pullback",
          mean_reversion(pullback, "BULLISH") == "BUY_LONG")
    check("mean reversion passes on strength",
          mean_reversion(strong, "BULLISH") == "NO_TRADE")

    print()
    print("Guards")

    check("mean reversion needs an uptrend",
          mean_reversion(pullback, "BEARISH") == "NO_TRADE")

    breakdown = dict(pullback, close=90.0)
    check("a breakdown is not a pullback",
          mean_reversion(breakdown, "BULLISH") == "NO_TRADE")

    check("trend-only takes any uptrend bar",
          trend_only(pullback, "BULLISH") == "BUY_LONG")
    check("and nothing otherwise",
          trend_only(strong, "NEUTRAL") == "NO_TRADE")

    check("strongest momentum is a subset of momentum",
          strong_momentum_only(pullback, "BULLISH") == "NO_TRADE")

    weak_rsi = dict(strong, rsi=52.0)
    check("and it rejects the weaker half",
          strong_momentum_only(weak_rsi, "BULLISH") == "NO_TRADE"
          and momentum_baseline(weak_rsi, "BULLISH") == "BUY_LONG")

    print()
    print("Malformed rows never raise")

    for rule in CANDIDATES.values():
        try:
            rule({}, "BULLISH")
        except Exception as error:
            failures.append(f"{rule.__name__} raised {error}")

    check("every rule survives an empty row", not failures)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All experimental-rule checks passed.")
    return 0


if __name__ == "__main__":
    import sys

    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(__doc__)
