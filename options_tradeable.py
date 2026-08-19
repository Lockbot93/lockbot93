"""
options_tradeable.py -- what can LOCKBOT actually buy right now?

READ ONLY. No orders, no state changes. Fetches live chains and applies
the real entry gates, then writes a timestamped report.

WHY THIS EXISTS
    Options went live on 2026-08-14 and the binding question is not
    whether the signal is good -- it is whether ANY contract survives the
    5% spread gate during market hours. Measured after hours on 2026-08-14
    the answer was zero of 644 contracts, but overnight books are wide and
    that number means nothing. Only a live-session run answers it.

    The quote sampler in options_scanner records chains for the one to
    three stocks that produce a SIGNAL each cycle. This covers the WHOLE
    universe, which is a different question: not "what did we look at"
    but "what was available".

WHAT IT CANNOT TELL YOU
    Which options will be profitable. Spread is a cost paid for certain;
    the move is a guess. This ranks the certain part and says nothing
    about the other.

USAGE
    python options_tradeable.py                 scan and print
    python options_tradeable.py --save          also write a report file
    python options_tradeable.py --limit 30      sample N stocks, not all
    python options_tradeable.py --self-test     offline checks
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lockbot_config as config

VERSION = "1.0"
REPORT_PREFIX = "options_tradeable_"


def gates_for(equity: float) -> dict[str, Any]:
    """Every live entry gate, read from config -- never a local copy."""

    return dict(
        account_equity=equity,
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


def scan(limit: int | None = None, verbose: bool = True) -> dict[str, Any]:
    """Fetch live chains across the universe and apply the entry gates."""

    import options_contracts as contracts
    from lockbot_startup_reconciliation import get_trading_client
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=str(config.PROJECT_FOLDER / ".env"))

    trading = get_trading_client()
    clock = trading.get_clock()
    equity = float(trading.get_account().equity)

    option_data = OptionHistoricalDataClient(
        os.getenv(config.ALPACA_API_KEY_ENV),
        os.getenv(config.ALPACA_SECRET_KEY_ENV),
    )

    with open(config.UNIVERSE_FILE, newline="", encoding="utf-8") as handle:
        universe = list(csv.DictReader(handle))

    if limit:
        universe = universe[:limit]

    gates = gates_for(equity)
    cap = equity * config.OPTIONS_MAX_RISK_PER_TRADE_PERCENT

    if verbose:
        print("=" * 78)
        print(f"WHAT LOCKBOT CAN BUY RIGHT NOW   v{VERSION}")
        print("=" * 78)
        print(f"  market open : {clock.is_open}")
        if not clock.is_open:
            print("  WARNING: books are WIDE outside market hours. A run now")
            print("  measures the overnight book, not the one LOCKBOT trades.")
        print(f"  equity      : ${equity:,.2f}   debit cap ${cap:,.2f}")
        print(f"  spread gate : {config.OPTIONS_MAX_SPREAD_PERCENT:.1%} of mid")
        print(f"  universe    : {len(universe)} stocks\n")

    passing: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    listed = 0
    scanned = 0

    for row in universe:
        symbol = row["symbol"]
        try:
            price = float(row["last_close"])
        except (TypeError, ValueError, KeyError):
            continue

        for kind in ("call", "put"):
            try:
                quotes = contracts.fetch_chain_quotes(
                    option_data, underlying_symbol=symbol,
                    contract_type=kind, underlying_price=price,
                    min_dte=config.OPTIONS_MIN_DTE,
                    max_dte=config.OPTIONS_MAX_DTE,
                )
            except Exception:
                continue

            listed += len(quotes)

            for quote in quotes:
                verdict = contracts.evaluate_contract(
                    quote, underlying_price=price, **gates)

                if verdict.accepted:
                    spread = (quote.ask - quote.bid) / quote.mid if quote.mid else None
                    passing.append({
                        "symbol": symbol,
                        "option_symbol": quote.symbol,
                        "type": kind,
                        "strike": quote.strike,
                        "expiry": str(quote.expiration),
                        "dte": quote.days_to_expiration,
                        "bid": quote.bid,
                        "ask": quote.ask,
                        "cost": round(quote.cost_to_open, 2),
                        "spread_percent": None if spread is None else round(spread * 100, 2),
                        "delta": quote.delta,
                    })
                else:
                    reasons[verdict.status] = reasons.get(verdict.status, 0) + 1

        scanned += 1

        if verbose and scanned % 25 == 0:
            print(f"  ...{scanned}/{len(universe)} stocks, "
                  f"{len(passing)} tradable so far")

    passing.sort(key=lambda r: (r["spread_percent"] is None,
                                r["spread_percent"], r["cost"]))

    return {
        "market_open": clock.is_open, "equity": equity, "cap": cap,
        "stocks": scanned, "listed": listed, "passing": passing,
        "reasons": reasons,
        "stamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def report(result: dict[str, Any], top: int = 100) -> None:
    passing = result["passing"]

    print("\n" + "=" * 78)
    print(f"TRADABLE RIGHT NOW: {len(passing)} of {result['listed']} contracts "
          f"across {result['stocks']} stocks")
    print("=" * 78)

    if not passing:
        print("  NOTHING passes the gates.")
        if not result["market_open"]:
            print("  Expected with the market closed -- rerun during a session.")
        else:
            print("  This is during market hours, so it is the real answer:")
            print("  LOCKBOT will place no options trades under these settings.")
    else:
        print(f"{'stock':<7}{'type':<6}{'strike':>9}{'dte':>5}{'cost':>9}"
              f"{'spread':>9}{'delta':>8}  contract")
        print("-" * 78)
        for row in passing[:top]:
            delta = "--" if row["delta"] is None else f"{row['delta']:.2f}"
            print(f"{row['symbol']:<7}{row['type']:<6}{row['strike']:>9.2f}"
                  f"{row['dte']:>5}{row['cost']:>9.2f}"
                  f"{row['spread_percent']:>8.2f}%{delta:>8}  {row['option_symbol']}")
        if len(passing) > top:
            print(f"  ... and {len(passing) - top} more")

        costs = [r["cost"] for r in passing]
        spreads = [r["spread_percent"] for r in passing
                   if r["spread_percent"] is not None]
        print("-" * 78)
        print(f"  cost   median ${statistics.median(costs):.2f}  "
              f"cheapest ${min(costs):.2f}")
        if spreads:
            print(f"  spread median {statistics.median(spreads):.2f}%  "
                  f"tightest {min(spreads):.2f}%")
        print(f"  distinct stocks with something tradable: "
              f"{len({r['symbol'] for r in passing})}")

    print("\n  why the rest were rejected:")
    for reason, count in sorted(result["reasons"].items(), key=lambda i: -i[1]):
        print(f"    {count:>6}  {reason}")

    print("\n  This ranks what is CHEAP TO TRADE, not what will be profitable.")
    print("  Spread is a cost paid for certain; the move is a guess.")


def save(result: dict[str, Any]) -> Path:
    stamp = result["stamp"].replace(":", "").replace("-", "")[:15]
    path = config.PROJECT_FOLDER / f"{REPORT_PREFIX}{stamp}.csv"

    rows = result["passing"]
    if not rows:
        path.write_text("no tradable contracts\n", encoding="utf-8")
        return path

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    return path


def notify(result: dict[str, Any]) -> str:
    """Push the headline to the phone. A file nobody opens answers nothing.

    Never raises -- a failed alert must not look like a failed scan.
    """

    passing = result["passing"]
    stocks = len({r["symbol"] for r in passing})

    if passing:
        best = passing[0]
        title = f"{len(passing)} option(s) tradable"
        message = (
            f"{len(passing)} of {result['listed']} contracts pass, "
            f"across {stocks} stocks. Tightest: {best['symbol']} "
            f"{best['strike']:.0f}{best['type'][0].upper()} at "
            f"{best['spread_percent']:.1f}% spread, ${best['cost']:.0f}."
        )
    else:
        title = "Nothing tradable"
        widest = result["reasons"].get("SPREAD_TOO_WIDE", 0)
        message = (
            f"0 of {result['listed']} contracts pass at ${result['equity']:,.0f} "
            f"equity. {widest} rejected on spread. "
            f"{'Market OPEN -- this is real.' if result['market_open'] else 'Market closed.'}"
        )

    try:
        import notifications
        status = notifications.send_smart_notification(
            symbol="OPTIONS", event_type="TRADEABLE_SNAPSHOT",
            title=title, message=message, force=True)
        return str(status)
    except Exception as error:  # noqa: BLE001 -- alerting is never fatal
        return f"notification failed: {type(error).__name__}: {error}"


def _boom(**_kwargs):
    raise RuntimeError("pushover is down")


def _self_test() -> int:
    failures = []

    def check(label, condition):
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")

    print("Gates come from config, never a local copy")
    g = gates_for(650.0)
    check("spread gate matches config",
          g["max_spread_percent"] == config.OPTIONS_MAX_SPREAD_PERCENT)
    check("delta band matches config",
          g["delta_min"] == config.OPTIONS_TARGET_DELTA_MIN
          and g["delta_max"] == config.OPTIONS_TARGET_DELTA_MAX)
    check("risk cap matches config",
          g["max_risk_percent"] == config.OPTIONS_MAX_RISK_PER_TRADE_PERCENT)
    check("the cap scales with equity",
          gates_for(1300.0)["account_equity"] == 1300.0)

    print("\nIt is read only")
    src = Path(__file__).read_text(encoding="utf-8")
    body = src.split("def _self_test")[0]
    check("no order submission", "submit_order" not in body)
    check("no position or state writes",
          "save_positions" not in body and "risk_state" not in body)
    check("the only file written is its own report",
          body.count('.open("w"') == 1 and "REPORT_PREFIX" in body)

    print("\nThe alert reports the empty answer as loudly as a full one")
    import notifications
    sent = []
    real = notifications.send_smart_notification
    notifications.send_smart_notification = (
        lambda **kw: sent.append(kw) or "SENT")
    try:
        empty = {"passing": [], "listed": 644, "equity": 651.4,
                 "market_open": True, "reasons": {"SPREAD_TOO_WIDE": 585}}
        check("an empty result still sends", notify(empty) == "SENT")
        check("it says nothing is tradable",
              "Nothing tradable" in sent[-1]["title"])
        check("and that the market was open, so the zero is real",
              "Market OPEN" in sent[-1]["message"])

        full = {"passing": [{"symbol": "IBIT", "strike": 36.0, "type": "call",
                             "spread_percent": 1.9, "cost": 32.0}],
                "listed": 644, "equity": 651.4, "market_open": True,
                "reasons": {}}
        check("a full result names the tightest contract",
              notify(full) == "SENT" and "IBIT" in sent[-1]["message"])

        notifications.send_smart_notification = _boom
        check("a broken alert never raises",
              notify(empty).startswith("notification failed"))
    finally:
        notifications.send_smart_notification = real

    print("\nIt refuses to claim profitability")
    check("the docstring says what it cannot tell you",
          "Which options will be profitable" in (__doc__ or ""))
    check("and the report repeats it",
          "not what will be profitable" in body)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("All options-tradeable checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="What can LOCKBOT buy now?")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    result = scan(limit=args.limit)
    report(result, top=args.top)

    if args.save:
        print(f"\n  written to {save(result).name}")

    if args.notify:
        print(f"  notification: {notify(result)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
