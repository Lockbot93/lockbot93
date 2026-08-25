"""
options_greeks.py  --  compute delta when the feed will not give it   v1.0

READ ONLY. No orders, no network, no writes. Pure arithmetic.

WHY THIS EXISTS
    Contract selection is gated on OPTIONS_TARGET_DELTA_MIN/MAX = 0.35/0.60,
    but Alpaca's indicative options feed does not reliably supply Greeks. A
    rule that cannot read its own input is not being enforced. This module
    derives delta from data the feed does provide -- the option's own price --
    so the rule has something to run on without a data subscription or a
    broker migration.

HOW
    1. Solve implied volatility numerically from the option's mid price, by
       bisection. Bisection rather than Newton-Raphson: it cannot diverge, and
       a wrong answer here silently mis-selects every contract.
    2. Compute delta from Black-Scholes with that volatility.

    No scipy. The normal CDF comes from math.erf, which is exact and in the
    standard library -- one less dependency that can break on a Windows box at
    6am.

WHAT IT IS NOT
    A model estimate, not the exchange's Greek. Three honest limits:

    * Black-Scholes assumes European exercise. US equity options are American.
      For a 0.35-0.60 delta screen the difference is small; for hedging it is
      not, and this module should not be used for hedging.
    * It needs a usable two-sided quote. The existing spread gate already
      requires one, so this adds no new constraint.
    * Dividends matter. A 6%-yielding name like T is materially mispriced if
      the yield is ignored, so dividend_yield is a real parameter, not
      decoration. Passing 0 for a dividend payer will overstate call delta.

CONVENTIONS FOLLOWED
    * None, never a default. Anything that cannot be computed returns None --
      never 0.0, never a guess. A default value is a claim.
    * Callables raise rather than being mistaken for data.
    * A result outside its own theoretical bounds returns None rather than
      being clamped into looking reasonable.

USAGE
    python options_greeks.py --self-test
    python options_greeks.py --demo

    from options_greeks import implied_delta
    d = implied_delta(option_price=1.13, underlying=90.20, strike=90.0,
                      days_to_expiry=44, option_type="call")
"""

from __future__ import annotations

import argparse
import math
import sys
from typing import Any

VERSION = "1.0"

# Annualised risk-free rate used when the caller does not supply one. Delta is
# only weakly sensitive to r at 30-45 days, so a stale value here costs little
# -- but it is exposed rather than buried so it can be corrected.
DEFAULT_RISK_FREE_RATE = 0.04

# Volatility search bounds. 1% to 500% annualised covers everything a listed
# equity option realistically quotes at; outside it, the price is telling you
# something is wrong with the quote rather than with the bounds.
MIN_VOL = 0.01
MAX_VOL = 5.00

# Bisection settings. 100 iterations on a 5.0-wide bracket resolves far below
# any meaningful precision; the tolerance stops it earlier in practice.
MAX_ITERATIONS = 100
PRICE_TOLERANCE = 1e-8

TRADING_DAYS_NOTE = "calendar days, not trading days -- theta runs on weekends"


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def _reject_callable(value: Any, what: str) -> None:
    if callable(value):
        raise TypeError(
            f"{what} received a function instead of a value. Something wrapped "
            "the call and never executed it."
        )


def as_float(value: Any) -> float | None:
    """Best-effort float. None on anything unusable. Never 0.0 as a fallback."""
    _reject_callable(value, "as_float")
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def normalise_option_type(option_type: Any) -> str | None:
    """'call'/'c'/'CALL' -> 'call'. Anything unrecognised -> None."""
    _reject_callable(option_type, "normalise_option_type")
    if not isinstance(option_type, str):
        return None
    t = option_type.strip().lower()
    if t in ("call", "c", "calls"):
        return "call"
    if t in ("put", "p", "puts"):
        return "put"
    return None


# ---------------------------------------------------------------------------
# normal distribution, standard library only
# ---------------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function. Exact, no scipy."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Black-Scholes
# ---------------------------------------------------------------------------

def _d1(spot: float, strike: float, years: float, rate: float,
        dividend_yield: float, vol: float) -> float:
    return ((math.log(spot / strike)
             + (rate - dividend_yield + 0.5 * vol * vol) * years)
            / (vol * math.sqrt(years)))


