"""Core risk controls for LockBot."""

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