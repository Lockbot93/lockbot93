"""
event_risk.py — is the market pricing a known event before this expires?

WHY THIS EXISTS

Every gate LOCKBOT applies to a contract asks about the contract: is the
spread payable, is the delta right, is the premium affordable, is the
implied volatility fair against realised. Not one of them asks what is
scheduled to HAPPEN to the underlying while the position is held.

That gap has a name. Buying an option into an earnings announcement is
the most reliable way to be right about direction and lose money anyway:
implied volatility inflates ahead of the report and collapses the moment
it lands. The stock moves 4% in your favour, the option loses a third of
its value, and nothing in the trade log explains why. LOCKBOT could not
see it coming, because it had no idea the company reported at all.

WHY NOT AN EARNINGS CALENDAR

That was the obvious fix and it was checked first. Alpaca does not carry
earnings dates — its corporate actions product covers splits, dividends
and mergers only. The alternatives were an unofficial scraper or a paid
feed, and hanging a risk gate on a rate-limited library that breaks
without warning is worse than not having the gate.

WHAT THIS DOES INSTEAD

It reads the answer out of the option prices, which are already flowing.

Implied volatility normally RISES with time to expiry: more time, more
that can happen. When near-dated IV exceeds far-dated, that ordering is
inverted, and an inverted term structure means the market is pricing
something it knows about before the near expiry. Earnings is the usual
cause. It is not the only one — FDA decisions, court rulings, index
rebalances and Fed meetings all show up the same way, and an earnings
calendar would have missed every one of them.

So this measures the thing that actually matters (an event is priced in)
rather than a proxy for it (a date on a calendar).

THE MISTAKE THIS MODULE IS BUILT TO AVOID

The first version of this check compared whatever contract came back for
each expiry and called IBIT's structure inverted. It was not. It had
compared a 37.00 strike against a 45.00 strike, and far out-of-the-money
options carry higher IV from volatility SKEW — a permanent feature of
every chain, present whether or not anything is scheduled.

Skew is a function of strike. Term structure is a function of expiry. To
read one you must hold the other still, so every comparison below is
between the nearest-the-money strike at each expiry, and a comparison
whose strikes do not line up closely enough returns UNKNOWN rather than
a number. Most of this file is that discipline.

SCOPE

This assumes LOCKBOT is BUYING premium, which it is — long calls, long
puts and debit verticals are all hurt by a volatility collapse. A
premium seller wants exactly the inversion this module refuses. If
LOCKBOT ever sells premium, this gate must be inverted with it, not
merely disabled.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# Below this the term structure is normal: the far month costs more, as
# it should. Above 1.0 the ordering is inverted.
NORMAL_SLOPE = 1.0

# How close to expiry the near leg may sit.
#
# An option in its final days carries structurally inflated implied
# volatility -- pin risk, and the annualisation of moves that are small
# in absolute terms. That inflation is permanent and has nothing to do
# with anything being scheduled, so a near leg taken from the expiry
# week reads it as an event.
#
# This is not a guess. Sixteen live symbols, 2026-08-04, verdict as the
# near leg is pushed out:
#
#     near leg      1d     5d     7d    10d    14d
#     blocked        7      5      5      5      2
#
#     F           1.40   0.88   0.88   0.88   1.01   <- artifact
#     XLU         1.56   1.07   1.07   1.07   1.10   <- artifact
#     BAC         1.12   1.08   1.08   1.08   1.03   <- artifact
#     PLTR        1.62   1.19   1.19   1.19   1.12   <- persists
#     WBD         2.94   5.63   5.63   5.63   2.19   <- persists
#
# At 1d the check flags Ford and a utilities ETF, neither of which had
# anything scheduled. From 5d to 10d the answers are identical -- a
# stable plateau -- and what survives it are the names that genuinely
# had events. By 14d the near leg has been pushed past most events and
# the signal washes out.
#
# 7 sits in the middle of the plateau and inside any holding period
# LOCKBOT takes.
MIN_NEAR_DTE = 7


@dataclass
class TermStructure:
    """Implied volatility across expiries, at constant moneyness."""

    near_dte: int
    far_dte: int
    near_iv: float
    far_iv: float
    near_strike: float
    far_strike: float
    slope: float               # near / far. Above 1.0 is inverted.
    usable: bool
    note: str


@dataclass
class EventRisk:
    """Whether something is scheduled inside the holding period."""

    verdict: str               # CLEAR | ELEVATED | EVENT_LIKELY | UNKNOWN
    slope: float
    structure: TermStructure | None
    reasons: list[str] = field(default_factory=list)

    @property
    def blocks_entry(self) -> bool:
        """Only a positive reading blocks. UNKNOWN is not evidence."""

        return self.verdict == "EVENT_LIKELY"

    @property
    def clear(self) -> bool:
        return self.verdict == "CLEAR"


def _iv_of(quote: Any) -> float:
    """Implied volatility as a positive float, or 0.0 when unusable."""

    raw = getattr(quote, "implied_volatility", None)

    if raw is None:
        return 0.0

    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0

    if value != value or value <= 0:     # NaN or nonsense
        return 0.0

    return value


def nearest_the_money(quotes: list[Any], underlying_price: float) -> Any | None:
    """The quote whose strike sits closest to spot, with a usable IV.

    This is the whole defence against reading skew as term structure.
    Picking "some contract at this expiry" is what produced the false
    IBIT reading described in the module docstring.
    """

    usable = [q for q in quotes if _iv_of(q) > 0]

    if not usable or underlying_price <= 0:
        return None

    return min(usable, key=lambda q: abs(float(q.strike) - underlying_price))


def term_structure(
    quotes: list[Any],
    *,
    underlying_price: float,
    min_expiry_gap_days: int = 10,
    strike_tolerance_percent: float = 0.02,
) -> TermStructure:
    """Compare near-dated against far-dated IV at matched moneyness.

    Returns a TermStructure whose `usable` flag says whether the slope
    means anything. An unusable reading is not a neutral one — callers
    must not treat it as evidence the structure is normal.
    """

    empty = TermStructure(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, False, "")

    if underlying_price <= 0:
        empty.note = "no underlying price"
        return empty

    by_expiry: dict[date, list[Any]] = {}

    for quote in quotes:
        expiry = getattr(quote, "expiration", None)

        if expiry is not None and _iv_of(quote) > 0:
            by_expiry.setdefault(expiry, []).append(quote)

    if len(by_expiry) < 2:
        empty.note = (
            f"only {len(by_expiry)} expiry with implied volatility; "
            "a term structure needs two"
        )
        return empty

    expiries = sorted(by_expiry)
    near_pool = by_expiry[expiries[0]]
    far_pool = by_expiry[expiries[-1]]

    # Prefer the SAME strike at both expiries. Skew is a function of
    # strike, so holding it exactly constant removes the contamination
    # rather than merely bounding it, and most chains carry a common
    # strike near the money. The nearest-the-money fallback below is for
    # the ones that do not.
    #
    # This matters more than it sounds. With a tolerance-only rule, five
    # of sixteen live symbols were refused as unreadable purely because
    # strike increments are coarse relative to price -- PCG's 17.50 and
    # 17.00 are 2.9% of a $17 stock. Every one of them has a shared
    # strike available.
    common = (
        {float(q.strike) for q in near_pool}
        & {float(q.strike) for q in far_pool}
    )

    if common:
        strike = min(common, key=lambda s: abs(s - underlying_price))
        near = min(near_pool, key=lambda q: abs(float(q.strike) - strike))
        far = min(far_pool, key=lambda q: abs(float(q.strike) - strike))
    else:
        near = nearest_the_money(near_pool, underlying_price)
        far = nearest_the_money(far_pool, underlying_price)

    if near is None or far is None:
        empty.note = "no at-the-money contract with implied volatility"
        return empty

    near_dte = int(getattr(near, "days_to_expiration", 0))
    far_dte = int(getattr(far, "days_to_expiration", 0))
    near_iv, far_iv = _iv_of(near), _iv_of(far)
    near_strike, far_strike = float(near.strike), float(far.strike)

    reading = TermStructure(
        near_dte=near_dte,
        far_dte=far_dte,
        near_iv=round(near_iv, 4),
        far_iv=round(far_iv, 4),
        near_strike=near_strike,
        far_strike=far_strike,
        slope=round(near_iv / far_iv, 3) if far_iv > 0 else 0.0,
        usable=False,
        note="",
    )

    # Two expiries a few days apart carry the same information. The
    # comparison only says something across a real gap in time.
    if far_dte - near_dte < min_expiry_gap_days:
        reading.note = (
            f"expiries {near_dte}d and {far_dte}d are {far_dte - near_dte} "
            f"days apart, under the {min_expiry_gap_days} needed"
        )
        return reading

    # The guard that makes this a term-structure reading rather than a
    # skew reading.
    drift = abs(near_strike - far_strike) / underlying_price

    if drift > strike_tolerance_percent:
        reading.note = (
            f"strikes {near_strike:.2f} and {far_strike:.2f} differ by "
            f"{drift:.1%} of spot — that measures skew, not term structure"
        )
        return reading

    reading.usable = True
    reading.note = (
        f"IV {near_iv:.0%} at {near_dte}d against {far_iv:.0%} at "
        f"{far_dte}d, strike {near_strike:.2f}"
    )

    return reading


def assess_event_risk(
    quotes: list[Any],
    *,
    underlying_price: float,
    max_inversion: float = 1.10,
    min_expiry_gap_days: int = 10,
    strike_tolerance_percent: float = 0.02,
) -> EventRisk:
    """Judge whether an event is priced in before the near expiry.

    UNKNOWN never means CLEAR. A missing reading is the absence of
    information, and the one place this project has been bitten hardest
    was reading a missing value as permission (see day_trade_tracker.py).
    Whether UNKNOWN should also BLOCK is a separate decision, left to the
    caller through `blocks_entry` — see the note there.
    """

    reading = term_structure(
        quotes,
        underlying_price=underlying_price,
        min_expiry_gap_days=min_expiry_gap_days,
        strike_tolerance_percent=strike_tolerance_percent,
    )

    if not reading.usable:
        return EventRisk(
            verdict="UNKNOWN",
            slope=0.0,
            structure=reading,
            reasons=[reading.note or "term structure unreadable"],
        )

    slope = reading.slope

    if slope > max_inversion:
        return EventRisk(
            verdict="EVENT_LIKELY",
            slope=slope,
            structure=reading,
            reasons=[
                f"near-dated IV is {slope:.2f}x the far-dated "
                f"({reading.near_iv:.0%} at {reading.near_dte}d vs "
                f"{reading.far_iv:.0%} at {reading.far_dte}d) — the market "
                f"is pricing an event before {reading.near_dte}d, and "
                "buying premium into one loses to the volatility crush"
            ],
        )

    if slope > NORMAL_SLOPE:
        return EventRisk(
            verdict="ELEVATED",
            slope=slope,
            structure=reading,
            reasons=[
                f"term structure mildly inverted at {slope:.2f}x, under "
                f"the {max_inversion:.2f}x threshold — worth noting, not "
                "enough to refuse"
            ],
        )

    return EventRisk(
        verdict="CLEAR",
        slope=slope,
        structure=reading,
        reasons=[
            f"term structure normal at {slope:.2f}x ({reading.note})"
        ],
    )


def measure_event_risk(
    data_client: Any,
    *,
    underlying_symbol: str,
    underlying_price: float,
    contract_type: str,
    max_dte: int,
    max_inversion: float = 1.10,
) -> EventRisk:
    """Fetch the chain wide enough to read a term structure, then judge it.

    The trading window is deliberately not reused here. LOCKBOT buys 21
    to 45 days out, and an earnings report ten days from now sits inside
    that holding period while being invisible to a chain that starts at
    21. The near end has to reach closer to today than anything LOCKBOT
    would actually buy, or the check cannot see the event it exists for.

    It does NOT reach all the way to tomorrow, though: see MIN_NEAR_DTE
    for what happens when it does.

    A failure to fetch yields UNKNOWN rather than raising. This runs in
    the entry path, and a data outage must not stop the scanner.
    """

    try:
        from options_contracts import fetch_chain_quotes

        quotes = fetch_chain_quotes(
            data_client,
            underlying_symbol=underlying_symbol,
            contract_type=contract_type,
            underlying_price=underlying_price,
            min_dte=MIN_NEAR_DTE,
            max_dte=max(max_dte, 45),
            strike_window_percent=0.06,
        )

    except Exception as error:
        return EventRisk(
            verdict="UNKNOWN",
            slope=0.0,
            structure=None,
            reasons=[f"chain unavailable: {type(error).__name__}: {error}"],
        )

    return assess_event_risk(
        quotes,
        underlying_price=underlying_price,
        max_inversion=max_inversion,
    )


def explain(risk: EventRisk) -> str:
    """One readable line, for a log or a spoken answer."""

    return f"{risk.verdict}: " + "; ".join(risk.reasons)


def _self_test() -> int:
    """Offline checks. No network, no credentials, no config needed."""

    from datetime import timedelta

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    @dataclass
    class Q:
        strike: float
        implied_volatility: float | None
        days_to_expiration: int
        expiration: date

    today = date(2026, 8, 4)

    def quote(strike, iv, dte):
        return Q(strike, iv, dte, today + timedelta(days=dte))

    print("Picking the contract that holds moneyness still")

    chain = [quote(37.0, 0.32, 10), quote(40.0, 0.36, 10), quote(45.0, 0.44, 10)]
    picked = nearest_the_money(chain, 40.0)
    check("the nearest-the-money strike is chosen", picked.strike == 40.0,
          str(picked.strike))
    check("not the highest-IV one", picked.implied_volatility == 0.36)
    check("no usable IV yields nothing",
          nearest_the_money([quote(40.0, None, 10)], 40.0) is None)
    check("a zero price yields nothing", nearest_the_money(chain, 0.0) is None)

    print()
    print("A normal term structure")

    normal = [quote(40.0, 0.30, 10), quote(40.0, 0.38, 45)]
    reading = term_structure(normal, underlying_price=40.0)
    check("it is usable", reading.usable is True, reading.note)
    check("and slopes upward", reading.slope < 1.0, str(reading.slope))

    risk = assess_event_risk(normal, underlying_price=40.0)
    check("the verdict is CLEAR", risk.verdict == "CLEAR", explain(risk))
    check("and it does not block", risk.blocks_entry is False)

    print()
    print("An inverted one — the case this module exists for")

    # Front month at 70%, back month at 35%: a classic pre-earnings shape.
    inverted = [quote(40.0, 0.70, 10), quote(40.0, 0.35, 45)]
    risk = assess_event_risk(inverted, underlying_price=40.0)
    check("the verdict is EVENT_LIKELY", risk.verdict == "EVENT_LIKELY",
          explain(risk))
    check("and it blocks the entry", risk.blocks_entry is True)
    check("the slope is reported", abs(risk.slope - 2.0) < 0.01, str(risk.slope))
    check("and the reason explains the loss mechanism",
          "volatility crush" in explain(risk), explain(risk))

    mild = [quote(40.0, 0.42, 10), quote(40.0, 0.40, 45)]
    risk = assess_event_risk(mild, underlying_price=40.0)
    check("a mild inversion is ELEVATED, not blocked",
          risk.verdict == "ELEVATED" and risk.blocks_entry is False,
          explain(risk))

    print()
    print("Skew must not be read as term structure")

    # The real IBIT chain: a 37.00 strike near, a 45.00 strike far. The
    # far one has higher IV purely because it is further out of the
    # money. Reading this as a term structure is the bug this guards.
    skewed = [quote(37.0, 0.326, 10), quote(45.0, 0.443, 27)]
    reading = term_structure(skewed, underlying_price=38.0)
    check("mismatched strikes are refused", reading.usable is False,
          reading.note)
    check("and the note says why", "skew" in reading.note, reading.note)

    risk = assess_event_risk(skewed, underlying_price=38.0)
    check("which makes the verdict UNKNOWN", risk.verdict == "UNKNOWN",
          explain(risk))
    check("and UNKNOWN does not block", risk.blocks_entry is False)
    check("but UNKNOWN is also not CLEAR", risk.clear is False)

    # The same strikes, close enough together, must still work.
    matched = [quote(37.5, 0.70, 10), quote(38.0, 0.35, 45)]
    reading = term_structure(matched, underlying_price=38.0)
    check("strikes within tolerance are accepted", reading.usable is True,
          reading.note)

    print()
    print("A strike shared by both expiries beats a closer unshared one")

    # PCG's real shape: 17.50 and 17.00 exist near, only 17.00 far. The
    # tolerance rule refused this at 2.9% of a $17.43 spot. Matching on
    # the shared strike reads it exactly instead of not at all.
    shared = [
        quote(17.5, 0.50, 10), quote(17.0, 0.52, 10),
        quote(17.0, 0.48, 45),
    ]
    reading = term_structure(shared, underlying_price=17.43)
    check("the shared strike is used", reading.usable is True, reading.note)
    check("on both legs", reading.near_strike == reading.far_strike == 17.0,
          f"{reading.near_strike} vs {reading.far_strike}")
    check("even though 17.50 is nearer spot",
          abs(17.5 - 17.43) < abs(17.0 - 17.43))
    check("and the slope compares like with like",
          abs(reading.slope - 0.52 / 0.48) < 0.01, str(reading.slope))

    # With no shared strike the tolerance rule must still apply.
    unshared = [quote(37.0, 0.33, 10), quote(45.0, 0.44, 45)]
    check("no shared strike falls back to the tolerance guard",
          term_structure(unshared, underlying_price=38.0).usable is False)

    print()
    print("The near leg is kept off the expiry week")

    check("MIN_NEAR_DTE clears the terminal-expiry distortion",
          MIN_NEAR_DTE >= 5, str(MIN_NEAR_DTE))
    check("while staying inside a holding period", MIN_NEAR_DTE <= 14,
          str(MIN_NEAR_DTE))

    print()
    print("Readings that cannot mean anything")

    check("one expiry is not a term structure",
          term_structure([quote(40.0, 0.30, 10)],
                         underlying_price=40.0).usable is False)

    close = [quote(40.0, 0.60, 10), quote(40.0, 0.30, 14)]
    reading = term_structure(close, underlying_price=40.0)
    check("expiries 4 days apart are refused", reading.usable is False,
          reading.note)
    check("even when the slope looks dramatic", reading.slope > 1.5,
          str(reading.slope))

    check("an empty chain is UNKNOWN",
          assess_event_risk([], underlying_price=40.0).verdict == "UNKNOWN")
    check("a missing price is UNKNOWN",
          assess_event_risk(normal, underlying_price=0.0).verdict == "UNKNOWN")

    junk = [quote(40.0, float("nan"), 10), quote(40.0, -1.0, 45)]
    check("NaN and negative IV are discarded",
          assess_event_risk(junk, underlying_price=40.0).verdict == "UNKNOWN")

    print()
    print("The threshold is honoured")

    borderline = [quote(40.0, 0.44, 10), quote(40.0, 0.40, 45)]
    check("1.10x is not blocked at a 1.10 threshold",
          assess_event_risk(borderline, underlying_price=40.0,
                            max_inversion=1.10).blocks_entry is False)
    check("but is blocked at a 1.05 threshold",
          assess_event_risk(borderline, underlying_price=40.0,
                            max_inversion=1.05).blocks_entry is True)

    print()
    print("Only a positive reading ever blocks")

    for verdict in ("CLEAR", "ELEVATED", "UNKNOWN"):
        check(f"{verdict} does not block",
              EventRisk(verdict, 0.0, None).blocks_entry is False)

    check("EVENT_LIKELY does block",
          EventRisk("EVENT_LIKELY", 2.0, None).blocks_entry is True)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All event-risk checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(__doc__)
