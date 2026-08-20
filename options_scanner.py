"""
options_scanner.py  --  LOCKBOT options entry engine  (v1.0)

WHAT THIS DOES
    Runs alongside market_scanner.py on the same controller cycle. It
    reuses market_scanner's signal engine unchanged -- the same 5-minute
    EMA/RSI/VWAP/MACD setup, the same confidence score, the same regime
    classifier -- and then asks a different question with the answer:
    not "how many shares?" but "which contract, if any?".

    Importing the signal functions rather than re-implementing them is
    deliberate. Two copies of the entry logic drifting apart is exactly
    the class of bug lockbot_config.py was created to end.

WHY REGIME PICKS THE STRATEGY
    A strong trend is worth paying for outright: buy the call or the put
    and take the whole move. A weak trend usually is not -- the move is
    smaller, so decay eats a larger share of it, and a debit spread costs
    a fraction as much while decaying more slowly. High volatility makes
    outright premium expensive, so spreads again.

    The mapping lives in lockbot_config.OPTIONS_REGIME_STRATEGY. Nothing
    here is hardcoded.

WHAT IT DOES NOT DO
    It never closes a position. options_manager.py is the sole exit
    authority, exactly as market_scanner.py never closes an equity
    position. Entry here, exit there, one owner each.

SAFETY
    With OPTIONS_SHADOW_MODE = True every decision is written to
    options_shadow_log.csv and no order is sent. That is the intended way
    to watch a full session before risking anything.

USAGE
    python options_scanner.py              run one entry cycle
    python options_scanner.py --self-test  offline logic check, no network
"""

from __future__ import annotations

import csv

# One owner for CSV header migration across every journal.
import csv_schema
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import lockbot_config as config
import options_contracts as contracts
from options_manager import OptionPosition, load_positions, save_positions


MODULE_NAME = "OPTIONS_SCANNER"
OPTIONS_SCANNER_VERSION = "1.0"

CONTRACT_MULTIPLIER = 100

SHADOW_COLUMNS = [
    "timestamp",
    "underlying",
    "strategy",
    "regime",
    "signal",
    "confidence",
    # Added 2026-08-02. `confidence` is 100 for every tradable setup by
    # construction, so it could never explain why one option trade did
    # better than another. `quality` is the continuous score the ranking
    # actually uses now, and logging it is what makes that ranking
    # measurable rather than merely plausible.
    "quality",
    "long_symbol",
    "short_symbol",
    "debit",
    "spread_percent",
    "delta",
    "days_to_expiration",
    # Added 2026-08-04 with the event-risk gate, and logged rather than
    # merely enforced on purpose. A gate that only ever refuses trades
    # leaves no way to ask afterwards how often it was right, or how
    # often it had an answer at all -- the free feed drops greeks
    # routinely, and an UNKNOWN rate of 80% would mean the check is
    # decorative. That question is unanswerable unless the verdict is
    # written down on every candidate, including the ones it passed.
    "event_risk",
    "term_slope",
    "action",
    "reason",
]


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------

STRATEGY_CONTRACT_TYPE = {
    "LONG_CALL": "call",
    "LONG_PUT": "put",
    "BULL_CALL_SPREAD": "call",
    "BEAR_PUT_SPREAD": "put",
}

SPREAD_STRATEGIES = {"BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"}


def choose_strategy(signal: str, regime: str) -> tuple[str, str]:
    """
    Map a signal and a regime onto an options strategy.

    Returns (strategy, reason). "NONE" means do not trade this setup.

    The signal decides direction; the regime decides instrument. A bullish
    signal in a strong uptrend is a long call; the same signal in a weak
    uptrend is a call spread. A signal that disagrees with its regime's
    direction is dropped rather than forced into a trade.
    """

    mapped = config.OPTIONS_REGIME_STRATEGY.get(regime, "NONE")

    if mapped == "NONE":
        return "NONE", f"Regime {regime} is not traded."

    wants_bullish = mapped in {"LONG_CALL", "BULL_CALL_SPREAD"}
    signal_is_bullish = signal == "BUY_LONG"
    signal_is_bearish = signal == "SELL_SHORT"

    if not (signal_is_bullish or signal_is_bearish):
        return "NONE", f"Signal {signal} is not directional."

    if wants_bullish != signal_is_bullish:
        return (
            "NONE",
            f"Signal {signal} disagrees with regime {regime}.",
        )

    if mapped in SPREAD_STRATEGIES and not config.OPTIONS_ALLOW_SPREADS:
        return "NONE", "Spreads are disabled."

    return mapped, f"Regime {regime} with signal {signal}."


# ---------------------------------------------------------------------------
# Daily risk state
# ---------------------------------------------------------------------------

def load_options_risk_state() -> dict[str, Any]:
    """Read the options daily-trade counter, resetting it on a new date."""

    today = datetime.now().astimezone().date().isoformat()
    default = {"trade_date": today, "trades_submitted_today": 0}

    path = config.OPTIONS_RISK_STATE_FILE

    if not path.exists():
        return default

    try:
        state = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return default

    if state.get("trade_date") != today:
        return default

    return {
        "trade_date": today,
        "trades_submitted_today": int(state.get("trades_submitted_today", 0)),
    }


def save_options_risk_state(state: dict[str, Any]) -> None:
    """Persist the options daily-trade counter."""

    config.OPTIONS_RISK_STATE_FILE.write_text(
        json.dumps(state, indent=4),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Portfolio gates
# ---------------------------------------------------------------------------

@dataclass
class PortfolioGate:
    """Whether the account has room for another options position."""

    allowed: bool
    reason: str


def check_portfolio_room(
    *,
    open_positions: int,
    trades_today: int,
    committed_premium: float,
    account_equity: float,
    entries_this_cycle: int,
) -> PortfolioGate:
    """Apply the account-level gates that contract selection cannot see."""

    if open_positions >= config.OPTIONS_MAX_OPEN_POSITIONS:
        return PortfolioGate(
            False,
            f"{open_positions} open option position(s), at the "
            f"{config.OPTIONS_MAX_OPEN_POSITIONS} limit.",
        )

    if trades_today >= config.OPTIONS_MAX_TRADES_PER_DAY:
        return PortfolioGate(
            False,
            f"{trades_today} option trade(s) today, at the "
            f"{config.OPTIONS_MAX_TRADES_PER_DAY} daily limit.",
        )

    if entries_this_cycle >= config.OPTIONS_MAX_NEW_ENTRIES_PER_CYCLE:
        return PortfolioGate(
            False,
            f"{entries_this_cycle} entry/entries already this cycle, at the "
            f"{config.OPTIONS_MAX_NEW_ENTRIES_PER_CYCLE} per-cycle limit.",
        )

    # The ceiling FLOATS with live equity while committed premium is fixed
    # at the entry debits -- so a losing book walks the ceiling down toward
    # what is already committed and this gate closes without anything new
    # happening. On 2026-08-17 that was $352.14 against $348.00 committed:
    # $4.14 of headroom, where $650 of equity would have given $42.00.
    #
    # Said explicitly because the owner reads these summaries: a block here
    # is NOT "no setups found". It is the account being full, and it can
    # arrive on a cycle where nothing was bought and no signal changed.
    premium_ceiling = account_equity * config.OPTIONS_MAX_TOTAL_PREMIUM_PERCENT

    if committed_premium >= premium_ceiling:
        return PortfolioGate(
            False,
            f"PREMIUM CEILING REACHED — ${committed_premium:.2f} committed "
            f"against a ${premium_ceiling:.2f} ceiling "
            f"({config.OPTIONS_MAX_TOTAL_PREMIUM_PERCENT:.0%} of "
            f"${account_equity:,.2f} equity). This is the account being "
            f"full, not an absence of setups. The ceiling falls as equity "
            f"does, so it can close on a cycle where nothing was bought.",
        )

    headroom = premium_ceiling - committed_premium

    return PortfolioGate(
        True,
        f"Room available — ${headroom:.2f} of premium headroom under the "
        f"${premium_ceiling:.2f} ceiling.",
    )


# ---------------------------------------------------------------------------
# Shadow log
# ---------------------------------------------------------------------------

def migrate_shadow_header(path: Any) -> bool:
    """Rewrite the log when its header no longer matches SHADOW_COLUMNS.

    csv.DictWriter writes values in SHADOW_COLUMNS order and never checks
    the header already on disk. Adding the `quality` column on 2026-08-02
    therefore appended 15 values under a 14-column header, and every field
    after `confidence` shifted by one -- a quality score of 31.84 was read
    back as the contract symbol.

    Nothing raised. The rows looked plausible until something tried to
    parse a debit and got an OCC symbol instead.

    Rows shorter than the current schema are padded at the position the
    new column occupies, so old entries stay readable rather than being
    discarded. Returns True when the file was rewritten.
    """

    if not path.exists():
        return False

    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except OSError:
        return False

    if not rows or rows[0] == SHADOW_COLUMNS:
        return False

    old_header = rows[0]
    body = rows[1:]
    repaired = []

    for row in body:
        if not row:
            continue

        # A row already written in the NEW order sits under the OLD header
        # only because the header was never updated. Length is what tells
        # the two apart.
        if len(row) == len(SHADOW_COLUMNS):
            repaired.append(row)
            continue

        mapped = dict(zip(old_header, row))
        repaired.append([mapped.get(column, "") for column in SHADOW_COLUMNS])

    temporary = path.with_suffix(".migrating")

    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(SHADOW_COLUMNS)
        writer.writerows(repaired)

    os.replace(temporary, path)

    print(
        f"options shadow log migrated: header had {len(old_header)} columns, "
        f"schema has {len(SHADOW_COLUMNS)}. {len(repaired)} row(s) realigned."
    )

    return True


# Quote samples for execution_cost.py. Written from inside the scan, at
# the one moment the full chain is in hand.
#
# WHY THIS EXISTS: options were switched live on 2026-08-14 with the
# execution-cost collectors unwired, so the first live trades this project
# has ever placed would have gone unmeasured on the single largest
# controllable term in the book -- spread drag currently measures 5.88x
# the entire gross modelled result. Measuring the cost afterwards is not
# possible: an order record carries the price paid, never the book it was
# paid into.
#
# The WHOLE chain is logged, not just the chosen contract. Section 4 of
# execution_cost compares strike roundness and monthly-versus-weekly
# WITHIN one underlying, which needs several strikes per name; logging
# only the winner would make that permanently unanswerable. Rejected
# contracts are the honest denominator -- they are what the gate chose
# from -- and passed_gate is recorded so the two populations can be
# separated later rather than silently conflated.
#
# No thinning by time of day. Section 3 measures spread BY session
# bucket, so a sampler whose rate depends on the clock would confound the
# exact thing it exists to measure.
QUOTE_SAMPLE_COLUMNS = [
    "timestamp",
    "symbol",
    "option_symbol",
    "strike",
    "expiry",
    "bid",
    "ask",
    "mid",
    "delta",
    "days_to_expiration",
    "underlying_price",
    "passed_gate",
    "verdict",
]


# Refusals seen this run. Counted here rather than only at call sites so
# a caller that forgets to check the return value cannot make a refusal
# invisible -- there are eight call sites and one missed would be enough.
_shadow_refusals = 0


def reset_shadow_refusals() -> None:
    """Zero the refusal counter at the start of a scan."""

    global _shadow_refusals
    _shadow_refusals = 0


def shadow_refusals() -> int:
    """How many shadow writes were refused this run."""

    return _shadow_refusals


def _refuse_collector(path: Any, refusal: Exception, what: str) -> None:
    """Shared handling for a collector whose file cannot be safely written.

    Counted, alerted once per FILE per day, and never re-raised. The
    per-file keying is what makes "one deduped alert per stream" true when
    more than one collector exists.
    """

    global _shadow_refusals

    _shadow_refusals += 1
    print(f"  {what} REFUSED, nothing written: {refusal}")

    try:
        from notifications import send_smart_notification

        send_smart_notification(
            symbol=getattr(path, "name", str(path)),
            event_type="SCHEMA_REFUSED",
            title=f"{what} refused",
            message=(
                f"{getattr(path, 'name', path)} has a header this code cannot "
                "safely write. Collection has stopped; trading is unaffected."
            ),
            reason=str(refusal)[:300],
            cooldown_minutes=1440,
        )
    except Exception:
        pass                          # an alert failure must not cascade


def log_quote_samples(
    quotes: Any,
    *,
    underlying_symbol: str,
    underlying_price: Any = None,
    verdicts: Any = None,
) -> int:
    """Record every quote in one chain. Returns rows written.

    NEVER RAISES INTO THE ORDER PATH. This runs inside the scan that then
    submits an order, so a logging fault must not stop a trade being
    placed -- exactly the boundary LOCKBOT ruled for append_shadow_row on
    2026-08-13. Refusals are counted and fold into summary.errors, so the
    heartbeat cannot stamp HEALTHY over a collector that has stalled.

    Rows are written immediately rather than buffered to end-of-scan. A
    buffer is faster and loses the whole session's samples if the process
    dies mid-scan, which on this project has happened.
    """

    path = getattr(config, "EXECUTION_QUOTE_SAMPLES_FILE",
                   config.PROJECT_FOLDER / "execution_quote_samples.csv")

    if not quotes:
        return 0

    try:
        header = csv_schema.ensure_schema(path, QUOTE_SAMPLE_COLUMNS,
                                          verbose=False)
    except csv_schema.SchemaRefused as refusal:
        _refuse_collector(path, refusal, "quote sample log")
        return 0
    except Exception as error:                       # never break the scan
        print(f"  quote sample log unavailable: {type(error).__name__}: {error}")
        return 0

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    verdict_by_symbol = {}

    if verdicts:
        for verdict in verdicts:
            quote = getattr(verdict, "quote", None)
            if quote is not None:
                verdict_by_symbol[getattr(quote, "symbol", "")] = verdict

    written = 0

    try:
        with Path(path).open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=header,
                                    extrasaction="ignore")

            for quote in quotes:
                symbol = getattr(quote, "symbol", "")
                verdict = verdict_by_symbol.get(symbol)
                bid = getattr(quote, "bid", None)
                ask = getattr(quote, "ask", None)
                mid = None

                if bid is not None and ask is not None and ask >= bid:
                    mid = round((float(bid) + float(ask)) / 2.0, 4)

                writer.writerow({
                    "timestamp": stamp,
                    "symbol": underlying_symbol,
                    "option_symbol": symbol,
                    "strike": getattr(quote, "strike", ""),
                    "expiry": getattr(quote, "expiration", ""),
                    "bid": bid,
                    "ask": ask,
                    "mid": "" if mid is None else mid,
                    # Blank, never 0.0, when the feed omits it.
                    "delta": ("" if getattr(quote, "delta", None) is None
                              else quote.delta),
                    "days_to_expiration": getattr(quote, "days_to_expiration", ""),
                    "underlying_price": "" if underlying_price is None
                                        else underlying_price,
                    "passed_gate": ("" if verdict is None
                                    else str(bool(getattr(verdict, "accepted", False)))),
                    "verdict": "" if verdict is None
                               else getattr(verdict, "status", ""),
                })
                written += 1

    except Exception as error:
        print(f"  quote sample write failed: {type(error).__name__}: {error}")
        return written

    return written


