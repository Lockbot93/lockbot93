"""
LOCKBOT Risk Manager v0.2

Responsibilities:
- Decide whether LOCKBOT may open a new trade.
- Enforce account-level and trade-level risk limits.
- Block duplicate or conflicting positions.
- Enforce maximum daily trades and account exposure.
- Support an emergency kill switch.
- Track successful trade submissions by local calendar date.
- Return a clear approval or rejection reason.

This module does not submit, modify, replace, or cancel orders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import lockbot_config as config


# ============================================================
# Module information
# ============================================================

MODULE_NAME = "RISK_MANAGER"
RISK_MANAGER_VERSION = "0.2"


# ============================================================
# Project files
# ============================================================

PROJECT_FOLDER = Path(__file__).resolve().parent
RISK_STATE_FILE = config.RISK_STATE_FILE


# ============================================================
# Risk configuration
# Sourced from lockbot_config.py — the single source of truth.
# (v0.2 defined MAX_OPEN_POSITIONS=2 here while market_scanner.py
# separately enforced MAX_OPEN_TRADES=1; both now come from the same
# MAX_OPEN_POSITIONS value in lockbot_config.py.)
# ============================================================

MAX_OPEN_POSITIONS = config.MAX_OPEN_POSITIONS
MAX_TRADES_PER_DAY = config.MAX_TRADES_PER_DAY

MAX_POSITION_VALUE_PERCENT = config.MAX_POSITION_VALUE_PERCENT
MAX_TOTAL_EXPOSURE_PERCENT = config.MAX_TOTAL_EXPOSURE_PERCENT
MAX_RISK_PER_TRADE_PERCENT = config.MAX_RISK_PER_TRADE_PERCENT
MAX_DAILY_LOSS_PERCENT = config.MAX_DAILY_LOSS_PERCENT


# ============================================================
# Persisted risk state
# ============================================================

DEFAULT_RISK_STATE = {
    "kill_switch_active": False,
    "kill_switch_reason": "",
    "trade_date": "",
    "trades_submitted_today": 0,
}


# ============================================================
# Operational models
# ============================================================

@dataclass
class TradeRiskRequest:
    """Information required to evaluate a proposed trade."""

    symbol: str
    side: str
    quantity: float
    estimated_entry_price: float
    estimated_stop_price: float
    account_equity: float
    daily_profit_loss: float
    trades_today: int
    open_position_count: int
    total_open_exposure: float
    existing_position_quantity: float = 0.0
    duplicate_open_order_exists: bool = False


@dataclass
class RiskDecision:
    """Final Risk Manager decision."""

    approved: bool
    decision: str
    reason: str
    symbol: str
    side: str
    position_value: float
    estimated_risk_dollars: float
    estimated_risk_percent: float
    projected_exposure: float
    projected_exposure_percent: float
    risk_manager_version: str = RISK_MANAGER_VERSION


# ============================================================
# State management
# ============================================================

def initialize_risk_state() -> Path:
    """Create the risk-state file when it does not exist."""

    if RISK_STATE_FILE.exists():
        return RISK_STATE_FILE

    save_risk_state(DEFAULT_RISK_STATE.copy())

    return RISK_STATE_FILE


def load_risk_state() -> dict[str, Any]:
    """Load the persisted Risk Manager state."""

    initialize_risk_state()

    try:
        with RISK_STATE_FILE.open(
            mode="r",
            encoding="utf-8",
        ) as state_file:
            loaded_state = json.load(state_file)

    except (
        json.JSONDecodeError,
        OSError,
    ) as error:
        raise RuntimeError(
            f"Could not load {RISK_STATE_FILE.name}: {error}"
        ) from error

    if not isinstance(loaded_state, dict):
        raise RuntimeError(
            f"{RISK_STATE_FILE.name} must contain a JSON object."
        )

    state = DEFAULT_RISK_STATE.copy()
    state.update(loaded_state)

    return state


def save_risk_state(
    state: dict[str, Any],
) -> None:
    """Persist Risk Manager state safely."""

    temporary_file = RISK_STATE_FILE.with_suffix(".tmp")

    try:
        with temporary_file.open(
            mode="w",
            encoding="utf-8",
        ) as state_file:
            json.dump(
                state,
                state_file,
                indent=4,
            )

        temporary_file.replace(RISK_STATE_FILE)

    finally:
        try:
            if temporary_file.exists():
                temporary_file.unlink()

        except OSError:
            pass


def activate_kill_switch(
    reason: str,
) -> None:
    """Block all new trade entries."""

    state = load_risk_state()

    state["kill_switch_active"] = True
    state["kill_switch_reason"] = (
        reason.strip()
        or "Kill switch activated manually."
    )

    save_risk_state(state)


def deactivate_kill_switch() -> None:
    """Allow Risk Manager evaluations to resume."""

    state = load_risk_state()

    state["kill_switch_active"] = False
    state["kill_switch_reason"] = ""

    save_risk_state(state)


# ============================================================
# Daily trade counter
# ============================================================

def get_current_trade_date() -> str:
    """Return the current local calendar date."""

    return datetime.now().astimezone().date().isoformat()


def get_trades_submitted_today() -> int:
    """
    Return today's submitted-trade count.

    The counter resets automatically when the local calendar
    date changes.
    """

    state = load_risk_state()
    current_date = get_current_trade_date()

    if state.get("trade_date") != current_date:
        state["trade_date"] = current_date
        state["trades_submitted_today"] = 0
        save_risk_state(state)

    return max(
        int(
            state.get(
                "trades_submitted_today",
                0,
            )
        ),
        0,
    )


def record_trade_submission() -> int:
    """
    Record one successfully submitted trade.

    Call this only after the broker confirms that an order was
    submitted successfully.
    """

    state = load_risk_state()
    current_date = get_current_trade_date()

    if state.get("trade_date") != current_date:
        state["trade_date"] = current_date
        state["trades_submitted_today"] = 0

    current_count = max(
        int(
            state.get(
                "trades_submitted_today",
                0,
            )
        ),
        0,
    )

    updated_count = current_count + 1

    state["trades_submitted_today"] = updated_count

    save_risk_state(state)

    return updated_count


def reset_daily_trade_counter() -> None:
    """
    Reset today's submitted-trade counter manually.

    This should normally only be used for controlled testing.
    """

    state = load_risk_state()

    state["trade_date"] = get_current_trade_date()
    state["trades_submitted_today"] = 0

    save_risk_state(state)


# ============================================================
# General helpers
# ============================================================

def _normalize_symbol(
    symbol: str,
) -> str:
    """Normalize and validate a trading symbol."""

    normalized_symbol = symbol.strip().upper()

    if not normalized_symbol:
        raise ValueError(
            "symbol cannot be blank."
        )

    return normalized_symbol


def _normalize_side(
    side: str,
) -> str:
    """Normalize and validate a trade side."""

    normalized_side = side.strip().upper()

    if normalized_side not in {
        "LONG",
        "SHORT",
    }:
        raise ValueError(
            "side must be LONG or SHORT."
        )

    return normalized_side


def _require_positive_number(
    value: float,
    field_name: str,
) -> float:
    """Validate a required positive numeric value."""

    result = float(value)

    if result <= 0:
        raise ValueError(
            f"{field_name} must be greater than zero."
        )

    return result


def _reject(
    *,
    request: TradeRiskRequest,
    reason: str,
    position_value: float,
    estimated_risk_dollars: float,
    estimated_risk_percent: float,
    projected_exposure: float,
    projected_exposure_percent: float,
) -> RiskDecision:
    """Build a rejected Risk Manager decision."""

    return RiskDecision(
        approved=False,
        decision="REJECTED",
        reason=reason,
        symbol=request.symbol,
        side=request.side,
        position_value=round(
            position_value,
            2,
        ),
        estimated_risk_dollars=round(
            estimated_risk_dollars,
            2,
        ),
        estimated_risk_percent=estimated_risk_percent,
        projected_exposure=round(
            projected_exposure,
            2,
        ),
        projected_exposure_percent=(
            projected_exposure_percent
        ),
    )


# ============================================================
# Risk evaluation
# ============================================================

def evaluate_trade_request(
    request: TradeRiskRequest,
) -> RiskDecision:
    """
    Approve or reject one proposed trade.

    Risk rules are evaluated before any brokerage order
    should be submitted.
    """

    request.symbol = _normalize_symbol(
        request.symbol
    )

    request.side = _normalize_side(
        request.side
    )

    request.quantity = _require_positive_number(
        request.quantity,
        "quantity",
    )

    request.estimated_entry_price = (
        _require_positive_number(
            request.estimated_entry_price,
            "estimated_entry_price",
        )
    )

    request.estimated_stop_price = (
        _require_positive_number(
            request.estimated_stop_price,
            "estimated_stop_price",
        )
    )

    request.account_equity = (
        _require_positive_number(
            request.account_equity,
            "account_equity",
        )
    )

    request.daily_profit_loss = float(
        request.daily_profit_loss
    )

    request.trades_today = int(
        request.trades_today
    )

    request.open_position_count = int(
        request.open_position_count
    )

    request.total_open_exposure = float(
        request.total_open_exposure
    )

    request.existing_position_quantity = float(
        request.existing_position_quantity
    )

    if request.trades_today < 0:
        raise ValueError(
            "trades_today cannot be negative."
        )

    if request.open_position_count < 0:
        raise ValueError(
            "open_position_count cannot be negative."
        )

    if request.total_open_exposure < 0:
        raise ValueError(
            "total_open_exposure cannot be negative."
        )

    position_value = (
        request.quantity
        * request.estimated_entry_price
    )

    estimated_risk_dollars = (
        abs(
            request.estimated_entry_price
            - request.estimated_stop_price
        )
        * request.quantity
    )

    estimated_risk_percent = (
        estimated_risk_dollars
        / request.account_equity
    )

    position_value_percent = (
        position_value
        / request.account_equity
    )

    projected_exposure = (
        request.total_open_exposure
        + position_value
    )

    projected_exposure_percent = (
        projected_exposure
        / request.account_equity
    )

    daily_loss_percent = 0.0

    if request.daily_profit_loss < 0:
        daily_loss_percent = (
            abs(request.daily_profit_loss)
            / request.account_equity
        )

    risk_state = load_risk_state()

    if risk_state["kill_switch_active"]:
        reason = (
            str(
                risk_state.get(
                    "kill_switch_reason",
                    "",
                )
            ).strip()
            or "Emergency kill switch is active."
        )

        return _reject(
            request=request,
            reason=reason,
            position_value=position_value,
            estimated_risk_dollars=estimated_risk_dollars,
            estimated_risk_percent=estimated_risk_percent,
            projected_exposure=projected_exposure,
            projected_exposure_percent=(
                projected_exposure_percent
            ),
        )

    if daily_loss_percent >= MAX_DAILY_LOSS_PERCENT:
        return _reject(
            request=request,
            reason=(
                "Daily loss limit reached. "
                f"Current daily loss is "
                f"{daily_loss_percent * 100:.2f}%."
            ),
            position_value=position_value,
            estimated_risk_dollars=estimated_risk_dollars,
            estimated_risk_percent=estimated_risk_percent,
            projected_exposure=projected_exposure,
            projected_exposure_percent=(
                projected_exposure_percent
            ),
        )

    if request.trades_today >= MAX_TRADES_PER_DAY:
        return _reject(
            request=request,
            reason=(
                "Maximum daily trade count reached. "
                f"Current count: {request.trades_today}. "
                f"Limit: {MAX_TRADES_PER_DAY}."
            ),
            position_value=position_value,
            estimated_risk_dollars=estimated_risk_dollars,
            estimated_risk_percent=estimated_risk_percent,
            projected_exposure=projected_exposure,
            projected_exposure_percent=(
                projected_exposure_percent
            ),
        )

    if request.open_position_count >= MAX_OPEN_POSITIONS:
        return _reject(
            request=request,
            reason=(
                "Maximum open-position count reached. "
                f"Current count: "
                f"{request.open_position_count}. "
                f"Limit: {MAX_OPEN_POSITIONS}."
            ),
            position_value=position_value,
            estimated_risk_dollars=estimated_risk_dollars,
            estimated_risk_percent=estimated_risk_percent,
            projected_exposure=projected_exposure,
            projected_exposure_percent=(
                projected_exposure_percent
            ),
        )

    if request.duplicate_open_order_exists:
        return _reject(
            request=request,
            reason=(
                "A duplicate open order already exists "
                f"for {request.symbol}."
            ),
            position_value=position_value,
            estimated_risk_dollars=estimated_risk_dollars,
            estimated_risk_percent=estimated_risk_percent,
            projected_exposure=projected_exposure,
            projected_exposure_percent=(
                projected_exposure_percent
            ),
        )

    if request.existing_position_quantity != 0:
        existing_side = (
            "LONG"
            if request.existing_position_quantity > 0
            else "SHORT"
        )

        if existing_side == request.side:
            reason = (
                f"An existing {existing_side} position "
                f"already exists for {request.symbol}."
            )

        else:
            reason = (
                f"A conflicting {existing_side} position "
                f"already exists for {request.symbol}."
            )

        return _reject(
            request=request,
            reason=reason,
            position_value=position_value,
            estimated_risk_dollars=estimated_risk_dollars,
            estimated_risk_percent=estimated_risk_percent,
            projected_exposure=projected_exposure,
            projected_exposure_percent=(
                projected_exposure_percent
            ),
        )

    if position_value_percent > MAX_POSITION_VALUE_PERCENT:
        return _reject(
            request=request,
            reason=(
                "Proposed position exceeds the maximum "
                f"position allocation of "
                f"{MAX_POSITION_VALUE_PERCENT * 100:.2f}%."
            ),
            position_value=position_value,
            estimated_risk_dollars=estimated_risk_dollars,
            estimated_risk_percent=estimated_risk_percent,
            projected_exposure=projected_exposure,
            projected_exposure_percent=(
                projected_exposure_percent
            ),
        )

    if estimated_risk_percent > MAX_RISK_PER_TRADE_PERCENT:
        return _reject(
            request=request,
            reason=(
                "Estimated trade risk exceeds the maximum "
                f"risk-per-trade limit of "
                f"{MAX_RISK_PER_TRADE_PERCENT * 100:.2f}%."
            ),
            position_value=position_value,
            estimated_risk_dollars=estimated_risk_dollars,
            estimated_risk_percent=estimated_risk_percent,
            projected_exposure=projected_exposure,
            projected_exposure_percent=(
                projected_exposure_percent
            ),
        )

    if (
        projected_exposure_percent
        > MAX_TOTAL_EXPOSURE_PERCENT
    ):
        return _reject(
            request=request,
            reason=(
                "Projected account exposure exceeds the "
                f"maximum exposure limit of "
                f"{MAX_TOTAL_EXPOSURE_PERCENT * 100:.2f}%."
            ),
            position_value=position_value,
            estimated_risk_dollars=estimated_risk_dollars,
            estimated_risk_percent=estimated_risk_percent,
            projected_exposure=projected_exposure,
            projected_exposure_percent=(
                projected_exposure_percent
            ),
        )

    return RiskDecision(
        approved=True,
        decision="APPROVED",
        reason=(
            "Trade passed all configured risk checks."
        ),
        symbol=request.symbol,
        side=request.side,
        position_value=round(
            position_value,
            2,
        ),
        estimated_risk_dollars=round(
            estimated_risk_dollars,
            2,
        ),
        estimated_risk_percent=estimated_risk_percent,
        projected_exposure=round(
            projected_exposure,
            2,
        ),
        projected_exposure_percent=(
            projected_exposure_percent
        ),
    )


# ============================================================
# Reporting
# ============================================================

def print_risk_decision(
    decision: RiskDecision,
) -> None:
    """Display one Risk Manager decision."""

    print("=" * 58)

    print(
        f"              LOCKBOT RISK MANAGER "
        f"v{RISK_MANAGER_VERSION}"
    )

    print("=" * 58)
    print(f"Decision       : {decision.decision}")
    print(f"Symbol         : {decision.symbol}")
    print(f"Side           : {decision.side}")
    print(f"Reason         : {decision.reason}")

    print(
        f"Position Value : "
        f"${decision.position_value:,.2f}"
    )

    print(
        f"Estimated Risk : "
        f"${decision.estimated_risk_dollars:,.2f}"
    )

    print(
        f"Risk %         : "
        f"{decision.estimated_risk_percent * 100:.4f}%"
    )

    print(
        f"Exposure       : "
        f"${decision.projected_exposure:,.2f}"
    )

    print(
        f"Exposure %     : "
        f"{decision.projected_exposure_percent * 100:.2f}%"
    )

    print("=" * 58)


def print_risk_state() -> None:
    """Display the current persisted Risk Manager state."""

    state = load_risk_state()

    print("=" * 58)
    print("              LOCKBOT RISK STATE")
    print("=" * 58)

    print(
        f"Kill Switch    : "
        f"{state['kill_switch_active']}"
    )

    print(
        f"Kill Reason    : "
        f"{state['kill_switch_reason'] or 'NONE'}"
    )

    print(
        f"Trade Date     : "
        f"{state.get('trade_date') or 'NOT SET'}"
    )

    print(
        f"Trades Today   : "
        f"{get_trades_submitted_today()}"
    )

    print(
        f"Daily Limit    : "
        f"{MAX_TRADES_PER_DAY}"
    )

    print("=" * 58)


# ============================================================
# Self-test
# ============================================================

def run_self_test() -> None:
    """Run one safe local Risk Manager test."""

    trades_today = get_trades_submitted_today()

    test_request = TradeRiskRequest(
        symbol="SPY",
        side="LONG",
        quantity=1,
        estimated_entry_price=750.00,
        estimated_stop_price=746.25,
        account_equity=100000.00,
        daily_profit_loss=0.00,
        trades_today=trades_today,
        open_position_count=0,
        total_open_exposure=0.00,
        existing_position_quantity=0.00,
        duplicate_open_order_exists=False,
    )

    decision = evaluate_trade_request(
        test_request
    )

    print_risk_decision(decision)

    print()
    print("Decision Data")

    print(
        json.dumps(
            asdict(decision),
            indent=4,
        )
    )

    print()
    print_risk_state()


def _self_test() -> int:
    """Offline checks. Every gate that stands between a signal and an order.

    The existing run_self_test() is a DEMO: it evaluates one request and
    prints it, against the real risk_state.json. That is useful and is
    not a test -- it asserts nothing and it mutates live state, which is
    why this module counted as untested while gating every entry.

    Everything here runs against a temporary state file. A test that can
    trip the real kill switch is worse than no test.
    """

    import tempfile

    global RISK_STATE_FILE

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    real_state = RISK_STATE_FILE
    RISK_STATE_FILE = Path(tempfile.mkdtemp()) / "risk_state.json"

    def request(**overrides):
        """A request that passes every gate unless overridden."""
        base = dict(
            symbol="TEST",
            # LONG/SHORT here, NOT the scanner's BUY_LONG/SELL_SHORT.
            # market_scanner translates at the call site; see the
            # contract check below, which exists so that translation
            # cannot be quietly dropped.
            side="LONG",
            quantity=1,
            estimated_entry_price=10.00,
            estimated_stop_price=9.90,      # 1% stop on a $10 position
            account_equity=10_000.00,
            daily_profit_loss=0.00,
            trades_today=0,
            open_position_count=0,
            total_open_exposure=0.00,
            existing_position_quantity=0.00,
            duplicate_open_order_exists=False,
        )
        base.update(overrides)
        return TradeRiskRequest(**base)

    try:
        print("A clean request is approved")

        clean = evaluate_trade_request(request())
        check("a request breaching nothing is approved",
              clean.approved is True, clean.reason)
        check("and says so", clean.decision == "APPROVED")
        check("position value is computed", clean.position_value == 10.00,
              str(clean.position_value))
        check("risk in dollars is entry minus stop, times quantity",
              abs(clean.estimated_risk_dollars - 0.10) < 1e-9,
              str(clean.estimated_risk_dollars))

        print()
        print("Every gate rejects")

        cases = [
            ("too many open positions",
             dict(open_position_count=MAX_OPEN_POSITIONS), "open-position"),
            ("too many trades today",
             dict(trades_today=MAX_TRADES_PER_DAY), "daily trade count"),
            ("a duplicate order exists",
             dict(duplicate_open_order_exists=True), "duplicate"),
            ("a position already exists on the same side",
             dict(existing_position_quantity=5), "existing"),
            ("a conflicting position exists",
             dict(existing_position_quantity=-5), "conflicting"),
            ("the position is too large",
             dict(quantity=10_000), "maximum"),
            ("the daily loss limit is hit",
             dict(daily_profit_loss=-10_000 * MAX_DAILY_LOSS_PERCENT),
             "Daily loss"),
            ("the stop is too far away",
             dict(estimated_stop_price=0.01, quantity=100), "risk"),
        ]

        for name, overrides, expected in cases:
            decision = evaluate_trade_request(request(**overrides))
            check(name, decision.approved is False, decision.reason)
            check(f"  and the reason names it ({expected})",
                  expected.lower() in decision.reason.lower(),
                  decision.reason)

        # Exposure is cumulative, so a small trade on top of a nearly
        # full book must still be refused.
        crowded = evaluate_trade_request(request(
            total_open_exposure=10_000 * MAX_TOTAL_EXPOSURE_PERCENT))
        check("projected exposure counts what is already open",
              crowded.approved is False, crowded.reason)

        print()
        print("The boundary is not off by one")

        at_limit = evaluate_trade_request(
            request(open_position_count=MAX_OPEN_POSITIONS - 1))
        check("one slot below the cap is allowed",
              at_limit.approved is True, at_limit.reason)

        one_below = evaluate_trade_request(
            request(trades_today=MAX_TRADES_PER_DAY - 1))
        check("one trade below the daily cap is allowed",
              one_below.approved is True, one_below.reason)

        print()
        print("The kill switch overrides everything")

        activate_kill_switch("testing")
        killed = evaluate_trade_request(request())
        check("an otherwise clean trade is refused",
              killed.approved is False, killed.reason)
        check("and the reason is the kill switch",
              "testing" in killed.reason, killed.reason)

        deactivate_kill_switch()
        check("deactivating restores trading",
              evaluate_trade_request(request()).approved is True)

        print()
        print("The side vocabulary differs from the scanner's, deliberately")

        # This module speaks LONG/SHORT; market_scanner speaks
        # BUY_LONG/SELL_SHORT and converts at the call site. Asserting
        # both halves means the conversion cannot be dropped without a
        # test failing rather than an entry being silently refused.
        try:
            evaluate_trade_request(request(side="BUY_LONG"))
            check("the scanner's vocabulary is rejected here", False,
                  "BUY_LONG was accepted")
        except ValueError:
            check("the scanner's vocabulary is rejected here", True)

        check("LONG is accepted",
              evaluate_trade_request(request(side="LONG")).approved is True)
        check("SHORT is accepted",
              evaluate_trade_request(
                  request(side="SHORT")).approved is True)
        check("and case does not matter",
              evaluate_trade_request(request(side="long")).approved is True)

        scanner = Path("market_scanner.py").read_text(
            encoding="utf-8", errors="ignore")
        check("market_scanner still performs the translation",
              '"LONG" if is_long else "SHORT"' in scanner)

        print()
        print("Bad input is refused, not guessed at")

        for name, overrides in (
            ("zero quantity", dict(quantity=0)),
            ("negative quantity", dict(quantity=-1)),
            ("zero entry price", dict(estimated_entry_price=0)),
            ("zero equity", dict(account_equity=0)),
            ("negative trades_today", dict(trades_today=-1)),
            ("negative open positions", dict(open_position_count=-1)),
            ("negative exposure", dict(total_open_exposure=-1)),
        ):
            try:
                evaluate_trade_request(request(**overrides))
                check(f"{name} raises", False, "it was accepted")
            except (ValueError, TypeError):
                check(f"{name} raises", True)

        print()
        print("Limits come from lockbot_config, not from here")

        check("open positions", MAX_OPEN_POSITIONS == config.MAX_OPEN_POSITIONS)
        check("trades per day", MAX_TRADES_PER_DAY == config.MAX_TRADES_PER_DAY)
        check("risk per trade",
              MAX_RISK_PER_TRADE_PERCENT == config.MAX_RISK_PER_TRADE_PERCENT)
        check("daily loss", MAX_DAILY_LOSS_PERCENT == config.MAX_DAILY_LOSS_PERCENT)
        check("exposure",
              MAX_TOTAL_EXPOSURE_PERCENT == config.MAX_TOTAL_EXPOSURE_PERCENT)

        print()
        print("State survives a round trip, and corruption does not crash it")

        reset_daily_trade_counter()
        first = record_trade_submission()
        second = record_trade_submission()
        check("submissions increment", second == first + 1,
              f"{first} then {second}")

        # A corrupt state file RAISES here, and that is correct.
        #
        # Falling back to defaults would set kill_switch_active to False
        # and re-enable trading, so a damaged file would quietly undo an
        # emergency stop. Refusing to load means refusing to trade,
        # which is the safe direction for this module.
        #
        # Note that options_manager.py deliberately chose the OPPOSITE
        # for its own state: an unreadable file there continues with no
        # tracked positions, because failing to run the stop loss is
        # worse than losing the entry price. Different files, different
        # safe directions, both on purpose.
        RISK_STATE_FILE.write_text("{ not json", encoding="utf-8")

        try:
            load_risk_state()
            check("a corrupt state file refuses to load", False,
                  "it returned defaults, which would clear the kill switch")
        except RuntimeError:
            check("a corrupt state file refuses to load", True)

        try:
            evaluate_trade_request(request())
            check("and no trade is evaluated against unreadable state",
                  False, "a decision was returned anyway")
        except RuntimeError:
            check("and no trade is evaluated against unreadable state", True)

    finally:
        RISK_STATE_FILE = real_state

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All risk-manager checks passed.")
    return 0


if __name__ == "__main__":
    import sys

    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    run_self_test()
