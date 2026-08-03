"""
Standalone test for LockBot's market regime module.
"""

import pandas as pd

from market_regime import classify_market_regime


TEST_CASES = [
    ("Strong Uptrend", 500.0, 1.5, 32.0, 30.0, 12.0, "STRONG_UPTREND"),
    ("Strong Downtrend", 500.0, 1.5, 30.0, 10.0, 28.0, "STRONG_DOWNTREND"),
    ("Weak Uptrend", 500.0, 1.5, 20.0, 24.0, 16.0, "WEAK_UPTREND"),
    ("Ranging Market", 500.0, 1.5, 12.0, 18.0, 17.0, "RANGING"),
    ("High Volatility", 500.0, 3.0, 35.0, 31.0, 14.0, "HIGH_VOLATILITY"),
]


print("=" * 50)
print("       LOCKBOT MARKET REGIME TEST")
print("=" * 50)

all_tests_passed = True

for (
    name,
    close,
    atr,
    adx,
    plus_di,
    minus_di,
    expected,
) in TEST_CASES:
    result = classify_market_regime(
        pd.Series(
            {
                "close": close,
                "atr": atr,
                "adx": adx,
                "plus_di": plus_di,
                "minus_di": minus_di,
            }
        )
    )

    actual = result["regime"]
    passed = actual == expected

    print(f"\n{name}")
    print(f"Expected       : {expected}")
    print(f"Actual         : {actual}")
    print(f"ATR Percent    : {result['atr_percent']:.2f}%")
    print(f"ADX            : {result['adx']:.2f}")
    print(f"Trend Direction: {result['trend_direction']}")
    print(f"Result         : {'PASS' if passed else 'FAIL'}")

    if not passed:
        all_tests_passed = False

print("\n" + "=" * 50)

if all_tests_passed:
    print("PASS: market_regime.py is working correctly.")
else:
    raise AssertionError(
        "One or more market regime tests failed."
    )