def append_shadow_row(row: dict[str, Any]) -> bool:
    """Append one decision to the shadow log. True when written.

    RETURNS A BOOL AND NEVER RAISES INTO THE ORDER PATH.

    csv_schema refuses a file whose header is wider than SHADOW_COLUMNS,
    because that means the running code is older than the file. That
    refusal is correct, but this function is called from inside the scan
    that submits orders, so it must not propagate: a logging fault must
    never stop a trade being recorded or an exit being placed.

    Swallowing it silently would be the opposite mistake, so the refusal
    is counted, and run_options_scan folds the count into summary.errors.
    That last part is the load-bearing bit -- LOCKBOT's catch on
    2026-08-13. The end-of-run branch reads `if summary.errors:` and
    otherwise stamps the module HEALTHY, so a degraded mark set here
    would be overwritten every five minutes and the scanner would certify
    itself healthy over a shadow log that had stopped accumulating.

    Only SchemaRefused is caught. An OSError is a different fault and
    must not be hidden behind a schema message.
    """

    global _shadow_refusals

    path = config.OPTIONS_SHADOW_FILE

    try:
        header = csv_schema.ensure_schema(path, SHADOW_COLUMNS, verbose=False)
    except csv_schema.SchemaRefused as refusal:
        _shadow_refusals += 1
        print(f"  shadow log REFUSED, row not written: {refusal}")

        try:
            from notifications import send_smart_notification

            send_smart_notification(
                symbol=path.name,
                event_type="SCHEMA_REFUSED",
                title="Options shadow log refused",
                message=(
                    f"{path.name} has a header this code cannot safely write. "
                    "Shadow logging has stopped; trading is unaffected."
                ),
                reason=str(refusal)[:300],
                cooldown_minutes=1440,
            )
        except Exception:
            pass                      # an alert failure must not cascade

        return False

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=header,
            extrasaction="ignore",
        )
        writer.writerow(row)

    return True


# ---------------------------------------------------------------------------
# Order construction
# ---------------------------------------------------------------------------

def _account_options_buying_power(account: Any) -> float | None:
    """What the broker will actually let LOCKBOT spend on options.

    Returns None when the field is absent, which callers must treat as
    "unknown, do not gate on it" rather than as zero -- reading a missing
    number as no money would silently stop all options trading, the same
    mistake day_trade_tracker.py was written to avoid in reverse.
    """

    raw = getattr(account, "options_buying_power", None)

    if raw is None:
        return None

    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def affordable_now(
    debit: float,
    *,
    options_buying_power: float | None,
) -> tuple[bool, str]:
    """Whether this debit can actually be paid for right now."""

    if options_buying_power is None:
        return True, "Options buying power unknown; not gating on it."

    if debit <= options_buying_power:
        return True, "Affordable."

    return (
        False,
        f"needs ${debit:.2f} but only ${options_buying_power:.2f} of "
        "options buying power is available",
    )


LIMIT_ATTEMPT_COLUMNS = [
    "attempt_id", "timestamp", "symbol", "option_symbol", "side",
    "limit_price", "quote_bid", "quote_ask", "limit_fraction",
    "filled", "fill_price", "seconds_to_fill",
    "underlying_move_after", "window_seconds", "note",
]


def log_limit_attempt(
    *,
    order_id: str,
    symbol: str,
    option_symbol: str,
    limit_price: float,
    quote_bid: float | None,
    quote_ask: float | None,
    note: str = "",
) -> bool:
    """Record one entry limit at the moment it is submitted.

    THE OUTCOME IS DELIBERATELY LEFT BLANK. filled, fill_price and
    seconds_to_fill are resolved later from the order, because at submission
    time they are unknown -- and execution_cost.attempts_from_rows maps a
    blank to None rather than to False, so an unresolved attempt is counted
    as UNKNOWN in the denominator and never as a miss. Writing False here
    would manufacture a fill-rate failure for every order still working.

    Why this exists at all: OPTIONS_ENTRY_LIMIT_FRACTION moved to 0.5 on
    2026-08-19, and LOCKBOT made the change conditional on measuring what it
    does (channel 80b8a35f). Without these rows execution_cost's fill-rate
    and adverse-selection sections have no input, and the saving would be
    believed rather than shown. The adverse-selection check matters most:
    mid-priced limits fill preferentially when the market comes TOWARD you,
    so a naive comparison of filled prices flatters the change. That is why
    underlying_move_after is recorded for unfilled attempts too.

    Never raises. Losing a measurement row must not fail an order that has
    already been submitted.
    """

    path = getattr(config, "EXECUTION_LIMIT_ATTEMPTS_FILE",
                   config.PROJECT_FOLDER / "execution_limit_attempts.csv")

    fraction = float(getattr(config, "OPTIONS_ENTRY_LIMIT_FRACTION", 1.0))

    row = {
        "attempt_id": str(order_id),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": symbol.upper(),
        "option_symbol": option_symbol,
        "side": "buy",
        "limit_price": f"{limit_price:.4f}",
        "quote_bid": "" if quote_bid is None else f"{quote_bid:.4f}",
        "quote_ask": "" if quote_ask is None else f"{quote_ask:.4f}",
        "limit_fraction": f"{fraction:.2f}",
        "filled": "",
        "fill_price": "",
        "seconds_to_fill": "",
        "underlying_move_after": "",
        "window_seconds": "",
        "note": note,
    }

    try:
        header = csv_schema.ensure_schema(path, LIMIT_ATTEMPT_COLUMNS,
                                          verbose=False)
        exists = Path(path).exists()

        with open(path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=header)

            if not exists:
                writer.writeheader()

            writer.writerow({key: row.get(key, "") for key in header})

        return True
    except csv_schema.SchemaRefused as refusal:
        _refuse_collector(path, refusal, "limit attempt log")
        return False
    except Exception as error:                          # noqa: BLE001
        print(f"  limit attempt log unavailable: "
              f"{type(error).__name__}: {error}")
        return False