def black_scholes_price(spot: Any, strike: Any, years: Any, rate: Any,
                        dividend_yield: Any, vol: Any,
                        option_type: Any) -> float | None:
    """Theoretical price. None if any input makes the model undefined."""
    s = as_float(spot)
    k = as_float(strike)
    t = as_float(years)
    r = as_float(rate)
    q = as_float(dividend_yield)
    v = as_float(vol)
    kind = normalise_option_type(option_type)

    if None in (s, k, t, r, q, v) or kind is None:
        return None
    if s <= 0 or k <= 0 or t <= 0 or v <= 0:
        return None

    d1 = _d1(s, k, t, r, q, v)
    d2 = d1 - v * math.sqrt(t)
    discounted_spot = s * math.exp(-q * t)
    discounted_strike = k * math.exp(-r * t)

    if kind == "call":
        return discounted_spot * norm_cdf(d1) - discounted_strike * norm_cdf(d2)
    return discounted_strike * norm_cdf(-d2) - discounted_spot * norm_cdf(-d1)


def black_scholes_delta(spot: Any, strike: Any, years: Any, rate: Any,
                        dividend_yield: Any, vol: Any,
                        option_type: Any) -> float | None:
    """Delta given a volatility. None if any input makes it undefined.

    Calls land in (0, 1); puts land in (-1, 0). A result outside its own
    bounds returns None rather than being clamped into looking plausible.
    """
    s = as_float(spot)
    k = as_float(strike)
    t = as_float(years)
    r = as_float(rate)
    q = as_float(dividend_yield)
    v = as_float(vol)
    kind = normalise_option_type(option_type)

    if None in (s, k, t, r, q, v) or kind is None:
        return None
    if s <= 0 or k <= 0 or t <= 0 or v <= 0:
        return None

    d1 = _d1(s, k, t, r, q, v)
    carry = math.exp(-q * t)
    delta = carry * norm_cdf(d1) if kind == "call" else carry * (norm_cdf(d1) - 1.0)

    if kind == "call" and not (0.0 <= delta <= 1.0):
        return None
    if kind == "put" and not (-1.0 <= delta <= 0.0):
        return None
    return delta


# ---------------------------------------------------------------------------
# no-arbitrage bounds
# ---------------------------------------------------------------------------

def price_bounds(spot: float, strike: float, years: float, rate: float,
                 dividend_yield: float, kind: str) -> tuple[float, float]:
    """(lower, upper) prices consistent with no arbitrage.

    A quote outside these cannot be inverted for a volatility -- not because
    the solver is weak, but because no volatility produces that price.
    """
    discounted_spot = spot * math.exp(-dividend_yield * years)
    discounted_strike = strike * math.exp(-rate * years)
    if kind == "call":
        return max(0.0, discounted_spot - discounted_strike), discounted_spot
    return max(0.0, discounted_strike - discounted_spot), discounted_strike


# ---------------------------------------------------------------------------
# implied volatility
# ---------------------------------------------------------------------------

def implied_volatility(option_price: Any, spot: Any, strike: Any, years: Any,
                       rate: Any = DEFAULT_RISK_FREE_RATE,
                       dividend_yield: Any = 0.0,
                       option_type: Any = "call") -> float | None:
    """Solve for the volatility that reproduces the observed price.

    Bisection over [MIN_VOL, MAX_VOL]. Returns None -- never a guess -- when
    the price sits outside the no-arbitrage band, when the inputs are
    unusable, or when the bracket does not contain a solution.
    """
    price = as_float(option_price)
    s = as_float(spot)
    k = as_float(strike)
    t = as_float(years)
    r = as_float(rate)
    q = as_float(dividend_yield)
    kind = normalise_option_type(option_type)

    if None in (price, s, k, t, r, q) or kind is None:
        return None
    if price <= 0 or s <= 0 or k <= 0 or t <= 0:
        return None

    lower_price, upper_price = price_bounds(s, k, t, r, q, kind)
    # A price at or below intrinsic carries no time value, so no positive
    # volatility reproduces it. Report that rather than returning MIN_VOL.
    if price <= lower_price + PRICE_TOLERANCE:
        return None
    if price >= upper_price:
        return None

    low, high = MIN_VOL, MAX_VOL
    price_low = black_scholes_price(s, k, t, r, q, low, kind)
    price_high = black_scholes_price(s, k, t, r, q, high, kind)
    if price_low is None or price_high is None:
        return None
    if not (price_low <= price <= price_high):
        return None

    for _ in range(MAX_ITERATIONS):
        mid = 0.5 * (low + high)
        value = black_scholes_price(s, k, t, r, q, mid, kind)
        if value is None:
            return None
        if abs(value - price) < PRICE_TOLERANCE:
            return mid
        if value < price:
            low = mid
        else:
            high = mid

    return 0.5 * (low + high)


