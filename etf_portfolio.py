"""
etf_portfolio.py — a buy-and-hold sleeve, deliberately unlike the rest.

WHAT THIS IS

A fixed allocation across a few ETFs, bought once and held, rebalanced
only when it drifts. No signals. No stops. No exits. No timing.

Everything else in LOCKBOT tries to pick moments. Over 119 resolved
setups that machinery converts 20.2% into winners against a 33.3%
breakeven, and produced zero winners on three of four days. This module
exists because holding a broad basket does not require picking anything,
and at this account size the alternative is paying spread to be wrong.

WHY IT DOES NOT SHARE THE TRADING ENGINE'S PLUMBING

The broker cannot tell a long-term holding from a trade. To
market_scanner.py a held SCHG position is just an open equity position:
it counts toward MAX_OPEN_POSITIONS, position_monitor.py watches it for
an exit it must never take, and startup_reconciliation.py calls it
untracked. So the symbols are RESERVED in position_filters.py and the
trading engine is made blind to them. That separation is the whole
safety model — without it the two strategies fight over the same shares.

THE WHOLE-SHARE PROBLEM, STATED PLAINLY

Under ACCOUNT_PROFILE "small" only whole shares can be bought. At a $100
budget a 20% sleeve is $20, and there is no broad ETF that costs $20.
This module therefore reports what it CANNOT buy rather than quietly
allocating something else. An allocation that silently becomes 100% of
whatever happened to be cheap is not the allocation anyone chose.

It places no orders unless ETF_PORTFOLIO_ENABLED and ETF_PORTFOLIO_LIVE
are BOTH true. The default is plan-only.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import lockbot_config as config

MODULE_NAME = "ETF_PORTFOLIO"
VERSION = "1.0"


@dataclass
class Sleeve:
    """One line of the target allocation, priced against reality."""

    symbol: str
    target_weight: float
    price: float = 0.0
    held_shares: int = 0

    target_dollars: float = 0.0
    held_dollars: float = 0.0
    buyable_shares: int = 0
    shortfall_reason: str = ""

    @property
    def actual_weight(self) -> float:
        return 0.0

    def __post_init__(self) -> None:
        self.symbol = self.symbol.upper()


@dataclass
class Plan:
    """What the portfolio would do, and what it cannot."""

    sleeves: list[Sleeve] = field(default_factory=list)
    budget: float = 0.0
    invested: float = 0.0
    uninvestable: float = 0.0
    orders: list[tuple[str, int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_allocation(allocation: dict[str, float]) -> tuple[bool, str]:
    """Weights must be positive and sum to 1.0.

    A allocation summing to 0.9 would silently leave a tenth of the
    budget unassigned; one summing to 1.1 would overspend it. Neither
    fails loudly on its own, so it is checked here.
    """

    if not allocation:
        return False, "ETF_TARGET_ALLOCATION is empty."

    for symbol, weight in allocation.items():
        if not isinstance(weight, (int, float)) or weight <= 0:
            return False, f"{symbol}: weight must be a positive number."

        if weight > 1:
            return False, f"{symbol}: weight {weight} is above 1.0."

    total = sum(allocation.values())

    if abs(total - 1.0) > 0.001:
        return False, (
            f"weights sum to {total:.3f}, not 1.0. "
            "The difference would be silently unallocated."
        )

    return True, ""


def build_plan(
    allocation: dict[str, float],
    prices: dict[str, float],
    holdings: dict[str, int],
    budget: float,
) -> Plan:
    """Work out the target portfolio and what stands in its way.

    Pure: no broker, no files. Everything that decides how money is spent
    is testable without a network.
    """

    plan = Plan(budget=budget)

    ok, why = validate_allocation(allocation)

    if not ok:
        plan.warnings.append(f"allocation rejected: {why}")
        return plan

    if budget <= 0:
        plan.warnings.append("budget is zero or negative; nothing to invest.")
        return plan

    for symbol, weight in sorted(allocation.items()):
        symbol = symbol.upper()
        price = float(prices.get(symbol, 0.0) or 0.0)
        held = int(holdings.get(symbol, 0) or 0)

        sleeve = Sleeve(
            symbol=symbol,
            target_weight=weight,
            price=price,
            held_shares=held,
        )
        sleeve.target_dollars = budget * weight
        sleeve.held_dollars = held * price

        if price <= 0:
            sleeve.shortfall_reason = "no price available"
            plan.warnings.append(f"{symbol}: no price; skipped this run.")
            plan.sleeves.append(sleeve)
            continue

        wanted = int(sleeve.target_dollars // price)
        sleeve.buyable_shares = wanted

        if wanted == 0:
            # The honest failure. Do not substitute something cheaper.
            sleeve.shortfall_reason = (
                f"one share costs ${price:,.2f} but the sleeve is only "
                f"${sleeve.target_dollars:,.2f}"
            )
            plan.uninvestable += sleeve.target_dollars
            plan.warnings.append(f"{symbol}: {sleeve.shortfall_reason}.")

        difference = wanted - held

        if difference > 0:
            plan.orders.append((symbol, difference, "buy"))
        elif difference < 0:
            plan.orders.append((symbol, abs(difference), "sell"))

        plan.invested += wanted * price
        plan.sleeves.append(sleeve)

    return plan


def drift_report(
    sleeves: list[Sleeve],
) -> list[tuple[str, float, float, float]]:
    """(symbol, target %, actual %, drift in points) for held value."""

    total = sum(s.held_dollars for s in sleeves)

    if total <= 0:
        return [(s.symbol, s.target_weight * 100, 0.0,
                 -s.target_weight * 100) for s in sleeves]

    rows = []

    for sleeve in sleeves:
        actual = sleeve.held_dollars / total * 100
        target = sleeve.target_weight * 100
        rows.append((sleeve.symbol, target, actual, actual - target))

    return rows


def needs_rebalance(
    rows: list[tuple[str, float, float, float]],
    drift_points: float,
) -> list[str]:
    """Symbols that have drifted past the threshold."""

    return [symbol for symbol, _, _, drift in rows if abs(drift) >= drift_points]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state(path: Path | None = None) -> dict:
    """Portfolio history, tolerating a missing or corrupt file."""

    source = Path(path or config.ETF_PORTFOLIO_STATE_FILE)

    if not source.exists():
        return {"established": None, "contributions": 0.0, "events": []}

    try:
        return json.loads(source.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {"established": None, "contributions": 0.0, "events": []}


def save_state(state: dict, path: Path | None = None) -> None:
    """Write portfolio state atomically."""

    target = Path(path or config.ETF_PORTFOLIO_STATE_FILE)
    temporary = target.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=4)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temporary, target)


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------

def fetch_prices(symbols: list[str]) -> dict[str, float]:
    """Latest daily closes. Returns {} rather than raising."""

    try:
        from datetime import timedelta

        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from dotenv import load_dotenv

        load_dotenv()

        client = StockHistoricalDataClient(
            os.getenv(config.ALPACA_API_KEY_ENV),
            os.getenv(config.ALPACA_SECRET_KEY_ENV),
        )

        end = datetime.now(timezone.utc) - timedelta(minutes=20)

        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=end - timedelta(days=7),
            end=end,
            feed=config.ALPACA_DATA_FEED,
        ))

        return {
            symbol: float(bars[symbol][-1].close)
            for symbol in symbols
            if symbol in bars.data and bars[symbol]
        }

    except Exception as error:
        print(f"price fetch failed: {type(error).__name__}: {error}")
        return {}


def fetch_holdings(symbols: list[str]) -> dict[str, int]:
    """Shares currently held of the portfolio symbols only."""

    try:
        from position_filters import equity_positions
        from rearm_brackets import _client

        # include_reserved=True: this module is the owner of those
        # symbols, so it is the one caller that must see them.
        held = equity_positions(_client().get_all_positions(),
                                include_reserved=True)

        wanted = {s.upper() for s in symbols}

        return {
            str(p.symbol).upper(): int(float(p.qty))
            for p in held
            if str(p.symbol).upper() in wanted
        }

    except Exception as error:
        print(f"holdings fetch failed: {type(error).__name__}: {error}")
        return {}


def report(plan: Plan) -> None:
    """Print the plan in a form that can be acted on or ignored."""

    print("=" * 66)
    print(f"       LOCKBOT ETF PORTFOLIO v{VERSION}")
    print("=" * 66)
    live = getattr(config, "ETF_PORTFOLIO_LIVE", False)
    on = getattr(config, "ETF_PORTFOLIO_ENABLED", False)
    print(f"Mode          : {'LIVE' if (on and live) else 'PLAN ONLY'}")
    print(f"Budget        : ${plan.budget:,.2f}")
    print(f"Would invest  : ${plan.invested:,.2f}")

    if plan.uninvestable:
        print(f"Cannot invest : ${plan.uninvestable:,.2f}  (see warnings)")

    print()
    print(f"  {'etf':<6} {'target':>8} {'price':>9} {'held':>5} "
          f"{'want':>5} {'value':>10}")
    print("  " + "-" * 56)

    for s in plan.sleeves:
        print(f"  {s.symbol:<6} {s.target_weight:>7.0%} {s.price:>9.2f} "
              f"{s.held_shares:>5} {s.buyable_shares:>5} "
              f"{s.buyable_shares * s.price:>10,.2f}")

    rows = drift_report(plan.sleeves)

    if any(s.held_shares for s in plan.sleeves):
        print()
        print(f"  {'etf':<6} {'target':>8} {'actual':>8} {'drift':>8}")
        print("  " + "-" * 34)
        for symbol, target, actual, drift in rows:
            print(f"  {symbol:<6} {target:>7.1f}% {actual:>7.1f}% "
                  f"{drift:>+7.1f}")

        drifted = needs_rebalance(
            rows, getattr(config, "ETF_REBALANCE_DRIFT_POINTS", 10.0)
        )
        print()
        print("  rebalance needed" if drifted else "  within tolerance")

    if plan.orders:
        print()
        print("  ORDERS THIS PLAN WOULD PLACE")
        for symbol, qty, side in plan.orders:
            print(f"    {side.upper():<5} {qty} {symbol}")

    if plan.warnings:
        print()
        print("  WARNINGS")
        for w in plan.warnings:
            print(f"    - {w}")

    print("=" * 66)

    if not (on and live):
        print("Nothing was ordered. Set ETF_PORTFOLIO_ENABLED and")
        print("ETF_PORTFOLIO_LIVE to True to act on this plan.")


def run() -> Plan:
    """Build a plan from live prices and holdings. Places no orders."""

    allocation = getattr(config, "ETF_TARGET_ALLOCATION", {}) or {}
    symbols = sorted(allocation)

    prices = fetch_prices(symbols)
    holdings = fetch_holdings(symbols)

    plan = build_plan(
        allocation,
        prices,
        holdings,
        float(getattr(config, "ETF_PORTFOLIO_BUDGET", 0.0)),
    )

    report(plan)

    return plan


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    print("Allocation validation")

    ok, _ = validate_allocation({"SCHG": 0.5, "SCHD": 0.5})
    check("a valid allocation is accepted", ok is True)

    ok, why = validate_allocation({"SCHG": 0.5, "SCHD": 0.4})
    check("weights that miss 1.0 are rejected", ok is False, why)
    check("and the reason names the risk",
          "unallocated" in why or "sum" in why, why)

    ok, why = validate_allocation({"SCHG": 1.5})
    check("a weight above 1.0 is rejected", ok is False, why)
    ok, why = validate_allocation({"SCHG": -0.5, "SCHD": 1.5})
    check("a negative weight is rejected", ok is False, why)
    ok, why = validate_allocation({})
    check("an empty allocation is rejected", ok is False, why)

    print()
    print("Whole shares, and saying what cannot be bought")

    # The real case: a $20 sleeve against a $72 ETF.
    plan = build_plan(
        {"VEA": 0.20, "SCHG": 0.80},
        {"VEA": 72.21, "SCHG": 35.50},
        {},
        100.0,
    )
    vea = next(s for s in plan.sleeves if s.symbol == "VEA")
    check("an unaffordable sleeve buys nothing", vea.buyable_shares == 0)
    check("and says exactly why", "one share costs" in vea.shortfall_reason,
          vea.shortfall_reason)
    check("the money is reported as uninvestable",
          abs(plan.uninvestable - 20.0) < 0.01, str(plan.uninvestable))
    check("it does NOT substitute something cheaper",
          not any(o[0] == "VEA" for o in plan.orders), str(plan.orders))

    schg = next(s for s in plan.sleeves if s.symbol == "SCHG")
    check("the affordable sleeve still buys", schg.buyable_shares == 2,
          str(schg.buyable_shares))

    print()
    print("Orders are the difference from what is held")

    plan = build_plan(
        {"SCHG": 0.5, "SCHD": 0.5},
        {"SCHG": 35.50, "SCHD": 33.85},
        {"SCHG": 1},
        100.0,
    )
    orders = dict((o[0], (o[1], o[2])) for o in plan.orders)
    check("it buys only the shortfall", orders.get("SCHG") == (0, "buy")
          or "SCHG" not in orders, str(orders))
    check("and buys the untouched sleeve", orders.get("SCHD") == (1, "buy"),
          str(orders))

    over = build_plan(
        {"SCHG": 1.0}, {"SCHG": 35.50}, {"SCHG": 10}, 100.0
    )
    check("holding too much produces a sell",
          any(o[2] == "sell" for o in over.orders), str(over.orders))

    print()
    print("Budget is a ceiling, not a suggestion")

    plan = build_plan(
        {"SCHG": 0.5, "SCHD": 0.5},
        {"SCHG": 35.50, "SCHD": 33.85},
        {},
        100.0,
    )
    check("spending stays within budget", plan.invested <= 100.0,
          f"${plan.invested:.2f}")

    zero = build_plan({"SCHG": 1.0}, {"SCHG": 35.50}, {}, 0.0)
    check("a zero budget buys nothing", zero.orders == [], str(zero.orders))

    print()
    print("Drift")

    sleeves = [
        Sleeve("SCHG", 0.5, price=35.0, held_shares=2),
        Sleeve("SCHD", 0.5, price=35.0, held_shares=2),
    ]
    for s in sleeves:
        s.held_dollars = s.held_shares * s.price

    rows = drift_report(sleeves)
    check("a balanced portfolio shows no drift",
          all(abs(d) < 0.01 for _, _, _, d in rows), str(rows))
    check("and needs no rebalance", needs_rebalance(rows, 10.0) == [])

    sleeves[0].held_dollars = 140.0     # SCHG ran up
    rows = drift_report(sleeves)
    drifted = needs_rebalance(rows, 10.0)
    check("a run-up is detected", "SCHG" in drifted, str(rows))
    check("and so is its mirror", "SCHD" in drifted, str(drifted))
    check("a wide tolerance ignores it", needs_rebalance(rows, 50.0) == [])

    empty = drift_report([Sleeve("SCHG", 1.0, price=35.0)])
    check("an unbuilt portfolio does not divide by zero",
          empty[0][2] == 0.0, str(empty))

    print()
    print("Missing data is survivable")

    plan = build_plan({"SCHG": 1.0}, {}, {}, 100.0)
    check("no price means no order", plan.orders == [], str(plan.orders))
    check("and a warning", any("no price" in w for w in plan.warnings),
          str(plan.warnings))

    print()
    print("The trading engine cannot see these symbols")

    from position_filters import equity_positions, reserved_symbols

    class Fake:
        def __init__(self, symbol):
            self.symbol = symbol
            self.asset_class = "us_equity"

    reserved = reserved_symbols()
    check("portfolio symbols are reserved", "SCHG" in reserved, str(reserved))

    positions = [Fake("SCHG"), Fake("NVO")]
    visible = [p.symbol for p in equity_positions(positions)]
    check("the trading engine is blind to them", visible == ["NVO"],
          str(visible))

    everything = [p.symbol for p in
                  equity_positions(positions, include_reserved=True)]
    check("but the portfolio itself can see them",
          set(everything) == {"SCHG", "NVO"}, str(everything))

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All ETF-portfolio checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    run()
