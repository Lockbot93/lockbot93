"""Core risk controls for LockBot.

THE DAILY LOSS LIMIT IS PER BOOK, NOT PER ACCOUNT

Both scanners used to pass `account.equity` and `account.last_equity`
into check_daily_loss_limit -- the whole account, including marks on
positions the calling book does not own. Measured on 2026-08-06:

    equity change today      -8.01  (-3.02%)  -> equity entries BLOCKED
      from option marks      -8.00
      from shares            +0.17
    option trades closed         0
    realised P&L today       +0.00

Nothing was sold and nothing was lost. The IBIT spread's two legs marked
independently (-23 and +18, net -5) and PCG marked -3, and that was
enough to lock the equity path for the day.

The mirror bug was live too: options_scanner used the same account
number, so equity losses and ETF marks gated OPTION entries. And the ETF
sleeve leaked into both, despite position_filters existing specifically
so the trading engine "pretends it cannot see" those symbols -- the
reservation was applied to position counts and never to this gate.

So each book is now measured on its own profit and loss:

    realised today  from that book's journal
    plus            unrealized_intraday_pl over that book's open
                    positions, which measures from the prior close for
                    overnight holds and from cost for same-day opens
    against         last_equity as the denominator for both

The denominator stays account-level deliberately. Splitting capital per
book would invent an allocation that exists nowhere else in the project.
Same dollar budget, each book gated on its own losses.

KNOWN LIMITS OF THE ARITHMETIC, stated rather than hidden:

  - A position opened yesterday and closed today contributes its LIFETIME
    profit to today's realised figure, because the journal stores lifetime
    P&L per trade and closed positions vanish from the positions endpoint.
    Yesterday's move is therefore counted twice across the two days. With
    ~1-day holds and 2-6% stops the error is bounded to a few tenths of a
    percent; fixing it exactly needs per-position prior-close snapshots
    that nothing currently stores.
  - Between a fill closing a position and trade_manager journalling it,
    the loss is invisible to the gate. Normally one cycle.

Both were identified by LOCKBOT when this fix was consulted rather than
found afterwards.
"""

from datetime import date

import lockbot_config as config

# Sourced from lockbot_config.py so this can never silently drift from
# risk_manager.py's own daily-loss check.
MAX_DAILY_LOSS_PERCENT = config.MAX_DAILY_LOSS_PERCENT


def check_daily_loss_limit(
    current_equity,
    previous_close_equity,
    max_daily_loss_percent=MAX_DAILY_LOSS_PERCENT,
):
    """
    Check whether LockBot has reached its maximum daily loss.

    A freshly created paper account has no prior closing balance, so Alpaca
    reports last_equity as 0. Treating that as invalid data blocked trading
    for an entire session (the 158 INVALID_PREVIOUS_EQUITY rejections on
    2026-07-23, and again on the new $250 account). When there is no prior
    close to compare against, today's starting equity IS the baseline:
    profit and loss for the day is zero and the limit cannot have been hit.

    The tradeoff, stated plainly: if Alpaca ever returns 0 for last_equity
    mid-session because of an API problem rather than a new account, the
    daily loss limit is skipped for that cycle instead of halting trading.
    The per-trade risk cap, position caps and exposure ceiling all still
    apply, so this is not the only thing standing between LockBot and a
    bad day.

    Returns:
        tuple:
            loss_limit_reached (bool)
            daily_pnl (float)
            daily_pnl_percent (float)
            reason (str)
    """
    current_equity = float(current_equity)
    previous_close_equity = float(previous_close_equity)
    max_daily_loss_percent = float(max_daily_loss_percent)

    if current_equity <= 0:
        return True, 0.0, 0.0, "INVALID_CURRENT_EQUITY"

    if not 0 < max_daily_loss_percent < 1:
        return True, 0.0, 0.0, "INVALID_DAILY_LOSS_LIMIT"

    if previous_close_equity <= 0:
        # No prior close on record — a new account, or a bad read. Use
        # today's equity as the baseline rather than halting the session.
        return (
            False,
            0.0,
            0.0,
            "NO_PRIOR_CLOSE_USING_CURRENT_EQUITY",
        )

    daily_pnl = current_equity - previous_close_equity
    daily_pnl_percent = daily_pnl / previous_close_equity

    loss_limit_reached = (
        daily_pnl_percent <= -max_daily_loss_percent
    )

    if loss_limit_reached:
        reason = "DAILY_LOSS_LIMIT_REACHED"
    else:
        reason = "DAILY_LOSS_LIMIT_OK"

    return (
        loss_limit_reached,
        daily_pnl,
        daily_pnl_percent,
        reason,
    )


