"""
exposure_groups.py  --  count correlated names as one bet, not many

WHY THIS EXISTS

    An independent read of the equity shadow book on 2026-08-29 reported
    that long setups entered before 3pm averaged +0.10R, and called it the
    first positive slice this project had produced.

    It then retracted its own finding. Fifty-four crypto-linked rows
    carried the entire result:

        long, before 3pm, all names            469 setups   +0.10R
        long, before 3pm, crypto removed       415 setups   -0.05R
        long, before 3pm, crypto only           54 setups   +1.25R

    And inside those names the shape was one bet, not many:

        IBIT long   37   +0.85R        IBIT short  16   -1.00R
        BITO long   23   +1.28R        BITO short   7   -1.00R
                                       ETHA short   6   -1.00R

    Every long won, every short lost, on instruments tracking the same
    asset. Bitcoin rose during the sample. That is ONE observation
    reported as sixty.

    The duplication was flagged in July -- BITO and IBIT were both noted
    as sitting in the universe as versions of each other -- and nothing
    was done, so it went on to contaminate an analysis.

WHAT THIS IS AND IS NOT

    It is a GROUPING KEY for measurement. It changes no trade, no gate and
    no order. It lets a report say "1 bitcoin bet" where the raw file says
    "158 independent rows".

    It does NOT deduplicate the universe or block entries. Whether LOCKBOT
    should hold two versions of the same asset at once is a separate
    question for the exposure caps, and answering it here would smuggle a
    trading change into a reporting fix.

WHY A HAND-KEPT LIST RATHER THAN MEASURED CORRELATION

    Measured correlation would be circular: it is computed on the same
    window whose result is in question, and two assets that happened to
    move together in one rally would be grouped BECAUSE of the rally the
    grouping exists to discount. A named list is auditable, arguable and
    wrong in visible ways. A fitted one is wrong invisibly.

    Every entry therefore carries its reason, and anything unrecognised
    returns its own symbol -- an unknown name is its own bet, never
    silently folded into a group.

USAGE
    python exposure_groups.py --self-test
    python exposure_groups.py --audit shadow_trades.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

VERSION = "1.0"

# symbol -> (group, why). The reason is not decoration: a future reader
# has to be able to disagree with a specific claim rather than a vibe.
GROUPS: dict[str, tuple[str, str]] = {
    "IBIT": ("BITCOIN", "spot bitcoin ETF"),
    "BITO": ("BITCOIN", "bitcoin futures ETF -- same asset as IBIT"),
    "FBTC": ("BITCOIN", "spot bitcoin ETF"),
    "ARKB": ("BITCOIN", "spot bitcoin ETF"),
    "BITB": ("BITCOIN", "spot bitcoin ETF"),
    "GBTC": ("BITCOIN", "spot bitcoin trust"),
    "MSTR": ("BITCOIN", "balance sheet is a bitcoin position"),
    "MARA": ("BITCOIN", "miner -- revenue tracks bitcoin"),
    "RIOT": ("BITCOIN", "miner"),
    "CLSK": ("BITCOIN", "miner"),
    "CIFR": ("BITCOIN", "miner"),
    "WULF": ("BITCOIN", "miner"),
    "HUT": ("BITCOIN", "miner"),
    "BTBT": ("BITCOIN", "miner"),
    "HIVE": ("BITCOIN", "miner"),
    "COIN": ("BITCOIN", "exchange -- revenue tracks crypto volume"),

    "ETHA": ("ETHEREUM", "spot ether ETF"),
    "ETHE": ("ETHEREUM", "ether trust"),

    # Long-duration US treasuries. Different tickers, one rates bet, and
    # they are also the quietest names in the book -- the 0-for-55 rows.
    "TLT": ("LONG_TREASURY", "20+ year treasuries"),
    "TLH": ("LONG_TREASURY", "10-20 year treasuries"),
    "EDV": ("LONG_TREASURY", "extended duration treasuries"),
    "ZROZ": ("LONG_TREASURY", "zero-coupon long treasuries"),

    "LQD": ("IG_CREDIT", "investment grade corporate bonds"),
    "VCIT": ("IG_CREDIT", "intermediate IG corporates"),
    "VCLT": ("IG_CREDIT", "long IG corporates"),
    "AGG": ("IG_CREDIT", "aggregate bond index -- mostly IG and treasuries"),
    "BND": ("IG_CREDIT", "aggregate bond index"),

    "HYG": ("HIGH_YIELD", "high yield corporate bonds"),
    "JNK": ("HIGH_YIELD", "high yield corporate bonds"),

    "SPY": ("US_LARGE", "S&P 500"),
    "IVV": ("US_LARGE", "S&P 500"),
    "VOO": ("US_LARGE", "S&P 500"),
    "QQQ": ("US_LARGE", "Nasdaq 100 -- overlaps SPY heavily at the top"),
    "SCHG": ("US_LARGE", "large cap growth"),
    "SCHD": ("US_LARGE", "large cap dividend"),

    "GLD": ("GOLD", "spot gold"),
    "IAU": ("GOLD", "spot gold"),
    "GDX": ("GOLD", "gold miners -- levered to the same metal"),
}


def group_of(symbol: str) -> str:
    """The exposure group for one symbol, or the symbol itself.

    An unrecognised name is ITS OWN GROUP. Folding an unknown into a
    catch-all would understate independence in exactly the direction that
    flatters a result.
    """

    key = (symbol or "").strip().upper()

    if not key:
        return ""

    return GROUPS.get(key, (key, ""))[0]


def reason_for(symbol: str) -> str:
    """Why a symbol is grouped where it is. Empty when ungrouped."""

    return GROUPS.get((symbol or "").strip().upper(), ("", ""))[1]


def is_grouped(symbol: str) -> bool:
    """Whether this symbol shares a group with anything else."""

    return (symbol or "").strip().upper() in GROUPS


def independent_count(symbols) -> int:
    """How many genuinely separate bets a list of symbols represents.

    The number the shadow book should have been reporting. 158 crypto
    rows across six tickers are not 158 observations.
    """

    return len({group_of(s) for s in symbols if (s or "").strip()})


def summarise(rows, symbol_key: str = "symbol") -> dict[str, Counter]:
    """Row counts per exposure group, for reporting."""

    out: dict[str, Counter] = {}

    for row in rows:
        symbol = (row.get(symbol_key) or "").strip().upper()

        if not symbol:
            continue

        out.setdefault(group_of(symbol), Counter())[symbol] += 1

    return out


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}"
              + (f" - {detail}" if detail and not condition else ""))

    print("The names that contaminated the 08-29 analysis group together")
    for sym in ("IBIT", "BITO", "MSTR", "CIFR", "WULF"):
        check(f"{sym} is BITCOIN", group_of(sym) == "BITCOIN", group_of(sym))
    check("ETHA is its own asset, not folded into bitcoin",
          group_of("ETHA") == "ETHEREUM", group_of("ETHA"))

    print()
    print("An unknown name is its OWN bet, never folded in")
    check("an unlisted symbol returns itself",
          group_of("NVDA") == "NVDA", group_of("NVDA"))
    check("case does not matter", group_of("ibit") == "BITCOIN")
    check("whitespace does not matter", group_of("  BITO ") == "BITCOIN")
    check("an empty symbol is empty, not a group", group_of("") == "")

    print()
    print("Counting bets, not rows")
    # The exact shape that produced the false +0.10R.
    crypto = ["IBIT"] * 37 + ["BITO"] * 23 + ["ETHA"] * 15
    check("75 crypto rows are 2 bets, not 75",
          independent_count(crypto) == 2, str(independent_count(crypto)))
    mixed = ["IBIT", "BITO", "NVDA", "AAPL"]
    check("mixed counts groups and singletons together",
          independent_count(mixed) == 3, str(independent_count(mixed)))
    check("an empty list is zero bets", independent_count([]) == 0)

    print()
    print("The quiet names that went 0-for-55 are grouped too")
    for sym in ("TLT", "EDV"):
        check(f"{sym} is LONG_TREASURY", group_of(sym) == "LONG_TREASURY")
    for sym in ("LQD", "VCIT", "VCLT", "AGG"):
        check(f"{sym} is IG_CREDIT", group_of(sym) == "IG_CREDIT")
    check("HYG is high yield, not investment grade",
          group_of("HYG") == "HIGH_YIELD", group_of("HYG"))

    print()
    print("Every grouping states a reason it can be argued with")
    check("no entry has an empty reason",
          all(why.strip() for _, why in GROUPS.values()),
          str([s for s, (_, w) in GROUPS.items() if not w.strip()]))
    check("an ungrouped symbol has no reason", reason_for("NVDA") == "")

    print()
    print("It changes nothing that trades")
    src = Path(__file__).read_text(encoding="utf-8").split("def _self_test")[0]
    check("no order submission", "submit_order" not in src)
    check("it does not read config", "lockbot_config" not in src)
    check("it writes no files", ".write(" not in src and 'open(' not in src)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All exposure-group checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Count correlated names as one bet")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--audit", metavar="CSV",
                        help="report how many bets a shadow file really holds")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if not args.audit:
        parser.print_help()
        return 0

    path = Path(args.audit)

    if not path.exists():
        print(f"  no such file: {path}")
        return 1

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    groups = summarise(rows)
    shared = {g: c for g, c in groups.items() if len(c) > 1}

    print(f"EXPOSURE AUDIT v{VERSION}  --  {path.name}")
    print(f"  {len(rows)} rows across {len(groups)} exposure groups")
    print()
    print("  Groups holding MORE THAN ONE ticker -- these are the rows a")
    print("  report would otherwise count as independent:")

    for group, counter in sorted(shared.items(),
                                 key=lambda kv: -sum(kv[1].values())):
        total = sum(counter.values())
        names = ", ".join(f"{s} {n}" for s, n in counter.most_common())
        print(f"    {group:<16} {total:>5} rows over {len(counter)} tickers"
              f"   ({names})")

    inflated = sum(sum(c.values()) - 1 for c in shared.values())
    print()
    print(f"  {inflated} rows overstate independence if counted one-by-one.")
    print("  A report must state every slice with and without these.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
