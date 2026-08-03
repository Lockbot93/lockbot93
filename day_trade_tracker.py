"""
day_trade_tracker.py  --  LOCKBOT local pattern-day-trader guard  (v1.0)

WHY THIS EXISTS
    Alpaca used to report daytrade_count on the account object, and
    market_scanner.py read it to enforce MAX_DAY_TRADES_PER_5_DAYS.
    Alpaca removed that field on 2026-07-06 (FINRA intraday-margin
    migration) and it now always returns None.

    The old code read it as `int(getattr(account, "daytrade_count", 0) or 0)`,
    which turned None into 0. So the guard printed "0/3" every cycle and
    could never once fire. The single tightest limit on a small account
    was silently switched off for weeks.

    This module counts round trips from filled order history instead, so
    the limit is enforced from data that still exists.

WHY IT MATTERS MORE NOW
    Options day trades count toward the same limit, and LOCKBOT can now
    open positions on two paths in the same cycle. Under $25,000 an
    account gets 3 same-day round trips per 5 business days; blowing
    through that gets the account restricted, and a restricted account
    cannot trade at all.

DEFINITION AND ITS DELIBERATE IMPRECISION
    A day trade is buying and selling the same security on the same day.
    This counts min(buy fills, sell fills) per symbol per day, which
    slightly OVER-counts: a sell that closes yesterday's position is
    counted if anything was also bought that day.

    Over-counting is the safe direction. Stopping one trade early costs
    an opportunity; miscounting the other way costs the account its
    ability to trade. The lookback is 7 calendar days rather than exactly
    5 business days for the same reason.

    Positions held overnight are not day trades and do not count.

USAGE
    python day_trade_tracker.py              show the current count
    python day_trade_tracker.py --self-test  offline logic check
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


# Deliberately wider than 5 business days. See the docstring.
LOOKBACK_CALENDAR_DAYS = 7


@dataclass(frozen=True)
class DayTradeCount:
    """The rolling round-trip count and how it was reached."""

    total: int
    by_day: dict[str, int]
    detail: list[str]
    counted_from: str

    @property
    def is_estimate(self) -> bool:
        """This is always LOCKBOT's own count, never the broker's."""

        return True


def _enum_text(value: Any) -> str:
    """Return normalized text for Alpaca enums or plain values."""

    return str(getattr(value, "value", value) or "").strip().lower()


def count_day_trades_from_orders(orders: list) -> DayTradeCount:
    """
    Count same-day round trips in a list of orders.

    Pure function over order objects so it can be tested without a
    network call. Only filled orders count; anything unfilled never
    moved a share.
    """

    buys: dict[tuple[str, str], int] = defaultdict(int)
    sells: dict[tuple[str, str], int] = defaultdict(int)

    for order in orders or []:
        if _enum_text(getattr(order, "status", "")) != "filled":
            continue

        filled_at = getattr(order, "filled_at", None)

        if filled_at is None:
            continue

        symbol = str(getattr(order, "symbol", "")).upper()

        if not symbol:
            continue

        day = filled_at.date().isoformat()
        side = _enum_text(getattr(order, "side", ""))

        if side == "buy":
            buys[(symbol, day)] += 1
        elif side == "sell":
            sells[(symbol, day)] += 1

    by_day: dict[str, int] = defaultdict(int)
    detail: list[str] = []

    for key in sorted(set(buys) | set(sells)):
        symbol, day = key
        round_trips = min(buys.get(key, 0), sells.get(key, 0))

        if round_trips > 0:
            by_day[day] += round_trips
            detail.append(f"{day} {symbol}: {round_trips} round trip(s)")

    return DayTradeCount(
        total=sum(by_day.values()),
        by_day=dict(by_day),
        detail=detail,
        counted_from="filled order history",
    )


def get_day_trade_count(trading_client: Any) -> DayTradeCount:
    """Fetch recent orders and count round trips."""

    from alpaca.common.enums import Sort
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    after = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_CALENDAR_DAYS)

    orders = trading_client.get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=after,
            direction=Sort.ASC,
            limit=500,
        )
    )

    return count_day_trades_from_orders(list(orders or []))


def day_trade_limit_reached(
    trading_client: Any,
    max_day_trades: int,
) -> tuple[bool, str]:
    """
    Return whether new positions should be blocked, and why.

    A failure to count is NOT treated as permission to trade. That is the
    exact mistake the old broker-field check made.
    """

    if max_day_trades <= 0:
        return False, "Day-trade limit is disabled (set to 0)."

    try:
        count = get_day_trade_count(trading_client)

    except Exception as error:
        return (
            True,
            f"Could not count day trades ({type(error).__name__}: {error}). "
            "Blocking new entries rather than assuming there is room.",
        )

    if count.total >= max_day_trades:
        return (
            True,
            f"{count.total}/{max_day_trades} round trips in the last "
            f"{LOOKBACK_CALENDAR_DAYS} days. No new positions.",
        )

    return (
        False,
        f"{count.total}/{max_day_trades} round trips in the last "
        f"{LOOKBACK_CALENDAR_DAYS} days.",
    )


def _self_test() -> int:
    """Offline checks. No network, no credentials."""

    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    class FakeOrder:
        def __init__(self, symbol, side, day, status="filled"):
            self.symbol = symbol
            self.side = side
            self.status = status
            self.filled_at = (
                datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
                if status == "filled"
                else None
            )

    print("Round-trip counting")

    check("empty history counts zero",
          count_day_trades_from_orders([]).total == 0)

    same_day = [
        FakeOrder("NVO", "buy", "2026-07-29"),
        FakeOrder("NVO", "sell", "2026-07-29"),
    ]
    check("buy and sell same day is one round trip",
          count_day_trades_from_orders(same_day).total == 1,
          str(count_day_trades_from_orders(same_day).total))

    overnight = [
        FakeOrder("NVO", "buy", "2026-07-28"),
        FakeOrder("NVO", "sell", "2026-07-29"),
    ]
    check("held overnight is not a day trade",
          count_day_trades_from_orders(overnight).total == 0,
          str(count_day_trades_from_orders(overnight).total))

    buy_only = [FakeOrder("NVO", "buy", "2026-07-29")]
    check("an open position is not a day trade",
          count_day_trades_from_orders(buy_only).total == 0)

    two_symbols = [
        FakeOrder("NVO", "buy", "2026-07-29"),
        FakeOrder("NVO", "sell", "2026-07-29"),
        FakeOrder("LVS", "buy", "2026-07-29"),
        FakeOrder("LVS", "sell", "2026-07-29"),
    ]
    check("two symbols same day is two round trips",
          count_day_trades_from_orders(two_symbols).total == 2,
          str(count_day_trades_from_orders(two_symbols).total))

    unfilled = [
        FakeOrder("NVO", "buy", "2026-07-29"),
        FakeOrder("NVO", "sell", "2026-07-29", status="canceled"),
    ]
    check("cancelled orders do not count",
          count_day_trades_from_orders(unfilled).total == 0,
          str(count_day_trades_from_orders(unfilled).total))

    # Options are securities too and count toward the same limit.
    options = [
        FakeOrder("EWZ260821C00036000", "buy", "2026-07-29"),
        FakeOrder("EWZ260821C00036000", "sell", "2026-07-29"),
    ]
    check("option round trips count",
          count_day_trades_from_orders(options).total == 1,
          str(count_day_trades_from_orders(options).total))

    multiple = [
        FakeOrder("NVO", "buy", "2026-07-29"),
        FakeOrder("NVO", "sell", "2026-07-29"),
        FakeOrder("NVO", "buy", "2026-07-29"),
        FakeOrder("NVO", "sell", "2026-07-29"),
    ]
    check("two round trips in one symbol count twice",
          count_day_trades_from_orders(multiple).total == 2,
          str(count_day_trades_from_orders(multiple).total))

    print()
    print("Limit behaviour")

    class BrokenClient:
        def get_orders(self, **_):
            raise RuntimeError("broker unreachable")

    blocked, reason = day_trade_limit_reached(BrokenClient(), 3)
    check("a counting failure blocks rather than allows", blocked, reason)

    disabled, reason = day_trade_limit_reached(BrokenClient(), 0)
    check("a zero limit disables the check", not disabled, reason)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All day-trade-tracker checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    from alpaca.trading.client import TradingClient
    from dotenv import load_dotenv

    import lockbot_config as config

    load_dotenv()

    client = TradingClient(
        os.getenv(config.ALPACA_API_KEY_ENV),
        os.getenv(config.ALPACA_SECRET_KEY_ENV),
        paper=config.PAPER_TRADING,
    )

    result = get_day_trade_count(client)

    print("=" * 56)
    print("LOCKBOT DAY-TRADE COUNT")
    print("=" * 56)
    print(f"Lookback      : {LOOKBACK_CALENDAR_DAYS} calendar days")
    print(f"Round trips   : {result.total}")
    print(f"Limit         : {config.MAX_DAY_TRADES_PER_5_DAYS}")
    print(f"Counted from  : {result.counted_from}")

    if result.detail:
        print()
        for line in result.detail:
            print(f"  {line}")
    else:
        print()
        print("  No same-day round trips in the window.")

    print("=" * 56)
