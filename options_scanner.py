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
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
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

    premium_ceiling = account_equity * config.OPTIONS_MAX_TOTAL_PREMIUM_PERCENT

    if committed_premium >= premium_ceiling:
        return PortfolioGate(
            False,
            f"${committed_premium:.2f} of premium committed, at the "
            f"${premium_ceiling:.2f} ceiling.",
        )

    return PortfolioGate(True, "Room available.")


# ---------------------------------------------------------------------------
# Shadow log
# ---------------------------------------------------------------------------

def append_shadow_row(row: dict[str, Any]) -> None:
    """Append one decision to the shadow log, creating it when needed."""

    path = config.OPTIONS_SHADOW_FILE
    is_new = not path.exists()

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=SHADOW_COLUMNS,
            extrasaction="ignore",
        )

        if is_new:
            writer.writeheader()

        writer.writerow(row)


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


def entry_limit_price(price: float) -> float:
    """Price an entry limit a small buffer above the quoted ask.

    See build_open_request for the two unfilled PBR orders this exists
    for. A zero buffer restores the old at-the-touch behaviour exactly.
    """

    buffer_percent = getattr(
        config, "OPTIONS_ENTRY_LIMIT_BUFFER_PERCENT", 0.0
    )

    return round(price * (1.0 + buffer_percent), 2)


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

        # ---- Contract selection and entry
        entries_this_cycle = 0

        for candidate in candidates:
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
                limit_per_contract = entry_limit_price(long_quote.ask)
            else:
                limit_per_contract = entry_limit_price(
                    max(long_quote.ask - short_quote.bid, 0.01)
                )

            debit = limit_per_contract * CONTRACT_MULTIPLIER
            risk_at_stop = debit * config.OPTIONS_STOP_LOSS_PERCENT
            risk_ceiling = (
                account_equity * config.OPTIONS_MAX_RISK_PER_TRADE_PERCENT
            )

            print(
                f"  {long_quote.symbol} strike ${long_quote.strike:.2f}, "
                f"{long_quote.days_to_expiration}d, "
                f"spread {long_quote.spread_percent:.1%}"
                + (f", short {short_quote.symbol}" if short_quote else "")
            )
            print(f"  Debit ${debit:.2f}, risk at stop ${risk_at_stop:.2f}")

            if risk_at_stop > risk_ceiling:
                print(
                    f"  Rejected: the entry buffer lifts risk to "
                    f"${risk_at_stop:.2f}, above the ${risk_ceiling:.2f} "
                    "ceiling."
                )
                summary.rejection_reasons["BUFFERED_RISK_TOO_HIGH"] = (
                    summary.rejection_reasons.get("BUFFERED_RISK_TOO_HIGH", 0) + 1
                )
                continue

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
            }

            if config.OPTIONS_SHADOW_MODE:
                append_shadow_row({**shadow_row, "action": "SHADOW", "reason": "Shadow mode"})
                summary.shadow_logged += 1
                entries_this_cycle += 1
                print("  SHADOW MODE — logged, no order sent.")
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

    print("All options-scanner checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    run_options_scanner()
