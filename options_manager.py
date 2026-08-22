"""
options_manager.py  --  LOCKBOT option exit authority  (v1.0)

READ THIS FIRST
    This is the most safety-critical file in LOCKBOT.

    On the equity side, the stop loss lives at the broker. market_scanner.py
    submits a bracket order at entry and the broker enforces the stop even
    if every LOCKBOT process dies. That safety net does not exist here:
    Alpaca supports bracket/OCO/OTO for equities but only 'simple' and
    'mleg' for options, and options are day-only -- there is no GTC.

    So the stop lives in software, in this file. If this module stops
    running, open option positions have NO stop loss of any kind. That is
    a property of the broker's API, not a design choice, and it is the
    single biggest risk difference between LOCKBOT's equity and options
    paths. The watchdog treats a stale options heartbeat as critical for
    this reason.

WHAT IT DOES
    Once per cycle: reads open option positions, prices them, decides
    whether each should be closed, and closes the ones that should be.

WHY THERE ARE FOUR EXIT RULES
    take profit   +50% on the premium paid.
    stop loss     -35% on the premium paid.
    max hold      An option bleeds every day it is held. A stock position
                  that goes sideways costs nothing; this one costs theta.
                  After OPTIONS_MAX_HOLD_DAYS the thesis has not worked
                  and the decay is no longer worth paying.
    min DTE       Close before the last two weeks, where theta decay
                  accelerates hardest and a small adverse move can wipe
                  out the remaining premium.

HOW EXITS ARE PLACED
    A limit order at the current bid, not a market order. Option books
    are often wide, and a market order into a wide book is how a
    -35% stop becomes a -60% fill. The bid is where a buyer is already
    standing, so a limit there is marketable in normal conditions.

    If an exit does not fill, the next cycle cancels it and re-submits at
    the new bid. Chasing the bid down guarantees the position eventually
    leaves. Because options are day-only, any unfilled exit dies at the
    close and is re-submitted the following morning.

USAGE
    python options_manager.py              run one exit cycle
    python options_manager.py --self-test  offline logic check, no network
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import lockbot_config as config


MODULE_NAME = "OPTIONS_MANAGER"
OPTIONS_MANAGER_VERSION = "1.0"

CONTRACT_MULTIPLIER = 100

# Read through getattr so an older lockbot_config.py cannot stop the exit
# engine from starting. A missing flag defaults to the safe, previous
# behaviour: settle nothing, let the next cycle reconcile.
OPTIONS_BACKFILL_ENABLED = getattr(config, "OPTIONS_BACKFILL_ENABLED", False)

PROJECT_FOLDER = Path(__file__).resolve().parent

# Read from config, never redeclared here. A second module naming this
# file independently is how the equity journal once silently drifted and
# zeroed every performance report.
OPTIONS_COMPLETED_FILE = getattr(
    config,
    "OPTIONS_COMPLETED_FILE",
    PROJECT_FOLDER / "options_completed_trades.csv",
)

COMPLETED_COLUMNS = [
    "position_id",
    "underlying",
    "strategy",
    "long_symbol",
    "short_symbol",
    "contracts",
    "entry_time",
    "exit_time",
    "entry_debit",
    "exit_credit",
    "profit_loss",
    "return_percent",
    "holding_hours",
    "exit_reason",
    "market_regime",
    "confidence",
    "paper_trade",
]

# Exit reasons
TAKE_PROFIT = "TAKE_PROFIT"
STOP_LOSS = "STOP_LOSS"
MAX_HOLD = "MAX_HOLD_DAYS"
NEAR_EXPIRY = "NEAR_EXPIRY"
HOLD = "HOLD"


# ---------------------------------------------------------------------------
# Position state
# ---------------------------------------------------------------------------

@dataclass
class OptionPosition:
    """One LOCKBOT options position, single-leg or vertical."""

    position_id: str
    underlying: str
    strategy: str
    long_symbol: str
    contracts: int
    entry_debit: float          # dollars paid per contract-pair, net
    entry_time: str             # ISO 8601
    expiration: str             # ISO date of the long leg
    short_symbol: str | None = None
    market_regime: str = "UNKNOWN"
    confidence: int = 0
    highest_value: float = 0.0
    entry_order_id: str | None = None
    entry_filled: bool = False
    # Consecutive cycles the stop condition has held. See decide_exit --
    # a single wide-book bid print is not evidence of a 35% loss.
    stop_strikes: int = 0
    exit_order_id: str | None = None
    exit_reason: str | None = None
    paper_trade: bool = True

    def __post_init__(self) -> None:
        """Round the dollar fields to the cent, however they arrived.

        WHY HERE AND NOT AT THE CALL SITES

        `0.56 * 100` is 56.00000000000001. That value reached the PCG
        put's entry_debit, made its +50% target 84.00000000000001, and
        stopped it taking profit at exactly $84.00 -- it needed $84.01.

        The first fix rounded in net_fill_dollars and again in
        load_positions, which covered the two paths that were known
        about. It missed a third: options_scanner.py computes
        `limit_per_contract * CONTRACT_MULTIPLIER` and passes it
        straight to entry_debit, so every NEW position could arrive
        dirty even with both other fixes in place.

        Rounding in the constructor covers every path that exists and
        every one added later, which is the only version of this fix
        that stays true. Premiums are cent-denominated, so this removes
        representation error without discarding anything real.
        """

        try:
            self.entry_debit = round(float(self.entry_debit or 0.0), 2)
        except (TypeError, ValueError):
            self.entry_debit = 0.0

        try:
            self.highest_value = round(float(self.highest_value or 0.0), 2)
        except (TypeError, ValueError):
            self.highest_value = 0.0

    @property
    def is_spread(self) -> bool:
        return bool(self.short_symbol)

    def expiration_date(self) -> date:
        return date.fromisoformat(self.expiration)


def load_positions(path: Path | None = None) -> dict[str, OptionPosition]:
    """Read the options position state, tolerating a missing file."""

    state_path = path or config.OPTIONS_STATE_FILE

    if not state_path.exists():
        return {}

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        # A corrupt state file must not stop the exit engine. Losing the
        # entry price is bad; failing to run the stop loss is worse.
        print(
            "WARNING: options position state is unreadable. Continuing with "
            "no tracked positions — any open option position will be "
            "reconciled as untracked and reported."
        )
        return {}

    positions = {}

    for position_id, values in (raw or {}).items():
        try:
            position = OptionPosition(**values)
        except TypeError as error:
            print(f"Skipping malformed position {position_id}: {error}")
            continue

        # Clean the debit on the way IN, not only on the way out.
        #
        # net_fill_dollars now rounds, but the PCG put's
        # 56.00000000000001 was already written to disk before it did,
        # and every exit band is a percentage OF that number. Fixing the
        # source alone leaves the position that is open right now still
        # needing $84.01 to hit an $84.00 target.
        #
        # Applied to every position on every load rather than as a
        # one-off repair, because the same dirt can arrive from any
        # older state file or a hand edit.
        position.entry_debit = round(float(position.entry_debit or 0.0), 2)
        position.highest_value = round(float(position.highest_value or 0.0), 2)

        positions[position_id] = position

    return positions


def save_positions(
    positions: dict[str, OptionPosition],
    path: Path | None = None,
) -> None:
    """Write the options position state atomically."""

    state_path = path or config.OPTIONS_STATE_FILE
    payload = {
        position_id: asdict(position)
        for position_id, position in positions.items()
    }

    temporary = state_path.with_name(
        f"{state_path.stem}.{os.getpid()}.{time.time_ns()}.tmp"
    )

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=4)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, state_path)

    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------

def _quote_pair(quote: Any) -> tuple[float, float] | None:
    """Pull (bid, ask) from a quote object, or None when unusable."""

    if quote is None:
        return None

    bid = getattr(quote, "bid_price", None)
    ask = getattr(quote, "ask_price", None)

    try:
        bid = float(bid)
        ask = float(ask)
    except (TypeError, ValueError):
        return None

    if bid < 0 or ask <= 0 or ask < bid:
        return None

    return bid, ask


def current_exit_value(
    position: OptionPosition,
    quotes: dict[str, Any],
) -> float | None:
    """
    What this position is worth if closed right now, per contract, in dollars.

    Closing a long leg means selling it at the BID. Closing a short leg
    means buying it back at the ASK. Using mid prices here would make
    every position look better than it is and would delay real stops.

    A debit spread cannot be worth less than zero -- LOCKBOT only opens
    verticals where the long strike is the cheaper one, so the long leg
    is always worth at least as much as the short. On 2026-07-30 a JD
    32.5/33.0 call spread was priced at long_bid - short_ask on a stale
    book, came out NEGATIVE, and read as a -203% return five minutes
    after entry. The software stop fired on a number that could not
    happen. An impossible value is now treated as no value at all:
    returning None holds the position and leaves the time-based rules in
    charge, which is the safe reading of a broken quote.
    """

    long_quote = _quote_pair(quotes.get(position.long_symbol))

    if long_quote is None:
        return None

    long_bid, _ = long_quote
    value = long_bid

    if position.is_spread:
        short_quote = _quote_pair(quotes.get(position.short_symbol))

        if short_quote is None:
            return None

        _, short_ask = short_quote
        value = long_bid - short_ask

        if value < 0:
            # Not a loss -- a crossed or stale book. Do not act on it.
            return None

    # Rounded for the same reason every other option dollar field is
    # rounded at its constructor (channel 528499d2, the 56.00000000000001
    # case): 0.60 - 0.32 is 0.27999999999999997 in binary floating point,
    # and an exit value carrying that drift is compared against stop
    # bands, divided into returns, and priced into orders. Rounding here
    # is what lets the decision and the order be provably equal rather
    # than equal to within a rounding error.
    return round(value * CONTRACT_MULTIPLIER, 2)


def exit_value_per_share(
    position: OptionPosition,
    quotes: dict[str, Any],
) -> float | None:
    """The same valuation as current_exit_value, per share rather than in
    dollars. None means the book cannot be trusted.

    THIS EXISTS SO THE DECISION AND THE ORDER CANNOT DISAGREE.

    Until 2026-08-21 build_close_request computed its own net credit as
    max(long_bid - short_ask, 0.01), while current_exit_value returned
    None for exactly the same negative input on the grounds that a crossed
    or stale book is not a price. So the STOP DECISION refused to act on a
    broken quote and the ORDER PRICE clamped it to a penny and sold
    anyway.

    It could not fire on a price-based exit, because a None valuation
    holds the position. It could fire on a TIME-based one -- NEAR_EXPIRY
    and MAX_HOLD are checked before price precisely so they work without a
    quote -- and would then have submitted a spread worth real money as a
    $0.01 credit.

    This is the third instance of one quantity computed in two places,
    after the debit cap (three) and the entry limit (two, found the same
    day). The fix is the same each time: compute it once.
    """

    value = current_exit_value(position, quotes)

    if value is None:
        return None

    return round(value / CONTRACT_MULTIPLIER, 4)


# ---------------------------------------------------------------------------
# Exit decision -- pure logic, fully testable offline
# ---------------------------------------------------------------------------

@dataclass
class ExitDecision:
    """Whether a position should be closed, and why."""

    should_exit: bool
    reason: str
    detail: str = ""
    return_percent: float = 0.0


STRIKE_EVENT_COLUMNS = [
    "event_id", "opened_at", "position_id", "underlying", "entry_debit",
    "v0", "confirm_cycles", "resolved_at", "vf", "path", "delta",
]


def strike_log_path() -> Any:
    return getattr(config, "OPTIONS_STRIKE_EVENTS_FILE",
                   config.PROJECT_FOLDER / "options_strike_events.csv")


def log_strike_open(position: Any, v0: float) -> None:
    """Record the moment a position first crosses its stop.

    WHY THIS FILE HAS TO EXIST AT ALL. OPTIONS_STOP_CONFIRM_CYCLES makes
    every real stop wait a cycle, and it was adopted on 2026-08-03 after an
    EWZ call exited at -8.1% against a -35% stop on a single bad bid print.
    Its COST is visible in every confirmed stop. Its BENEFIT is a
    non-event -- a position that crossed the stop and recovered -- and
    stop_strikes lives only in position state, where a recovery RESETS it
    to zero and erases the evidence.

    So the rule bills a visible cost and banks an invisible benefit, which
    is exactly why it sat in the registry as an orphan that could never
    fail. LOCKBOT's ruling (channel e86b3e97): without this row the
    benefit side is unobservable and the rule stays unjudgeable.

    V0 is current_exit_value at the transition -- already bid-priced, so
    it is what the position could actually have been sold for at the
    moment the rule chose to wait. Gap events need no exclusion because V0
    is captured AFTER the gap.

    Never raises. A lost measurement must not interrupt a stop.
    """

    try:
        import csv as _csv
        import csv_schema as _schema

        path = strike_log_path()
        header = _schema.ensure_schema(path, STRIKE_EVENT_COLUMNS, verbose=False)
        exists = Path(path).exists()

        with open(path, "a", newline="", encoding="utf-8") as handle:
            writer = _csv.DictWriter(handle, fieldnames=header)

            if not exists:
                writer.writeheader()

            writer.writerow({
                "event_id": f"{position.position_id}:{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
                "opened_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "position_id": position.position_id,
                "underlying": position.underlying,
                "entry_debit": f"{position.entry_debit:.2f}",
                "v0": f"{v0:.2f}",
                "confirm_cycles": getattr(
                    config, "OPTIONS_STOP_CONFIRM_CYCLES", 2),
                "resolved_at": "", "vf": "", "path": "", "delta": "",
            })
    except Exception as error:                          # noqa: BLE001
        print(f"  strike event not logged: {type(error).__name__}: {error}")


def resolve_strike_events(position: Any, vf: float, path_kind: str) -> int:
    """Close out any open strike events for a position at its real exit.

    Same formula on both paths, per the ruling: delta = (Vf - V0) / debit.
    A confirmed stop resolves at its realised credit; a recovered position
    stays open until whatever eventually closes it. The verdict is the MEAN
    delta in fractions of the debit, never a count of events -- one
    INTC-scale decay outweighs a great many small recoveries.
    """

    try:
        import csv as _csv
        import csv_schema as _schema

        path = strike_log_path()
        rows = _schema.read_rows(path, strict=False)

        if not rows:
            return 0

        touched = 0

        for row in rows:
            if row.get("position_id") != position.position_id:
                continue
            if (row.get("resolved_at") or "").strip():
                continue

            try:
                v0 = float(row["v0"])
                debit = float(row["entry_debit"])
            except (KeyError, TypeError, ValueError):
                continue

            if debit <= 0:
                continue

            row["resolved_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
            row["vf"] = f"{vf:.2f}"
            row["path"] = path_kind
            row["delta"] = f"{(vf - v0) / debit:.4f}"
            touched += 1

        if touched:
            header = list(rows[0].keys())

            with open(path, "w", newline="", encoding="utf-8") as handle:
                writer = _csv.DictWriter(handle, fieldnames=header)
                writer.writeheader()
                writer.writerows(rows)

        return touched
    except Exception as error:                          # noqa: BLE001
        print(f"  strike events not resolved: {type(error).__name__}: {error}")
        return 0


def decide_exit(
    position: OptionPosition,
    current_value: float | None,
    *,
    now: datetime,
    today: date,
    take_profit_percent: float,
    stop_loss_percent: float,
    max_hold_days: int,
    min_dte_exit: int,
) -> ExitDecision:
    """
    Decide whether one position should be closed.

    Time-based rules are checked BEFORE price-based ones, because they
    apply whether or not a quote is available. An option two days from
    expiry must be closed even if the chain is unreachable.
    """

    days_to_expiration = (position.expiration_date() - today).days

    if days_to_expiration <= min_dte_exit:
        return ExitDecision(
            True,
            NEAR_EXPIRY,
            f"{days_to_expiration} days to expiration is at or below the "
            f"{min_dte_exit}-day floor. Theta decay accelerates from here.",
        )

    entry_time = datetime.fromisoformat(position.entry_time)

    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)

    held_days = (now - entry_time).total_seconds() / 86400.0

    if held_days >= max_hold_days:
        return ExitDecision(
            True,
            MAX_HOLD,
            f"Held {held_days:.1f} days, at or past the {max_hold_days}-day "
            "limit. The thesis has not worked and theta is still being paid.",
        )

    if current_value is None:
        return ExitDecision(
            False,
            HOLD,
            "No usable quote this cycle. Time-based exits were still "
            "checked; price-based exits could not be.",
        )

    if position.entry_debit <= 0:
        return ExitDecision(
            False,
            HOLD,
            "Entry debit is not positive, so return cannot be computed.",
        )

    return_percent = (
        current_value - position.entry_debit
    ) / position.entry_debit

    if return_percent >= take_profit_percent:
        return ExitDecision(
            True,
            TAKE_PROFIT,
            f"Up {return_percent:.1%} against a {take_profit_percent:.0%} target.",
            return_percent,
        )

    if return_percent <= -stop_loss_percent:
        # The stop must hold across consecutive cycles before it fires.
        #
        # On 2026-08-03 an EWZ call stopped out at -8.1% against a -35%
        # stop. It was not a market move: the sell went out as a limit at
        # the stop price of 0.48 and filled at 0.68, and no trade in that
        # window printed below 0.57. The quote feed had simply shown a bid
        # of 0.48 while the contract was worth about 0.70.
        #
        # Measuring the live book explains it. Option quotes here are
        # fresh -- 2 to 6 seconds old -- but very wide and jittery: 16% to
        # 28% spreads, with the bid swinging 8% between polls seconds
        # apart. A single bid print is therefore not evidence of anything,
        # and the last trade is useless as a cross-check because on a
        # contract this illiquid it can be days old.
        #
        # Requiring the condition to survive a second look costs at most
        # one cycle of delay on a genuine stop, and removes an entire class
        # of exit that never should have happened. Confirmation is reset
        # the moment price recovers, so only a persistent loss fires it.
        required = max(1, getattr(config, "OPTIONS_STOP_CONFIRM_CYCLES", 2))
        position.stop_strikes += 1

        if position.stop_strikes < required:
            return ExitDecision(
                False,
                HOLD,
                f"Down {return_percent:.1%}, past the "
                f"-{stop_loss_percent:.0%} stop, but on a book this wide one "
                f"reading is not enough. Confirmation "
                f"{position.stop_strikes}/{required}; exiting next cycle if "
                "it holds.",
                return_percent,
            )

        return ExitDecision(
            True,
            STOP_LOSS,
            f"Down {return_percent:.1%} against a -{stop_loss_percent:.0%} "
            f"stop, confirmed over {position.stop_strikes} cycles.",
            return_percent,
        )

    # Price recovered into the band, so any part-built confirmation is
    # discarded. Two bad prints an hour apart must not add up to an exit.
    position.stop_strikes = 0

    return ExitDecision(
        False,
        HOLD,
        f"At {return_percent:+.1%}, inside the exit band.",
        return_percent,
    )


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def build_close_request(
    position: OptionPosition,
    quotes: dict[str, Any],
) -> Any:
    """
    Build the order that closes this position.

    Single leg  -> a simple sell-to-close limit at the bid.
    Vertical    -> an mleg order closing both legs together, which is the
                   only order class Alpaca offers for multi-leg options.
                   Closing the legs separately would leave a naked short
                   leg in between, which a small account cannot support.
    """

    from alpaca.trading.enums import (
        OrderSide,
        OrderClass,
        PositionIntent,
        TimeInForce,
    )
    from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

    long_quote = _quote_pair(quotes.get(position.long_symbol))

    if long_quote is None:
        raise ValueError(
            f"No usable quote for {position.long_symbol}; cannot price an exit."
        )

    long_bid, _ = long_quote

    if not position.is_spread:
        return LimitOrderRequest(
            symbol=position.long_symbol,
            qty=position.contracts,
            side=OrderSide.SELL,
            type="limit",
            time_in_force=TimeInForce.DAY,
            limit_price=round(max(long_bid, 0.01), 2),
            position_intent=PositionIntent.SELL_TO_CLOSE,
        )

    short_quote = _quote_pair(quotes.get(position.short_symbol))

    if short_quote is None:
        raise ValueError(
            f"No usable quote for {position.short_symbol}; cannot price an exit."
        )

    # The SAME valuation the stop decision used. A book this function
    # cannot trust is one it must not price against -- raising here puts
    # the position on the existing retry path, alongside a missing quote,
    # rather than selling it for a penny.
    net_credit = exit_value_per_share(position, quotes)

    if net_credit is None or net_credit <= 0:
        raise ValueError(
            f"{position.underlying}: the book prices this exit at or below "
            "zero, which cannot be true for a debit spread. Refusing to "
            "sell into a crossed or stale quote; will retry next cycle."
        )

    return LimitOrderRequest(
        qty=position.contracts,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        limit_price=round(net_credit, 2),
        legs=[
            OptionLegRequest(
                symbol=position.long_symbol,
                ratio_qty=1,
                side=OrderSide.SELL,
                position_intent=PositionIntent.SELL_TO_CLOSE,
            ),
            OptionLegRequest(
                symbol=position.short_symbol,
                ratio_qty=1,
                side=OrderSide.BUY,
                position_intent=PositionIntent.BUY_TO_CLOSE,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Completed-trade journal
# ---------------------------------------------------------------------------

def initialize_completed_file() -> Path:
    """Create the options completed-trades file when needed."""

    if OPTIONS_COMPLETED_FILE.exists():
        return OPTIONS_COMPLETED_FILE

    with OPTIONS_COMPLETED_FILE.open("w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=COMPLETED_COLUMNS).writeheader()

    return OPTIONS_COMPLETED_FILE


def record_completed_option_trade(
    position: OptionPosition,
    *,
    exit_credit: float,
    exit_time: datetime,
    exit_reason: str,
) -> None:
    """Append one completed options trade."""

    initialize_completed_file()

    entry_time = datetime.fromisoformat(position.entry_time)

    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)

    profit_loss = (exit_credit - position.entry_debit) * position.contracts

    return_percent = (
        (exit_credit - position.entry_debit) / position.entry_debit
        if position.entry_debit > 0
        else 0.0
    )

    holding_hours = (exit_time - entry_time).total_seconds() / 3600.0

    row = {
        "position_id": position.position_id,
        "underlying": position.underlying,
        "strategy": position.strategy,
        "long_symbol": position.long_symbol,
        "short_symbol": position.short_symbol or "",
        "contracts": position.contracts,
        "entry_time": position.entry_time,
        "exit_time": exit_time.isoformat(timespec="seconds"),
        "entry_debit": round(position.entry_debit, 2),
        "exit_credit": round(exit_credit, 2),
        "profit_loss": round(profit_loss, 2),
        "return_percent": round(return_percent * 100, 4),
        "holding_hours": round(holding_hours, 2),
        "exit_reason": exit_reason,
        "market_regime": position.market_regime,
        "confidence": position.confidence,
        "paper_trade": position.paper_trade,
    }

    with OPTIONS_COMPLETED_FILE.open("a", newline="", encoding="utf-8") as handle:
        csv.DictWriter(
            handle,
            fieldnames=COMPLETED_COLUMNS,
            extrasaction="ignore",
        ).writerow(row)


# ---------------------------------------------------------------------------
# Entry fill verification
#
# On 2026-07-30 a PBR long call was submitted, never filled, and sat at the
# broker with status "new". Because the broker held no such position, the
# reconciler below treated it as a position that had opened and then vanished,
# and journaled it at `highest_value or 0.0` — a fabricated -$56.00, -100%
# trade in options_completed_trades.csv. The order itself stayed live, so a
# later fill would have produced an option position that LOCKBOT no longer
# tracked and therefore would never have applied a stop loss to.
#
# Absence from the broker is therefore ambiguous: it means "closed" only if
# the entry actually filled. These helpers resolve that ambiguity from the
# entry order instead of assuming the worse-looking answer.
# ---------------------------------------------------------------------------

WORKING_ORDER_STATUSES = {
    "new",
    "accepted",
    "pending_new",
    "accepted_for_bidding",
    "partially_filled",
    "held",
    "replaced",
    "pending_replace",
}

FILLED_ORDER_STATUSES = {"filled"}

# The only statuses that mean "this order ended and bought nothing".
# Anything not here is treated as still working -- see classify_entry_order
# for why that direction is the safe one.
TERMINAL_WITHOUT_FILL = {
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "done_for_day",
}


def _order_status_text(order: Any) -> str:
    """Order status as a plain lowercase string, enum or not."""

    return str(getattr(order.status, "value", order.status)).lower()


def classify_entry_order(trading_client: Any, position: OptionPosition) -> str:
    """Resolve what actually happened to a position's entry order.

    Returns one of:
      FILLED   - the entry filled; absence from the broker means it closed
      WORKING  - still live at the broker; the slot is committed, not closed
      DEAD     - cancelled, rejected or expired without filling; no trade
      UNKNOWN  - no order id, or the lookup failed

    UNKNOWN deliberately falls back to the old "treat as closed" behaviour.
    A position we cannot verify is one we should stop tracking, because the
    alternative is holding a slot open forever on a guess.
    """

    if not position.entry_order_id:
        return "UNKNOWN"

    try:
        order = trading_client.get_order_by_id(position.entry_order_id)
    except Exception as error:
        print(
            f"{position.underlying}: could not read entry order "
            f"{position.entry_order_id}: {type(error).__name__}: {error}"
        )
        return "UNKNOWN"

    status = _order_status_text(order)

    if status in FILLED_ORDER_STATUSES:
        return "FILLED"

    # A partial fill is a real position, not a working order, once any
    # quantity has been bought.
    try:
        if float(order.filled_qty or 0) > 0:
            return "FILLED"
    except (TypeError, ValueError):
        pass

    # DEAD is asserted ONLY for statuses that are terminal without a fill.
    # Everything else -- known-working, or a status this code has never
    # seen -- is WORKING, and holds its slot.
    #
    # This was the other way round until 2026-08-19: WORKING_ORDER_STATUSES
    # was an allowlist and anything absent from it fell through to DEAD.
    # Nine of Alpaca's statuses were missing, and five of those are not
    # terminal at all (calculated, pending_review, pending_cancel, stopped,
    # suspended).
    #
    # It cost a live position. The XLF 58/58.5 spread was submitted at
    # 14:25:38 on 2026-08-19; at 14:30:24 this returned DEAD on an
    # unrecognised status, journaled it ENTRY_NOT_FILLED at zero P&L, and
    # deleted it from state. The order then FILLED at 14:32:57. The result
    # was a real position at the broker that nothing tracked -- and with no
    # broker-side stop on options, an untracked position has no stop at all.
    # That is the PBR failure of 2026-07-30 in a mirror: there an unfilled
    # order was journaled as a loss, here a filled one was journaled as
    # nothing.
    #
    # The asymmetry decides the direction. Wrongly holding a slot costs one
    # slot until the timeout clears it. Wrongly releasing one costs an
    # unprotected live position. An unknown status is not evidence of death.
    if status in TERMINAL_WITHOUT_FILL:
        return "DEAD"

    return "WORKING"


def net_fill_dollars(order: Any) -> float | None:
    """The cash a filled order moved, in dollars per contract-pair.

    Positive means LOCKBOT paid; negative means it received.

    Alpaca reports a multi-leg order's `filled_avg_price` as a signed NET
    across the legs: a spread opened for a debit fills at +0.24, and the
    same spread closed for a credit fills at -0.16. Single-leg orders are
    always reported positive, with the side carrying the direction.

    Reading that sign literally as a credit is what turned the JD spread
    on 2026-07-30 into a journaled -$45.00 (-155%) loss. Its real result
    was a $24 debit closed for a $16 credit: -$8.00. A debit spread's
    maximum possible loss is the debit paid, so -155% was never a
    reachable number.

    THE RESULT IS ROUNDED TO THE CENT, AND THAT IS NOT COSMETIC.

    `0.56 * 100` is 56.00000000000001 in binary floating point. That
    value was persisted as the PCG put's entry debit, so its +50% target
    became 84.00000000000001, and the take-profit test

        current_value >= target   ->   84.0 >= 84.00000000000001

    was False. The position could not take profit at exactly $84.00 --
    it needed $84.01. The comparison was never wrong; it was fed a dirty
    number, and the self-test covering "takes profit exactly at target"
    passed because it used clean inputs.

    Premiums are quoted in cents, so rounding to the cent is exact
    rather than a fudge: it removes representation error without
    discarding anything real.
    """

    price = getattr(order, "filled_avg_price", None)

    if price is None:
        return None

    try:
        amount = float(price) * CONTRACT_MULTIPLIER
    except (TypeError, ValueError):
        return None

    return round(amount, 2)


def exit_credit_from_order(order: Any) -> float | None:
    """What closing actually paid, in dollars. Never negative."""

    amount = net_fill_dollars(order)

    if amount is None:
        return None

    order_class = str(
        getattr(getattr(order, "order_class", None), "value",
                getattr(order, "order_class", ""))
    ).lower()

    if order_class == "mleg":
        # Negative net == credit received. Flip it to a credit.
        credit = -amount
    else:
        # A single-leg sell reports the price it sold at, unsigned.
        credit = abs(amount)

    # Closing a long position cannot cost money in LOCKBOT's structures.
    # If the arithmetic says otherwise the fill data is unusable.
    return credit if credit >= 0 else None


def _entry_order(trading_client: Any, position: OptionPosition) -> Any | None:
    """Fetch a position's entry order, or None if it cannot be read.

    Small because it exists for one caller: the adopt path needs the order
    OBJECT to re-derive a fill basis, while classify_entry_order only
    returns a verdict. Never raises -- a basis we cannot read leaves the
    quoted debit in place, which is wrong by a little, where dropping the
    position would be wrong by the whole stop.
    """

    if not position.entry_order_id:
        return None

    try:
        return trading_client.get_order_by_id(position.entry_order_id)
    except Exception:                                  # noqa: BLE001
        return None


def entry_debit_from_order(order: Any) -> float | None:
    """What opening actually cost, in dollars. Never negative."""

    amount = net_fill_dollars(order)

    if amount is None:
        return None

    debit = abs(amount)

    return debit if debit > 0 else None


def refund_daily_trade_slot() -> int | None:
    """Give back the daily trade slot an unfilled entry consumed.

    options_scanner.py increments the daily counter when it *submits* an
    order. An entry that never fills is not a trade, so leaving the count
    raised spends the day's budget on something that did not happen -- on
    2026-07-30 an unfilled PBR call took the fourth and last slot.

    This writes the counter file options_scanner.py owns. That is a
    deliberate exception, made here rather than by importing the scanner,
    because options_manager.py must never depend on the entry path: the
    exit engine has to keep running even if the scanner cannot be
    imported. Returns the new count, or None if it could not be adjusted.
    """

    path = config.OPTIONS_RISK_STATE_FILE
    today = datetime.now().astimezone().date().isoformat()

    try:
        state = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return None

    if state.get("trade_date") != today:
        # A stale counter resets on its own tomorrow. Nothing to refund.
        return None

    count = int(state.get("trades_submitted_today", 0))

    if count <= 0:
        return None

    state["trades_submitted_today"] = count - 1

    try:
        path.write_text(json.dumps(state, indent=4), encoding="utf-8")
    except OSError:
        return None

    return count - 1


def entry_pending_too_long(position: OptionPosition, *, now: datetime) -> bool:
    """Whether an unfilled entry has held its slot past the timeout."""

    timeout = getattr(config, "OPTIONS_ENTRY_FILL_TIMEOUT_MINUTES", 15)

    if timeout <= 0:
        return False

    entry_time = datetime.fromisoformat(position.entry_time)

    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)

    return (now - entry_time).total_seconds() > timeout * 60


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------

@dataclass
class OptionsManagerSummary:
    """Metrics from one exit cycle."""

    tracked_before: int = 0
    broker_positions: int = 0
    closed: int = 0
    entries_confirmed: int = 0
    entries_pending: int = 0
    entries_unfilled: int = 0
    exits_submitted: int = 0
    exits_rechased: int = 0
    slots_freed: int = 0
    holding: int = 0
    untracked: list[str] = field(default_factory=list)
    quote_failures: int = 0
    errors: int = 0
    tracked_after: int = 0
    duration_seconds: float = 0.0


def fetch_quotes(data_client: Any, symbols: list[str]) -> dict[str, Any]:
    """Fetch the latest quote for a list of option symbols."""

    if not symbols:
        return {}

    from alpaca.data.requests import OptionLatestQuoteRequest

    return data_client.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=symbols)
    ) or {}


def run_options_manager() -> OptionsManagerSummary:
    """Run one complete options exit cycle."""

    from alpaca.trading.client import TradingClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from dotenv import load_dotenv

    from system_heartbeat import (
        mark_module_critical,
        mark_module_degraded,
        mark_module_healthy,
        mark_module_starting,
    )

    started_at = time.perf_counter()
    summary = OptionsManagerSummary()

    mark_module_starting(
        MODULE_NAME,
        message="Options manager is starting.",
        details={"version": OPTIONS_MANAGER_VERSION},
    )

    try:
        load_dotenv()

        api_key = os.getenv(config.ALPACA_API_KEY_ENV)
        secret_key = os.getenv(config.ALPACA_SECRET_KEY_ENV)

        if not api_key or not secret_key:
            raise RuntimeError("Alpaca API keys were not found in the .env file.")

        trading_client = TradingClient(api_key, secret_key, paper=config.PAPER_TRADING)
        data_client = OptionHistoricalDataClient(api_key, secret_key)

        positions = load_positions()
        summary.tracked_before = len(positions)

        from position_filters import option_positions

        broker_positions = {
            position.symbol: position
            for position in option_positions(trading_client.get_all_positions())
        }
        summary.broker_positions = len(broker_positions)

        now = datetime.now(timezone.utc)
        today = now.date()

        # ---- Repair: more tracked entries than the broker actually holds
        #
        # entry_filled is sticky once set, so an entry wrongly marked
        # filled stays wrong forever. That is how the duplicate IBIT spread
        # survived: both entries shared a long_symbol, the filled one
        # satisfied the symbol check for both, and neither was ever
        # re-examined.
        #
        # Quantity is the arbiter. If three entries claim a contract the
        # broker holds one of, at least two are phantoms, and the order id
        # says which. This runs before anything else so the counts every
        # later step depends on are honest.
        by_symbol: dict[str, list[str]] = {}

        for position_id, position in positions.items():
            by_symbol.setdefault(position.long_symbol, []).append(position_id)

        for symbol, ids in by_symbol.items():
            broker_qty = 0

            if symbol in broker_positions:
                try:
                    broker_qty = abs(int(float(broker_positions[symbol].qty)))
                except (TypeError, ValueError):
                    broker_qty = 1

            if len(ids) <= broker_qty:
                continue

            print(
                f"{symbol}: {len(ids)} tracked entries but the broker holds "
                f"{broker_qty}. Resolving by order id."
            )

            for position_id in ids:
                position = positions[position_id]

                if position.entry_order_id is None:
                    continue

                if classify_entry_order(trading_client, position) == "FILLED":
                    continue

                # This entry's own order did not fill. Another order put
                # the contract in the account.
                refunded = refund_daily_trade_slot()

                record_completed_option_trade(
                    position,
                    exit_credit=position.entry_debit,
                    exit_time=now,
                    exit_reason="ENTRY_NOT_FILLED",
                )

                print(
                    f"  {position.underlying}: order {position.entry_order_id} "
                    "never filled. Removed as a phantom."
                    + (f" Daily count returned to {refunded}."
                       if refunded is not None else "")
                )

                del positions[position_id]
                summary.entries_unfilled += 1

        # ---- Reconcile: anything we track that the broker no longer holds
        for position_id in list(positions):
            position = positions[position_id]

            # Presence of the symbol is NOT proof this entry filled.
            #
            # On 2026-08-03 LOCKBOT submitted the same IBIT 36/36.5 spread
            # twice. One filled, one sat at "new" and never did. Both
            # tracked entries carry the same long_symbol, so the filled
            # one satisfied this check for both and the unfilled entry was
            # marked filled too -- inflating the position count, double
            # counting $23 of premium against the ceiling, and disabling
            # the timeout that would otherwise have cancelled it.
            #
            # The order id distinguishes them and was already on the
            # position. Ask the order, and fall back to the symbol only
            # when there is no order to ask.
            symbol_present = position.long_symbol in broker_positions

            if symbol_present and not position.entry_filled:
                verdict = classify_entry_order(trading_client, position)

                if verdict == "WORKING":
                    # Someone else's fill, not this entry's. Leave it
                    # pending so the timeout can still reach it.
                    summary.entries_pending += 1
                    print(
                        f"{position.underlying}: {position.long_symbol} is held, "
                        "but THIS entry's order is still working. Another "
                        "order filled it. Holding, journaling nothing."
                    )
                    continue

                if verdict == "DEAD":
                    refunded = refund_daily_trade_slot()
                    record_completed_option_trade(
                        position,
                        exit_credit=position.entry_debit,
                        exit_time=now,
                        exit_reason="ENTRY_NOT_FILLED",
                    )
                    print(
                        f"{position.underlying}: entry order never filled "
                        f"though {position.long_symbol} is held by another "
                        "entry. Journaled as ENTRY_NOT_FILLED."
                        + (f" Daily count returned to {refunded}."
                           if refunded is not None else "")
                    )
                    del positions[position_id]
                    summary.entries_unfilled += 1
                    continue

            if symbol_present:
                # Seen at the broker: the entry definitively filled. Record
                # that, so a later absence is read as "closed" rather than
                # re-checked against an order that has long since aged out.
                if not position.entry_filled:
                    position.entry_filled = True
                    summary.entries_confirmed += 1

                    # Re-base on what the entry actually cost. Until now
                    # entry_debit came from the quote at submission time,
                    # so every stop and target was measured against a
                    # price LOCKBOT never paid -- the JD spread was sized
                    # off $29 against a real fill of $24, and EWZ off $67
                    # against $65. The bands are percentages of this
                    # number, so an inaccurate basis moves both of them.
                    if position.entry_order_id:
                        try:
                            entry_order = trading_client.get_order_by_id(
                                position.entry_order_id
                            )
                            actual = entry_debit_from_order(entry_order)

                            if actual is not None and actual != position.entry_debit:
                                print(
                                    f"{position.underlying}: entry filled at "
                                    f"${actual:.2f}, quoted ${position.entry_debit:.2f}. "
                                    "Re-basing the exit bands on the fill."
                                )
                                position.entry_debit = actual

                        except Exception as fill_error:
                            print(
                                f"{position.underlying}: could not read the entry "
                                f"fill price: {type(fill_error).__name__}: "
                                f"{fill_error}. Keeping the quoted debit."
                            )

                continue

            # Absent from the broker. That means "closed" only if the entry
            # ever filled — see classify_entry_order above for the bug this
            # distinction was written for.
            if not position.entry_filled:
                verdict = classify_entry_order(trading_client, position)

                if verdict == "WORKING":
                    if not entry_pending_too_long(position, now=now):
                        summary.entries_pending += 1
                        print(
                            f"{position.underlying}: entry order still "
                            "working, not filled yet. Holding its slot, "
                            "journaling nothing."
                        )
                        continue

                    # Past the timeout. Cancel rather than leave a live order
                    # that could fill into a position nothing is tracking.
                    try:
                        trading_client.cancel_order_by_id(position.entry_order_id)
                        print(
                            f"{position.underlying}: entry order unfilled past "
                            f"the timeout. Cancel requested for "
                            f"{position.entry_order_id}."
                        )
                    except Exception as cancel_error:
                        summary.errors += 1
                        print(
                            f"{position.underlying}: entry order unfilled past "
                            "the timeout but the cancel FAILED: "
                            f"{type(cancel_error).__name__}: {cancel_error}. "
                            "Leaving the position tracked so its slot is not "
                            "released while the order can still fill."
                        )
                        continue

                    # A CANCEL REQUEST IS NOT A CANCELLED ORDER.
                    #
                    # This used to set verdict = "DEAD" the moment the cancel
                    # call returned, and book ENTRY_NOT_FILLED on that basis.
                    # But cancel is a request, not an outcome: it races the
                    # fill, and the broker decides. If the fill wins, the old
                    # code had already written a zero-P&L trade and dropped
                    # the position -- leaving a live spread with no software
                    # stop, because options have no broker-side stop to fall
                    # back on.
                    #
                    # That is not hypothetical. The XLF 58/58.5 spread on
                    # 2026-08-19 was abandoned at 14:30:24 and filled at
                    # 14:32:57, then sat untracked for five and a half hours
                    # and ran to -72% against a -35% stop. LOCKBOT filed it
                    # as 50d2f36d; this is its fix.
                    #
                    # So: re-read the order and let its ACTUAL status decide.
                    # A fill that beat the cancel is adopted rather than
                    # abandoned, and its basis is re-derived from the fill.
                    verdict = classify_entry_order(trading_client, position)

                    if verdict == "FILLED":
                        summary.errors += 0
                        position.entry_filled = True

                        filled = entry_debit_from_order(
                            _entry_order(trading_client, position)
                        )

                        if filled is not None and filled > 0:
                            position.entry_debit = filled
                            position.highest_value = filled

                        print(
                            f"{position.underlying}: THE FILL BEAT THE CANCEL. "
                            f"Adopting it rather than abandoning it -- basis "
                            f"re-derived from the order at "
                            f"${position.entry_debit:.2f}. It keeps its slot "
                            "and its stop."
                        )
                        summary.entries_confirmed += 1
                        continue

                    if verdict == "WORKING":
                        print(
                            f"{position.underlying}: cancel requested but the "
                            "order is still live at the broker. Holding the "
                            "position tracked until its status is terminal -- "
                            "an unconfirmed cancel is not a dead order."
                        )
                        continue

                    # UNKNOWN falls through with DEAD to the booking path
                    # below, which is the pre-existing behaviour for an
                    # order that cannot be read at all.

                if verdict == "DEAD":
                    # No trade happened. Journal it at cost so it stays
                    # visible without inventing a profit or a loss.
                    record_completed_option_trade(
                        position,
                        exit_credit=position.entry_debit,
                        exit_time=now,
                        exit_reason="ENTRY_NOT_FILLED",
                    )

                    refunded = refund_daily_trade_slot()
                    refund_note = (
                        f" Daily trade count returned to {refunded}."
                        if refunded is not None
                        else ""
                    )

                    print(
                        f"{position.underlying}: entry never filled. Journaled "
                        "as ENTRY_NOT_FILLED with zero P&L and slot released."
                        + refund_note
                    )

                    del positions[position_id]
                    summary.entries_unfilled += 1
                    continue

                # FILLED or UNKNOWN fall through to the normal close path.

            # The position is gone. ASK THE BROKER WHAT IT SOLD FOR.
            #
            # This used to journal `position.highest_value or 0.0` -- the
            # best price the position ever reached, or zero. Both are
            # fabrications. highest_value is by definition the most
            # flattering number in the position's life, so a losing trade
            # closed here was recorded at its high-water mark.
            #
            # It happened on 2026-08-20. The XLF 58/58.5 spread was
            # restored with highest_value seeded at its $32.00 entry debit,
            # then closed at 0.09 -- a $9.00 credit, a real -$23.00 loss.
            # It was journaled exit_credit $32.00, P/L $0.00, a breakeven
            # that never occurred. The ledger then understated the day by
            # $23 and the options daily-loss check disagreed with
            # market_scanner's by more than eleven points, which blocked
            # entries on a number that was wrong rather than on risk.
            #
            # The exit order id is right there. Read it. Fall back to the
            # last mark ONLY when the order cannot be read, and say so in
            # the exit reason rather than passing a guess off as a fill.
            exit_credit = None
            credit_source = "fill"

            if position.exit_order_id:
                try:
                    exit_order = trading_client.get_order_by_id(
                        position.exit_order_id
                    )
                except Exception:                      # noqa: BLE001
                    exit_order = None

                if exit_order is not None:
                    exit_credit = exit_credit_from_order(exit_order)

            if exit_credit is None:
                exit_credit = position.highest_value or 0.0
                credit_source = "estimate"

            record_completed_option_trade(
                position,
                exit_credit=exit_credit,
                exit_time=now,
                exit_reason=(
                    position.exit_reason or "CLOSED_AT_BROKER"
                ) + ("" if credit_source == "fill" else "_EST"),
            )

            # Close out any strike event this position left open, whichever
            # way it went. A confirmed stop and a recovery that later exited
            # for another reason both resolve with the same formula.
            resolve_strike_events(
                position, exit_credit,
                "confirmed" if position.exit_reason == STOP_LOSS
                else "recovered",
            )

            print(
                f"{position.underlying}: position closed at the broker. "
                f"Journaled as {position.exit_reason or 'CLOSED_AT_BROKER'}."
            )

            del positions[position_id]
            summary.closed += 1

        # ---- Close the loop on entry limits before anything else
        #
        # options_scanner records every entry limit with a BLANK outcome,
        # because at submission nobody knows it. Nothing wrote it back, so
        # on 2026-08-20 the report read "attempts 4, filled 0, unfilled 0,
        # unknown 4" -- LOCKBOT approved f=0.5 on the condition its effect
        # be measured, and the measurement could accumulate rows forever
        # without ever producing a verdict.
        #
        # Here rather than in a script because a resolver nobody runs is
        # the same defect one layer out. This module already talks to the
        # broker every cycle and only asks about attempts still blank,
        # which is normally none.
        try:
            import resolve_attempts

            outcome = resolve_attempts.resolve(trading_client, verbose=False)

            if outcome.get("resolved"):
                print(f"Entry attempts resolved: {outcome['resolved']} "
                      f"({outcome['still_working']} still working)")
        except Exception as error:                      # noqa: BLE001
            print(f"Entry attempts not resolved: "
                  f"{type(error).__name__}: {error}")

        # ---- Anything the broker holds that we do not track
        tracked_symbols = set()

        for position in positions.values():
            tracked_symbols.add(position.long_symbol)

            if position.short_symbol:
                tracked_symbols.add(position.short_symbol)

        summary.untracked = sorted(
            set(broker_positions) - tracked_symbols
        )

        if summary.untracked:
            print(
                "WARNING: option positions held at the broker that LOCKBOT "
                f"does not track: {', '.join(summary.untracked)}. These have "
                "NO software stop loss."
            )

            # This printed into a DEGRADED heartbeat and nothing else, which
            # is how the XLF spread sat unprotected from 14:32 to 20:00 on
            # 2026-08-19 with a correct warning written down the whole time.
            # An untracked option position is the single worst state this
            # system can be in -- there is no broker-side stop for options,
            # so nothing at all limits the loss -- and it is the one thing
            # that must reach a person rather than a log file.
            #
            # Keyed on the symbols so a standing orphan alerts once a day
            # rather than every five minutes, and never suppressed by the
            # cooldown when the SET of untracked symbols changes.
            try:
                from notifications import send_smart_notification

                send_smart_notification(
                    symbol="OPTIONS",
                    event_type="UNTRACKED_POSITION",
                    title="LOCKBOT: option position with NO stop loss",
                    message=(
                        f"{len(summary.untracked)} option leg(s) held at the "
                        f"broker that LOCKBOT does not track:\n"
                        f"{', '.join(summary.untracked)}\n\n"
                        "Options have no broker-side stop, so these are "
                        "unprotected. Nothing will close them."
                    ),
                    reason=",".join(summary.untracked),
                    force=True,
                    cooldown_minutes=1440,
                )
            except Exception as alert_error:           # noqa: BLE001
                print(
                    "  and the ALERT about it failed: "
                    f"{type(alert_error).__name__}: {alert_error}"
                )

        # ---- Price and evaluate what remains
        symbols_to_quote = sorted(tracked_symbols)
        quotes = {}

        if symbols_to_quote:
            try:
                quotes = fetch_quotes(data_client, symbols_to_quote)
            except Exception as quote_error:
                summary.quote_failures += 1
                print(
                    "Quote fetch failed: "
                    f"{type(quote_error).__name__}: {quote_error}. "
                    "Time-based exits will still be evaluated."
                )

        for position_id, position in list(positions.items()):
            if not position.entry_filled:
                # The entry is still working. There is nothing to sell yet,
                # and an exit order against a position the broker does not
                # hold would simply be rejected.
                continue

            try:
                value = current_exit_value(position, quotes)

                if value is None:
                    summary.quote_failures += 1

                if value is not None and value > position.highest_value:
                    position.highest_value = value

                strikes_before = position.stop_strikes

                decision = decide_exit(
                    position,
                    value,
                    now=now,
                    today=today,
                    take_profit_percent=config.OPTIONS_TAKE_PROFIT_PERCENT,
                    stop_loss_percent=config.OPTIONS_STOP_LOSS_PERCENT,
                    max_hold_days=config.OPTIONS_MAX_HOLD_DAYS,
                    min_dte_exit=config.OPTIONS_MIN_DTE_EXIT,
                )

                # THE FIRST STRIKE, 0 -> 1: the cycle where the rule chose to
                # wait rather than sell. V0 is what the position could have
                # been sold for at that instant. Everything the confirmation
                # rule can ever be judged on starts here -- see
                # log_strike_open for why state alone cannot carry it.
                if strikes_before == 0 and position.stop_strikes == 1                         and value is not None:
                    log_strike_open(position, value)

                label = f"{position.underlying} {position.strategy}"

                if not decision.should_exit:
                    summary.holding += 1
                    print(f"{label}: HOLD — {decision.detail}")
                    continue

                print(f"{label}: EXIT ({decision.reason}) — {decision.detail}")

                # An exit order may already be working from a prior cycle.
                # Cancel it and re-price, because the bid has moved.
                if position.exit_order_id:
                    try:
                        trading_client.cancel_order_by_id(position.exit_order_id)
                        summary.exits_rechased += 1
                    except Exception:
                        # Already filled or already cancelled. Either way the
                        # reconciliation pass above will settle it next cycle.
                        pass

                    position.exit_order_id = None

                # Shadow mode does NOT block exits. It never should have.
                #
                # It was written when there were no positions, where it
                # sensibly meant "decide but do not trade". Once contracts
                # are held the same flag means something entirely
                # different: refusing to close a position that has hit its
                # stop, on an instrument with no broker-side stop to fall
                # back on.
                #
                # On 2026-08-04 options were paused into shadow mode to
                # stop losses while the edge is measured, and it silently
                # removed the only protection the two open positions had.
                # Pausing entries is a strategy decision; abandoning open
                # risk is not the same thing and must not be a side effect
                # of it.
                #
                # Entries are gated in options_scanner.py, which is where
                # shadow mode belongs.

                if value is None:
                    print(
                        f"{label}: exit is due but there is no quote to price "
                        "it against. Will retry next cycle."
                    )
                    continue

                request = build_close_request(position, quotes)
                order = trading_client.submit_order(order_data=request)

                position.exit_order_id = str(order.id)
                position.exit_reason = decision.reason
                summary.exits_submitted += 1

                print(f"{label}: exit order {order.id} submitted.")

            except Exception as position_error:
                summary.errors += 1
                print(
                    f"Could not process {position.underlying}: "
                    f"{type(position_error).__name__}: {position_error}"
                )

        # ---- Settle exits in this cycle so the freed slot is usable now
        #
        # options_scanner.py runs immediately after this module in the
        # controller's cycle, and reads the same position state to decide
        # whether there is room for a new entry. Without this pass a closed
        # position keeps holding its slot until the *next* reconciliation,
        # so a slot freed at 10:00 could not be refilled until 10:05.
        # Waiting a few seconds here for the exit to fill hands the scanner
        # an accurate slot count while it can still act on it.
        if summary.exits_submitted and OPTIONS_BACKFILL_ENABLED:
            settle_seconds = getattr(config, "OPTIONS_EXIT_SETTLE_SECONDS", 20)

            print(
                f"\nWaiting up to {settle_seconds}s for "
                f"{summary.exits_submitted} exit(s) to fill, so a freed slot "
                "can be refilled this cycle."
            )

            deadline = time.monotonic() + settle_seconds

            while time.monotonic() < deadline:
                time.sleep(min(4.0, max(0.0, deadline - time.monotonic())))

                pending_exits = [
                    position
                    for position in positions.values()
                    if position.exit_order_id
                ]

                if not pending_exits:
                    break

                try:
                    still_held = {
                        position.symbol
                        for position in option_positions(
                            trading_client.get_all_positions()
                        )
                    }
                except Exception as settle_error:
                    summary.errors += 1
                    print(
                        "Could not re-read positions while settling exits: "
                        f"{type(settle_error).__name__}: {settle_error}. "
                        "The next cycle will reconcile normally."
                    )
                    break

                for position in pending_exits:
                    if position.long_symbol in still_held:
                        continue

                    # The exit filled. Prefer the order's actual fill price
                    # over highest_value — it is what the position really
                    # sold for.
                    exit_credit = position.highest_value or 0.0

                    try:
                        exit_order = trading_client.get_order_by_id(
                            position.exit_order_id
                        )
                        actual = exit_credit_from_order(exit_order)

                        if actual is not None:
                            exit_credit = actual
                    except Exception:
                        # Fall back to highest_value rather than lose the row.
                        pass

                    record_completed_option_trade(
                        position,
                        exit_credit=exit_credit,
                        exit_time=datetime.now(timezone.utc),
                        exit_reason=position.exit_reason or "CLOSED_AT_BROKER",
                    )

                    print(
                        f"{position.underlying}: exit filled at "
                        f"${exit_credit:.2f}. Slot released for this cycle."
                    )

                    del positions[position.position_id]
                    summary.closed += 1
                    summary.slots_freed += 1

            if summary.slots_freed:
                print(
                    f"{summary.slots_freed} slot(s) freed and available to "
                    "options_scanner.py this cycle."
                )

        save_positions(positions)
        summary.tracked_after = len(positions)
        summary.duration_seconds = round(time.perf_counter() - started_at, 3)

        details = asdict(summary)
        details["version"] = OPTIONS_MANAGER_VERSION
        details["shadow_mode"] = config.OPTIONS_SHADOW_MODE

        if summary.errors or summary.untracked:
            mark_module_degraded(
                MODULE_NAME,
                message=(
                    f"Options manager completed with {summary.errors} error(s) "
                    f"and {len(summary.untracked)} untracked position(s)."
                ),
                details=details,
            )
        else:
            mark_module_healthy(
                MODULE_NAME,
                message=(
                    "Options manager completed successfully. "
                    f"{summary.holding} holding, {summary.exits_submitted} exit(s) "
                    f"submitted, {summary.closed} closed."
                ),
                details=details,
            )

        print_summary(summary)
        return summary

    except Exception as error:
        summary.duration_seconds = round(time.perf_counter() - started_at, 3)

        mark_module_critical(
            MODULE_NAME,
            message="Options manager failed with an unhandled exception.",
            details={
                **asdict(summary),
                "version": OPTIONS_MANAGER_VERSION,
                "exception_type": type(error).__name__,
            },
        )

        print()
        print("=" * 56)
        print("OPTIONS MANAGER FAILURE")
        print("-" * 56)
        print(f"Error Type    : {type(error).__name__}")
        print(f"Error Message : {error}")
        print("Open option positions have NO software stop while this is down.")
        print("=" * 56)

        raise


def print_summary(summary: OptionsManagerSummary) -> None:
    """Display one exit-cycle summary."""

    print("=" * 56)
    print(f"       LOCKBOT OPTIONS MANAGER v{OPTIONS_MANAGER_VERSION}")
    print("=" * 56)
    # The exit engine is always live. Shadow mode pauses ENTRIES, in
    # options_scanner.py; saying "SHADOW" here implied the stop had been
    # switched off, which is precisely the misreading that made disabling
    # exits look acceptable in the first place.
    print(
        "Mode            : EXITS ALWAYS LIVE"
        + ("  (entries paused — shadow mode)" if config.OPTIONS_SHADOW_MODE
           else "")
    )
    print(f"Tracked Before  : {summary.tracked_before}")
    print(f"Broker Positions: {summary.broker_positions}")
    print(f"Holding         : {summary.holding}")
    print(f"Exits Submitted : {summary.exits_submitted}")
    print(f"Exits Re-chased : {summary.exits_rechased}")
    print(f"Closed          : {summary.closed}")
    print(f"Slots Freed     : {summary.slots_freed}")
    print(f"Entries Pending : {summary.entries_pending}")
    print(f"Entries Unfilled: {summary.entries_unfilled}")
    print(f"Untracked       : {len(summary.untracked)}")
    print(f"Quote Failures  : {summary.quote_failures}")
    print(f"Errors          : {summary.errors}")
    print(f"Tracked After   : {summary.tracked_after}")
    print(f"Duration        : {summary.duration_seconds:.2f} seconds")
    print("=" * 56)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    """Offline checks of the exit logic. No network, no credentials."""

    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    today = now.date()

    def make(
        *,
        entry_debit=70.0,
        entry_days_ago=1,
        dte=30,
        short_symbol=None,
    ):
        from datetime import timedelta

        return OptionPosition(
            position_id="test-1",
            underlying="EWZ",
            strategy="LONG_CALL" if short_symbol is None else "BULL_CALL_SPREAD",
            long_symbol="EWZ260821C00036000",
            short_symbol=short_symbol,
            contracts=1,
            entry_debit=entry_debit,
            entry_time=(now - timedelta(days=entry_days_ago)).isoformat(),
            expiration=(today + timedelta(days=dte)).isoformat(),
        )

    rules = dict(
        take_profit_percent=0.50,
        stop_loss_percent=0.35,
        max_hold_days=10,
        min_dte_exit=14,
    )

    print("Exit decisions")

    flat = decide_exit(make(), 70.0, now=now, today=today, **rules)
    check("holds a flat position", not flat.should_exit, flat.reason)

    winner = decide_exit(make(), 106.0, now=now, today=today, **rules)
    check("takes profit at +51%", winner.should_exit and winner.reason == TAKE_PROFIT,
          winner.reason)

    at_target = decide_exit(make(), 105.0, now=now, today=today, **rules)
    check("takes profit exactly at target",
          at_target.should_exit and at_target.reason == TAKE_PROFIT, at_target.reason)

    # The check above passed for months while a real position could not
    # take profit, because it used a clean 70.0 debit. The PCG put was
    # holding 56.00000000000001 -- 0.56 * 100 in binary floating point --
    # so its target was 84.00000000000001 and $84.00 was not enough.
    #
    # Reproduce with the dirty number rather than the tidy one, and
    # build it the way options_scanner.py does: a raw multiplication
    # handed straight to the constructor.
    dirty = make(entry_debit=0.56 * CONTRACT_MULTIPLIER)
    check("a debit straight from 0.56 * 100 is cleaned by the constructor",
          dirty.entry_debit == 56.0, repr(dirty.entry_debit))

    exact = decide_exit(dirty, 84.0, now=now, today=today, **rules)
    check("takes profit at exactly $84.00 on a $56.00 debit",
          exact.should_exit and exact.reason == TAKE_PROFIT, exact.reason)

    check("net_fill_dollars rounds to the cent",
          net_fill_dollars(type("O", (), {"filled_avg_price": 0.56})()) == 56.0,
          repr(net_fill_dollars(type("O", (), {"filled_avg_price": 0.56})())))

    # And the dirt already on disk must be cleaned on the way in, or the
    # position open right now keeps its bad target.
    import tempfile as _tempfile

    _dirty_state = Path(_tempfile.mkdtemp()) / "options_position_state.json"
    _dirty_state.write_text(json.dumps({
        "p1": {
            "position_id": "p1", "underlying": "PCG", "strategy": "LONG_PUT",
            "long_symbol": "PCG260821P00017500", "contracts": 1,
            "entry_debit": 56.00000000000001,
            "entry_time": "2026-08-01T14:00:00+00:00",
            "expiration": "2026-08-21", "highest_value": 84.00000000000001,
        }
    }), encoding="utf-8")

    _loaded = load_positions(_dirty_state)
    check("a dirty debit already on disk is cleaned on load",
          _loaded["p1"].entry_debit == 56.0, repr(_loaded["p1"].entry_debit))
    check("and so is the high-water mark",
          _loaded["p1"].highest_value == 84.0,
          repr(_loaded["p1"].highest_value))

    # A stop now needs confirming, so this takes two looks at the same
    # position rather than one at a fresh one. Reusing make() here would
    # hand each call a position with no strike history and never confirm.
    loser = make()
    first_look = decide_exit(loser, 45.0, now=now, today=today, **rules)
    check("a single -36% reading does not sell yet",
          not first_look.should_exit, first_look.reason)

    second_look = decide_exit(loser, 45.0, now=now, today=today, **rules)
    check("stops out at -36% once confirmed",
          second_look.should_exit and second_look.reason == STOP_LOSS,
          second_look.reason)

    near_stop = decide_exit(make(), 46.0, now=now, today=today, **rules)
    check("holds just inside the stop", not near_stop.should_exit, near_stop.reason)

    stale = decide_exit(make(entry_days_ago=11), 70.0, now=now, today=today, **rules)
    check("closes past the hold limit",
          stale.should_exit and stale.reason == MAX_HOLD, stale.reason)

    expiring = decide_exit(make(dte=10), 70.0, now=now, today=today, **rules)
    check("closes near expiry",
          expiring.should_exit and expiring.reason == NEAR_EXPIRY, expiring.reason)

    # The critical one: time-based exits must still fire without a quote.
    no_quote_expiring = decide_exit(make(dte=5), None, now=now, today=today, **rules)
    check("closes near expiry even with no quote",
          no_quote_expiring.should_exit and no_quote_expiring.reason == NEAR_EXPIRY,
          no_quote_expiring.reason)

    no_quote_flat = decide_exit(make(), None, now=now, today=today, **rules)
    check("holds without a quote when no time rule applies",
          not no_quote_flat.should_exit, no_quote_flat.reason)

    # Expiry beats the hold limit when both apply — the tighter rule wins.
    both = decide_exit(make(dte=3, entry_days_ago=30), 70.0, now=now, today=today, **rules)
    check("expiry takes precedence over hold limit",
          both.should_exit and both.reason == NEAR_EXPIRY, both.reason)

    print()
    print("Valuation")

    class FakeQuote:
        def __init__(self, bid, ask):
            self.bid_price = bid
            self.ask_price = ask

    single = make()
    value = current_exit_value(single, {"EWZ260821C00036000": FakeQuote(0.66, 0.70)})
    check("single leg values at the bid", value == 66.0, str(value))

    spread = make(short_symbol="EWZ260821C00037000")
    spread_value = current_exit_value(
        spread,
        {
            "EWZ260821C00036000": FakeQuote(1.00, 1.04),
            "EWZ260821C00037000": FakeQuote(0.60, 0.63),
        },
    )
    # Sell the long at 1.00, buy back the short at 0.63 -> 0.37 net.
    check("spread values long bid minus short ask",
          abs(spread_value - 37.0) < 1e-9, str(spread_value))

    missing = current_exit_value(single, {})
    check("missing quote returns None", missing is None, str(missing))

    crossed = current_exit_value(
        single, {"EWZ260821C00036000": FakeQuote(1.20, 0.80)}
    )
    check("crossed book returns None", crossed is None, str(crossed))

    zero_ask = current_exit_value(
        single, {"EWZ260821C00036000": FakeQuote(0.0, 0.0)}
    )
    check("empty book returns None", zero_ask is None, str(zero_ask))

    print()
    print("State round trip")

    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        state_path = Path(folder) / "state.json"
        original = {"test-1": make()}

        save_positions(original, state_path)
        restored = load_positions(state_path)

        check("saves and reloads", "test-1" in restored)
        check(
            "preserves the entry debit",
            restored["test-1"].entry_debit == 70.0,
            str(restored["test-1"].entry_debit),
        )

        state_path.write_text("{ this is not json", encoding="utf-8")
        check("survives a corrupt state file", load_positions(state_path) == {})

        # A position written before entry_order_id existed must still load.
        state_path.write_text(
            json.dumps(
                {
                    "legacy-1": {
                        "position_id": "legacy-1",
                        "underlying": "EWZ",
                        "strategy": "LONG_CALL",
                        "long_symbol": "EWZ260821C00036000",
                        "contracts": 1,
                        "entry_debit": 70.0,
                        "entry_time": "2026-07-30T13:50:19+00:00",
                        "expiration": "2026-08-21",
                    }
                }
            ),
            encoding="utf-8",
        )
        legacy = load_positions(state_path)
        check("loads state written before entry_order_id", "legacy-1" in legacy)
        check(
            "legacy position defaults to unfilled",
            bool(legacy) and legacy["legacy-1"].entry_filled is False,
        )

    print()
    print("Entry fill verification")

    class FakeOrder:
        def __init__(self, status, filled_qty="0"):
            self.status = status
            self.filled_qty = filled_qty

    class FakeClient:
        def __init__(self, order=None, raises=False):
            self.order = order
            self.raises = raises

        def get_order_by_id(self, order_id):
            if self.raises:
                raise RuntimeError("broker unreachable")
            return self.order

    pending = make()
    pending.entry_order_id = "order-1"

    check(
        "a working order is not a closed position",
        classify_entry_order(FakeClient(FakeOrder("new")), pending) == "WORKING",
    )
    check(
        "a filled order counts as filled",
        classify_entry_order(FakeClient(FakeOrder("filled", "1")), pending)
        == "FILLED",
    )
    check(
        "a partial fill counts as filled",
        classify_entry_order(FakeClient(FakeOrder("partially_filled", "1")), pending)
        == "FILLED",
    )
    check(
        "a cancelled order is dead, not a loss",
        classify_entry_order(FakeClient(FakeOrder("canceled")), pending) == "DEAD",
    )
    check(
        "a rejected order is dead",
        classify_entry_order(FakeClient(FakeOrder("rejected")), pending) == "DEAD",
    )

    no_id = make()
    no_id.entry_order_id = None
    check(
        "no order id falls back to UNKNOWN",
        classify_entry_order(FakeClient(FakeOrder("new")), no_id) == "UNKNOWN",
    )
    check(
        "a lookup failure falls back to UNKNOWN",
        classify_entry_order(FakeClient(raises=True), pending) == "UNKNOWN",
    )

    reference = datetime(2026, 7, 30, 15, 30, tzinfo=timezone.utc)

    fresh = make()
    fresh.entry_time = "2026-07-30T15:25:00+00:00"
    check(
        "a 5-minute-old entry still has time",
        entry_pending_too_long(fresh, now=reference) is False,
    )

    stale = make()
    stale.entry_time = "2026-07-30T15:00:00+00:00"
    check(
        "a 30-minute-old entry is past the timeout",
        entry_pending_too_long(stale, now=reference) is True,
    )

    naive = make()
    naive.entry_time = "2026-07-30T15:00:00"
    check(
        "a naive entry timestamp does not raise",
        entry_pending_too_long(naive, now=reference) is True,
    )

    print()
    print("Shadow mode must never disarm the stop")

    # Pausing entries on 2026-08-04 silently removed the only protection
    # two open positions had, because shadow mode was checked in the exit
    # path. Options have no broker-side stop; this file IS the stop.
    real_shadow = config.OPTIONS_SHADOW_MODE

    try:
        config.OPTIONS_SHADOW_MODE = True

        stopping = make()
        stopping.entry_debit = 100.0
        stopping.entry_time = "2026-08-01T14:00:00+00:00"
        stopping.expiration = "2026-08-21"

        rules_now = dict(
            now=datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc),
            today=date(2026, 8, 4),
            take_profit_percent=0.50,
            stop_loss_percent=0.35,
            max_hold_days=10,
            min_dte_exit=14,
        )

        first = decide_exit(stopping, 60.0, **rules_now)
        second = decide_exit(stopping, 60.0, **rules_now)

        check(
            "a stop still fires while entries are paused",
            second.should_exit is True and second.reason == STOP_LOSS,
            f"{second.reason} after {first.reason}",
        )
        check(
            "take profit also still fires",
            decide_exit(make(), 200.0, **rules_now).should_exit is True,
        )
        check(
            "and the near-expiry rule",
            decide_exit(make(dte=5), 100.0, **rules_now).should_exit is True,
        )

    finally:
        config.OPTIONS_SHADOW_MODE = real_shadow

    print()
    print("A stop must survive a second look")

    def at(value):
        """decide_exit against a live value, with the real thresholds."""
        return decide_exit(
            confirming,
            value,
            now=datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc),
            today=date(2026, 8, 3),
            take_profit_percent=0.50,
            stop_loss_percent=0.35,
            max_hold_days=10,
            min_dte_exit=14,
        )

    # The real EWZ trade: entry 74, stop level 48.10. A single quoted bid
    # of 48 while the contract traded at 70.
    confirming = make()
    confirming.entry_debit = 74.0
    confirming.entry_time = "2026-08-01T14:00:00+00:00"
    confirming.expiration = "2026-08-21"

    first = at(48.0)
    check("one bad print does not sell", first.should_exit is False, first.reason)
    check("but it is counted", confirming.stop_strikes == 1,
          str(confirming.stop_strikes))
    check("and it says so", "onfirmation 1/2" in first.detail, first.detail)

    # Next cycle the book prints sensibly again — as it did in reality,
    # where trades were happening at 0.70.
    recovered = at(70.0)
    check("a recovery holds", recovered.should_exit is False)
    check("and clears the confirmation", confirming.stop_strikes == 0,
          str(confirming.stop_strikes))

    # Two bad prints an hour apart must not add up to an exit.
    at(48.0)
    at(70.0)
    check("confirmation does not accumulate across recoveries",
          confirming.stop_strikes == 0, str(confirming.stop_strikes))

    # A genuine, persistent loss still exits — one cycle later.
    genuine = at(45.0)
    check("a real loss is held once", genuine.should_exit is False)
    second = at(44.0)
    check("and sold on confirmation", second.should_exit is True, second.reason)
    check("recorded as a stop", second.reason == STOP_LOSS, second.reason)
    check("saying it was confirmed", "confirmed over 2" in second.detail,
          second.detail)

    # Take profit and the time rules must NOT wait for confirmation.
    quick = make()
    quick.entry_debit = 74.0
    quick.entry_time = "2026-08-01T14:00:00+00:00"
    quick.expiration = "2026-08-21"
    confirming = quick
    profit = at(120.0)
    check("take profit is immediate", profit.should_exit is True, profit.reason)

    expiring = make()
    expiring.entry_debit = 74.0
    expiring.entry_time = "2026-08-01T14:00:00+00:00"
    expiring.expiration = "2026-08-10"
    confirming = expiring
    near = at(48.0)
    check("near-expiry still exits immediately",
          near.should_exit is True and near.reason == NEAR_EXPIRY, near.reason)

    print()
    print("Fill price sign handling")

    class FakeFill:
        def __init__(self, price, order_class=None):
            self.filled_avg_price = price
            self.order_class = order_class

    # The real JD spread: opened for a 0.24 debit, closed for a 0.16
    # credit, which Alpaca reports as -0.16 on a multi-leg order.
    check(
        "mleg close credit is read as a credit, not a loss",
        exit_credit_from_order(FakeFill(-0.16, "mleg")) == 16.0,
        str(exit_credit_from_order(FakeFill(-0.16, "mleg"))),
    )
    check(
        "mleg open debit is read as a cost",
        entry_debit_from_order(FakeFill(0.24, "mleg")) == 24.0,
        str(entry_debit_from_order(FakeFill(0.24, "mleg"))),
    )
    check(
        "single-leg close credit is unsigned",
        exit_credit_from_order(FakeFill(1.04)) == 104.0,
        str(exit_credit_from_order(FakeFill(1.04))),
    )
    check(
        "single-leg open debit is unsigned",
        entry_debit_from_order(FakeFill(0.65)) == 65.0,
        str(entry_debit_from_order(FakeFill(0.65))),
    )
    check(
        "a missing fill price yields None",
        exit_credit_from_order(FakeFill(None)) is None,
    )
    check(
        "an unparseable fill price yields None",
        entry_debit_from_order(FakeFill("not a number")) is None,
    )

    # The JD trade end to end, against the real fills.
    jd_entry = entry_debit_from_order(FakeFill(0.24, "mleg"))
    jd_exit = exit_credit_from_order(FakeFill(-0.16, "mleg"))
    check(
        "the JD spread reconstructs as -$8.00, not -$45.00",
        abs((jd_exit - jd_entry) - (-8.0)) < 1e-9,
        f"{jd_exit - jd_entry:+.2f}",
    )
    check(
        "the JD loss cannot exceed the debit paid",
        abs(jd_exit - jd_entry) <= jd_entry,
    )

    print()
    print("Impossible spread values")

    spread_position = make()
    spread_position.short_symbol = "EWZ260821C00037000"

    crossed = current_exit_value(
        spread_position,
        {
            "EWZ260821C00036000": FakeQuote(0.90, 0.95),
            "EWZ260821C00037000": FakeQuote(1.15, 1.20),
        },
    )
    check(
        "a negative spread value is rejected, not stopped on",
        crossed is None,
        str(crossed),
    )

    healthy = current_exit_value(
        spread_position,
        {
            "EWZ260821C00036000": FakeQuote(1.10, 1.15),
            "EWZ260821C00037000": FakeQuote(0.85, 0.90),
        },
    )
    check(
        "a normal spread still prices at long bid minus short ask",
        healthy is not None and abs(healthy - 20.0) < 1e-9,
        str(healthy),
    )

    print()
    print("Daily trade slot refund")

    with tempfile.TemporaryDirectory() as folder:
        counter = Path(folder) / "options_risk_state.json"
        real_path = config.OPTIONS_RISK_STATE_FILE
        today = datetime.now().astimezone().date().isoformat()

        try:
            config.OPTIONS_RISK_STATE_FILE = counter

            counter.write_text(
                json.dumps({"trade_date": today, "trades_submitted_today": 4}),
                encoding="utf-8",
            )
            check("refunds one slot", refund_daily_trade_slot() == 3)
            check(
                "the refund is persisted",
                json.loads(counter.read_text(encoding="utf-8"))[
                    "trades_submitted_today"
                ]
                == 3,
            )

            counter.write_text(
                json.dumps({"trade_date": today, "trades_submitted_today": 0}),
                encoding="utf-8",
            )
            check(
                "never refunds below zero",
                refund_daily_trade_slot() is None,
            )

            counter.write_text(
                json.dumps(
                    {"trade_date": "2026-01-01", "trades_submitted_today": 4}
                ),
                encoding="utf-8",
            )
            check(
                "does not touch another day's counter",
                refund_daily_trade_slot() is None,
            )

            counter.write_text("{ not json", encoding="utf-8")
            check(
                "a corrupt counter does not raise",
                refund_daily_trade_slot() is None,
            )

            counter.unlink()
            check(
                "a missing counter does not raise",
                refund_daily_trade_slot() is None,
            )

        finally:
            config.OPTIONS_RISK_STATE_FILE = real_path

    print()
    print("The exit order cannot price what the stop refused to trust")

    # The 2026-08-21 audit finding. current_exit_value returned None on a
    # crossed book; build_close_request clamped the same input to 0.01 and
    # sold. A time exit fires without a quote by design, so this could
    # have sent a spread worth real money out as a penny credit.
    def _q(bid, ask):
        return type("Q", (), {"bid_price": bid, "ask_price": ask,
                              "bid": bid, "ask": ask})()

    broken = OptionPosition(
        position_id="t", underlying="TEST", strategy="BULL_CALL_SPREAD",
        long_symbol="L", short_symbol="S", contracts=1, entry_debit=32.0,
        entry_time="2026-08-20T14:00:00+00:00", expiration="2026-09-11",
    )
    # A crossed book: the long bids BELOW what the short asks.
    crossed = {"L": _q(0.20, 0.24), "S": _q(0.30, 0.34)}

    check("a crossed book values as None, not as a number",
          current_exit_value(broken, crossed) is None)
    check("and per-share agrees with it",
          exit_value_per_share(broken, crossed) is None)

    try:
        build_close_request(broken, crossed)
        priced = True
    except ValueError:
        priced = False

    check("the ORDER refuses to price it too, instead of selling at 0.01",
          not priced)

    healthy = {"L": _q(0.60, 0.66), "S": _q(0.28, 0.32)}
    check("a healthy book still values",
          current_exit_value(healthy and broken, healthy) == 28.0,
          str(current_exit_value(broken, healthy)))

    req = build_close_request(broken, healthy)
    check("and the order prices at exactly that value per share",
          float(req.limit_price) == 0.28, str(req.limit_price))
    # Rounded on the way back up, because 0.28 * 100 is 28.000000000000004
    # in binary floating point -- the test's own arithmetic, not the
    # system's. The invariant being asserted is that one valuation feeds
    # both, and it does.
    check("so the decision and the order can never disagree",
          round(float(req.limit_price) * CONTRACT_MULTIPLIER, 2)
          == current_exit_value(broken, healthy))

    print("A cancel REQUEST is not a cancelled order")

    # LOCKBOT item 50d2f36d. The XLF spread was abandoned at 14:30:24 on a
    # cancel that had only been REQUESTED, and filled at 14:32:57.
    src = Path(__file__).read_text(encoding="utf-8")
    body = src.split("def _self_test")[0]

    check(
        "the cancel path re-classifies instead of assuming DEAD",
        "verdict = classify_entry_order(trading_client, position)" in body,
    )
    check(
        "a fill that beat the cancel is ADOPTED, not booked as unfilled",
        "THE FILL BEAT THE CANCEL" in body and "entry_filled = True" in body,
    )
    check(
        "an unconfirmed cancel holds the position rather than freeing it",
        "an unconfirmed cancel is not a dead order" in body,
    )
    check(
        "the adopted basis is re-derived from the ORDER, not the quote",
        "entry_debit_from_order(" in body and "_entry_order(" in body,
    )
    # Comments are stripped first: the fix's own comment quotes the line it
    # replaced, and a test that cannot tell code from a description of code
    # would fail on an accurate explanation.
    code_only = "\n".join(
        line for line in body.splitlines()
        if not line.lstrip().startswith("#")
    )
    check(
        "verdict is no longer hard-set to DEAD after a cancel call",
        'verdict = "DEAD"' not in code_only,
    )

    print()
    print("An untracked option position must reach a person")

    check(
        "it pages rather than only printing",
        "UNTRACKED_POSITION" in body and "send_smart_notification" in body,
    )
    check(
        "the alert says the position is unprotected",
        "no broker-side stop" in body and "Nothing will close them" in body,
    )
    check(
        "a failed alert is reported, never swallowed silently",
        "and the ALERT about it failed" in body,
    )

    print()
    print("An unknown order status is not evidence of death")

    class _Order:
        def __init__(self, status, filled_qty=0):
            self.status = status
            self.filled_qty = filled_qty

    class _Client:
        def __init__(self, order):
            self._order = order
        def get_order_by_id(self, _id):
            return self._order

    def _verdict(status, filled_qty=0):
        pos = OptionPosition(
            position_id="t", underlying="TEST", strategy="LONG_CALL",
            long_symbol="TEST260101C00010000", contracts=1, entry_debit=50.0,
            entry_time="2026-08-19T14:25:38+00:00", expiration="2026-09-11",
            entry_order_id="abc",
        )
        return classify_entry_order(_Client(_Order(status, filled_qty)), pos)

    # The 2026-08-19 XLF case: submitted 14:25:38, called DEAD at 14:30:24
    # on an unrecognised status, filled 14:32:57.
    for status in ("pending_review", "calculated", "suspended", "stopped",
                   "pending_cancel"):
        check(f"{status} holds its slot rather than being buried",
              _verdict(status) == "WORKING", _verdict(status))

    for status in ("canceled", "expired", "rejected", "done_for_day"):
        check(f"{status} is correctly DEAD", _verdict(status) == "DEAD",
              _verdict(status))

    check("filled is FILLED", _verdict("filled") == "FILLED")
    check("a status nobody has ever seen holds its slot",
          _verdict("some_future_alpaca_status") == "WORKING")
    check("a partial fill is a real position, whatever the status says",
          _verdict("pending_cancel", filled_qty=1) == "FILLED")
    check("no order id is still UNKNOWN, not DEAD",
          classify_entry_order(_Client(_Order("filled")),
                               OptionPosition(
                                   position_id="t", underlying="TEST",
                                   strategy="LONG_CALL",
                                   long_symbol="X", contracts=1,
                                   entry_debit=1.0,
                                   entry_time="2026-08-19T00:00:00+00:00",
                                   expiration="2026-09-11",
                               )) == "UNKNOWN")

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All options-manager checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    run_options_manager()