# ---------------------------------------------------------------------------
# Per-book profit and loss
# ---------------------------------------------------------------------------

def _today() -> str:
    """Today's date, in the local calendar the journals record in."""

    return date.today().isoformat()


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def intraday_marks(positions, *, opened_today: set[str] | None = None) -> float:
    """Today's mark change across these positions.

    WHICH FIELD IS CORRECT DEPENDS ON WHEN THE POSITION WAS OPENED, and
    getting that wrong is the cause of the daily-loss lockouts filed
    three times (2026-08-14, 08-20, 08-21, channel d62e4060).

    For a position HELD FROM A PREVIOUS SESSION, unrealized_intraday_pl
    is right and unrealized_pl is wrong: the latter measures from entry
    over the position's whole life, so it drags every prior session into
    a figure that is supposed to describe today.

    For a position OPENED TODAY the fields swap roles, because Alpaca
    computes unrealized_intraday_pl from the CONTRACT'S PREVIOUS CLOSE
    rather than from your fill. It therefore charges the book for a move
    that happened before LOCKBOT owned anything. Measured 2026-08-24 on
    a contract bought that same session:

        NOK   cost $29.00   now $27.00   a $2 loss
              unrealized_pl           -2.00   correct
              unrealized_intraday_pl -13.00   includes $11 of pre-entry move

    That inflated the options book to -$22 against a true -$12, and it
    is why this module's number has been consistently more negative than
    market_scanner's on the same minute. For a position opened today,
    P&L since entry IS today's P&L, so unrealized_pl is exact.

    opened_today is a set of broker symbols. Callers that cannot supply
    it get the previous behaviour unchanged.
    """

    total = 0.0
    opened_today = opened_today or set()

    for position in positions or []:
        symbol = str(getattr(position, "symbol", ""))

        if symbol in opened_today:
            total += _as_float(getattr(position, "unrealized_pl", 0.0))
        else:
            total += _as_float(getattr(position, "unrealized_intraday_pl", 0.0))

    return total


def realised_today(path, *, exit_field: str, pnl_field: str = "profit_loss",
                   today: str | None = None) -> float:
    """Profit and loss from trades in this journal that closed today.

    Returns 0.0 for a missing or unreadable journal rather than raising.
    A gate that crashes on a missing file fails open, which is the wrong
    direction for a risk control.
    """

    import csv
    from pathlib import Path

    source = Path(path)
    stamp = today or _today()

    if not source.exists():
        return 0.0

    total = 0.0

    try:
        with source.open(newline="", encoding="utf-8", errors="ignore") as fh:
            for row in csv.DictReader(fh):
                if stamp in str(row.get(exit_field, "")):
                    total += _as_float(row.get(pnl_field))
    except OSError:
        return 0.0

    return total


def equity_book_pnl(positions, *, today: str | None = None) -> float:
    """Today's profit and loss for the SHARE trading book.

    Excludes option contracts and excludes the reserved ETF sleeve.
    position_filters exists so the trading engine cannot see buy-and-hold
    holdings; applying that here is the whole point of the fix -- a red
    day in SCHD must not lock the trading engine out.
    """

    from position_filters import equity_positions

    return (
        realised_today(config.COMPLETED_TRADES_FILE,
                       exit_field="exit_time", today=today)
        + intraday_marks(equity_positions(positions))
    )


