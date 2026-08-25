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

import options_greeks


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

# A spread whose NET debit exceeds the per-trade risk cap.
#
# Deliberately distinct from RISK_TOO_HIGH, which is the single-leg
# full-debit refusal, so the two can be counted separately. They express
# the same rule on different instruments, and how often each binds is the
# evidence for whether OPTIONS_MAX_RISK_PER_TRADE_PERCENT is set right.
DEBIT_EXCEEDS_RISK_CAP = "DEBIT_EXCEEDS_RISK_CAP"

CONTRACT_MULTIPLIER = 100


# ---------------------------------------------------------------------------
# The debit ceiling -- ONE authority, because it was bypassed twice
# ---------------------------------------------------------------------------

def debit_ceiling(
    account_equity: float,
    *,
    max_risk_percent: float,
    max_debit_percent: float | None = None,
) -> float:
    """The most premium one position may commit, in dollars.

    Defaults to the per-trade risk limit so the orphaned worst case equals
    risk already accepted.
    """

    return account_equity * (
        max_risk_percent if max_debit_percent is None else max_debit_percent
    )


def debit_within_ceiling(
    debit: float,
    account_equity: float,
    *,
    max_risk_percent: float,
    max_debit_percent: float | None = None,
) -> tuple[bool, float, str]:
    """Does this debit fit under the ceiling? Returns (ok, ceiling, why).

    THE ONLY PLACE THE RULE LIVES, and it exists because the same rule was
    written three times and disagreed with itself twice.

      2026-08-13  evaluate_contract gained a full-debit cap. Single legs
                  only.
      2026-08-17  select_vertical_spread was still testing
                  `net_debit * stop_loss_percent`, roughly 2.9x looser, so
                  every spread LOCKBOT held had entered through it. Fixed.
      2026-08-19  options_scanner was found re-checking the BUFFERED debit
                  -- the number actually submitted -- as `debit * 0.35`
                  again. So a spread could clear selection at the ceiling,
                  gain the 3% entry buffer, and be submitted above it.

    Three call sites, three chances to measure the wrong thing, and two of
    them did. The rule now has one home and the callers ask it.

    Why the debit and never the stop distance: Alpaca has no stop order
    type for options, so options_manager IS the stop and nothing rests at
    the broker. If it stops running the position loses the entire debit.
    A ceiling measured against a software-only stop measures something
    nobody will enforce -- and the controller was down nine hours over
    2026-08-15/16, so that is a description of what happens, not a worry.
    """

    ceiling = debit_ceiling(
        account_equity,
        max_risk_percent=max_risk_percent,
        max_debit_percent=max_debit_percent,
    )

    if debit <= ceiling:
        return True, ceiling, ""

    return (
        False,
        ceiling,
        f"${debit:.2f} of premium exceeds the ${ceiling:.2f} per-trade "
        f"ceiling. Max loss is the full debit: Alpaca has no stop order "
        f"type for options, so options_manager is the only stop and "
        f"nothing rests at the broker to cap this.",
    )


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

    # Where the two greeks above came from. "feed" is the exchange's own
    # number; "model" is Black-Scholes solved from this contract's own
    # quote by options_greeks. They are NOT the same thing and must never
    # be pooled in a measurement without being able to split them again --
    # a modelled delta assumes European exercise and no dividend, and is
    # good enough for a 0.20-0.60 screen but not for anything finer.
    delta_source: str = "none"
    iv_source: str = "none"

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
    underlying_price: float | None = None,
) -> ContractQuote | None:
    """
    Convert one Alpaca chain snapshot into a ContractQuote.

    Returns None when the snapshot carries no usable two-sided quote,
    which is common outside market hours.

    WHY underlying_price IS HERE (added 2026-08-24)

        Alpaca's indicative feed does not reliably carry greeks. Measured
        over 350 contracts across seven names on 2026-08-24:

            delta missing        188 of 350   54%
            IV present           162 of 350   46%

        So the delta gate -- the rule that decides WHICH contract gets
        bought -- could not run on more than half the chain. It fell
        through to a distance-from-the-money proxy, which is a different
        rule wearing the same name.

        options_greeks solves both from the contract's own quote. It was
        written for exactly this on 2026-08-13 and then sat unreachable
        for eleven days because the file was saved as "options greeks.py"
        -- a space, which Python cannot import. Nothing referenced it and
        nothing could.

        Given a spot price the gate now runs on every contract with a
        two-sided quote. Without one the behaviour is unchanged, so this
        can only widen coverage, never narrow it.
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
    iv = _as_float(getattr(snapshot, "implied_volatility", None))

    if iv is None and greeks is not None:
        # Belt and braces: the field has been seen on the snapshot only,
        # never on greeks, but reading both costs nothing and a feed
        # change that moved it would otherwise look like missing data.
        iv = _as_float(getattr(greeks, "implied_volatility", None))

    delta_source = "feed" if delta is not None else "none"
    iv_source = "feed" if iv is not None else "none"

    days_to_expiration = (parts.expiration - as_of).days

    if underlying_price and underlying_price > 0 and days_to_expiration > 0:
        if delta is None:
            modelled = options_greeks.implied_delta(
                bid=bid, ask=ask,
                underlying=underlying_price,
                strike=parts.strike,
                days_to_expiry=days_to_expiration,
                option_type=parts.contract_type,
            )

            if modelled is not None:
                delta, delta_source = modelled, "model"

        if iv is None:
            modelled_iv = options_greeks.implied_volatility(
                0.5 * (bid + ask), underlying_price, parts.strike,
                days_to_expiration / 365.0,
                options_greeks.DEFAULT_RISK_FREE_RATE, 0.0,
                parts.contract_type,
            )

            if modelled_iv is not None:
                iv, iv_source = modelled_iv, "model"

    return ContractQuote(
        symbol=symbol,
        underlying=parts.underlying,
        expiration=parts.expiration,
        contract_type=parts.contract_type,
        strike=parts.strike,
        bid=bid,
        ask=ask,
        delta=delta,
        implied_volatility=iv,
        days_to_expiration=days_to_expiration,
        delta_source=delta_source,
        iv_source=iv_source,
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
    max_debit_percent: float | None = None,
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

    # THE FULL DEBIT IS THE REAL WORST CASE, NOT THE STOP DISTANCE.
    #
    # The check above sizes against a 35% stop, which assumes the stop can
    # be executed. For options that assumption rests entirely on
    # options_manager staying alive: Alpaca offers no bracket, no stop
    # order type and no GTC on options, so there is nothing resting at the
    # broker that fires on a loss. If that process dies, the position runs
    # to expiry and the loss is the WHOLE premium.
    #
    # Sizing on the stop alone therefore authorises far more than the
    # accepted risk. Measured at $650 equity on 2026-08-12: the stop rule
    # admits a $185.71 debit, which is 28.6% of the account per position
    # and 85.7% across three slots, all of it undefended if the manager
    # stops.
    #
    # Capping the FULL debit at the same per-trade limit makes the
    # orphaned worst case equal to risk already accepted -- $65.00 here,
    # 10.0% per position and 30.0% across three. It is a tightening: every
    # contract this rejects was already reachable under the old rule.
    # Defaults to the per-trade risk limit, so the orphaned worst case
    # equals risk already accepted. Kept as its own parameter rather than
    # reusing max_risk_percent silently, so the rule is visible at the
    # call site and testable on its own.
    fits, _ceiling, why = debit_within_ceiling(
        quote.cost_to_open,
        account_equity,
        max_risk_percent=max_risk_percent,
        max_debit_percent=max_debit_percent,
    )

    if not fits:
        return ContractVerdict(quote, RISK_TOO_HIGH, why)

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

        # The NET debit is the max loss, and the max loss is what gets
        # capped.
        #
        # This tested `net_debit * stop_loss_percent > risk_cap` until
        # 2026-08-17 -- the INTENDED loss if the software stop fires, not
        # the worst case. At a 35% stop that is 2.9x looser than the debit,
        # and it admitted two $155 spreads against a $59 cap on an account
        # of $587: 24% of equity each, on a rule that reads as 10%.
        #
        # The old comment claimed this matched the single-leg path. That
        # stopped being true on 2026-08-14, when evaluate_contract gained a
        # full-debit ceiling and this did not -- so the spread path was the
        # only way into the book, and every position LOCKBOT held had been
        # admitted by the looser of two rules that claimed to be one.
        #
        # Why the debit and not the stop: Alpaca has no stop order type for
        # options, so options_manager IS the stop. Nothing rests at the
        # broker. If that process is not running the position loses the
        # whole debit, and the controller was down for nine hours over
        # 2026-08-15/16. A cap measured against a software-only stop is
        # measuring something nobody will enforce.
        #
        # Refused outright rather than rescued. Narrowing the strikes or
        # tightening the stop to force a fit would be choosing the trade
        # first and the risk limit second.
        fits, _ceiling, why = debit_within_ceiling(
            net_debit, account_equity, max_risk_percent=max_risk_percent
        )

        if not fits:
            verdicts.append(
                ContractVerdict(long_leg, DEBIT_EXCEEDS_RISK_CAP, why)
            )
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
        quote = snapshot_to_quote(symbol, snapshot,
                                  as_of=reference_date,
                                  underlying_price=underlying_price)

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

    # Equity raised from 250 to 750 on 2026-08-12, when the full-debit cap
    # was added. These gates exist to exercise the DELTA, DTE, spread and
    # moneyness checks; at 250 the new debit ceiling is $25 and a $70
    # contract fails it before those checks can be reached, which tests
    # the cap rather than what these cases are about. The cap has its own
    # dedicated checks below, at a stated equity.
    gates = dict(
        account_equity=750.0,
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
    # Run at the SMALL equity: the point is that a risk ceiling bites
    # before the premium ceiling does, and at $750 a $73 contract clears
    # both, so the case only exists on a small account.
    risky = make(36.0, 0.72, 0.73)
    check(
        "risk ceiling binds before premium ceiling",
        evaluate_contract(risky, **dict(gates, account_equity=250.0)).status
        == RISK_TOO_HIGH,
        evaluate_contract(risky, **dict(gates, account_equity=250.0)).status,
    )

    # ---- the full-debit cap, added 2026-08-12
    #
    # With no broker-side stop on options, the whole premium is the worst
    # case if options_manager stops running. Sizing on the 35% stop alone
    # authorised 28.6% of equity per position and 85.7% across three slots.
    small = dict(gates, account_equity=250.0)
    check(
        "the full debit is capped, not just the stop distance",
        evaluate_contract(make(36.0, 0.66, 0.70), **small).status == RISK_TOO_HIGH,
        evaluate_contract(make(36.0, 0.66, 0.70), **small).status,
    )
    check(
        "and the rejection names the missing broker stop",
        "options_manager" in evaluate_contract(make(36.0, 0.66, 0.70),
                                               **small).detail,
    )
    check(
        "a debit inside the cap still passes",
        evaluate_contract(make(36.0, 0.22, 0.24), **small).accepted,
        evaluate_contract(make(36.0, 0.22, 0.24), **small).detail,
    )
    check(
        "the cap is a TIGHTENING -- it never admits what the stop rule refused",
        not evaluate_contract(make(36.0, 0.72, 0.73), **small).accepted,
    )
    check(
        "it can be set independently of the risk percent",
        not evaluate_contract(make(36.0, 0.66, 0.70),
                              **dict(gates, max_debit_percent=0.05)).accepted,
    )
    check(
        "and defaults to the risk percent when unset",
        evaluate_contract(make(36.0, 0.66, 0.70), **gates).accepted,
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

    # Re-priced 2026-08-17. The short leg was 0.60/0.63, making a $44 net
    # debit against this fixture's $25 cap ($250 equity x 10%) -- which the
    # net-debit ceiling now correctly refuses. These checks exist to prove
    # LEG SELECTION (lower bought, higher sold, net below the outright), so
    # the fixture moved under the cap rather than the cap moving for it.
    spread_chain = [
        make(36.0, 1.00, 1.04, delta=0.51),
        make(37.0, 0.82, 0.85, delta=0.40),
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
    print("The ceiling has ONE home")

    ok, ceiling, why = debit_within_ceiling(50.0, 650.0, max_risk_percent=0.10)
    check("a debit under the ceiling fits", ok and ceiling == 65.0, str(ceiling))
    check("and gives no complaint", why == "")

    ok2, _, why2 = debit_within_ceiling(155.0, 650.0, max_risk_percent=0.10)
    check("a debit over it does not", not ok2)
    check("and the reason names options_manager, not the stop distance",
          "options_manager" in why2 and "0.35" not in why2, why2)

    at, _, _ = debit_within_ceiling(65.0, 650.0, max_risk_percent=0.10)
    over, _, _ = debit_within_ceiling(65.01, 650.0, max_risk_percent=0.10)
    check("exactly at the ceiling fits", at)
    check("a cent over does not", not over)

    check("the ceiling scales with equity",
          debit_ceiling(650.0, max_risk_percent=0.10) == 65.0
          and debit_ceiling(500.0, max_risk_percent=0.10) == 50.0)
    check("and max_debit_percent overrides it when given",
          debit_ceiling(650.0, max_risk_percent=0.10,
                        max_debit_percent=0.05) == 32.5)

    # The 08-19 defect in one assertion: the buffered debit is what gets
    # submitted, so the ceiling must be applied AFTER the buffer, not before.
    ceiling_650 = debit_ceiling(650.0, max_risk_percent=0.10)
    buffered = ceiling_650 * 1.03
    check(
        "a debit at the ceiling FAILS once the 3% entry buffer is added",
        not debit_within_ceiling(buffered, 650.0, max_risk_percent=0.10)[0],
        f"{buffered:.2f} vs {ceiling_650:.2f}",
    )

    print()
    print("The NET debit is capped, because the stop is software only")

    # A debit vertical's max loss IS the net debit. options_manager is the
    # only stop -- Alpaca has no stop order type for options -- so when it
    # is not running the loss is the whole debit, not the stop distance.
    # The controller was down for nine hours over 2026-08-15/16.
    #
    # Sized against a $650 account at a 10% cap: the ceiling is $65.00.
    def spread_of(long_ask, short_bid, equity=650.0, risk=0.10):
        return select_vertical_spread(
            [make(36.0, long_ask - 0.04, long_ask, delta=0.55),
             make(37.0, short_bid, short_bid + 0.04, delta=0.42)],
            underlying_price=36.0, contract_type="call", width_strikes=1,
            min_dte=21, max_dte=45, delta_min=0.35, delta_max=0.60,
            stop_loss_percent=0.35, max_spread_percent=0.20,
            account_equity=equity, max_premium_percent=0.30,
            max_risk_percent=risk,
        )

    # $155 net -- the INTC and NVDA shape. Old rule: 155 x 0.35 = $54.25,
    # under the cap, admitted. New rule: $155 > $65.00, refused.
    over_pair, over_verdicts = spread_of(2.55, 1.00)
    check("a $155 net debit is REFUSED at a $65 cap", over_pair is None)
    check(
        "and it is refused for the debit, not for the stop distance",
        any(v.status == DEBIT_EXCEEDS_RISK_CAP for v in over_verdicts),
        str(sorted({v.status for v in over_verdicts})),
    )
    check(
        "the reason is distinct from RISK_TOO_HIGH on singles",
        DEBIT_EXCEEDS_RISK_CAP != RISK_TOO_HIGH,
    )

    # $38 net -- the SOFI shape. Under the cap either way.
    under_pair, _ = spread_of(1.10, 0.72)
    check("a $38 net debit is still ADMITTED", under_pair is not None)

    # Boundary: exactly at the cap must pass, one cent over must not.
    at_cap, _ = spread_of(1.65, 1.00)          # (1.65 - 1.00) x 100 = $65.00
    check("exactly at the cap is admitted", at_cap is not None)

    over_cap, _ = spread_of(1.66, 1.00)        # $66.00
    check("one dollar over the cap is refused", over_cap is None)

    # The relaxation at the leg level must survive: a long leg that is
    # individually unaffordable is still fine if the short leg pays it down.
    rescued, _ = spread_of(3.00, 2.45)         # $55 net, $300 long leg
    check(
        "an unaffordable long leg still builds when the NET fits",
        rescued is not None,
    )

    # Raising the cap deliberately is the remedy, not measuring on the stop.
    raised, _ = spread_of(2.55, 1.00, risk=0.25)   # $155 net vs $162.50 cap
    check("raising the risk percent admits it again", raised is not None)

    # -----------------------------------------------------------------
    # Greeks solved locally when the feed will not supply them.
    # 54% of contracts arrived with no delta on 2026-08-24, so the gate
    # that picks the contract could not run on most of the chain.
    # -----------------------------------------------------------------
    print()
    print("Greeks fall back to the model, and say so")

    class _Q:
        def __init__(self, bid, ask):
            self.bid_price, self.ask_price = bid, ask

    class _S:
        def __init__(self, bid, ask, delta=None, iv=None):
            self.latest_quote = _Q(bid, ask)
            self.implied_volatility = iv
            self.greeks = type("G", (), {"delta": delta})() if delta is not None else None

    sym, day = "F260918C00014500", date(2026, 8, 24)

    modelled = snapshot_to_quote(sym, _S(0.30, 0.32), as_of=day,
                                 underlying_price=14.20)
    check("a contract with no feed greeks still gets a delta",
          modelled is not None and modelled.delta is not None,
          str(modelled.delta if modelled else None))
    check("and it is labelled as modelled, not measured",
          modelled.delta_source == "model" and modelled.iv_source == "model",
          f"{modelled.delta_source}/{modelled.iv_source}")
    check("the modelled delta is in [0, 1] for a call",
          0.0 <= modelled.delta <= 1.0, str(modelled.delta))

    # The feed is the exchange's own number. A model must never overwrite it.
    fed = snapshot_to_quote(sym, _S(0.30, 0.32, delta=0.55, iv=0.44),
                            as_of=day, underlying_price=14.20)
    check("a feed delta is preferred over the model",
          fed.delta == 0.55 and fed.delta_source == "feed",
          f"{fed.delta} {fed.delta_source}")
    check("a feed IV is preferred over the model",
          fed.implied_volatility == 0.44 and fed.iv_source == "feed",
          f"{fed.implied_volatility} {fed.iv_source}")

    # Widening coverage must never change behaviour where it already worked.
    bare = snapshot_to_quote(sym, _S(0.30, 0.32), as_of=day)
    check("without a spot price nothing changes",
          bare.delta is None and bare.delta_source == "none",
          f"{bare.delta} {bare.delta_source}")

    # An expired or same-day contract has no time value to solve against.
    expired = snapshot_to_quote(sym, _S(0.30, 0.32), as_of=date(2026, 9, 18),
                                underlying_price=14.20)
    check("a zero-day contract is not solved, and returns None not 0.0",
          expired.delta is None, str(expired.delta))

    # A put must come back with the opposite sign convention handled.
    put = snapshot_to_quote("F260918P00014500", _S(0.30, 0.32), as_of=day,
                            underlying_price=14.20)
    check("a put also solves, and the gate reads its magnitude",
          put.delta is not None and 0.0 <= abs(put.delta) <= 1.0,
          str(put.delta if put else None))

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
