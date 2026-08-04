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


if __name__ == "__main__":
    class _Fake:
        def __init__(self, symbol, asset_class):
            self.symbol = symbol
            self.asset_class = asset_class

    class _Enum:
        def __init__(self, value):
            self.value = value

    sample = [
        _Fake("NVO", "us_equity"),
        _Fake("EWZ260821C00036000", "us_option"),
        _Fake("LVS", _Enum("us_equity")),
        _Fake("SPY260821P00450000", _Enum("us_option")),
        _Fake("LEGACY", None),
    ]

    equities = [p.symbol for p in equity_positions(sample)]
    options = [p.symbol for p in option_positions(sample)]

    assert equities == ["NVO", "LVS", "LEGACY"], equities
    assert options == ["EWZ260821C00036000", "SPY260821P00450000"], options
    assert equity_positions(None) == []
    assert option_positions([]) == []

    print("position_filters checks passed.")
    print(f"  equities: {equities}")
    print(f"  options : {options}")