def options_opened_today(*, today: str | None = None) -> set[str]:
    """Broker symbols of option legs LOCKBOT opened during today's session.

    Read from LOCKBOT's own position state rather than from the broker,
    because the broker's position object carries no entry timestamp. A
    state file that cannot be read yields an empty set, which restores
    the previous behaviour rather than inventing membership either way.
    """

    import json

    # _today(), the SAME definition realised_today uses. A second notion
    # of "today" here would put the two halves of one P&L figure on
    # different calendars: at 21:50 local on 08-24 the UTC date is
    # already 08-25, so a UTC stamp matched nothing and every position
    # silently fell back to the broker's intraday field -- the exact bug
    # this function exists to fix, reintroduced inside the fix.
    stamp = today or _today()
    symbols: set[str] = set()

    try:
        path = config.PROJECT_FOLDER / "options_position_state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001
        return symbols

    for position in (state or {}).values():
        if not isinstance(position, dict):
            continue

        if str(position.get("entry_time", ""))[:10] != stamp:
            continue

        for key in ("long_symbol", "short_symbol"):
            leg = position.get(key)

            if leg:
                symbols.add(str(leg))

    return symbols


def options_book_pnl(positions, *, today: str | None = None) -> float:
    """Today's profit and loss for the OPTIONS book."""

    from position_filters import option_positions

    return (
        realised_today(config.PROJECT_FOLDER / "options_completed_trades.csv",
                       exit_field="exit_time", today=today)
        + intraday_marks(option_positions(positions),
                         opened_today=options_opened_today(today=today))
    )


def check_book_daily_loss(
    book_pnl: float,
    previous_close_equity: float,
    max_daily_loss_percent: float = MAX_DAILY_LOSS_PERCENT,
):
    """Apply the daily loss limit to ONE book's own profit and loss.

    Feeds the existing, tested check_daily_loss_limit a book-scoped
    figure rather than the account total: last_equity stays the
    denominator, so both books share one dollar budget while each is
    gated only on what it actually did.
    """

    return check_daily_loss_limit(
        current_equity=previous_close_equity + book_pnl,
        previous_close_equity=previous_close_equity,
        max_daily_loss_percent=max_daily_loss_percent,
    )