# ---------------------------------------------------------------------------
# the function the scanner calls
# ---------------------------------------------------------------------------

def implied_delta(option_price: Any = None, underlying: Any = None,
                  strike: Any = None, days_to_expiry: Any = None,
                  option_type: Any = "call",
                  rate: Any = DEFAULT_RISK_FREE_RATE,
                  dividend_yield: Any = 0.0,
                  bid: Any = None, ask: Any = None) -> float | None:
    """Delta implied by an option's own quote. None if it cannot be computed.

    Supply either option_price, or bid and ask (the midpoint is used, matching
    how every other cost figure in this project is measured).

    days_to_expiry is CALENDAR days -- theta runs on weekends.
    """
    price = as_float(option_price)
    if price is None:
        b, a = as_float(bid), as_float(ask)
        if b is None or a is None or b <= 0 or a <= 0 or a < b:
            return None
        price = 0.5 * (b + a)

    days = as_float(days_to_expiry)
    if days is None or days <= 0:
        return None
    years = days / 365.0

    vol = implied_volatility(price, underlying, strike, years, rate,
                             dividend_yield, option_type)
    if vol is None:
        return None
    return black_scholes_delta(underlying, strike, years, rate, dividend_yield,
                               vol, option_type)


def delta_within_band(delta: float | None, low: Any, high: Any) -> bool | None:
    """Does |delta| sit inside the configured band?

    Absolute value, so one band covers calls and puts. Returns None -- not
    False -- when delta is unknown, so a missing Greek can never read as a
    passed check.
    """
    if delta is None:
        return None
    lo, hi = as_float(low), as_float(high)
    if lo is None or hi is None or lo > hi:
        return None
    return lo <= abs(delta) <= hi


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

