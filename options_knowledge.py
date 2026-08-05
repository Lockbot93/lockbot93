"""
options_knowledge.py — what a contract costs, beyond its price.

WHY THIS EXISTS

LOCKBOT gates a contract on expiry, delta, spread, open interest, volume
and affordability. Every one of those asks "can this be traded". None
asks "is this option expensive".

Those are different questions. The spread is what the market charges to
get in and out. Implied volatility is what the market charges for the
option itself — and a call bought at 47% IV on a stock that actually
moves 25% is overpriced whatever its spread looks like. LOCKBOT has been
reading implied_volatility off the feed and discarding it.

The same is true of theta. A $0.74 contract decaying at $0.0201 a day
loses 2.7% of its value daily to time alone. Over the 21-45 day window
LOCKBOT buys in, that is the largest single cost in the trade, and
nothing in the selection path has ever looked at it.

WHY THESE ARE SAFE TO ACT ON WITHOUT SHADOW EVIDENCE

Everywhere else in this project I argue against acting on unmeasured
ideas. This is the exception, and the distinction matters: IV premium and
theta burn are COSTS, not predictions. Paying 47% for 25% of movement is
a bad price the same way a 20% bid-ask spread is a bad price — it does
not depend on whether the signal works. The spread gate was accepted on
exactly this reasoning.

What is NOT claimed here: that low IV predicts direction. It does not.
This only measures what a position costs to hold.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass

TRADING_DAYS = 252


@dataclass
class CostAssessment:
    """What holding this contract costs, separate from whether it wins."""

    implied_volatility: float
    realized_volatility: float
    iv_premium: float          # IV / realized, 1.0 = fairly priced
    theta_burn_daily: float    # fraction of premium lost per day
    theta_burn_to_target: float
    verdict: str
    reasons: list[str]

    @property
    def acceptable(self) -> bool:
        return self.verdict == "ACCEPTABLE"


def realized_volatility(closes: list[float]) -> float:
    """Annualised volatility from daily closes.

    The honest comparator for implied volatility: how much the underlying
    ACTUALLY moved, against how much the option's price says it will.
    Returns 0.0 when there is not enough data to say, and callers must
    treat that as "unknown", never as "calm".
    """

    usable = [float(c) for c in closes if c and float(c) > 0]

    if len(usable) < 10:
        return 0.0

    returns = [
        math.log(usable[i] / usable[i - 1])
        for i in range(1, len(usable))
        if usable[i - 1] > 0
    ]

    if len(returns) < 5:
        return 0.0

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)

    return math.sqrt(variance) * math.sqrt(TRADING_DAYS)


def theta_burn(theta: float, premium_per_share: float) -> float:
    """Daily time decay as a fraction of the premium paid.

    theta arrives per share per day and is negative for a long option.
    Returned positive: 0.027 means 2.7% of the position evaporates daily
    even if the underlying does not move.
    """

    if premium_per_share <= 0:
        return 0.0

    return abs(float(theta or 0.0)) / premium_per_share


def assess_cost(
    *,
    implied_vol: float | None,
    underlying_closes: list[float],
    theta: float | None,
    premium_per_share: float,
    days_to_expiration: int,
    max_iv_premium: float = 1.60,
    max_daily_theta: float = 0.030,
) -> CostAssessment:
    """Judge what this contract costs to own.

    Unknown inputs produce UNKNOWN, never ACCEPTABLE. A missing implied
    volatility is not evidence of a fair price, and treating it as one
    would repeat the mistake day_trade_tracker.py exists to avoid.
    """

    reasons: list[str] = []

    iv = float(implied_vol or 0.0)
    realized = realized_volatility(underlying_closes)
    burn = theta_burn(theta, premium_per_share)
    to_target = burn * max(days_to_expiration, 0)

    if iv <= 0:
        reasons.append("implied volatility unavailable")

    if realized <= 0:
        reasons.append("not enough history to measure realised volatility")

    premium_ratio = (iv / realized) if (iv > 0 and realized > 0) else 0.0

    if not reasons:
        if premium_ratio > max_iv_premium:
            reasons.append(
                f"IV {iv:.0%} is {premium_ratio:.2f}x the {realized:.0%} the "
                f"underlying actually moves — the option is expensive"
            )

        if burn > max_daily_theta:
            reasons.append(
                f"theta burns {burn:.1%} of the premium per day, "
                f"{to_target:.0%} over {days_to_expiration} days"
            )

    if any("unavailable" in r or "not enough history" in r for r in reasons):
        verdict = "UNKNOWN"
    elif reasons:
        verdict = "EXPENSIVE"
    else:
        verdict = "ACCEPTABLE"
        reasons.append(
            f"IV {iv:.0%} against {realized:.0%} realised "
            f"({premium_ratio:.2f}x), theta {burn:.1%}/day"
        )

    return CostAssessment(
        implied_volatility=round(iv, 4),
        realized_volatility=round(realized, 4),
        iv_premium=round(premium_ratio, 3),
        theta_burn_daily=round(burn, 4),
        theta_burn_to_target=round(to_target, 4),
        verdict=verdict,
        reasons=reasons,
    )


def explain(assessment: CostAssessment) -> str:
    """One readable line, for a log or a spoken answer."""

    return f"{assessment.verdict}: " + "; ".join(assessment.reasons)


def _self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    print("Realised volatility")

    flat = [100.0] * 40
    check("a flat series has no volatility",
          realized_volatility(flat) == 0.0 or realized_volatility(flat) < 1e-9,
          str(realized_volatility(flat)))

    # 1% daily moves annualise to roughly 16%.
    alternating = []
    price = 100.0
    for i in range(40):
        price *= 1.01 if i % 2 else 0.99
        alternating.append(price)
    vol = realized_volatility(alternating)
    check("1% daily moves annualise near 16%", 0.10 < vol < 0.25, f"{vol:.3f}")

    check("too little data returns zero, not a guess",
          realized_volatility([100.0, 101.0]) == 0.0)
    check("empty input is safe", realized_volatility([]) == 0.0)
    check("zeros and None are filtered",
          realized_volatility([0, None, 100.0, 101.0]) == 0.0)

    print()
    print("Theta burn")

    # The real PCG contract: theta -0.0201 on a 0.74 option.
    burn = theta_burn(-0.0201, 0.74)
    check("PCG burns about 2.7% a day", abs(burn - 0.0272) < 0.001,
          f"{burn:.4f}")
    check("sign does not matter", theta_burn(0.0201, 0.74) == burn)
    check("a zero premium does not divide by zero",
          theta_burn(-0.02, 0.0) == 0.0)
    check("missing theta is zero burn", theta_burn(None, 1.0) == 0.0)

    print()
    print("Cost assessment")

    calm = []
    price = 100.0
    for i in range(60):
        price *= 1.005 if i % 2 else 0.995
        calm.append(price)

    cheap = assess_cost(
        implied_vol=0.10, underlying_closes=calm, theta=-0.001,
        premium_per_share=1.00, days_to_expiration=30,
    )
    check("a fairly priced option is acceptable",
          cheap.verdict == "ACCEPTABLE", explain(cheap))

    dear = assess_cost(
        implied_vol=0.90, underlying_closes=calm, theta=-0.001,
        premium_per_share=1.00, days_to_expiration=30,
    )
    check("an option at 9x realised vol is expensive",
          dear.verdict == "EXPENSIVE", explain(dear))
    check("and the reason names the multiple",
          "the option is expensive" in explain(dear), explain(dear))

    burning = assess_cost(
        implied_vol=0.10, underlying_closes=calm, theta=-0.10,
        premium_per_share=1.00, days_to_expiration=30,
    )
    check("heavy theta is flagged", burning.verdict == "EXPENSIVE",
          explain(burning))
    check("and quantified over the hold",
          "over 30 days" in explain(burning), explain(burning))

    print()
    print("Unknown never means acceptable")

    no_iv = assess_cost(
        implied_vol=None, underlying_closes=calm, theta=-0.001,
        premium_per_share=1.00, days_to_expiration=30,
    )
    check("missing IV is UNKNOWN, not ACCEPTABLE",
          no_iv.verdict == "UNKNOWN", explain(no_iv))
    check("and it is not acceptable", no_iv.acceptable is False)

    no_history = assess_cost(
        implied_vol=0.30, underlying_closes=[100.0, 101.0], theta=-0.001,
        premium_per_share=1.00, days_to_expiration=30,
    )
    check("missing history is UNKNOWN", no_history.verdict == "UNKNOWN",
          explain(no_history))

    check("an acceptable one IS acceptable", cheap.acceptable is True)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All options-knowledge checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(__doc__)