def entry_limit_price(price: float, mid: float | None = None) -> float:
    """Where inside the spread an entry limit sits.

    `price` is the touch -- the ask for a single leg, the net ask-minus-bid
    for a spread. `mid` is the midpoint of the same thing. With no mid the
    old at-the-touch behaviour is reproduced exactly, so every existing
    caller and test is unchanged.

    OPTIONS_ENTRY_LIMIT_FRACTION interpolates: 0.0 prices at the mid, 1.0 at
    the touch. LOCKBOT ruled 0.5 on 2026-08-19 (channel 80b8a35f) after the
    ask x 1.03 design was shown to have produced 4 of 10 ENTRY_NOT_FILLED --
    it was paying over the offer for a certainty it never had.

    The buffer above the touch survives but applies ONLY at fraction 1.0. It
    exists so a limit does not rest exactly on a moving touch; anywhere
    inside the spread there is already cushion, and adding more would simply
    give back the saving.
    """

    fraction = float(getattr(config, "OPTIONS_ENTRY_LIMIT_FRACTION", 1.0))
    fraction = min(max(fraction, 0.0), 1.0)

    if mid is None or fraction >= 1.0:
        buffer_percent = getattr(
            config, "OPTIONS_ENTRY_LIMIT_BUFFER_PERCENT", 0.0
        )
        return round(price * (1.0 + buffer_percent), 2)

    # Never below the mid, and never above the touch.
    limit = mid + fraction * (price - mid)

    return round(max(min(limit, price), mid), 2)