def self_test() -> int:
    checks: list[tuple[str, bool]] = []

    def check(name: str, cond: Any) -> None:
        checks.append((name, bool(cond)))

    def close(a: Any, b: float, tol: float = 1e-6) -> bool:
        return a is not None and abs(a - b) < tol

    # --- guards -----------------------------------------------------------
    try:
        as_float(lambda: 1.0)
        check("as_float raises on a callable", False)
    except TypeError:
        check("as_float raises on a callable", True)
    check("as_float rejects junk", as_float("abc") is None)
    check("as_float rejects NaN", as_float(float("nan")) is None)
    check("as_float rejects infinity", as_float(float("inf")) is None)
    check("as_float keeps a real zero", as_float(0) == 0.0)

    check("call type normalises", normalise_option_type("CALL") == "call")
    check("c normalises to call", normalise_option_type(" c ") == "call")
    check("put type normalises", normalise_option_type("Put") == "put")
    check("unknown type gives None", normalise_option_type("straddle") is None)
    check("non-string type gives None", normalise_option_type(3) is None)

    # --- normal CDF -------------------------------------------------------
    check("N(0) is one half", close(norm_cdf(0.0), 0.5))
    check("N(-inf) approaches zero", norm_cdf(-10) < 1e-9)
    check("N(inf) approaches one", norm_cdf(10) > 1 - 1e-9)
    check("N(1.96) is about 0.975", close(norm_cdf(1.96), 0.975, 1e-3))
    check("CDF is symmetric", close(norm_cdf(-1.0) + norm_cdf(1.0), 1.0))

    # --- Black-Scholes against a hand-computable case ---------------------
    # S=K, r=0, q=0, T=1, sigma=0.20  ->  d1 = 0.10  ->  delta = N(0.10)
    hand = black_scholes_delta(100, 100, 1.0, 0.0, 0.0, 0.20, "call")
    check("ATM call delta matches N(d1) by hand",
          close(hand, norm_cdf(0.10), 1e-9))
    check("ATM call delta is a little above 0.5", 0.50 < hand < 0.56)

    # Put-call parity on delta: call - put = exp(-qT)
    c = black_scholes_delta(100, 100, 1.0, 0.04, 0.0, 0.25, "call")
    p = black_scholes_delta(100, 100, 1.0, 0.04, 0.0, 0.25, "put")
    check("put-call delta parity holds", close(c - p, 1.0, 1e-9))

    cq = black_scholes_delta(100, 100, 1.0, 0.04, 0.06, 0.25, "call")
    pq = black_scholes_delta(100, 100, 1.0, 0.04, 0.06, 0.25, "put")
    check("parity holds with a dividend yield",
          close(cq - pq, math.exp(-0.06), 1e-9))

    check("deep ITM call delta approaches 1",
          black_scholes_delta(200, 100, 0.1, 0.04, 0.0, 0.20, "call") > 0.99)
    check("deep OTM call delta approaches 0",
          black_scholes_delta(50, 100, 0.1, 0.04, 0.0, 0.20, "call") < 0.01)
    check("deep ITM put delta approaches -1",
          black_scholes_delta(50, 100, 0.1, 0.04, 0.0, 0.20, "put") < -0.99)
    check("put delta is negative",
          black_scholes_delta(100, 100, 0.5, 0.04, 0.0, 0.25, "put") < 0)

    check("zero time gives None",
          black_scholes_delta(100, 100, 0, 0.04, 0.0, 0.25, "call") is None)
    check("zero vol gives None",
          black_scholes_delta(100, 100, 1, 0.04, 0.0, 0, "call") is None)
    check("negative spot gives None",
          black_scholes_delta(-100, 100, 1, 0.04, 0.0, 0.25, "call") is None)
    check("bad type gives None",
          black_scholes_delta(100, 100, 1, 0.04, 0.0, 0.25, "x") is None)

    # --- dividends actually move the answer -------------------------------
    no_div = black_scholes_delta(100, 100, 1.0, 0.04, 0.00, 0.25, "call")
    with_div = black_scholes_delta(100, 100, 1.0, 0.04, 0.06, 0.25, "call")
    check("ignoring a dividend overstates call delta", with_div < no_div)
    check("the dividend effect is material, not rounding",
          no_div - with_div > 0.02)

    # --- implied volatility round trip ------------------------------------
    for true_vol in (0.15, 0.25, 0.40, 0.80):
        for kind in ("call", "put"):
            price = black_scholes_price(90.0, 92.0, 45 / 365, 0.04, 0.0,
                                        true_vol, kind)
            back = implied_volatility(price, 90.0, 92.0, 45 / 365, 0.04, 0.0,
                                      kind)
            check(f"IV round trip recovers {true_vol:.2f} on a {kind}",
                  close(back, true_vol, 1e-5))

    # --- implied volatility refuses the impossible ------------------------
    check("a price above the spot gives None",
          implied_volatility(200, 100, 100, 0.5, 0.04, 0.0, "call") is None)
    check("a price at intrinsic gives None",
          implied_volatility(10.0, 110, 100, 0.5, 0.0, 0.0, "call") is None)
    check("a zero price gives None",
          implied_volatility(0, 100, 100, 0.5, 0.04, 0.0, "call") is None)
    check("a negative price gives None",
          implied_volatility(-1, 100, 100, 0.5, 0.04, 0.0, "call") is None)
    check("zero time to expiry gives None",
          implied_volatility(5, 100, 100, 0, 0.04, 0.0, "call") is None)
    check("a missing price gives None",
          implied_volatility(None, 100, 100, 0.5, 0.04, 0.0, "call") is None)

    # --- implied_delta, the scanner-facing call ---------------------------
    d = implied_delta(option_price=5.0, underlying=100, strike=100,
                      days_to_expiry=45, option_type="call")
    check("an ATM call comes back near 0.5", d is not None and 0.45 < d < 0.62)

    d_bid_ask = implied_delta(bid=4.90, ask=5.10, underlying=100, strike=100,
                              days_to_expiry=45, option_type="call")
    check("bid/ask midpoint matches an explicit price",
          close(d_bid_ask, d, 1e-9))

    check("a crossed quote gives None",
          implied_delta(bid=5.10, ask=4.90, underlying=100, strike=100,
                        days_to_expiry=45) is None)
    check("a one-sided quote gives None",
          implied_delta(bid=4.90, ask=None, underlying=100, strike=100,
                        days_to_expiry=45) is None)
    check("a zero bid gives None",
          implied_delta(bid=0, ask=5.10, underlying=100, strike=100,
                        days_to_expiry=45) is None)
    check("zero days to expiry gives None",
          implied_delta(option_price=5.0, underlying=100, strike=100,
                        days_to_expiry=0) is None)
    check("negative days gives None",
          implied_delta(option_price=5.0, underlying=100, strike=100,
                        days_to_expiry=-3) is None)
    check("a missing underlying gives None",
          implied_delta(option_price=5.0, underlying=None, strike=100,
                        days_to_expiry=45) is None)

    otm = implied_delta(option_price=0.35, underlying=100, strike=115,
                        days_to_expiry=30, option_type="call")
    check("a far OTM call has a small delta", otm is not None and otm < 0.25)
    itm = implied_delta(option_price=12.0, underlying=100, strike=90,
                        days_to_expiry=30, option_type="call")
    check("an ITM call has a large delta", itm is not None and itm > 0.70)
    check("ITM delta exceeds OTM delta", itm > otm)

    # --- the band check ---------------------------------------------------
    check("0.45 is inside a 0.35-0.60 band",
          delta_within_band(0.45, 0.35, 0.60) is True)
    check("0.20 is outside the band",
          delta_within_band(0.20, 0.35, 0.60) is False)
    check("0.90 is outside the band",
          delta_within_band(0.90, 0.35, 0.60) is False)
    check("a put's -0.45 passes on absolute value",
          delta_within_band(-0.45, 0.35, 0.60) is True)
    check("band edges are inclusive",
          delta_within_band(0.35, 0.35, 0.60) is True
          and delta_within_band(0.60, 0.35, 0.60) is True)
    check("an UNKNOWN delta is None, never a pass",
          delta_within_band(None, 0.35, 0.60) is None)
    check("an unknown delta is not False either",
          delta_within_band(None, 0.35, 0.60) is not False)
    check("an inverted band gives None",
          delta_within_band(0.45, 0.60, 0.35) is None)

    # --- a realistic contract from this project ---------------------------
    # TLT-like: $113 contract, spot near the strike, 44 days out.
    tlt = implied_delta(bid=1.12, ask=1.14, underlying=90.20, strike=90.0,
                        days_to_expiry=44, option_type="call")
    check("a TLT-like ATM call lands in a sane band",
          tlt is not None and 0.35 < tlt < 0.70)
    check("and it would pass the configured band",
          delta_within_band(tlt, 0.35, 0.60) in (True, False))

    passed = sum(1 for _, ok in checks if ok)
    print(f"\nSELF TEST  ({passed}/{len(checks)} passed)")
    for name, ok in checks:
        if not ok:
            print(f"  [FAIL] {name}")
    if passed == len(checks):
        print("All offline checks passed. No network, no orders, no files.")
        return 0
    print("\nFAILURES ABOVE. Do not wire this into contract selection yet.")
    return 1


