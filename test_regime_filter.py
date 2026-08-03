from regime_filter import check_regime_approval


test_cases = [
    (
        "Bullish call allowed",
        "BUY_CALL",
        {
            "regime": "STRONG_UPTREND",
            "trend_direction": "UP",
        },
        True,
        "REGIME_APPROVED",
    ),
    (
        "Bearish call blocked",
        "BUY_CALL",
        {
            "regime": "WEAK_DOWNTREND",
            "trend_direction": "DOWN",
        },
        False,
        "REGIME_OPPOSES_CALL",
    ),
    (
        "Bearish put allowed",
        "BUY_PUT",
        {
            "regime": "STRONG_DOWNTREND",
            "trend_direction": "DOWN",
        },
        True,
        "REGIME_APPROVED",
    ),
    (
        "Bullish put blocked",
        "BUY_PUT",
        {
            "regime": "WEAK_UPTREND",
            "trend_direction": "UP",
        },
        False,
        "REGIME_OPPOSES_PUT",
    ),
    (
        "Ranging market blocked",
        "BUY_CALL",
        {
            "regime": "RANGING",
            "trend_direction": "UP",
        },
        False,
        "REGIME_BLOCKED_RANGING",
    ),
    (
        "High volatility blocked",
        "BUY_PUT",
        {
            "regime": "HIGH_VOLATILITY",
            "trend_direction": "DOWN",
        },
        False,
        "REGIME_BLOCKED_HIGH_VOLATILITY",
    ),
]


print("=" * 50)
print("       LOCKBOT REGIME FILTER TEST")
print("=" * 50)

all_tests_passed = True

for (
    name,
    signal,
    market_regime,
    expected_approved,
    expected_reason,
) in test_cases:
    actual_approved, actual_reason = check_regime_approval(
        signal,
        market_regime,
    )

    passed = (
        actual_approved == expected_approved
        and actual_reason == expected_reason
    )

    print(f"\n{name}")
    print(f"Expected Approved: {expected_approved}")
    print(f"Actual Approved  : {actual_approved}")
    print(f"Expected Reason  : {expected_reason}")
    print(f"Actual Reason    : {actual_reason}")
    print(f"Result           : {'PASS' if passed else 'FAIL'}")

    if not passed:
        all_tests_passed = False


print("\n" + "=" * 50)

if all_tests_passed:
    print("PASS: regime_filter.py is working correctly.")
else:
    print("FAIL: One or more regime filter tests failed.")