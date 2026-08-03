"""Market-regime approval rules for LockBot stock signals."""

BLOCKED_REGIMES = {
    "RANGING",
    "HIGH_VOLATILITY",
    "UNKNOWN",
}


def check_regime_approval(signal, market_regime):
    """
    Decide whether the current market regime supports a stock signal.

    Returns:
        tuple[bool, str]: approval status and reason.
    """
    regime = market_regime.get("regime", "UNKNOWN")
    trend_direction = market_regime.get(
        "trend_direction",
        "UNKNOWN",
    )

    if regime in BLOCKED_REGIMES:
        return False, f"REGIME_BLOCKED_{regime}"

    if signal == "BUY_LONG":
        if trend_direction != "UP":
            return False, "REGIME_OPPOSES_LONG"

        return True, "REGIME_APPROVES_LONG"

    if signal == "SELL_SHORT":
        if trend_direction != "DOWN":
            return False, "REGIME_OPPOSES_SHORT"

        return True, "REGIME_APPROVES_SHORT"

    return False, "REGIME_SIGNAL_NOT_SUPPORTED"
