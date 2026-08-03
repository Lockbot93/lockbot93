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
    exit_order_id: str | None = None
    exit_reason: str | None = None
    paper_trade: bool = True

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
            positions[position_id] = OptionPosition(**values)
        except TypeError as error:
            print(f"Skipping malformed position {position_id}: {error}")

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

    return value * CONTRACT_MULTIPLIER


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
        return ExitDecision(
            True,
            STOP_LOSS,
            f"Down {return_percent:.1%} against a -{stop_loss_percent:.0%} stop.",
            return_percent,
        )

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

    _, short_ask = short_quote
    net_credit = max(long_bid - short_ask, 0.01)

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

    if status in WORKING_ORDER_STATUSES:
        # A partial fill is a real position, not a working order, once any
        # quantity has been bought.
        try:
            if float(order.filled_qty or 0) > 0:
                return "FILLED"
        except (TypeError, ValueError):
            pass

        return "WORKING"

    return "DEAD"


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
    """

    price = getattr(order, "filled_avg_price", None)

    if price is None:
        return None

    try:
        amount = float(price) * CONTRACT_MULTIPLIER
    except (TypeError, ValueError):
        return None

    return amount


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

        # ---- Reconcile: anything we track that the broker no longer holds
        for position_id in list(positions):
            position = positions[position_id]

            if position.long_symbol in broker_positions:
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
                            f"the timeout. Cancelled {position.entry_order_id}."
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

                    verdict = "DEAD"

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

            # The position is gone. Journal it using the last known exit
            # value if we have one; otherwise record it with zero credit
            # and flag the reason, so it is never silently dropped.
            record_completed_option_trade(
                position,
                exit_credit=position.highest_value or 0.0,
                exit_time=now,
                exit_reason=position.exit_reason or "CLOSED_AT_BROKER",
            )

            print(
                f"{position.underlying}: position closed at the broker. "
                f"Journaled as {position.exit_reason or 'CLOSED_AT_BROKER'}."
            )

            del positions[position_id]
            summary.closed += 1

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

                if config.OPTIONS_SHADOW_MODE:
                    print(f"{label}: SHADOW MODE — no exit order was sent.")
                    position.exit_reason = decision.reason
                    continue

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
    print(f"Mode            : {'SHADOW' if config.OPTIONS_SHADOW_MODE else 'LIVE'}")
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

    loser = decide_exit(make(), 45.0, now=now, today=today, **rules)
    check("stops out at -36%", loser.should_exit and loser.reason == STOP_LOSS,
          loser.reason)

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

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All options-manager checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    run_options_manager()
