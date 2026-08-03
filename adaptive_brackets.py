"""
adaptive_brackets.py  --  LOCKBOT per-stock stop and target  (v1.0)

THE PROBLEM THIS SOLVES
-----------------------
Every LOCKBOT trade currently uses the same 2% stop and 4% target no
matter what it buys. That fits almost nothing:

    VTEB moves 0.30% a day  -> a 4% target is ~13 days away. Dead slot.
    CELH moves 4.49% a day  -> a 2% stop is smaller than a normal wiggle.
                               It gets hit at random, not because the
                               trade was wrong.

The fix is to measure each stock and size the stop to fit it.

THE PART THAT MATTERS MOST
--------------------------
A wider stop with the SAME position size means more money at risk.
If LOCKBOT keeps buying 40% of the account and the stop goes from 2%
to 4%, risk per trade doubles from $2 to $4 without anyone deciding
that. So this module sizes the POSITION from the risk budget instead:

    risk budget  = equity x MAX_RISK_PER_TRADE_PERCENT
    position     = risk budget / stop distance
    shares       = floor(position / price)

Wider stop -> smaller position. Risk per trade stays put. That is the
whole point, and it is why this cannot be done by changing the stop
percentage alone.

WHERE THE MOVEMENT NUMBER COMES FROM
------------------------------------
universe_volatility.py already writes universe_volatility_report.csv
every morning with each symbol's average daily movement. This module
reads that file. No extra price requests, nothing new to schedule.

If a symbol is missing from that file, this falls back to the old fixed
2%/4% bracket rather than guessing.

THIS MODULE PLACES NO ORDERS. It only does arithmetic and returns
numbers. Wiring it into market_scanner.py is a separate, deliberate step.

USAGE
-----
    python adaptive_brackets.py --self-test
    python adaptive_brackets.py --preview             # $250 account
    python adaptive_brackets.py --preview --equity 350
"""

import argparse
import csv
import math
import os
import sys

# ---------------------------------------------------------------------------
# Defaults -- overridden by lockbot_config.py where those values exist
# ---------------------------------------------------------------------------

# How many normal days of movement the stop should sit outside of.
# At 1.0 the stop sits exactly at a typical day's range, so ordinary
# noise reaches it about half the time. 1.5 gives the trade room to
# breathe without drifting into "never stops out" territory.
DEFAULT_ATR_STOP_MULTIPLIER = 1.5

# Target = stop x this. 2.0 keeps the existing 2%/4% shape, so a winner
# still pays twice what a loser costs and the win-rate math is unchanged.
DEFAULT_REWARD_RATIO = 2.0

# Floor and ceiling on the stop itself, whatever the measurement says.
# The floor keeps the stop outside spread and normal jitter on very quiet
# names. The ceiling stops one wild stock from eating the whole account.
DEFAULT_MIN_STOP_PERCENT = 0.015
DEFAULT_MAX_STOP_PERCENT = 0.060

# Used only when a symbol has no movement data at all.
FALLBACK_STOP_PERCENT = 0.02
FALLBACK_TARGET_PERCENT = 0.04

# Sizing fallbacks if lockbot_config.py cannot be read.
FALLBACK_MAX_RISK_PER_TRADE_PERCENT = 0.01
FALLBACK_MAX_POSITION_VALUE_PERCENT = 0.40

ATR_REPORT_FILE = "universe_volatility_report.csv"
UNIVERSE_FILE = "universe.csv"