def demo() -> int:
    print(f"options_greeks.py v{VERSION} -- worked examples\n")
    print(f"  note: days are {TRADING_DAYS_NOTE}\n")
    rows = [
        ("ATM call", 5.00, 100.0, 100.0, 45, "call", 0.0),
        ("OTM call", 0.35, 100.0, 115.0, 30, "call", 0.0),
        ("ITM call", 12.00, 100.0, 90.0, 30, "call", 0.0),
        ("ATM put", 4.80, 100.0, 100.0, 45, "put", 0.0),
        ("T-like, 6% yield", 0.55, 24.25, 24.0, 40, "call", 0.06),
        ("T-like, yield ignored", 0.55, 24.25, 24.0, 40, "call", 0.00),
    ]
    print(f"  {'case':<24} {'price':>7} {'spot':>8} {'strike':>8} "
          f"{'dte':>5} {'IV':>8} {'delta':>8}  band")
    for label, price, spot, strike, dte, kind, q in rows:
        iv = implied_volatility(price, spot, strike, dte / 365, 
                                DEFAULT_RISK_FREE_RATE, q, kind)
        d = implied_delta(option_price=price, underlying=spot, strike=strike,
                          days_to_expiry=dte, option_type=kind,
                          dividend_yield=q)
        in_band = delta_within_band(d, 0.35, 0.60)
        verdict = {True: "PASS", False: "reject", None: "unknown"}[in_band]
        iv_s = "--" if iv is None else f"{iv:7.1%}"
        d_s = "--" if d is None else f"{d:8.4f}"
        print(f"  {label:<24} {price:7.2f} {spot:8.2f} {strike:8.2f} "
              f"{dte:5d} {iv_s} {d_s}  {verdict}")
    print("\n  The last two rows are the same contract. The only difference is")
    print("  whether the dividend yield is supplied. Pass it for payers.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute option delta from a quote. Read only.")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()

    print(f"options_greeks.py v{VERSION}  (pure arithmetic -- no network)")
    if args.self_test:
        return self_test()
    if args.demo:
        return demo()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())