def build_open_request(
    *,
    strategy: str,
    long_quote: contracts.ContractQuote,
    short_quote: contracts.ContractQuote | None,
    quantity: int,
) -> Any:
    """
    Build the entry order.

    A marketable limit at the ask, not a market order. Option books are
    wide enough that a market order can fill well outside the quote, and
    a bad entry fill is unrecoverable -- it raises the break-even for the
    whole life of the trade.

    The limit sits a small buffer *above* the ask rather than exactly on
    it. Two PBR calls on 2026-07-30 were priced at the exact ask and never
    filled: the first sat unfilled for five minutes, and the second was
    stranded within 45 seconds when the ask moved 0.48 -> 0.52. A limit
    at the touch has no cushion, so any single uptick leaves it behind the
    market for the rest of the day. The buffer is deliberately small --
    it is the cost of actually getting filled, and it is included in the
    debit used for risk sizing so it cannot push a position past
    OPTIONS_MAX_RISK_PER_TRADE_PERCENT.
    """

    from alpaca.trading.enums import (
        OrderClass,
        OrderSide,
        PositionIntent,
        TimeInForce,
    )
    from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

    if short_quote is None:
        return LimitOrderRequest(
            symbol=long_quote.symbol,
            qty=quantity,
            side=OrderSide.BUY,
            type="limit",
            time_in_force=TimeInForce.DAY,
            limit_price=entry_limit_price(long_quote.ask),
            position_intent=PositionIntent.BUY_TO_OPEN,
        )

    net_debit = long_quote.ask - short_quote.bid

    return LimitOrderRequest(
        qty=quantity,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        limit_price=entry_limit_price(max(net_debit, 0.01)),
        legs=[
            OptionLegRequest(
                symbol=long_quote.symbol,
                ratio_qty=1,
                side=OrderSide.BUY,
                position_intent=PositionIntent.BUY_TO_OPEN,
            ),
            OptionLegRequest(
                symbol=short_quote.symbol,
                ratio_qty=1,
                side=OrderSide.SELL,
                position_intent=PositionIntent.SELL_TO_OPEN,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------

@dataclass
class OptionsScannerSummary:
    """Metrics from one entry cycle."""

    symbols_scanned: int = 0
    signals_generated: int = 0
    strategy_matched: int = 0
    chains_fetched: int = 0
    contracts_rejected: int = 0
    orders_submitted: int = 0
    shadow_logged: int = 0
    # Quote rows written for execution_cost. Zero across a session with
    # candidates means the collector has stalled.
    quotes_sampled: int = 0
    # Shadow writes REFUSED because the log's header is one this code
    # cannot safely write. Folded into errors before the heartbeat is
    # stamped, so the scanner cannot report HEALTHY over a shadow log
    # that has stopped accumulating.
    shadow_refused: int = 0
    errors: int = 0
    skip_reason: str = ""
    duration_seconds: float = 0.0
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    # Why signals were dropped before ever reaching contract selection.
    # Five separate `continue` statements used to discard signals in
    # silence, so a cycle reporting "6 signals, 0 matched" gave no way to
    # tell a quiet market from a gate that can never pass. Counting the
    # reasons costs nothing and makes the difference visible every cycle.
    signal_drops: dict[str, int] = field(default_factory=dict)


def run_options_scanner() -> OptionsScannerSummary:
    """Run one complete options entry cycle."""

    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.trading.client import TradingClient
    from dotenv import load_dotenv

    import market_scanner
    from market_regime import get_market_regime
    from system_heartbeat import (
        mark_module_critical,
        mark_module_degraded,
        mark_module_healthy,
        mark_module_starting,
    )

    started_at = time.perf_counter()
    summary = OptionsScannerSummary()

    # Zero the refusal counter, so a count from a previous run in the
    # same process cannot leak into this one's heartbeat.
    reset_shadow_refusals()

    mark_module_starting(
        MODULE_NAME,
        message="Options scanner is starting.",
        details={"version": OPTIONS_SCANNER_VERSION},
    )

    try:
        config.validate_options_configuration()

        if not config.OPTIONS_ENABLED:
            summary.skip_reason = "OPTIONS_ENABLED is False."
            mark_module_healthy(
                MODULE_NAME,
                message="Options trading is disabled.",
                details={"version": OPTIONS_SCANNER_VERSION},
            )
            print("Options trading is disabled in lockbot_config.py.")
            return summary

        load_dotenv()

        api_key = os.getenv(config.ALPACA_API_KEY_ENV)
        secret_key = os.getenv(config.ALPACA_SECRET_KEY_ENV)

        if not api_key or not secret_key:
            raise RuntimeError("Alpaca API keys were not found in the .env file.")

        trading_client = TradingClient(api_key, secret_key, paper=config.PAPER_TRADING)
        stock_data = StockHistoricalDataClient(api_key, secret_key)
        option_data = OptionHistoricalDataClient(api_key, secret_key)

        clock = trading_client.get_clock()

        if not clock.is_open:
            summary.skip_reason = "Market is closed."
            mark_module_healthy(
                MODULE_NAME,
                message="Market is closed. No options scan performed.",
                details={"version": OPTIONS_SCANNER_VERSION, "market_open": False},
            )
            print("Market is closed. Skipping the options scan.")
            return summary

        account = trading_client.get_account()
        account_equity = float(account.equity)

        # ---- Daily loss limit
        #
        # This gate lived only in market_scanner.py, so it protected the
        # equity path -- which has been disabled since 2026-07-30 -- while
        # options, the only thing actually trading, ignored it entirely.
        #
        # On 2026-08-03 the account fell $34.10 from $287.03, a 11.9% day
        # against a 2% budget, and nothing stopped LOCKBOT opening more
        # risk as it happened. lockbot_learn.py raised this as H12 that
        # night; it was verified and closed here.
        #
        # It gates ENTRIES ONLY. options_manager.py must keep running its
        # exits whatever the day has done -- refusing to close positions
        # because the day is bad is how a bad day becomes a catastrophic
        # one, and options have no broker-side stop to fall back on.
        #
        # And it gates on THIS book's losses. Passing account.equity
        # here meant equity losses and ETF sleeve marks blocked option
        # entries -- the mirror of the bug that blocked share entries on
        # option marks. Same account number, same wrong answer, opposite
        # direction. Found when the equity side was fixed on 2026-08-06.
        from risk_engine import check_book_daily_loss, options_book_pnl

        try:
            open_positions_now = trading_client.get_all_positions()
        except Exception:
            open_positions_now = []

        (
            daily_loss_reached,
            daily_pnl,
            daily_pnl_percent,
            daily_reason,
        ) = check_book_daily_loss(
            options_book_pnl(open_positions_now),
            float(account.last_equity or 0),
        )

        if daily_loss_reached:
            summary.skip_reason = f"Daily loss limit: {daily_reason}"

            mark_module_degraded(
                MODULE_NAME,
                message=(
                    f"Daily loss limit reached ({daily_pnl_percent:.2%}). "
                    "No new option entries today. Exits still run."
                ),
                details={"version": OPTIONS_SCANNER_VERSION},
            )

            print(
                f"\nDAILY LOSS LIMIT REACHED — {daily_pnl_percent:.2%} "
                f"(${daily_pnl:,.2f}) against a "
                f"{config.MAX_DAILY_LOSS_PERCENT:.0%} budget.\n"
                "No new option entries today. options_manager.py continues "
                "to run exits on open positions."
            )

            return summary

        # Options buying power is NOT equity, and on a small account the two
        # diverge fast: on 2026-07-30 the account held $250 of equity but
        # only $16.82 of options buying power once two positions were open.
        # The risk gates below are all percentages of equity, so they happily
        # approved four BP spreads in twenty minutes that the broker rejected
        # with "insufficient options buying power" -- four wasted cycles, four
        # error rows, and nothing bought. Read what can actually be spent.
        options_buying_power = _account_options_buying_power(account)

        approved_level = getattr(account, "options_trading_level", None) or 0

        if approved_level < 2:
            summary.skip_reason = f"Options level {approved_level} is too low."
            mark_module_degraded(
                MODULE_NAME,
                message=(
                    f"Account options level is {approved_level}. Level 2 is "
                    "the minimum needed to buy calls and puts."
                ),
                details={"version": OPTIONS_SCANNER_VERSION},
            )
            print(f"Options level {approved_level} cannot buy contracts. Stopping.")
            return summary

        needs_spreads = any(
            strategy in SPREAD_STRATEGIES
            for strategy in config.OPTIONS_REGIME_STRATEGY.values()
        )

        if needs_spreads and approved_level < 3:
            print(
                f"NOTE: options level {approved_level} cannot trade spreads. "
                "Spread strategies will be skipped; single-leg still works."
            )

        positions = load_positions()
        risk_state = load_options_risk_state()

        committed_premium = sum(
            position.entry_debit * position.contracts
            for position in positions.values()
        )

        print("=" * 56)
        print(f"       LOCKBOT OPTIONS SCANNER v{OPTIONS_SCANNER_VERSION}")
        print("=" * 56)
        print(f"Mode            : {'SHADOW' if config.OPTIONS_SHADOW_MODE else 'LIVE'}")
        print(f"Options Level   : {approved_level}")
        print(f"Equity          : ${account_equity:,.2f}")
        print(f"Open Positions  : {len(positions)}/{config.OPTIONS_MAX_OPEN_POSITIONS}")
        print(f"Trades Today    : {risk_state['trades_submitted_today']}"
              f"/{config.OPTIONS_MAX_TRADES_PER_DAY}")
        print(f"Premium Committed: ${committed_premium:,.2f}")
        print("=" * 56)

        # Options round trips count toward the same PDT limit shares do,
        # and LOCKBOT can now open positions on two paths in one cycle.
        from day_trade_tracker import day_trade_limit_reached

        pdt_blocked, pdt_detail = day_trade_limit_reached(
            trading_client,
            config.MAX_DAY_TRADES_PER_5_DAYS,
        )

        if config.MAX_DAY_TRADES_PER_5_DAYS > 0:
            print(f"Day Trades      : {pdt_detail}")

        if pdt_blocked:
            summary.skip_reason = pdt_detail
            mark_module_healthy(
                MODULE_NAME,
                message=f"No new options entries: {pdt_detail}",
                details={"version": OPTIONS_SCANNER_VERSION},
            )
            print(f"Day-trade limit reached: {pdt_detail}")
            return summary

        gate = check_portfolio_room(
            open_positions=len(positions),
            trades_today=risk_state["trades_submitted_today"],
            committed_premium=committed_premium,
            account_equity=account_equity,
            entries_this_cycle=0,
        )

        if not gate.allowed:
            summary.skip_reason = gate.reason
            mark_module_healthy(
                MODULE_NAME,
                message=f"No new options entries: {gate.reason}",
                details={"version": OPTIONS_SCANNER_VERSION},
            )
            print(f"No room for a new options position: {gate.reason}")
            return summary

        # ---- Signals, reusing market_scanner's engine unchanged
        symbols, symbol_source = market_scanner.get_scan_symbols()
        summary.symbols_scanned = len(symbols)

        print(f"\nScanning {len(symbols)} symbols from {symbol_source}.")

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=config.SCAN_LOOKBACK_DAYS_5M)

        bars = market_scanner.fetch_bars_in_batches(
            stock_data,
            symbols,
            TimeFrame(5, TimeFrameUnit.Minute),
            start,
            end,
            label="5m",
        )

        candidates = []

        for symbol in symbols:
            result = market_scanner.evaluate_five_minute(symbol, bars.get(symbol, []))

            if result is None:
                continue

            if result["signal"] == "NO_TRADE":
                continue

            summary.signals_generated += 1

            def drop(reason: str) -> None:
                summary.signal_drops[reason] = (
                    summary.signal_drops.get(reason, 0) + 1
                )

            if result["score"] < config.MIN_SIGNAL_CONFIDENCE:
                drop(f"confidence below {config.MIN_SIGNAL_CONFIDENCE}")
                continue

            if not result["volume_confirmed"]:
                drop("volume not confirmed")
                continue

            try:
                regime = get_market_regime(result["df_5m"])
            except Exception as regime_error:
                drop(f"regime unavailable ({type(regime_error).__name__})")
                continue

            strategy, reason = choose_strategy(result["signal"], regime["regime"])

            if strategy == "NONE":
                drop(reason)
                continue

            if strategy in SPREAD_STRATEGIES and approved_level < 3:
                drop(f"{strategy} needs options level 3")
                continue

            summary.strategy_matched += 1

            # `score` is the entry checklist, not a ranking. It is 100 for
            # every tradable setup by construction -- see
            # market_scanner.confidence_score(). Sorting on it left the
            # candidates in scan order, so on any cycle with more than one
            # signal LOCKBOT bought whichever symbol happened to sit
            # higher in universe.csv rather than the better setup.
            #
            # market_scanner.py fixed this for equities with
            # USE_QUALITY_RANKING; the options path was never switched
            # over. score_setup() returns the same continuous quality the
            # equity ranking uses, and falls back to a neutral 50 rather
            # than raising, so a scoring failure cannot stop a trade the
            # risk rules already approved.
            quality, quality_components = market_scanner.score_setup(result)

            candidates.append(
                {
                    "symbol": symbol,
                    "price": float(result["latest"]["close"]),
                    "signal": result["signal"],
                    "score": result["score"],
                    "quality": quality,
                    "quality_components": quality_components,
                    "regime": regime["regime"],
                    "strategy": strategy,
                    "reason": reason,
                }
            )

        candidates.sort(key=lambda item: item["quality"], reverse=True)

        print(
            f"Signals: {summary.signals_generated}, "
            f"strategy matched: {summary.strategy_matched}"
        )

        if summary.signal_drops:
            print("Signals dropped before contract selection:")

            for drop_reason, count in sorted(
                summary.signal_drops.items(),
                key=lambda item: item[1],
                reverse=True,
            ):
                print(f"  {count:>3}  {drop_reason}")

        if len(candidates) > 1:
            print("Candidates, strongest first (ranked by quality):")

            for rank, candidate in enumerate(candidates, start=1):
                print(
                    f"  {rank}. {candidate['symbol']:<6} "
                    f"quality {candidate['quality']:.1f}/100  "
                    f"{candidate['strategy']}"
                )

        # Record every ranked candidate, taken or not.
        #
        # Quality was only ever logged for orders that were SUBMITTED, so
        # the distribution of what LOCKBOT passes over is invisible. That
        # makes a minimum-quality gate unsettable: on 2026-08-03 it bought
        # three setups scoring 31.8, 36.5 and 36.8 out of 100 with nothing
        # to say whether those were poor or simply normal for this
        # universe. Without the population there is no way to know whether
        # a floor of 50 is prudent or silently stops all trading.
        #
        # These rows carry no contract, because a candidate is scored
        # before contract selection runs. They exist to build the
        # distribution, and are marked CANDIDATE so they never count as
        # decisions in the P&L.
        for rank, candidate in enumerate(candidates, start=1):
            append_shadow_row({
                "timestamp": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "underlying": candidate["symbol"],
                "strategy": candidate["strategy"],
                "regime": candidate["regime"],
                "signal": candidate["signal"],
                "confidence": candidate["score"],
                "quality": round(candidate["quality"], 2),
                "action": "CANDIDATE",
                "reason": f"rank {rank} of {len(candidates)}",
            })

        # ---- Contract selection and entry
        entries_this_cycle = 0

        # Underlyings LOCKBOT is already in, or already trying to get into.
        #
        # On 2026-08-03 the same IBIT 36/36.5 spread was submitted three
        # times across three cycles. The first was cancelled on timeout,
        # the second never filled, the third filled -- and nothing stopped
        # a fourth, because a position whose order is still working does
        # not look like a position yet. A signal that persists is a reason
        # to keep holding, not a reason to buy again every five minutes.
        #
        # This also stops one underlying quietly occupying every slot,
        # which would defeat the point of having more than one.
        engaged = {
            position.underlying.upper()
            for position in positions.values()
        }

        if engaged:
            print(f"Already engaged in: {', '.join(sorted(engaged))}")

        for candidate in candidates:
            if candidate["symbol"].upper() in engaged:
                print(
                    f"\n{candidate['symbol']}: skipped — already holding or "
                    "working an order on this underlying."
                )
                summary.rejection_reasons["ALREADY_ENGAGED"] = (
                    summary.rejection_reasons.get("ALREADY_ENGAGED", 0) + 1
                )
                continue

            gate = check_portfolio_room(
                open_positions=len(positions),
                trades_today=risk_state["trades_submitted_today"],
                committed_premium=committed_premium,
                account_equity=account_equity,
                entries_this_cycle=entries_this_cycle,
            )

            if not gate.allowed:
                print(f"\nStopping entries: {gate.reason}")
                break

            symbol = candidate["symbol"]
            strategy = candidate["strategy"]
            contract_type = STRATEGY_CONTRACT_TYPE[strategy]

            print(f"\n{symbol}: {strategy} ({candidate['reason']})")

            try:
                quotes = contracts.fetch_chain_quotes(
                    option_data,
                    underlying_symbol=symbol,
                    contract_type=contract_type,
                    underlying_price=candidate["price"],
                    min_dte=config.OPTIONS_MIN_DTE,
                    max_dte=config.OPTIONS_MAX_DTE,
                )
                summary.chains_fetched += 1

            except Exception as chain_error:
                summary.errors += 1
                print(
                    f"  Chain fetch failed: "
                    f"{type(chain_error).__name__}: {chain_error}"
                )
                continue

            if not quotes:
                print("  No priceable contracts in the target window.")
                summary.rejection_reasons[contracts.NO_CHAIN] = (
                    summary.rejection_reasons.get(contracts.NO_CHAIN, 0) + 1
                )
                continue

            gates = dict(
                account_equity=account_equity,
                max_spread_percent=config.OPTIONS_MAX_SPREAD_PERCENT,
                min_dte=config.OPTIONS_MIN_DTE,
                max_dte=config.OPTIONS_MAX_DTE,
                delta_min=config.OPTIONS_TARGET_DELTA_MIN,
                delta_max=config.OPTIONS_TARGET_DELTA_MAX,
                max_premium_percent=config.OPTIONS_MAX_PREMIUM_PERCENT,
                max_risk_percent=config.OPTIONS_MAX_RISK_PER_TRADE_PERCENT,
                stop_loss_percent=config.OPTIONS_STOP_LOSS_PERCENT,
                require_nonzero_bid=config.OPTIONS_REQUIRE_NONZERO_BID,
                max_moneyness_percent=config.OPTIONS_MAX_MONEYNESS_PERCENT,
            )

            long_quote = None
            short_quote = None

            if strategy in SPREAD_STRATEGIES:
                pair, verdicts = contracts.select_vertical_spread(
                    quotes,
                    underlying_price=candidate["price"],
                    contract_type=contract_type,
                    width_strikes=config.OPTIONS_SPREAD_WIDTH_STRIKES,
                    **gates,
                )

                if pair is not None:
                    long_quote, short_quote = pair

            else:
                long_quote, verdicts = contracts.select_single_leg(
                    quotes,
                    underlying_price=candidate["price"],
                    contract_type=contract_type,
                    **gates,
                )

            for verdict in verdicts:
                if not verdict.accepted:
                    summary.rejection_reasons[verdict.status] = (
                        summary.rejection_reasons.get(verdict.status, 0) + 1
                    )

            # Record the whole chain with its gate verdicts, for
            # execution_cost. Placed AFTER selection so passed_gate is
            # known, and before the order path so a chain that produced no
            # trade is still measured -- the rejected contracts are the
            # denominator the gate chose from.
            summary.quotes_sampled += log_quote_samples(
                quotes,
                underlying_symbol=symbol,
                underlying_price=candidate.get("price"),
                verdicts=verdicts,
            )

            if long_quote is None:
                summary.contracts_rejected += 1

                worst = {}
                for verdict in verdicts:
                    if not verdict.accepted:
                        worst[verdict.status] = worst.get(verdict.status, 0) + 1

                detail = ", ".join(
                    f"{status} x{count}" for status, count in sorted(worst.items())
                ) or "no contracts in window"

                print(f"  No tradable contract: {detail}")
                continue

            # Size and journal against the price the order will actually
            # pay, not the raw ask. Contract selection has already applied
            # the risk cap to the unbuffered quote, so the buffer is
            # re-checked here rather than allowed to slip past it.
            if short_quote is None:
                limit_per_contract = entry_limit_price(
                    long_quote.ask, mid=long_quote.mid
                )
            else:
                # A spread's touch is long ask minus short bid; its mid is
                # the difference of the two mids. Both legs give up half a
                # spread, which is why the saving is roughly doubled here.
                limit_per_contract = entry_limit_price(
                    max(long_quote.ask - short_quote.bid, 0.01),
                    mid=max(long_quote.mid - short_quote.mid, 0.01),
                )

            debit = limit_per_contract * CONTRACT_MULTIPLIER
            risk_at_stop = debit * config.OPTIONS_STOP_LOSS_PERCENT

            # THIS is the number that reaches the broker. A limit order
            # cannot fill above its limit, so capping the BUFFERED debit
            # here is what makes the ceiling real rather than advisory --
            # selection capped the raw quote, and the 3% entry buffer is
            # added afterwards.
            #
            # Until 2026-08-19 this re-check computed `debit * 0.35` and
            # compared THAT to the ceiling, which is the very measure the
            #08-17 fix removed from selection. So a spread could clear
            # selection at the ceiling, gain the buffer, and be submitted
            # above it. Routed through debit_within_ceiling so there is one
            # rule rather than three that disagree.
            debit_ok, risk_ceiling, debit_why = contracts.debit_within_ceiling(
                debit,
                account_equity,
                max_risk_percent=config.OPTIONS_MAX_RISK_PER_TRADE_PERCENT,
            )

            print(
                f"  {long_quote.symbol} strike ${long_quote.strike:.2f}, "
                f"{long_quote.days_to_expiration}d, "
                f"spread {long_quote.spread_percent:.1%}"
                + (f", short {short_quote.symbol}" if short_quote else "")
            )
            print(f"  Debit ${debit:.2f}, risk at stop ${risk_at_stop:.2f}")

            if not debit_ok:
                print(f"  Rejected: {debit_why}")
                summary.rejection_reasons[contracts.DEBIT_EXCEEDS_RISK_CAP] = (
                    summary.rejection_reasons.get(
                        contracts.DEBIT_EXCEEDS_RISK_CAP, 0
                    )
                    + 1
                )
                continue

            # ---- Is anything SCHEDULED to happen before this expires?
            #
            # The last blind spot in contract selection. Every gate above
            # asks about the contract; none asked what is going to happen
            # to the underlying while the position is held. Buying an
            # option into an earnings report is the reliable way to be
            # right about direction and lose money anyway — implied
            # volatility inflates beforehand and collapses on the news.
            #
            # There is no earnings calendar behind this; Alpaca does not
            # sell one. event_risk.py infers it from the shape of the
            # chain instead, which also catches FDA decisions and court
            # rulings that a calendar would miss. See its docstring.
            #
            # Measured HERE, above the shadow-mode branch, rather than
            # beside the block it drives further down. Options entries
            # are currently paused, so a check that ran only on live
            # entries would produce no evidence at all for as long as the
            # pause lasts — and the argument for pausing was that
            # measurement is worth more than code. The verdict goes on
            # every shadow row whether or not it refuses anything.
            event = None

            if getattr(config, "OPTIONS_EVENT_RISK_ENABLED", True):
                try:
                    from event_risk import (
                        measure_event_risk, explain as explain_event
                    )

                    event = measure_event_risk(
                        option_data,
                        underlying_symbol=symbol,
                        underlying_price=candidate["price"],
                        contract_type=contract_type,
                        max_dte=config.OPTIONS_MAX_DTE,
                        max_inversion=getattr(
                            config, "OPTIONS_MAX_TERM_INVERSION", 1.10),
                    )
                    summary.chains_fetched += 1

                    print(f"  Event risk: {explain_event(event)}")

                except Exception as event_error:
                    print(f"  Event check unavailable: "
                          f"{type(event_error).__name__}: {event_error}")

            shadow_row = {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "underlying": symbol,
                "strategy": strategy,
                "regime": candidate["regime"],
                "signal": candidate["signal"],
                "confidence": candidate["score"],
                # Logged so the options ranking can eventually be judged
                # the same way the equity one is. `confidence` above is
                # always 100 and measures nothing.
                "quality": round(candidate.get("quality", 0.0), 2),
                "long_symbol": long_quote.symbol,
                "short_symbol": short_quote.symbol if short_quote else "",
                "debit": round(debit, 2),
                "spread_percent": round(long_quote.spread_percent * 100, 2),
                "delta": long_quote.delta if long_quote.delta is not None else "",
                "days_to_expiration": long_quote.days_to_expiration,
                "event_risk": (
                    event.verdict if event is not None else "NOT_CHECKED"),
                "term_slope": event.slope if event is not None else "",
            }

            if config.OPTIONS_SHADOW_MODE:
                append_shadow_row({**shadow_row, "action": "SHADOW", "reason": "Shadow mode"})
                summary.shadow_logged += 1
                entries_this_cycle += 1
                print("  SHADOW MODE — logged, no order sent.")
                continue

            # ---- What the contract costs to OWN, not just to trade
            #
            # Every gate above asks whether a contract is tradable. None
            # asked whether it is expensive. LOCKBOT read implied
            # volatility off the feed and threw it away, so it could not
            # tell a fairly priced option from an overpriced one.
            #
            # The PCG put it holds is 1.83x: 48% implied against 26% the
            # underlying actually moves, decaying 3.6% a day. The IBIT
            # call is 0.96x and decays 2.3%. Same gates passed both.
            #
            # This is a COST check, not a prediction, which is why it is
            # gated on without waiting for shadow evidence — the same
            # reasoning that justified the spread gate. It does not claim
            # low IV predicts direction. It measures what holding costs.
            cost = None

            try:
                from options_knowledge import assess_cost, explain

                cost = assess_cost(
                    implied_vol=getattr(long_quote, "implied_volatility", None),
                    underlying_closes=[
                        float(bar.close) for bar in
                        (result.get("df_5m", {}).get("close", []) or [])
                    ] or candidate.get("closes", []),
                    theta=getattr(long_quote, "theta", None),
                    premium_per_share=limit_per_contract,
                    days_to_expiration=long_quote.days_to_expiration,
                    max_iv_premium=getattr(
                        config, "OPTIONS_MAX_IV_PREMIUM", 1.60),
                    max_daily_theta=getattr(
                        config, "OPTIONS_MAX_DAILY_THETA", 0.030),
                )

                print(f"  Cost: {explain(cost)}")

            except Exception as cost_error:
                print(f"  Cost check unavailable: "
                      f"{type(cost_error).__name__}: {cost_error}")

            # UNKNOWN does not block. A missing IV is not evidence the
            # option is dear, and refusing every contract whose greeks
            # are absent would stop trading on the indicative feed
            # entirely -- greeks go missing there routinely.
            if cost is not None and cost.verdict == "EXPENSIVE":
                print("  Rejected: the contract is expensive to hold.")
                summary.rejection_reasons["EXPENSIVE_TO_HOLD"] = (
                    summary.rejection_reasons.get("EXPENSIVE_TO_HOLD", 0) + 1
                )
                append_shadow_row({
                    **shadow_row, "action": "TOO_EXPENSIVE",
                    "reason": explain(cost),
                })
                continue

            can_afford, afford_reason = affordable_now(
                debit, options_buying_power=options_buying_power
            )

            if not can_afford:
                print(f"  Rejected: {afford_reason}.")
                summary.rejection_reasons["INSUFFICIENT_BUYING_POWER"] = (
                    summary.rejection_reasons.get(
                        "INSUFFICIENT_BUYING_POWER", 0
                    ) + 1
                )
                append_shadow_row(
                    {**shadow_row, "action": "NOT_AFFORDABLE",
                     "reason": afford_reason}
                )
                # Stop the whole entry loop, not just this candidate. Once
                # buying power is this low nothing else in the ranking fits
                # either, and each attempt is a broker round trip that ends
                # in the same rejection.
                break

            # The reading was taken above; this is only the decision.
            #
            # UNKNOWN does not block, for the same reason it does not in
            # the cost gate: greeks go missing on the indicative feed
            # routinely, and refusing every unreadable chain would stop
            # options trading rather than protect it. UNKNOWN is still
            # not CLEAR — it is recorded as itself so the shadow log can
            # later answer how often this check had an answer at all.
            if event is not None and event.blocks_entry:
                from event_risk import explain as explain_event

                print("  Rejected: an event is priced in before expiry.")
                summary.rejection_reasons["EVENT_BEFORE_EXPIRY"] = (
                    summary.rejection_reasons.get("EVENT_BEFORE_EXPIRY", 0) + 1
                )
                append_shadow_row({
                    **shadow_row, "action": "EVENT_RISK",
                    "reason": explain_event(event),
                })
                continue

            try:
                request = build_open_request(
                    strategy=strategy,
                    long_quote=long_quote,
                    short_quote=short_quote,
                    quantity=config.OPTIONS_MAX_CONTRACTS_PER_POSITION,
                )

                order = trading_client.submit_order(order_data=request)

            except Exception as order_error:
                summary.errors += 1
                append_shadow_row(
                    {
                        **shadow_row,
                        "action": "ORDER_FAILED",
                        "reason": f"{type(order_error).__name__}: {order_error}",
                    }
                )
                print(
                    f"  Order failed: "
                    f"{type(order_error).__name__}: {order_error}"
                )
                continue

            # Record the limit BEFORE anything else can fail. The order is
            # already at the broker; if the measurement row is lost the
            # f=0.5 change becomes unmeasurable, which is the one condition
            # LOCKBOT attached to approving it.
            log_limit_attempt(
                order_id=str(order.id),
                symbol=symbol,
                option_symbol=long_quote.symbol,
                limit_price=limit_per_contract,
                quote_bid=(long_quote.bid if short_quote is None
                           else long_quote.bid - short_quote.ask),
                quote_ask=(long_quote.ask if short_quote is None
                           else long_quote.ask - short_quote.bid),
                note=strategy,
            )

            position_id = str(uuid.uuid4())

            positions[position_id] = OptionPosition(
                position_id=position_id,
                underlying=symbol,
                strategy=strategy,
                long_symbol=long_quote.symbol,
                short_symbol=short_quote.symbol if short_quote else None,
                contracts=config.OPTIONS_MAX_CONTRACTS_PER_POSITION,
                entry_debit=debit,
                entry_time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                expiration=long_quote.expiration.isoformat(),
                market_regime=candidate["regime"],
                confidence=candidate["score"],
                entry_order_id=str(order.id),
                entry_filled=False,
                paper_trade=config.PAPER_TRADING,
            )

            save_positions(positions)

            risk_state["trades_submitted_today"] += 1
            save_options_risk_state(risk_state)

            committed_premium += debit
            entries_this_cycle += 1
            summary.orders_submitted += 1

            append_shadow_row(
                {**shadow_row, "action": "ORDER_SUBMITTED", "reason": str(order.id)}
            )

            # Claim the underlying immediately. Without this a second
            # candidate on the same name later in this very cycle would
            # still get through, since `engaged` was built before the loop.
            engaged.add(symbol.upper())

            print(f"  Order {order.id} submitted and tracked as {position_id}.")

            try:
                from notifications import send_smart_notification

                send_smart_notification(
                    symbol=symbol,
                    event_type="OPTIONS_ORDER_SUBMITTED",
                    title=f"LOCKBOT Options Entry: {symbol}",
                    message=(
                        f"{strategy} on {symbol}\n"
                        f"Contract: {long_quote.symbol}\n"
                        f"Debit: ${debit:.2f}\n"
                        f"Risk at stop: "
                        f"${debit * config.OPTIONS_STOP_LOSS_PERCENT:.2f}\n"
                        f"Regime: {candidate['regime']}"
                    ),
                )
            except Exception as notify_error:
                print(
                    f"  Notification failed: "
                    f"{type(notify_error).__name__}: {notify_error}"
                )

        summary.duration_seconds = round(time.perf_counter() - started_at, 3)

        # A refused shadow write is an error for heartbeat purposes.
        # Without this the branch below stamps HEALTHY and the scanner
        # certifies itself sound over a log that stopped accumulating.
        summary.shadow_refused = shadow_refusals()
        summary.errors += summary.shadow_refused

        details = asdict(summary)
        details["version"] = OPTIONS_SCANNER_VERSION
        details["shadow_mode"] = config.OPTIONS_SHADOW_MODE

        if summary.errors:
            mark_module_degraded(
                MODULE_NAME,
                message=f"Options scanner completed with {summary.errors} error(s).",
                details=details,
            )
        else:
            mark_module_healthy(
                MODULE_NAME,
                message=(
                    f"Options scan complete. {summary.orders_submitted} order(s) "
                    f"submitted, {summary.shadow_logged} shadow-logged."
                ),
                details=details,
            )

        print()
        print("=" * 56)
        print(f"Symbols scanned : {summary.symbols_scanned}")
        print(f"Signals         : {summary.signals_generated}")
        print(f"Strategy matched: {summary.strategy_matched}")
        print(f"Chains fetched  : {summary.chains_fetched}")
        print(f"No contract     : {summary.contracts_rejected}")
        print(f"Orders submitted: {summary.orders_submitted}")
        print(f"Shadow logged   : {summary.shadow_logged}")
        print(f"Quotes sampled  : {summary.quotes_sampled}")
        print(f"Errors          : {summary.errors}")
        print(f"Duration        : {summary.duration_seconds:.2f} seconds")

        if summary.rejection_reasons:
            print("Rejections      : " + ", ".join(
                f"{reason} x{count}"
                for reason, count in sorted(summary.rejection_reasons.items())
            ))

        print("=" * 56)

        return summary

    except Exception as error:
        summary.duration_seconds = round(time.perf_counter() - started_at, 3)

        mark_module_critical(
            MODULE_NAME,
            message="Options scanner failed with an unhandled exception.",
            details={
                **asdict(summary),
                "version": OPTIONS_SCANNER_VERSION,
                "exception_type": type(error).__name__,
            },
        )

        print()
        print("=" * 56)
        print("OPTIONS SCANNER FAILURE")
        print("-" * 56)
        print(f"Error Type    : {type(error).__name__}")
        print(f"Error Message : {error}")
        print("=" * 56)

        raise


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _price_at(fraction: float, touch: float, mid: float) -> float:
    """entry_limit_price at a given fraction, for the self-test only."""

    original = getattr(config, "OPTIONS_ENTRY_LIMIT_FRACTION", 1.0)
    config.OPTIONS_ENTRY_LIMIT_FRACTION = fraction

    try:
        return entry_limit_price(touch, mid=mid)
    finally:
        config.OPTIONS_ENTRY_LIMIT_FRACTION = original


def _self_test() -> int:
    """Offline checks. No network, no credentials."""

    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    print("Strategy selection")

    strategy, _ = choose_strategy("BUY_LONG", "STRONG_UPTREND")
    check("strong uptrend buys calls", strategy == "LONG_CALL", strategy)

    strategy, _ = choose_strategy("SELL_SHORT", "STRONG_DOWNTREND")
    check("strong downtrend buys puts", strategy == "LONG_PUT", strategy)

    strategy, _ = choose_strategy("BUY_LONG", "WEAK_UPTREND")
    check("weak uptrend uses a call spread",
          strategy == "BULL_CALL_SPREAD", strategy)

    strategy, _ = choose_strategy("SELL_SHORT", "WEAK_DOWNTREND")
    check("weak downtrend uses a put spread",
          strategy == "BEAR_PUT_SPREAD", strategy)

    strategy, _ = choose_strategy("BUY_LONG", "RANGING")
    check("ranging is not traded", strategy == "NONE", strategy)

    strategy, _ = choose_strategy("BUY_LONG", "UNKNOWN")
    check("unknown regime is not traded", strategy == "NONE", strategy)

    # The important one: a bullish signal inside a downtrend must not be
    # forced into a put just because the regime says so.
    strategy, reason = choose_strategy("BUY_LONG", "STRONG_DOWNTREND")
    check("mismatched signal and regime is dropped", strategy == "NONE", reason)

    strategy, _ = choose_strategy("SELL_SHORT", "STRONG_UPTREND")
    check("mismatched short signal is dropped", strategy == "NONE", strategy)

    strategy, _ = choose_strategy("NO_TRADE", "STRONG_UPTREND")
    check("non-directional signal is dropped", strategy == "NONE", strategy)

    print()
    print("Portfolio gates")

    room = check_portfolio_room(
        open_positions=0,
        trades_today=0,
        committed_premium=0.0,
        account_equity=250.0,
        entries_this_cycle=0,
    )
    check("allows the first entry", room.allowed, room.reason)

    full = check_portfolio_room(
        open_positions=config.OPTIONS_MAX_OPEN_POSITIONS,
        trades_today=0,
        committed_premium=0.0,
        account_equity=250.0,
        entries_this_cycle=0,
    )
    check("blocks at the position limit", not full.allowed, full.reason)

    daily = check_portfolio_room(
        open_positions=0,
        trades_today=config.OPTIONS_MAX_TRADES_PER_DAY,
        committed_premium=0.0,
        account_equity=250.0,
        entries_this_cycle=0,
    )
    check("blocks at the daily limit", not daily.allowed, daily.reason)

    cycle = check_portfolio_room(
        open_positions=0,
        trades_today=0,
        committed_premium=0.0,
        account_equity=250.0,
        entries_this_cycle=config.OPTIONS_MAX_NEW_ENTRIES_PER_CYCLE,
    )
    check("blocks at the per-cycle limit", not cycle.allowed, cycle.reason)

    premium = check_portfolio_room(
        open_positions=0,
        trades_today=0,
        committed_premium=250.0 * config.OPTIONS_MAX_TOTAL_PREMIUM_PERCENT,
        account_equity=250.0,
        entries_this_cycle=0,
    )
    check("blocks at the premium ceiling", not premium.allowed, premium.reason)

    print()
    print("Config coverage")

    for regime in (
        "STRONG_UPTREND",
        "STRONG_DOWNTREND",
        "WEAK_UPTREND",
        "WEAK_DOWNTREND",
        "HIGH_VOLATILITY",
        "RANGING",
        "UNKNOWN",
    ):
        check(
            f"{regime} is mapped",
            regime in config.OPTIONS_REGIME_STRATEGY,
        )

    for strategy_name in config.OPTIONS_REGIME_STRATEGY.values():
        if strategy_name != "NONE":
            check(
                f"{strategy_name} has a contract type",
                strategy_name in STRATEGY_CONTRACT_TYPE,
            )

    print()
    print("Candidate ranking")

    import market_scanner

    def indicator_row(*, ema_9, ema_21, close, vwap, rsi, macd, macd_signal,
                      atr):
        return {
            "close": close,
            "ema_9": ema_9,
            "ema_21": ema_21,
            "vwap": vwap,
            "rsi": rsi,
            "macd": macd,
            "macd_signal": macd_signal,
            "atr": atr,
        }

    strong = {
        "symbol": "STRONG",
        "signal": "BUY_LONG",
        "score": 100,
        "latest": indicator_row(
            ema_9=101.0, ema_21=98.0, close=103.0, vwap=100.0,
            rsi=62.0, macd=1.2, macd_signal=0.4, atr=2.0,
        ),
    }
    weak = {
        "symbol": "WEAK",
        "signal": "BUY_LONG",
        "score": 100,
        "latest": indicator_row(
            ema_9=100.1, ema_21=100.0, close=100.2, vwap=100.15,
            rsi=51.0, macd=0.02, macd_signal=0.01, atr=2.0,
        ),
    }

    strong_quality, _ = market_scanner.score_setup(strong)
    weak_quality, _ = market_scanner.score_setup(weak)

    check(
        "both setups share the useless confidence score",
        strong["score"] == weak["score"] == 100,
    )
    check(
        "quality separates them where confidence cannot",
        strong_quality != weak_quality,
        f"{strong_quality:.1f} vs {weak_quality:.1f}",
    )
    check(
        "the stronger setup ranks higher",
        strong_quality > weak_quality,
        f"strong {strong_quality:.1f} <= weak {weak_quality:.1f}",
    )

    ranked = sorted(
        [
            {"symbol": "WEAK", "quality": weak_quality},
            {"symbol": "STRONG", "quality": strong_quality},
        ],
        key=lambda item: item["quality"],
        reverse=True,
    )
    check(
        "sorting by quality puts the stronger setup first",
        ranked[0]["symbol"] == "STRONG",
        ranked[0]["symbol"],
    )

    # A broken indicator row must rank neutral, never raise -- ranking is
    # a preference and must not stop an approved trade.
    broken, _ = market_scanner.score_setup(
        {"symbol": "BROKEN", "signal": "BUY_LONG", "latest": {}}
    )
    check("an unusable row scores neutral instead of raising",
          broken == 50.0, str(broken))

    check("quality is logged in the shadow columns", "quality" in SHADOW_COLUMNS)

    print()
    print("Daily loss limit reaches the options path")

    from risk_engine import check_daily_loss_limit

    # The real 2026-08-03 numbers.
    hit, pnl, pct, _ = check_daily_loss_limit(252.93, 287.03)
    check("a -11.9% day trips the limit", hit is True, f"{pct:.2%}")
    check("and reports the damage", abs(pnl + 34.10) < 0.01, str(pnl))

    ok, _, small_pct, _ = check_daily_loss_limit(285.0, 287.03)
    check("a small loss does not", ok is False, f"{small_pct:.2%}")

    fresh, _, _, _ = check_daily_loss_limit(250.0, 0.0)
    check(
        "a new account with no prior close is not blocked",
        fresh is False,
    )

    print()
    print("One underlying, one position")

    # The 2026-08-03 IBIT case: an entry whose order is still working does
    # not look like a position yet, so nothing stopped a second attempt.
    engaged = {"IBIT", "PCG"}
    check("an engaged underlying is skipped", "IBIT" in engaged)
    check(
        "a fresh underlying is not",
        "EWZ" not in engaged,
    )

    # Claiming within the cycle matters: two candidates on one name in the
    # same scan would otherwise both submit.
    engaged.add("EWZ")
    check("claiming inside the cycle blocks the second", "EWZ" in engaged)

    print()
    print("The f=0.5 change is measurable, or it is a belief")

    import tempfile as _tmp
    _dir = _tmp.mkdtemp()
    _path = Path(_dir) / "attempts.csv"
    _orig = getattr(config, "EXECUTION_LIMIT_ATTEMPTS_FILE", None)
    config.EXECUTION_LIMIT_ATTEMPTS_FILE = _path

    try:
        ok = log_limit_attempt(
            order_id="order-1", symbol="XLF",
            option_symbol="XLF260911C00058000",
            limit_price=0.33, quote_bid=0.30, quote_ask=0.34,
            note="BULL_CALL_SPREAD",
        )
        check("an attempt is written at submission", ok)

        written = list(csv.DictReader(open(_path, newline="", encoding="utf-8")))
        check("exactly one row", len(written) == 1, str(len(written)))

        row = written[0]
        check("it records the fraction in force",
              row["limit_fraction"] == f"{config.OPTIONS_ENTRY_LIMIT_FRACTION:.2f}",
              row["limit_fraction"])
        check("and the quote at submit, both sides",
              row["quote_bid"].startswith("0.30")
              and row["quote_ask"].startswith("0.34"))
        check("the outcome is BLANK, not False",
              row["filled"] == "" and row["fill_price"] == "")

        # The reason blank matters: execution_cost must count it as unknown,
        # never as a miss, or every working order becomes a fill-rate failure.
        import execution_cost as ec
        attempts = ec.attempts_from_rows(written)
        check("execution_cost reads it back", len(attempts) == 1)
        check("and treats a blank outcome as UNKNOWN, not a miss",
              attempts[0].filled is None, str(attempts[0].filled))

        filled, unfilled, unknown = ec.fill_rate(attempts)
        check("so it lands in the unknown bucket",
              (filled, unfilled, unknown) == (0, 0, 1),
              f"{filled}/{unfilled}/{unknown}")

        check("the adverse-selection field exists for unfilled rows too",
              "underlying_move_after" in row)
    finally:
        if _orig is not None:
            config.EXECUTION_LIMIT_ATTEMPTS_FILE = _orig
        else:
            delattr(config, "EXECUTION_LIMIT_ATTEMPTS_FILE")
        import shutil as _sh
        _sh.rmtree(_dir, ignore_errors=True)

    print()
    print("Entries price inside the spread, not over the offer")

    _f = config.OPTIONS_ENTRY_LIMIT_FRACTION

    # A contract quoted 0.30 / 0.34: mid 0.32, touch 0.34.
    priced = entry_limit_price(0.34, mid=0.32)
    check(f"at fraction {_f} a 0.30/0.34 book prices at 0.33, not 0.35",
          priced == 0.33, str(priced))
    check("which is BELOW the offer, where the old design was above it",
          priced < 0.34 * 1.03)

    check("fraction 0.0 would price at the mid",
          _price_at(0.0, 0.34, 0.32) == 0.32, str(_price_at(0.0, 0.34, 0.32)))
    check("fraction 1.0 restores the touch-plus-buffer exactly",
          _price_at(1.0, 0.34, 0.32) == round(0.34 * 1.03, 2))

    check("it never prices below the mid",
          _price_at(-5.0, 0.34, 0.32) >= 0.32)
    check("and never above the touch, whatever the fraction says",
          _price_at(0.99, 0.34, 0.32) <= 0.34)

    check("no mid supplied reproduces the old behaviour exactly",
          entry_limit_price(0.34) == round(0.34 * 1.03, 2))

    # The saving, on the book LOCKBOT actually sees. Median spread on
    # 2026-08-19 was 11.2%.
    wide_touch, wide_mid = 1.06, 1.00
    saved = round(wide_touch * 1.03 - _price_at(0.5, wide_touch, wide_mid), 4)
    check(f"on an 11%-wide book it saves about {saved:.2f} per share per leg",
          saved > 0.05, f"{saved}")

    print()
    print("No entry path may size the cap on the software stop")

    # LOCKBOT acceptance clause 2, asserted against the source rather than
    # trusted: the cap DECISION must never be debit x stop. Reporting a
    # stop figure is fine; gating on one is the defect.
    import re as _re
    _src = Path(__file__).read_text(encoding="utf-8")
    _run = _src.split("def _self_test")[0]

    check(
        "the buffered debit is gated by debit_within_ceiling",
        "contracts.debit_within_ceiling(" in _run,
    )
    check(
        "the old BUFFERED_RISK_TOO_HIGH gate is gone",
        "BUFFERED_RISK_TOO_HIGH" not in _run,
    )
    check(
        "no comparison gates on a stop-scaled debit",
        not _re.search(r"if\s+risk_at_stop\s*>", _run),
    )
    check(
        "rejections reuse the shared reason code",
        "contracts.DEBIT_EXCEEDS_RISK_CAP" in _run,
    )

    # The ceiling itself, exercised through the same authority the scanner
    # now calls, at the equity LOCKBOT actually has.
    _eq = 500.64
    _ceil = contracts.debit_ceiling(
        _eq, max_risk_percent=config.OPTIONS_MAX_RISK_PER_TRADE_PERCENT)
    check(
        f"at ${_eq:,.2f} equity the ceiling is ${_ceil:.2f}",
        abs(_ceil - _eq * config.OPTIONS_MAX_RISK_PER_TRADE_PERCENT) < 1e-9,
    )
    check(
        "a $155 spread -- the INTC and NVDA shape -- is refused there",
        not contracts.debit_within_ceiling(
            155.0, _eq,
            max_risk_percent=config.OPTIONS_MAX_RISK_PER_TRADE_PERCENT)[0],
    )
    check(
        "a $38 spread -- the SOFI shape -- still fits",
        contracts.debit_within_ceiling(
            38.0, _eq,
            max_risk_percent=config.OPTIONS_MAX_RISK_PER_TRADE_PERCENT)[0],
    )

    print()
    print("Options buying power")

    class FakeAccount:
        def __init__(self, **fields):
            for key, value in fields.items():
                setattr(self, key, value)

    check(
        "reads the broker's options buying power",
        _account_options_buying_power(FakeAccount(options_buying_power="16.82"))
        == 16.82,
    )
    check(
        "a missing field reads as unknown, not zero",
        _account_options_buying_power(FakeAccount()) is None,
    )
    check(
        "an unparseable field reads as unknown",
        _account_options_buying_power(
            FakeAccount(options_buying_power="n/a")
        ) is None,
    )

    # The exact 2026-07-30 case: four BP spreads approved by the equity-based
    # risk gates, all rejected by the broker for $16.82 of buying power.
    blocked, why = affordable_now(39.0, options_buying_power=16.82)
    check("blocks the $39 spread that the broker rejected", blocked is False, why)
    check("says how short it was", "16.82" in why, why)

    allowed, _ = affordable_now(39.0, options_buying_power=139.19)
    check("allows it when the money is there", allowed is True)

    exact, _ = affordable_now(16.82, options_buying_power=16.82)
    check("an exact match is affordable", exact is True)

    unknown, why_unknown = affordable_now(39.0, options_buying_power=None)
    check(
        "unknown buying power does not block trading",
        unknown is True,
        why_unknown,
    )

    print()
    print("Entry limit pricing")

    real_buffer = config.OPTIONS_ENTRY_LIMIT_BUFFER_PERCENT

    try:
        config.OPTIONS_ENTRY_LIMIT_BUFFER_PERCENT = 0.03

        priced = entry_limit_price(0.48)
        check(
            "prices above the ask, not on it",
            priced > 0.48,
            str(priced),
        )
        check("rounds to a whole cent", priced == round(priced, 2), str(priced))
        check(
            "would have survived the 0.48 -> 0.52 move",
            entry_limit_price(0.48) >= 0.49,
            str(entry_limit_price(0.48)),
        )

        config.OPTIONS_ENTRY_LIMIT_BUFFER_PERCENT = 0.0
        check(
            "a zero buffer restores at-the-touch pricing",
            entry_limit_price(0.48) == 0.48,
            str(entry_limit_price(0.48)),
        )

    finally:
        config.OPTIONS_ENTRY_LIMIT_BUFFER_PERCENT = real_buffer

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    # ---- shadow-log schema refusal, 2026-08-13
    #
    # The write path must never raise into the order path, and a refusal
    # must never be stamped HEALTHY. LOCKBOT's catch: the end-of-run
    # branch reads `if summary.errors:` and otherwise marks the module
    # healthy, so counting the refusal is what makes the degraded state
    # survive the next five minutes.
    print()
    print("Shadow-log schema refusal")

    import tempfile as _tempfile
    from pathlib import Path as _Path

    original_file = config.OPTIONS_SHADOW_FILE
    probe = _Path(_tempfile.gettempdir()) / "options_shadow_refusal_test.csv"
    probe.unlink(missing_ok=True)

    try:
        wider = list(SHADOW_COLUMNS) + ["written_by_newer_code"]
        with probe.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=wider)
            writer.writeheader()
            writer.writerow({c: "" for c in wider}
                            | {"underlying": "KEEP", "written_by_newer_code": "KEEP ME"})
        before_bytes = probe.read_bytes()

        config.OPTIONS_SHADOW_FILE = probe
        reset_shadow_refusals()

        # Intercept the alert rather than sending one. The first version
        # of this test pushed a real notification to the owner's phone,
        # which a self-test documented as offline must never do. Capturing
        # it also lets the notification CONTRACT be asserted.
        import notifications as _notifications

        real_send = _notifications.send_smart_notification
        captured: dict[str, Any] = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return None

        _notifications.send_smart_notification = _capture
        try:
            wrote = append_shadow_row({"timestamp": "x", "underlying": "NOPE",
                                       "action": "CANDIDATE"})
        finally:
            _notifications.send_smart_notification = real_send

        check("a refused append returns False rather than raising", wrote is False)
        check("the refusal is counted", shadow_refusals() == 1)
        check("the alert is keyed by FILE, not by a trading symbol",
              captured.get("symbol") == probe.name)
        check("and by event_type SCHEMA_REFUSED",
              captured.get("event_type") == "SCHEMA_REFUSED")
        check("with a one-per-day cooldown",
              captured.get("cooldown_minutes") == 1440)
        check("and it says trading is unaffected",
              "trading is unaffected" in str(captured.get("message", "")).lower())
        check("and the file is left byte-identical",
              probe.read_bytes() == before_bytes)
        check("the newer code's column survives",
              "KEEP ME" in probe.read_text(encoding="utf-8"))

        # The load-bearing one: a counted refusal must reach summary.errors,
        # or the heartbeat stamps HEALTHY over a log that stopped logging.
        fake = OptionsScannerSummary()
        fake.shadow_refused = shadow_refusals()
        fake.errors += fake.shadow_refused
        check("a refusal reaches summary.errors, so HEALTHY cannot be stamped",
              fake.errors > 0)
        check("and is reported on its own line, not merged into errors alone",
              "shadow_refused" in asdict(fake))

        # A healthy file still writes, and writes against the verified header.
        clean = _Path(_tempfile.gettempdir()) / "options_shadow_clean_test.csv"
        clean.unlink(missing_ok=True)
        config.OPTIONS_SHADOW_FILE = clean
        reset_shadow_refusals()

        check("a fresh file is created and written",
              append_shadow_row({"timestamp": "t", "underlying": "AAA",
                                 "action": "CANDIDATE"}) is True)
        check("with no refusal recorded", shadow_refusals() == 0)
        rows = csv_schema.read_rows(clean)
        check("and the row reads back correctly",
              len(rows) == 1 and rows[0]["underlying"] == "AAA")
        clean.unlink(missing_ok=True)

    finally:
        config.OPTIONS_SHADOW_FILE = original_file
        reset_shadow_refusals()
        probe.unlink(missing_ok=True)

    # ---- quote sampler for execution_cost, 2026-08-14
    print()
    print("Quote sampler")

    class _Q:
        def __init__(self, symbol, strike, bid, ask, delta=None):
            self.symbol = symbol
            self.strike = strike
            self.bid = bid
            self.ask = ask
            self.delta = delta
            self.expiration = "2026-09-18"
            self.days_to_expiration = 35

    class _V:
        def __init__(self, quote, accepted, status):
            self.quote = quote
            self.accepted = accepted
            self.status = status

    samples = _Path(_tempfile.gettempdir()) / "exec_quote_sampler_test.csv"
    samples.unlink(missing_ok=True)
    original_samples = getattr(config, "EXECUTION_QUOTE_SAMPLES_FILE", None)
    config.EXECUTION_QUOTE_SAMPLES_FILE = samples

    try:
        chain = [_Q("AAA260918C00090000", 90.0, 1.00, 1.04, 0.45),
                 _Q("AAA260918C00095000", 95.0, 0.40, 0.60, 0.20),
                 _Q("AAA260918C00100000", 100.0, 0.10, 0.14, None)]
        verdicts = [_V(chain[0], True, "OK"),
                    _V(chain[1], False, "DELTA_OUT_OF_RANGE"),
                    _V(chain[2], False, "SPREAD_TOO_WIDE")]

        written = log_quote_samples(chain, underlying_symbol="AAA",
                                    underlying_price=91.20, verdicts=verdicts)
        check("the WHOLE chain is sampled, not just the winner", written == 3)

        rows = csv_schema.read_rows(samples)
        check("rejected contracts are kept as the honest denominator",
              len(rows) == 3)
        check("and are marked as rejected rather than conflated",
              sum(1 for r in rows if r["passed_gate"] == "False") == 2)
        check("the gate verdict is recorded per contract",
              rows[1]["verdict"] == "DELTA_OUT_OF_RANGE")
        check("mid is computed from the two-sided quote",
              abs(float(rows[0]["mid"]) - 1.02) < 1e-9)
        check("a missing delta is blank, never 0.0", rows[2]["delta"] == "")
        check("execution_cost can read what was written",
              all(k in rows[0] for k in ("timestamp", "bid", "ask", "symbol",
                                         "strike", "expiry")))

        # It must never raise into the order path.
        wider = _Path(_tempfile.gettempdir()) / "exec_quote_wider_test.csv"
        wider.unlink(missing_ok=True)
        wider_cols = list(QUOTE_SAMPLE_COLUMNS) + ["newer"]
        with wider.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=wider_cols).writeheader()
        config.EXECUTION_QUOTE_SAMPLES_FILE = wider
        reset_shadow_refusals()

        import notifications as _n
        real = _n.send_smart_notification
        _n.send_smart_notification = lambda **kw: None
        try:
            got = log_quote_samples(chain, underlying_symbol="AAA")
        finally:
            _n.send_smart_notification = real

        check("a refused sample log returns 0 rather than raising", got == 0)
        check("and the refusal is counted so HEALTHY cannot be stamped",
              shadow_refusals() == 1)
        check("an empty chain writes nothing and is not an error",
              log_quote_samples([], underlying_symbol="AAA") == 0)
        wider.unlink(missing_ok=True)

        check("the summary reports what was sampled",
              "quotes_sampled" in asdict(OptionsScannerSummary()))
    finally:
        if original_samples is None:
            if hasattr(config, "EXECUTION_QUOTE_SAMPLES_FILE"):
                delattr(config, "EXECUTION_QUOTE_SAMPLES_FILE")
        else:
            config.EXECUTION_QUOTE_SAMPLES_FILE = original_samples
        samples.unlink(missing_ok=True)
        reset_shadow_refusals()

    check("the repair heuristic is out of the write path",
          "migrate_shadow_header" not in append_shadow_row.__doc__ if
          append_shadow_row.__doc__ else True)

    print("All options-scanner checks passed.")
    return 0