def _self_test() -> int:
    """Offline checks. No network, no credentials."""

    import tempfile
    from pathlib import Path

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    class Pos:
        def __init__(self, symbol, asset_class, intraday, lifetime=0.0):
            self.symbol = symbol
            self.asset_class = asset_class
            self.unrealized_intraday_pl = intraday
            self.unrealized_pl = lifetime

    # -----------------------------------------------------------------
    # A position opened TODAY is marked from the fill, not from the
    # contract's previous close. Channel d62e4060, filed three times.
    # -----------------------------------------------------------------
    print("Which mark is correct depends on when the position was opened")

    class _M:
        def __init__(self, symbol, intraday, lifetime):
            self.symbol = symbol
            self.unrealized_intraday_pl = intraday
            self.unrealized_pl = lifetime

    # The real 2026-08-24 numbers: bought at $29, worth $27, a $2 loss --
    # but the broker's intraday field says -$13 because it measures from
    # the contract's prior close, $11 of which LOCKBOT never owned.
    nok = _M("NOK260918C00011000", -13.0, -2.0)
    ford = _M("F260918C00014500", -2.0, -3.0)
    book = [nok, ford]

    check("without entry dates, the old behaviour is unchanged",
          intraday_marks(book) == -15.0, str(intraday_marks(book)))
    check("positions opened today are marked from the fill",
          intraday_marks(book, opened_today={nok.symbol, ford.symbol}) == -5.0,
          str(intraday_marks(book, opened_today={nok.symbol, ford.symbol})))
    check("a position HELD FROM YESTERDAY still uses the intraday field",
          intraday_marks(book, opened_today={ford.symbol}) == -16.0,
          str(intraday_marks(book, opened_today={ford.symbol})))
    check("an empty set is not treated as 'everything'",
          intraday_marks(book, opened_today=set()) == -15.0,
          str(intraday_marks(book, opened_today=set())))

    # The lockout this caused: -$22 on $391.37 reads -5.62%, the true
    # -$12 reads -3.07%. At larger marks the same error crossed the 10%
    # budget and blocked entries for whole mornings.
    wrong = check_book_daily_loss(-7.0 + -15.0, 391.37)[2]
    right = check_book_daily_loss(-7.0 + -5.0, 391.37)[2]
    check("the inflated figure is materially worse than the true one",
          abs(wrong - right) > 0.02, f"{wrong:.4f} vs {right:.4f}")

    # Both halves of one P&L figure must sit on the same calendar. A UTC
    # stamp here matched nothing after 19:00 local and silently restored
    # the bug inside its own fix.
    import inspect
    body = inspect.getsource(options_opened_today)
    check("opened_today shares realised_today's definition of today",
          "_today()" in body and "utcnow" not in body and
          "timezone.utc" not in body)

    print()
    print("The account-level gate still behaves")

    hit, pnl, pct, _ = check_daily_loss_limit(252.93, 287.03)
    check("a -11.9% day trips the limit", hit is True, f"{pct:.2%}")
    check("and reports the damage", abs(pnl + 34.10) < 0.01, str(pnl))
    check("a small loss does not",
          check_daily_loss_limit(285.0, 287.03)[0] is False)
    check("a new account with no prior close is not blocked",
          check_daily_loss_limit(250.0, 0.0)[0] is False)

    print()
    print("Marks are read from the INTRADAY field")

    # unrealized_pl measures from entry over the position's whole life.
    # Using it in a DAILY figure double counts every prior session, which
    # is the mistake made on the first diagnosis of this bug.
    misleading = [Pos("X", "us_equity", intraday=-1.0, lifetime=-500.0)]
    check("today's mark is used, not the position's lifetime",
          abs(intraday_marks(misleading) + 1.0) < 1e-9,
          str(intraday_marks(misleading)))
    check("no positions is zero", intraday_marks([]) == 0.0)
    check("None is safe", intraday_marks(None) == 0.0)

    class Broken:
        pass

    check("a position with no such field is skipped",
          intraday_marks([Broken()]) == 0.0)

    print()
    print("Realised is read from the journal, for today only")

    folder = Path(tempfile.mkdtemp())
    journal = folder / "trades.csv"
    journal.write_text(
        "symbol,exit_time,profit_loss\n"
        "AAA,2026-08-06T14:00:00Z,-5.00\n"
        "BBB,2026-08-06T15:00:00Z,+2.00\n"
        "CCC,2026-08-05T15:00:00Z,-99.00\n",
        encoding="utf-8",
    )

    total = realised_today(journal, exit_field="exit_time",
                           today="2026-08-06")
    check("today's trades are summed", abs(total + 3.0) < 1e-9, str(total))
    check("yesterday's are excluded", abs(total + 3.0) < 1e-9)
    check("a missing journal is zero, not an exception",
          realised_today(folder / "absent.csv", exit_field="exit_time") == 0.0)

    print()
    print("THE FIX: each book is gated on its own losses")

    from position_filters import (option_positions as option_positions_for_test,
                                  equity_positions as equity_positions_for_test)

    # The real 2026-08-06 shape: options marking down, equity book flat.
    positions = [
        Pos("IBIT260828C00036000", "us_option", intraday=-23.0),
        Pos("IBIT260828C00036500", "us_option", intraday=+18.0),
        Pos("PCG260821P00017500", "us_option", intraday=-3.0),
        Pos("SCHD", "us_equity", intraday=+0.11),
        Pos("SCHG", "us_equity", intraday=+0.06),
    ]

    last_equity = 265.27
    account_move = sum(p.unrealized_intraday_pl for p in positions)

    # These three checks asserted that a -3% day BLOCKS, which was true
    # when the limit was 0.02 and stopped being true when the owner
    # raised MAX_DAILY_LOSS_PERCENT to 0.10 on 2026-08-21. They have been
    # failing silently ever since, in the module that gates every entry.
    #
    # FIFTH TEST THIS WEEK TO PIN A CONFIGURATION VALUE. The scenario is
    # kept because it documents the real 2026-08-06 incident, but the
    # magnitudes are now scaled to whatever the limit actually is, so the
    # checks assert the CONTRACT -- each book gated on its own marks --
    # at any setting.
    limit = config.MAX_DAILY_LOSS_PERCENT
    breach = -(limit * last_equity * 1.5)      # comfortably over
    inside = -(limit * last_equity * 0.25)     # comfortably under

    old = check_daily_loss_limit(last_equity + breach, last_equity)
    check("the OLD account-wide gate blocks a breaching day",
          old[0] is True, f"{old[2]:.2%}")
    check("the same day, scoped to one book, still blocks it",
          check_book_daily_loss(breach, last_equity)[0] is True)
    check("a day inside the budget blocks nothing",
          check_book_daily_loss(inside, last_equity)[0] is False)

    # The shape that mattered on 08-06: the options book carried the
    # whole loss while the equity book was flat. Asserted as a
    # RELATIONSHIP, which holds at any limit.
    check("the options book carries the loss, the equity book does not",
          intraday_marks(option_positions_for_test(positions)) < 0
          <= intraday_marks(equity_positions_for_test(positions)),
          f"{account_move:.2f}")

    from position_filters import option_positions

    options_pnl = intraday_marks(option_positions(positions))

    check("and it is the OPTIONS marks that produce it",
          abs(options_pnl - (-8.0)) < 1e-9, str(options_pnl))

    # The equity book here is empty of TRADING positions; SCHD and SCHG
    # are reserved and must not count against it.
    import lockbot_config as _cfg

    original = getattr(_cfg, "ETF_TARGET_ALLOCATION", None)
    _cfg.ETF_TARGET_ALLOCATION = {"SCHD": 0.5, "SCHG": 0.5}

    try:
        from position_filters import equity_positions

        equity_pnl = intraday_marks(equity_positions(positions))
        new_equity = check_book_daily_loss(equity_pnl, last_equity)

        check("the equity book is NOT blocked -- it lost nothing",
              new_equity[0] is False, f"{new_equity[2]:.2%}")
        check("and the ETF sleeve does not leak into it",
              abs(equity_pnl) < 1e-9, str(equity_pnl))

        # The mirror case: equity book bleeding, options flat.
        mirror = [Pos("NVO", "us_equity", intraday=-9.0),
                  Pos("PCG260821P00017500", "us_option", intraday=0.0)]

        # Scaled to the limit, so this asserts the mirror RELATIONSHIP
        # rather than one historical magnitude.
        heavy = [Pos("NVO", "us_equity", intraday=breach),
                 Pos("PCG260821P00017500", "us_option", intraday=0.0)]

        check("a bad equity day blocks equity entries",
              check_book_daily_loss(
                  intraday_marks(equity_positions(heavy)),
                  last_equity)[0] is True)
        check("and leaves option entries free",
              check_book_daily_loss(
                  intraday_marks(option_positions(mirror)),
                  last_equity)[0] is False)

    finally:
        if original is None:
            delattr(_cfg, "ETF_TARGET_ALLOCATION")
        else:
            _cfg.ETF_TARGET_ALLOCATION = original

    print()
    print("The denominator stays account-level for both books")

    a = check_book_daily_loss(-10.0, 500.0)
    b = check_book_daily_loss(-10.0, 1000.0)
    check("the same loss is a smaller fraction of a bigger account",
          abs(a[2]) > abs(b[2]), f"{a[2]:.2%} vs {b[2]:.2%}")
    check("a book that made money is never blocked",
          check_book_daily_loss(+50.0, 265.27)[0] is False)
    check("a flat book is never blocked",
          check_book_daily_loss(0.0, 265.27)[0] is False)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All risk-engine checks passed.")
    return 0


if __name__ == "__main__":
    import sys

    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(__doc__)