"""
options_contracts.py  --  LOCKBOT option contract selection  (v1.0)

WHAT THIS DOES
    Turns "I want to be long EWZ" into "buy this exact contract, at this
    price, for this reason" -- or into a clear refusal.

    It fetches the option chain for one underlying, filters it down to
    contracts LOCKBOT is willing to trade, and picks the best one.

WHAT IT DOES NOT DO
    It places no orders, holds no state, and writes no files. Everything
    here is a pure decision given a chain snapshot. options_scanner.py
    decides what to do with the answer.

THE GATES, AND WHY EACH ONE EXISTS
    spread        The single most expensive thing about small-account
                  options. You buy at the ask and can only sell at the
                  bid, so a 20% spread means the trade starts 20% down
                  and the signal has to overcome that before it makes a
                  cent. This is the gate that matters most.
    non-zero bid  A zero bid means nobody will buy it at any price. The
                  position cannot be exited. Never enter one.
    delta         How much the option moves per dollar of underlying.
                  Too low and the stock can move the whole predicted
                  distance while the option barely responds.
    DTE           Under ~3 weeks, theta decay accelerates sharply. The
                  window keeps LOCKBOT out of that zone at entry.
    affordability One contract is 100 shares of exposure and cannot be
                  subdivided. On a small account most contracts simply
                  cost more than the risk budget allows.

A NOTE ON GREEKS
    Alpaca returns greeks on the option chain snapshot, but they can be
    absent on the indicative feed. When delta is missing, LOCKBOT falls
    back to ranking by distance from at-the-money, which is what delta
    approximates anyway. The fallback is reported in the reason string
    so it is never silently invisible.

USAGE
    python options_contracts.py --self-test    offline logic check, no network
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


# ---------------------------------------------------------------------------
# Rejection reasons. Strings, not exceptions -- a rejected contract is a
# normal outcome, not an error, and every one of these gets logged.
# ---------------------------------------------------------------------------

OK = "OK"
NO_CHAIN = "NO_CHAIN"
NO_QUOTE = "NO_QUOTE"
ZERO_BID = "ZERO_BID"
SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
DELTA_OUT_OF_RANGE = "DELTA_OUT_OF_RANGE"
DTE_OUT_OF_RANGE = "DTE_OUT_OF_RANGE"
TOO_EXPENSIVE = "TOO_EXPENSIVE"
RISK_TOO_HIGH = "RISK_TOO_HIGH"
NO_SPREAD_PARTNER = "NO_SPREAD_PARTNER"

CONTRACT_MULTIPLIER = 100


# ---------------------------------------------------------------------------
# OCC symbol handling
#
# An option symbol looks like:  EWZ260821C00036000
#                               ^^^ ^^^^^^ ^^^^^^^^
#                               |   |      |
#                               |   |      strike x 1000, 8 digits
#                               |   expiry YYMMDD
#                               underlying root
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OccSymbol:
    """The parts of an OCC option symbol."""

    underlying: str
    expiration: date
    contract_type: str  # "call" or "put"
    strike: float


def parse_occ_symbol(symbol: str) -> OccSymbol:
    """Split an OCC option symbol into its parts."""

    text = symbol.strip().upper()

    if len(text) < 15:
        raise ValueError(f"{symbol!r} is too short to be an OCC symbol.")

    strike_text = text[-8:]
    type_letter = text[-9]
    expiry_text = text[-15:-9]
    underlying = text[:-15]

    if not underlying:
        raise ValueError(f"{symbol!r} has no underlying root.")

    if type_letter not in {"C", "P"}:
        raise ValueError(
            f"{symbol!r} has contract-type letter {type_letter!r}, expected C or P."
        )

    if not strike_text.isdigit() or not expiry_text.isdigit():
        raise ValueError(f"{symbol!r} is not a well-formed OCC symbol.")

    return OccSymbol(
        underlying=underlying,
        expiration=datetime.strptime(expiry_text, "%y%m%d").date(),
        contract_type="call" if type_letter == "C" else "put",
        strike=int(strike_text) / 1000.0,
    )


def build_occ_symbol(
    *,
    underlying: str,
    expiration: date,
    contract_type: str,
    strike: float,
) -> str:
    """Build an OCC option symbol. Used by the self-test and by tooling."""

    type_letter = "C" if contract_type.strip().lower() == "call" else "P"

    return (
        f"{underlying.strip().upper()}"
        f"{expiration.strftime('%y%m%d')}"
        f"{type_letter}"
        f"{int(round(strike * 1000)):08d}"
    )


# ---------------------------------------------------------------------------
# Quote evaluation
# ---------------------------------------------------------------------------

@dataclass
class ContractQuote:
    """One contract, priced and measured."""

    symbol: str
    underlying: str
    expiration: date
    contract_type: str
    strike: float
    bid: float
    ask: float
    delta: float | None
    implied_volatility: float | None
    days_to_expiration: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_percent(self) -> float:
        """Spread as a fraction of the mid price."""

        if self.mid <= 0:
            return float("inf")

        return self.spread / self.mid

    @property
    def cost_to_open(self) -> float:
        """Dollars to buy one contract at the ask."""

        return self.ask * CONTRACT_MULTIPLIER

    @property
    def moneyness(self) -> float:
        """Absolute distance from the strike, used when delta is missing."""

        return abs(self.strike)


def _as_float(value: Any) -> float | None:
    """Convert to float, returning None for missing or unusable values."""

    if value is None:
        return None

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    if result != result:  # NaN
        return None

    return result


def snapshot_to_quote(
    symbol: str,
    snapshot: Any,
    *,
    as_of: date,
) -> ContractQuote | None:
    """
    Convert one Alpaca chain snapshot into a ContractQuote.

    Returns None when the snapshot carries no usable two-sided quote,
    which is common outside market hours.
    """

    quote = getattr(snapshot, "latest_quote", None)

    if quote is None:
        return None

    bid = _as_float(getattr(quote, "bid_price", None))
    ask = _as_float(getattr(quote, "ask_price", None))

    if bid is None or ask is None:
        return None

    if ask <= 0 or ask < bid:
        # A crossed or empty book. Common when the market is closed.
        return None

    try:
        parts = parse_occ_symbol(symbol)
    except ValueError:
        return None

    greeks = getattr(snapshot, "greeks", None)
    delta = _as_float(getattr(greeks, "delta", None)) if greeks else None

    return ContractQuote(
        symbol=symbol,
        underlying=parts.underlying,
        expiration=parts.expiration,
        contract_type=parts.contract_type,
        strike=parts.strike,
        bid=bid,
        ask=ask,
        delta=delta,
        implied_volatility=_as_float(
            getattr(snapshot, "implied_volatility", None)
        ),
        days_to_expiration=(parts.expiration - as_of).days,
    )


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

@dataclass
class ContractVerdict:
    """Why one contract was accepted or refused."""

    quote: ContractQuote
    status: str
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.status == OK


def evaluate_contract(
    quote: ContractQuote,
    *,
    account_equity: float,
    max_spread_percent: float,
    min_dte: int,
    max_dte: int,
    delta_min: float,
    delta_max: float,
    max_premium_percent: float,
    max_risk_percent: float,
    stop_loss_percent: float,
    require_nonzero_bid: bool = True,
    underlying_price: float | None = None,
    max_moneyness_percent: float = 0.07,
) -> ContractVerdict:
    """
    Apply every entry gate to one contract.

    When delta is missing (it can be absent on the indicative feed), the
    delta gate is replaced by a distance-from-the-money gate rather than
    skipped. Skipping it outright let a deep in-the-money contract through
    during the first live dry run -- F at $15.28 with a $13.00 strike --
    purely because that one contract had no greeks attached. A missing
    measurement is not permission to stop checking.
    """

    if require_nonzero_bid and quote.bid <= 0:
        return ContractVerdict(
            quote,
            ZERO_BID,
            "No bid — the position could not be exited at any price.",
        )

    if not min_dte <= quote.days_to_expiration <= max_dte:
        return ContractVerdict(
            quote,
            DTE_OUT_OF_RANGE,
            f"{quote.days_to_expiration} days to expiration is outside "
            f"the {min_dte}-{max_dte} window.",
        )

    if quote.spread_percent > max_spread_percent:
        return ContractVerdict(
            quote,
            SPREAD_TOO_WIDE,
            f"Spread is {quote.spread_percent:.1%} of mid, above the "
            f"{max_spread_percent:.1%} ceiling. Entering costs more than "
            "the edge is worth.",
        )

    if quote.delta is not None:
        if not delta_min <= abs(quote.delta) <= delta_max:
            return ContractVerdict(
                quote,
                DELTA_OUT_OF_RANGE,
                f"Delta {abs(quote.delta):.2f} is outside the "
                f"{delta_min:.2f}-{delta_max:.2f} window.",
            )

    elif underlying_price and underlying_price > 0:
        distance = abs(quote.strike - underlying_price) / underlying_price

        if distance > max_moneyness_percent:
            return ContractVerdict(
                quote,
                DELTA_OUT_OF_RANGE,
                f"No delta available, and strike ${quote.strike:.2f} is "
                f"{distance:.1%} from ${underlying_price:.2f} — outside the "
                f"{max_moneyness_percent:.1%} fallback window.",
            )

    premium_cap = account_equity * max_premium_percent

    if quote.cost_to_open > premium_cap:
        return ContractVerdict(
            quote,
            TOO_EXPENSIVE,
            f"${quote.cost_to_open:.2f} to open exceeds the "
            f"${premium_cap:.2f} premium cap.",
        )

    # The gate that actually protects a small account: one contract is
    # indivisible, so if the stop-loss on this premium costs more than the
    # risk budget, the answer is not "buy fewer" — it is "do not buy".
    dollars_at_risk = quote.cost_to_open * stop_loss_percent
    risk_cap = account_equity * max_risk_percent

    if dollars_at_risk > risk_cap:
        return ContractVerdict(
            quote,
            RISK_TOO_HIGH,
            f"A {stop_loss_percent:.0%} stop on ${quote.cost_to_open:.2f} "
            f"risks ${dollars_at_risk:.2f}, above the ${risk_cap:.2f} "
            "per-trade ceiling. One contract cannot be subdivided.",
        )

    return ContractVerdict(quote, OK, "Passed every entry gate.")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _rank_key(quote: ContractQuote, underlying_price: float) -> tuple:
    """
    Rank accepted contracts. Prefer delta nearest the middle of the target
    window; without delta, prefer the strike nearest the money. Tighter
    spreads break ties, because the spread is paid for certain and the
    predicted move is not.
    """

    if quote.delta is not None:
        primary = abs(abs(quote.delta) - 0.50)
    else:
        primary = abs(quote.strike - underlying_price) / max(underlying_price, 0.01)

    return (primary, quote.spread_percent)


def rank_accepted(
    quotes: list[ContractQuote],
    *,
    underlying_price: float,
    contract_type: str,
    **gate_kwargs: Any,
) -> tuple[list[ContractQuote], list[ContractVerdict]]:
    """Return every contract that passed the gates, best first."""

    verdicts = [
        evaluate_contract(quote, underlying_price=underlying_price, **gate_kwargs)
        for quote in quotes
        if quote.contract_type == contract_type
    ]

    accepted = [verdict.quote for verdict in verdicts if verdict.accepted]
    accepted.sort(key=lambda quote: _rank_key(quote, underlying_price))

    return accepted, verdicts


def select_single_leg(
    quotes: list[ContractQuote],
    *,
    underlying_price: float,
    contract_type: str,
    **gate_kwargs: Any,
) -> tuple[ContractQuote | None, list[ContractVerdict]]:
    """
    Pick one contract to buy outright.

    Returns the winner (or None) plus every verdict, so the caller can log
    exactly why a symbol produced no trade.
    """

    accepted, verdicts = rank_accepted(
        quotes,
        underlying_price=underlying_price,
        contract_type=contract_type,
        **gate_kwargs,
    )

    return (accepted[0] if accepted else None), verdicts


def select_vertical_spread(
    quotes: list[ContractQuote],
    *,
    underlying_price: float,
    contract_type: str,
    width_strikes: int,
    max_spread_percent: float,
    account_equity: float,
    max_premium_percent: float,
    max_risk_percent: float,
    stop_loss_percent: float,
    **gate_kwargs: Any,
) -> tuple[tuple[ContractQuote, ContractQuote] | None, list[ContractVerdict]]:
    """
    Pick a debit vertical: buy the near strike, sell one further out.

    A debit spread costs less than the outright option and decays more
    slowly, because the short leg's decay partly offsets the long leg's.
    The trade-off is a capped payoff. On a small account that trade is
    usually worth making -- the capped upside is still larger than the
    account can risk on an outright.

    The long leg is chosen on its MERITS ONLY -- delta, spread, expiration
    -- with the money gates deliberately relaxed, because what a spread
    costs is the net debit and not the long leg's sticker price. A $205
    long leg paired against a $180 short leg is a $25 trade. Judging the
    long leg on affordability first would throw away exactly the spreads
    that make options reachable on a small account.
    """

    candidates, verdicts = rank_accepted(
        quotes,
        underlying_price=underlying_price,
        contract_type=contract_type,
        account_equity=account_equity,
        max_spread_percent=max_spread_percent,
        max_premium_percent=1.0,
        max_risk_percent=1.0,
        stop_loss_percent=stop_loss_percent,
        **gate_kwargs,
    )

    premium_cap = account_equity * max_premium_percent
    risk_cap = account_equity * max_risk_percent

    # Work down the ranked candidates rather than committing to the top
    # one. The best long leg by delta may be the highest strike in the
    # chain, which has nothing above it to sell against -- that is a
    # reason to try the next candidate, not a reason to give up.
    for long_leg in candidates:
        same_expiry = sorted(
            (
                quote
                for quote in quotes
                if quote.contract_type == contract_type
                and quote.expiration == long_leg.expiration
                and quote.bid > 0
                and quote.spread_percent <= max_spread_percent
            ),
            key=lambda quote: quote.strike,
        )

        strikes = [quote.strike for quote in same_expiry]

        if long_leg.strike not in strikes:
            continue

        long_index = strikes.index(long_leg.strike)

        # A call spread sells a HIGHER strike; a put spread sells a LOWER one.
        step = width_strikes if contract_type == "call" else -width_strikes
        short_index = long_index + step

        if not 0 <= short_index < len(same_expiry):
            continue

        short_leg = same_expiry[short_index]

        # Net debit: pay the ask on the long leg, receive the bid on the short.
        net_debit = (long_leg.ask - short_leg.bid) * CONTRACT_MULTIPLIER

        if net_debit <= 0:
            continue

        if net_debit > premium_cap:
            continue

        # Risk is measured the same way as for a single leg: what the
        # software stop is expected to cost. Worth being explicit that
        # this is the INTENDED loss, not the worst case -- a gap through
        # the stop can cost the entire debit, on a spread exactly as on
        # an outright.
        if net_debit * stop_loss_percent > risk_cap:
            continue

        return (long_leg, short_leg), verdicts

    return None, verdicts


# ---------------------------------------------------------------------------
# Chain fetching
# ---------------------------------------------------------------------------

def fetch_chain_quotes(
    data_client: Any,
    *,
    underlying_symbol: str,
    contract_type: str,
    underlying_price: float,
    min_dte: int,
    max_dte: int,
    as_of: date | None = None,
    strike_window_percent: float = 0.15,
) -> list[ContractQuote]:
    """
    Fetch and price the part of the chain LOCKBOT could actually trade.

    The strike and expiration filters are applied by the API rather than
    locally, because a full chain for a liquid name is thousands of
    contracts and almost all of them are irrelevant.
    """

    from alpaca.data.requests import OptionChainRequest

    reference_date = as_of or date.today()

    request = OptionChainRequest(
        underlying_symbol=underlying_symbol,
        type=contract_type,
        expiration_date_gte=reference_date + _days(min_dte),
        expiration_date_lte=reference_date + _days(max_dte),
        strike_price_gte=round(underlying_price * (1 - strike_window_percent), 2),
        strike_price_lte=round(underlying_price * (1 + strike_window_percent), 2),
    )

    chain = data_client.get_option_chain(request)

    quotes = []

    for symbol, snapshot in (chain or {}).items():
        quote = snapshot_to_quote(symbol, snapshot, as_of=reference_date)

        if quote is not None:
            quotes.append(quote)

    return quotes


def _days(count: int):
    from datetime import timedelta

    return timedelta(days=count)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    """Offline checks. No network, no credentials, no config needed."""

    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    print("OCC symbol parsing")

    parsed = parse_occ_symbol("EWZ260821C00036000")
    check("underlying", parsed.underlying == "EWZ", parsed.underlying)
    check("expiration", parsed.expiration == date(2026, 8, 21), str(parsed.expiration))
    check("type", parsed.contract_type == "call", parsed.contract_type)
    check("strike", parsed.strike == 36.0, str(parsed.strike))

    put = parse_occ_symbol("SPY260821P00450500")
    check("put type", put.contract_type == "put", put.contract_type)
    check("fractional strike", put.strike == 450.5, str(put.strike))

    rebuilt = build_occ_symbol(
        underlying="EWZ",
        expiration=date(2026, 8, 21),
        contract_type="call",
        strike=36.0,
    )
    check("round trip", rebuilt == "EWZ260821C00036000", rebuilt)

    for bad in ("", "XX", "EWZ260821X00036000"):
        try:
            parse_occ_symbol(bad)
            check(f"rejects {bad!r}", False)
        except ValueError:
            check(f"rejects {bad!r}", True)

    print()
    print("Gates")

    today = date(2026, 7, 30)

    def make(strike, bid, ask, delta=0.50, dte=30):
        return ContractQuote(
            symbol=build_occ_symbol(
                underlying="TEST",
                expiration=today + _days(dte),
                contract_type="call",
                strike=strike,
            ),
            underlying="TEST",
            expiration=today + _days(dte),
            contract_type="call",
            strike=strike,
            bid=bid,
            ask=ask,
            delta=delta,
            implied_volatility=0.30,
            days_to_expiration=dte,
        )

    gates = dict(
        account_equity=250.0,
        max_spread_percent=0.10,
        min_dte=21,
        max_dte=45,
        delta_min=0.35,
        delta_max=0.60,
        max_premium_percent=0.30,
        max_risk_percent=0.10,
        stop_loss_percent=0.35,
    )

    good = make(36.0, 0.66, 0.70)
    check("accepts a good contract", evaluate_contract(good, **gates).accepted)

    check(
        "spread percent maths",
        abs(good.spread_percent - (0.04 / 0.68)) < 1e-9,
        f"{good.spread_percent}",
    )

    wide = make(36.0, 0.18, 0.32)
    check(
        "rejects a wide spread",
        evaluate_contract(wide, **gates).status == SPREAD_TOO_WIDE,
    )

    no_bid = make(36.0, 0.0, 1.28)
    check("rejects a zero bid", evaluate_contract(no_bid, **gates).status == ZERO_BID)

    # $3.81 x 100 = $381, far above the $75 premium cap. Tight spread so
    # that the cost gate is what fires, not the spread gate.
    expensive = make(36.0, 3.80, 3.81)
    check(
        "rejects an unaffordable contract",
        evaluate_contract(expensive, **gates).status == TOO_EXPENSIVE,
        evaluate_contract(expensive, **gates).status,
    )

    # $0.73 x 100 = $73, which is under the $75 premium cap. But a 35%
    # stop on it risks $25.55, just over the $25 per-trade ceiling — so
    # the risk gate is the one that has to catch this.
    risky = make(36.0, 0.72, 0.73)
    check(
        "risk ceiling binds before premium ceiling",
        evaluate_contract(risky, **gates).status == RISK_TOO_HIGH,
        evaluate_contract(risky, **gates).status,
    )

    far_dated = make(36.0, 0.66, 0.70, dte=90)
    check(
        "rejects a far expiration",
        evaluate_contract(far_dated, **gates).status == DTE_OUT_OF_RANGE,
    )

    low_delta = make(36.0, 0.66, 0.70, delta=0.12)
    check(
        "rejects a low delta",
        evaluate_contract(low_delta, **gates).status == DELTA_OUT_OF_RANGE,
    )

    missing_delta = make(36.0, 0.66, 0.70, delta=None)
    check(
        "accepts a missing delta near the money",
        evaluate_contract(missing_delta, underlying_price=36.0, **gates).accepted,
    )

    # The gap the first live dry run exposed: with no delta and no
    # underlying price, a deep in-the-money strike used to sail through.
    deep_itm = make(13.0, 0.66, 0.70, delta=None)
    check(
        "rejects a deep ITM strike when delta is missing",
        evaluate_contract(deep_itm, underlying_price=15.28, **gates).status
        == DELTA_OUT_OF_RANGE,
        evaluate_contract(deep_itm, underlying_price=15.28, **gates).status,
    )

    print()
    print("Selection")

    chain = [
        make(34.0, 0.66, 0.70, delta=0.72),
        make(36.0, 0.60, 0.64, delta=0.51),
        make(38.0, 0.30, 0.32, delta=0.28),
    ]

    winner, verdicts = select_single_leg(
        chain,
        underlying_price=36.0,
        contract_type="call",
        **gates,
    )

    check("picks the near-ATM strike", winner is not None and winner.strike == 36.0,
          str(winner.strike if winner else None))
    check("reports every verdict", len(verdicts) == 3, str(len(verdicts)))

    spread_chain = [
        make(36.0, 1.00, 1.04, delta=0.51),
        make(37.0, 0.60, 0.63, delta=0.40),
    ]

    pair, _ = select_vertical_spread(
        spread_chain,
        underlying_price=36.0,
        contract_type="call",
        width_strikes=1,
        min_dte=21,
        max_dte=45,
        delta_min=0.35,
        delta_max=0.60,
        stop_loss_percent=0.35,
        max_spread_percent=0.10,
        account_equity=250.0,
        max_premium_percent=0.30,
        max_risk_percent=0.10,
    )

    check("builds a call spread", pair is not None)

    if pair is not None:
        long_leg, short_leg = pair
        check("buys the lower strike", long_leg.strike == 36.0, str(long_leg.strike))
        check("sells the higher strike", short_leg.strike == 37.0, str(short_leg.strike))

        net_debit = (long_leg.ask - short_leg.bid) * CONTRACT_MULTIPLIER
        check("net debit is cheaper than the outright",
              net_debit < long_leg.cost_to_open,
              f"{net_debit} vs {long_leg.cost_to_open}")

    # A spread whose long leg is individually unaffordable should still be
    # buildable, because the short leg pays for most of it.
    pricey_chain = [
        make(36.0, 2.00, 2.05, delta=0.55),
        make(37.0, 1.80, 1.85, delta=0.45),
    ]

    pricey_pair, _ = select_vertical_spread(
        pricey_chain,
        underlying_price=36.0,
        contract_type="call",
        width_strikes=1,
        min_dte=21,
        max_dte=45,
        delta_min=0.35,
        delta_max=0.60,
        stop_loss_percent=0.35,
        max_spread_percent=0.10,
        account_equity=250.0,
        max_premium_percent=0.30,
        max_risk_percent=0.10,
    )

    check(
        "spread rescues an unaffordable long leg",
        pricey_pair is not None,
    )

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All contract-selection checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(__doc__)
    print("Run with --self-test for the offline logic check.")