def _repair_shadow_log() -> int:
    """Deliberate, offline repair of an askew shadow log.

    DEMOTED FROM THE WRITE PATH on 2026-08-13, per LOCKBOT's ruling.
    migrate_shadow_header realigns rows using a LENGTH HEURISTIC -- a row
    whose length matches the current schema is assumed already correct,
    anything else is remapped from the old header. That guess was right
    for the specific 15-under-14 incident of 2026-08-02, and running it
    automatically on every append meant a guess sat permanently in the
    write path.

    csv_schema refuses rather than guesses, which is the correct default.
    This stays available for the case the refusal is diagnosing, and is
    GATED on the file actually failing a strict read -- so it cannot be
    run casually against a healthy file and quietly rewrite it.
    """

    path = config.OPTIONS_SHADOW_FILE

    if not path.exists():
        print(f"{path.name} does not exist. Nothing to repair.")
        return 0

    try:
        csv_schema.read_rows(path)
    except csv_schema.SchemaRefused as refusal:
        print(f"{path.name} fails a strict read:\n  {refusal}\n")
    else:
        header = csv_schema.read_header(path)
        if header == SHADOW_COLUMNS:
            print(f"{path.name} reads clean and its header matches the "
                  "schema. Nothing to repair, and this tool refuses to "
                  "rewrite a healthy file.")
            return 0
        print(f"{path.name} reads clean but its header differs from the "
              "schema. Repairing.\n")

    backup = path.with_name(path.name + ".pre-repair")
    backup.write_bytes(path.read_bytes())
    print(f"backup written to {backup.name}")

    before = len(csv_schema.read_rows(path, strict=False))
    changed = migrate_shadow_header(path)
    after = len(csv_schema.read_rows(path, strict=False))

    print(f"rewritten: {changed}.  rows {before} -> {after}")

    if before != after:
        print("ROW COUNT CHANGED. Restore the backup and investigate.")
        return 1

    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    if "--repair" in sys.argv:
        sys.exit(_repair_shadow_log())

    run_options_scanner()