# Rejection reasons
OK = "OK"
NO_SHARES = "REJECTED_ZERO_SHARES"
BAD_PRICE = "REJECTED_BAD_PRICE"
BAD_EQUITY = "REJECTED_BAD_EQUITY"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_settings():
    """Read what lockbot_config.py provides; fall back to the defaults above."""
    settings = {
        "stop_multiplier": DEFAULT_ATR_STOP_MULTIPLIER,
        "reward_ratio": DEFAULT_REWARD_RATIO,
        "min_stop": DEFAULT_MIN_STOP_PERCENT,
        "max_stop": DEFAULT_MAX_STOP_PERCENT,
        "max_risk_percent": FALLBACK_MAX_RISK_PER_TRADE_PERCENT,
        "max_position_percent": FALLBACK_MAX_POSITION_VALUE_PERCENT,
        "source": "defaults",
    }
    try:
        import lockbot_config as cfg
        found = []
        mapping = [
            ("stop_multiplier", "ATR_STOP_MULTIPLIER"),
            ("reward_ratio", "ATR_REWARD_RATIO"),
            ("min_stop", "ATR_MIN_STOP_PERCENT"),
            ("max_stop", "ATR_MAX_STOP_PERCENT"),
            ("max_risk_percent", "MAX_RISK_PER_TRADE_PERCENT"),
            ("max_position_percent", "MAX_POSITION_VALUE_PERCENT"),
        ]
        for key, attr in mapping:
            if hasattr(cfg, attr):
                settings[key] = float(getattr(cfg, attr))
                found.append(attr)
        if found:
            settings["source"] = "lockbot_config.py"
    except Exception as exc:
        settings["source"] = f"defaults (lockbot_config.py unreadable: {exc})"
    return settings


# ---------------------------------------------------------------------------
# Movement lookup
# ---------------------------------------------------------------------------

