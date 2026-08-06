"""
position_filters.py  --  keep equity and options positions apart  (v1.0)

WHY THIS EXISTS
    Alpaca's get_all_positions() returns everything the account holds in
    one list: shares and option contracts together. Before options
    existed that was harmless, so four modules called it and used the
    result directly.

    The moment LOCKBOT can hold an option, every one of those call sites
    becomes a bug:

      startup_reconciliation.py  would write option contracts into
                                 position_state.json, the EQUITY tracker
      position_monitor.py        would evaluate a call contract against
                                 stock stop-loss percentages
      market_scanner.py          would count options against
                                 MAX_OPEN_POSITIONS and block equity trades
      watchdog.py                would report LOCKBOT's own option
                                 positions as untracked and alert on them

    One shared filter, imported everywhere, so the equity and options
    paths can never quietly start counting each other's positions.

    This module only filters lists. It never calls the broker.
"""

from __future__ import annotations

from typing import Any


US_EQUITY = "us_equity"
US_OPTION = "us_option"


def asset_class_of(position: Any) -> str:
    """Return a position's asset class as plain lowercase text."""

    raw = getattr(position, "asset_class", None)

    return str(getattr(raw, "value", raw) or "").strip().lower()


def reserved_symbols() -> set[str]:
    """Symbols the trading engine must pretend it cannot see.

    The buy-and-hold ETF portfolio lives in the same brokerage account as
    the trading bot, and the broker makes no distinction between them. To
    market_scanner.py a held SCHD position is simply an open equity
    position: it counts toward MAX_OPEN_POSITIONS, position_monitor.py
    watches it for exits it should never take, and
    startup_reconciliation.py reports it as untracked.

    None of that is wrong exactly -- the position IS there -- but a
    long-term holding is not a trade, and a system that treats it as one
    will eventually try to manage it. Reserving the symbols is what keeps
    the two strategies from fighting over the same shares.

    Read live from config so adding an ETF to the portfolio takes effect
    without editing this file.
    """

    try:
        import lockbot_config as config

        allocation = getattr(config, "ETF_TARGET_ALLOCATION", {}) or {}

        return {str(symbol).upper() for symbol in allocation}
    except Exception:
        return set()


def equity_positions(positions: Any, *, include_reserved: bool = False) -> list:
    """
    Return only the share positions the TRADING engine owns.

    Portfolio holdings are excluded by default. Pass include_reserved=True
    when you genuinely want every share position -- the portfolio module
    itself does, and so does anything reporting total account exposure.

    A position with no asset_class at all is treated as equity, because
    that is what every position was before options were added and the
    equity path must keep working against older or mocked objects.
    """

    shares = [
        position
        for position in (positions or [])
        if asset_class_of(position) in {US_EQUITY, ""}
    ]

    if include_reserved:
        return shares

    reserved = reserved_symbols()

    if not reserved:
        return shares

    return [
        position
        for position in shares
        if str(getattr(position, "symbol", "")).upper() not in reserved
    ]


def option_positions(positions: Any) -> list:
    """Return only the option-contract positions."""

    return [
        position
        for position in (positions or [])
        if asset_class_of(position) == US_OPTION
    ]


class _Fake:
    """A position object shaped like Alpaca's, for tests."""

    def __init__(self, symbol, asset_class):
        self.symbol = symbol
        self.asset_class = asset_class


class _Enum:
    """Alpaca returns enums whose .value holds the string."""

    def __init__(self, value):
        self.value = value


def _self_test() -> int:
    """Offline checks. This module enforces two separation invariants.

    It had asserts in a __main__ block but no --self-test, so it never
    ran with the suite -- while being the module that keeps option
    contracts out of the equity tracker and buy-and-hold ETFs out of the
    trading engine's sight.
    """

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    sample = [
        _Fake("NVO", "us_equity"),
        _Fake("EWZ260821C00036000", "us_option"),
        _Fake("LVS", _Enum("us_equity")),
        _Fake("SPY260821P00450000", _Enum("us_option")),
        _Fake("LEGACY", None),
    ]

    print("Equities and options never mix")

    equities = [p.symbol for p in equity_positions(sample)]
    options = [p.symbol for p in option_positions(sample)]

    check("shares are returned as equity",
          equities == ["NVO", "LVS", "LEGACY"], str(equities))
    check("contracts are returned as options",
          options == ["EWZ260821C00036000", "SPY260821P00450000"],
          str(options))
    check("an enum asset_class is unwrapped",
          "LVS" in equities and "SPY260821P00450000" in options)
    check("a missing asset_class is treated as equity",
          "LEGACY" in equities)

    # The property that matters: nothing may be invisible to BOTH
    # filters. A position neither path can see is one nobody manages.
    seen = set(equities) | set(options)
    check("no position is invisible to both filters",
          seen == {p.symbol for p in sample},
          str({p.symbol for p in sample} - seen))
    check("and none is claimed by both",
          not (set(equities) & set(options)))

    print()
    print("The ETF book is hidden from the trading engine")

    import lockbot_config as config

    original = getattr(config, "ETF_TARGET_ALLOCATION", None)
    config.ETF_TARGET_ALLOCATION = {"SCHG": 0.5, "SCHD": 0.5}

    try:
        book = sample + [_Fake("SCHD", "us_equity"), _Fake("SCHG", "us_equity")]

        visible = [p.symbol for p in equity_positions(book)]
        check("reserved ETFs are hidden by default",
              "SCHD" not in visible and "SCHG" not in visible, str(visible))
        check("while real trading positions stay visible",
              "NVO" in visible and "LVS" in visible)

        everything = [p.symbol for p in equity_positions(
            book, include_reserved=True)]
        check("include_reserved shows them again",
              "SCHD" in everything and "SCHG" in everything)

        check("reserved symbols come from config",
              reserved_symbols() == {"SCHG", "SCHD"},
              str(reserved_symbols()))

        # Case matters: the broker returns upper case, a config might not.
        config.ETF_TARGET_ALLOCATION = {"schd": 1.0}
        check("reserving is case-insensitive",
              "SCHD" not in [p.symbol for p in equity_positions(book)])

        # An option that happens to be ON a reserved underlying must NOT
        # be hidden -- it is a trade, not a holding.
        config.ETF_TARGET_ALLOCATION = {"SCHD": 1.0}
        with_option = [_Fake("SCHD260821C00035000", "us_option")]
        check("an option on a reserved underlying is still a trade",
              len(option_positions(with_option)) == 1)

        config.ETF_TARGET_ALLOCATION = {}
        check("no allocation means nothing is reserved",
              reserved_symbols() == set())
        check("and every share is visible again",
              "SCHD" in [p.symbol for p in equity_positions(book)])

    finally:
        if original is None:
            delattr(config, "ETF_TARGET_ALLOCATION")
        else:
            config.ETF_TARGET_ALLOCATION = original

    print()
    print("Nothing here may raise")

    check("None is safe", equity_positions(None) == []
          and option_positions(None) == [])
    check("an empty list is safe", equity_positions([]) == []
          and option_positions([]) == [])
    check("an object with no attributes at all is not an option",
          option_positions([object()]) == [])
    check("and is treated as equity",
          len(equity_positions([object()])) == 1)

    check("asset_class_of handles anything",
          asset_class_of(_Fake("X", None)) == ""
          and asset_class_of(_Fake("X", "US_EQUITY")) == "us_equity")

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All position-filter checks passed.")
    return 0


if __name__ == "__main__":
    import sys

    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(__doc__)
