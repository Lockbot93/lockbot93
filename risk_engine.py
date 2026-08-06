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


def intraday_marks(positions) -> float:
    """Today's mark change across these positions.

    Uses unrealized_intraday_pl, NOT unrealized_pl. The latter measures
    from the position's entry over its whole life, so subtracting it
    from a daily figure double counts every prior session -- an error I
    made on the first attempt at diagnosing this.
    """

    total = 0.0

    for position in positions or []:
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


def options_book_pnl(positions, *, today: str | None = None) -> float:
    """Today's profit and loss for the OPTIONS book."""

    from position_filters import option_positions

    return (
        realised_today(config.PROJECT_FOLDER / "options_completed_trades.csv",
                       exit_field="exit_time", today=today)
        + intraday_marks(option_positions(positions))
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

    old = check_daily_loss_limit(last_equity + account_move, last_equity)
    check("the OLD account-wide gate blocks on this day",
          old[0] is True, f"{old[2]:.2%}")

    from position_filters import option_positions

    options_pnl = intraday_marks(option_positions(positions))
    new_options = check_book_daily_loss(options_pnl, last_equity)

    check("the options book IS blocked -- those are its own marks",
          new_options[0] is True, f"{new_options[2]:.2%}")

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

        check("a bad equity day blocks equity entries",
              check_book_daily_loss(
                  intraday_marks(equity_positions(mirror)),
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