def load_atr_table(path=ATR_REPORT_FILE):
    """
    Read universe_volatility_report.csv into {symbol: daily movement fraction}.

    That file stores the movement as a PERCENT (2.42 meaning 2.42%), so it
    is converted to a fraction here to match every other percentage in the
    codebase.

    A missing file returns an empty table rather than raising -- callers
    then fall back to the fixed bracket, which is the safe direction.
    """
    table = {}
    if not os.path.exists(path):
        return table
    with open(path, "r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            symbol = (row.get("symbol") or "").strip().upper()
            raw = (row.get("atr_percent") or "").strip()
            if not symbol or not raw:
                continue
            try:
                table[symbol] = float(raw) / 100.0
            except ValueError:
                continue
    return table


# ---------------------------------------------------------------------------
# The calculation
# ---------------------------------------------------------------------------

def stop_percent_for(atr_pct, settings):
    """
    Turn a stock's typical daily movement into a stop distance, held
    between the floor and the ceiling.

    Returns (stop_percent, note).
    """
    if atr_pct is None or atr_pct <= 0:
        return FALLBACK_STOP_PERCENT, "no movement data - using fixed 2%"

    raw = atr_pct * settings["stop_multiplier"]

    if raw < settings["min_stop"]:
        return settings["min_stop"], "raised to the minimum stop"
    if raw > settings["max_stop"]:
        return settings["max_stop"], "capped at the maximum stop"
    return raw, "scaled to the stock"


def compute_bracket(symbol, price, equity, atr_pct=None, side="long",
                    settings=None):
    """
    Work out the stop, the target, and how many shares to buy.

    Returns a dict. Always check ["status"] -- a rejection is a normal
    outcome at a small account size, not an error.
    """
    settings = settings or load_settings()
    side = (side or "long").lower()

    result = {
        "symbol": (symbol or "").upper(),
        "side": side,
        "price": price,
        "atr_percent": atr_pct,
        "status": OK,
        "note": "",
    }

    try:
        price = float(price)
        equity = float(equity)
    except (TypeError, ValueError):
        result["status"] = BAD_PRICE
        result["note"] = "price or equity was not a number"
        return result

    if price <= 0:
        result["status"] = BAD_PRICE
        result["note"] = "price must be above zero"
        return result

    if equity <= 0:
        result["status"] = BAD_EQUITY
        result["note"] = "equity must be above zero"
        return result

    stop_pct, note = stop_percent_for(atr_pct, settings)
    target_pct = stop_pct * settings["reward_ratio"]

    # Risk budget in dollars, then the position that budget supports.
    risk_budget = equity * settings["max_risk_percent"]
    position_from_risk = risk_budget / stop_pct

    # The position cap still applies -- it is the tighter of the two.
    position_cap = equity * settings["max_position_percent"]
    target_position = min(position_from_risk, position_cap)

    binding = "risk budget" if position_from_risk <= position_cap \
        else "position cap"

    shares = int(math.floor(target_position / price))

    if shares < 1:
        result.update({
            "status": NO_SHARES,
            "stop_percent": stop_pct,
            "target_percent": target_pct,
            "shares": 0,
            "position_value": 0.0,
            "risk_dollars": 0.0,
            "note": (
                f"one share costs ${price:,.2f} but the risk budget only "
                f"allows ${target_position:,.2f} at a {stop_pct * 100:.2f}% stop"
            ),
        })
        return result

    position_value = shares * price
    risk_dollars = position_value * stop_pct

    if side == "short":
        stop_price = price * (1 + stop_pct)
        target_price = price * (1 - target_pct)
    else:
        stop_price = price * (1 - stop_pct)
        target_price = price * (1 + target_pct)

    result.update({
        "stop_percent": stop_pct,
        "target_percent": target_pct,
        "stop_price": round(stop_price, 2),
        "target_price": round(target_price, 2),
        "shares": shares,
        "position_value": round(position_value, 2),
        "position_percent": position_value / equity,
        "risk_dollars": round(risk_dollars, 2),
        "risk_percent": risk_dollars / equity,
        "reward_dollars": round(position_value * target_pct, 2),
        "limited_by": binding,
        "note": note,
    })
    return result


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def load_universe_prices(path=UNIVERSE_FILE):
    """Read {symbol: last_close} from universe.csv for the preview table."""
    prices = {}
    if not os.path.exists(path):
        return prices
    with open(path, "r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            symbol = (row.get("symbol") or "").strip().upper()
            raw = (row.get("last_close") or "").strip()
            if not symbol or not raw:
                continue
            try:
                prices[symbol] = float(raw)
            except ValueError:
                continue
    return prices


def preview(equity=250.0):
    settings = load_settings()
    atr_table = load_atr_table()
    prices = load_universe_prices()

    if not atr_table:
        print(f"{ATR_REPORT_FILE} not found. Run universe_volatility.py first.")
        return 1
    if not prices:
        print(f"{UNIVERSE_FILE} not found or has no last_close column.")
        return 1

    symbols = [s for s in prices if s in atr_table]
    symbols.sort(key=lambda s: atr_table[s], reverse=True)

    print("=" * 78)
    print(f"ADAPTIVE BRACKET PREVIEW  --  equity ${equity:,.2f}")
    print("=" * 78)
    print(f"Settings from: {settings['source']}")
    print(f"  stop = {settings['stop_multiplier']}x daily movement, "
          f"held between {settings['min_stop'] * 100:.2f}% and "
          f"{settings['max_stop'] * 100:.2f}%")
    print(f"  target = {settings['reward_ratio']}x the stop")
    print(f"  risk budget = {settings['max_risk_percent'] * 100:.2f}% of equity "
          f"(${equity * settings['max_risk_percent']:,.2f} per trade)")
    print(f"  position cap = {settings['max_position_percent'] * 100:.0f}% of equity")
    print()
    print(f"{'SYM':<6}{'PRICE':>8}{'MOVE':>7}{'STOP':>7}{'TGT':>7}"
          f"{'SH':>4}{'VALUE':>9}{'RISK':>7}{'WIN':>7}  LIMITED BY")
    print("-" * 78)

    tradeable = 0
    rejected = []
    for symbol in symbols:
        row = compute_bracket(symbol, prices[symbol], equity,
                              atr_pct=atr_table[symbol], settings=settings)
        if row["status"] != OK:
            rejected.append((symbol, prices[symbol], row["note"]))
            continue
        tradeable += 1
        print(f"{symbol:<6}{row['price']:>8.2f}"
              f"{row['atr_percent'] * 100:>6.2f}%"
              f"{row['stop_percent'] * 100:>6.2f}%"
              f"{row['target_percent'] * 100:>6.2f}%"
              f"{row['shares']:>4}"
              f"{row['position_value']:>9.2f}"
              f"{row['risk_dollars']:>7.2f}"
              f"{row['reward_dollars']:>7.2f}"
              f"  {row['limited_by']}")

    print("-" * 78)
    print(f"Tradeable at ${equity:,.2f}: {tradeable} of {len(symbols)}")

    if rejected:
        print(f"\nToo expensive for the risk budget ({len(rejected)}):")
        for symbol, price, note in rejected:
            print(f"  {symbol:<6} ${price:>7.2f}  {note}")

    print()
    print("Compare with today's fixed bracket: every trade uses a 2% stop and")
    print(f"a 4% target, buying {settings['max_position_percent'] * 100:.0f}% of "
          f"equity (${equity * settings['max_position_percent']:,.2f}) and risking "
          f"${equity * settings['max_position_percent'] * 0.02:,.2f} regardless "
          f"of the stock.")
    return 0


# ---------------------------------------------------------------------------
# Offline self-test
# ---------------------------------------------------------------------------

def self_test():
    passed, failed = [], []

    def check(name, condition):
        (passed if condition else failed).append(name)
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}")

    print("Running offline self-test (no network, no account access)")
    print("-" * 66)

    s = {
        "stop_multiplier": 1.5,
        "reward_ratio": 2.0,
        "min_stop": 0.015,
        "max_stop": 0.060,
        "max_risk_percent": 0.01,
        "max_position_percent": 0.40,
        "source": "test",
    }

    # --- stop sizing ------------------------------------------------
    stop, _ = stop_percent_for(0.02, s)
    check("a 2% mover gets a 3% stop", abs(stop - 0.03) < 1e-9)

    stop, note = stop_percent_for(0.005, s)
    check("a very quiet stock is raised to the 1.5% floor",
          abs(stop - 0.015) < 1e-9 and "minimum" in note)

    stop, note = stop_percent_for(0.20, s)
    check("a very wild stock is capped at the 6% ceiling",
          abs(stop - 0.060) < 1e-9 and "capped" in note)

    stop, note = stop_percent_for(None, s)
    check("no movement data falls back to the fixed 2% stop",
          abs(stop - 0.02) < 1e-9 and "fixed" in note)

    # --- the core promise: risk stays flat as the stop widens --------
    tight = compute_bracket("AAA", 10.0, 10000.0, atr_pct=0.014, settings=s)
    wide = compute_bracket("BBB", 10.0, 10000.0, atr_pct=0.035, settings=s)
    check("a wider stop produces a smaller position",
          wide["position_value"] < tight["position_value"])
    # Two different constraints bind in these two cases, and that is correct:
    # the quiet stock hits the 40% position cap first (so it ends up risking
    # LESS than the budget), while the volatile one is limited by the risk
    # budget itself and lands right at it.
    check("the quiet stock is limited by the position cap",
          tight["limited_by"] == "position cap")
    check("the volatile stock is limited by the risk budget",
          wide["limited_by"] == "risk budget")
    check("when the risk budget binds, risk lands close to it",
          abs(wide["risk_dollars"] - 100.0) < 5)
    check("when the position cap binds, risk comes in under budget",
          tight["risk_dollars"] < 100.0)

    # --- caps are never exceeded ------------------------------------
    cases = [
        ("quiet", 0.008), ("normal", 0.02), ("wild", 0.09), ("none", None),
    ]
    risk_ok, pos_ok = True, True
    for _, atr in cases:
        for price in (3.0, 27.5, 49.99):
            row = compute_bracket("X", price, 250.0, atr_pct=atr, settings=s)
            if row["status"] != OK:
                continue
            if row["risk_percent"] > s["max_risk_percent"] + 1e-9:
                risk_ok = False
            if row["position_percent"] > s["max_position_percent"] + 1e-9:
                pos_ok = False
    check("risk per trade never exceeds the 1% budget", risk_ok)
    check("position never exceeds the 40% cap", pos_ok)

    # --- reward shape ------------------------------------------------
    row = compute_bracket("VZ", 47.36, 100000.0, atr_pct=0.0242, settings=s)
    check("target is exactly twice the stop",
          abs(row["target_percent"] - row["stop_percent"] * 2) < 1e-9)
    check("a winner pays about twice what a loser costs",
          abs(row["reward_dollars"] - row["risk_dollars"] * 2) < 0.05)

    # --- direction ---------------------------------------------------
    long_row = compute_bracket("F", 12.0, 5000.0, atr_pct=0.0278, settings=s)
    check("long stop sits below the entry price",
          long_row["stop_price"] < long_row["price"])
    check("long target sits above the entry price",
          long_row["target_price"] > long_row["price"])

    short_row = compute_bracket("F", 12.0, 5000.0, atr_pct=0.0278,
                                side="short", settings=s)
    check("short stop sits above the entry price",
          short_row["stop_price"] > short_row["price"])
    check("short target sits below the entry price",
          short_row["target_price"] < short_row["price"])

    # --- whole shares and rejection ----------------------------------
    row = compute_bracket("Y", 33.33, 250.0, atr_pct=0.02, settings=s)
    check("share count is a whole number",
          isinstance(row.get("shares"), int))

    broke = compute_bracket("Z", 49.00, 250.0, atr_pct=0.05, settings=s)
    check("a stock too expensive for the risk budget is rejected",
          broke["status"] == NO_SHARES and broke["shares"] == 0)
    check("the rejection explains itself in dollars",
          "$" in broke["note"])

    # --- bad input ---------------------------------------------------
    for bad_price in (0, -5, "abc", None):
        row = compute_bracket("Q", bad_price, 250.0, atr_pct=0.02, settings=s)
        if row["status"] not in (BAD_PRICE,):
            check(f"a bad price ({bad_price!r}) is rejected", False)
            break
    else:
        check("bad prices are rejected rather than crashing", True)

    row = compute_bracket("Q", 10.0, 0, atr_pct=0.02, settings=s)
    check("zero equity is rejected", row["status"] == BAD_EQUITY)

    # --- the two real cases that started all this --------------------
    vteb = compute_bracket("VTEB", 49.74, 250.0, atr_pct=0.0030, settings=s)
    check("VTEB gets the 1.5% floor, not a 0.45% stop",
          abs(vteb["stop_percent"] - 0.015) < 1e-9)

    celh = compute_bracket("CELH", 29.19, 250.0, atr_pct=0.0449, settings=s)
    check("CELH gets a stop wider than its daily move, not 2%",
          celh["stop_percent"] > 0.0449)
    check("CELH's old 2% stop was inside its daily noise", 0.02 < 0.0449)

    # --- movement table parsing --------------------------------------
    tmp = "_selftest_atr_report.csv"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["symbol", "atr_percent", "atr_dollars",
                        "last_close", "bars_used", "decision"])
            w.writerow(["vz", "2.42", "1.14", "47.36", "40", "KEEP"])
            w.writerow(["BAD", "", "", "", "0", "NO_DATA"])
            w.writerow(["OOPS", "not-a-number", "", "", "40", "KEEP"])
        table = load_atr_table(tmp)
        check("movement table converts percent to fraction",
              abs(table.get("VZ", 0) - 0.0242) < 1e-9)
        check("rows without a measurement are skipped", "BAD" not in table)
        check("unparseable rows are skipped", "OOPS" not in table)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    check("a missing report file returns an empty table rather than raising",
          load_atr_table("_does_not_exist_.csv") == {})

    print("-" * 66)
    print(f"{len(passed)} passed, {len(failed)} failed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    print("All checks passed.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Per-stock stop, target, and position size for LOCKBOT."
    )
    parser.add_argument("--self-test", action="store_true",
                        help="offline math check, touches nothing")
    parser.add_argument("--preview", action="store_true",
                        help="show what every universe symbol would get")
    parser.add_argument("--equity", type=float, default=250.0,
                        help="account size to preview against (default 250)")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.preview:
        return preview(equity=args.equity)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())