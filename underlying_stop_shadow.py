"""
underlying_stop_shadow.py  --  measure a stop LOCKBOT does not yet use

WHY THIS EXISTS

    Every one of the nine option trades closed to date died on the stop.
    None timed out; none reached target. Measured 2026-08-24:

        died inside 24 hours     6 of 9
        configured stop          -35%
        actually realised        -47% mean
        mean overshoot           +12% past the stop
        worst                    +49%   (GDX, twelve minutes)

    The stop is not behaving like a -35% stop. The gap between configured
    and realised is not slippage on the sell -- it is the premium gapping
    through with nothing in between to exit at.

    The owner's playbook (Rule 7, 2026-08-25) stops on a different
    quantity entirely: the UNDERLYING closing through the strike by a
    fixed buffer, rather than the PREMIUM falling by a percentage. That is
    worth measuring precisely because it is not a re-tuning of the rule
    that is failing -- it is a different trigger on a different series.

WHY IT ONLY WATCHES

    Arming an untested exit on a live book would replace one unmeasured
    stop with another. So this decides nothing. It writes one row per open
    position per cycle recording what BOTH rules said AT THE SAME INSTANT,
    which is the only way to answer "would the other stop have been
    better" without comparing one month against another.

    LOCKBOT is offline (Anthropic balance empty, 2026-08-24) and cannot
    rule on arming it. This accumulates the evidence it will need.

    ONE DELIBERATE DEPARTURE FROM THE PLAYBOOK. Rule 7 exits on the NEXT
    trading session. Six of the nine losses died within 24 hours, from
    exactly the overnight gap a next-session rule sits through, so the
    shadow evaluates SAME-SESSION firing. A rule that waits for tomorrow
    is exposed to the mechanism doing the damage.

FIRST RESULT, 2026-08-24, ON THE FIRST CYCLE IT EVER RAN

    Both open positions logged KEN_ONLY -- the playbook stop fired, the
    live premium stop did not:

        F     strike 14.50   spot 13.9250   level 14.00   premium -3.4%
        NOK   strike 11.00   spot  9.9550   level 10.50   premium  0.0%

    Neither is a stop being hit. Both calls are OUT of the money, so spot
    sits BELOW the strike by definition, and "strike minus 50c" is
    therefore breached the instant the contract is bought. NOK was $1.045
    out of the money at entry.

    RULE 7 IS NOT WRITTEN FOR THESE CONTRACTS. It is the partner of
    Rule 2, which buys DEEP IN the money at delta > 0.75 -- where spot
    sits far ABOVE the strike and "strike minus 50c" is a real level some
    distance below the price. LOCKBOT cannot afford that tier: measured
    2026-08-24, the cheapest delta > 0.75 call across five of the
    cheapest names in the universe was $120 against a $37.13 ceiling, and
    the tier is 3-8x out of reach at this account size.

    So the two rules are a matched pair and only one half is affordable.
    Taking Rule 7 alone converts it into "exit immediately, always".

    THE FINDING IS RECORDED AND NOT RESCUED. An obvious repair suggests
    itself -- measure the stop from the underlying price AT ENTRY rather
    than from the strike -- and that is a DIFFERENT RULE, not this one.
    Inventing it here, after seeing the result, is precisely the re-cut
    that the pre-registrations in CLAUDE.md exist to forbid. If it is ever
    wanted it needs its own registration written before its first bar.

    The log keeps running. It costs nothing, it will record the same
    verdict for as long as LOCKBOT holds out-of-the-money contracts, and
    it will start carrying information the moment anything in the money is
    ever bought.

WHAT IT WILL NOT DO

    Submit, modify, cancel or price any order. Touch position state. Write
    any file but its own log. It is called from a try/except so a failure
    here can never interfere with a real exit -- the exit path is the most
    safety-critical code in the project and a measurement must never be
    able to break it.

USAGE
    python underlying_stop_shadow.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lockbot_config as config

VERSION = "1.0"

COLUMNS = [
    "timestamp", "underlying", "long_symbol", "contract_type", "strike",
    "underlying_price", "ken_stop_level", "ken_would_fire",
    "entry_debit", "current_value", "premium_return",
    "premium_stop_percent", "premium_would_fire", "verdict", "rule_param",
]

AGREE_HOLD = "AGREE_HOLD"
AGREE_EXIT = "AGREE_EXIT"
KEN_ONLY = "KEN_ONLY"
PREMIUM_ONLY = "PREMIUM_ONLY"


def log_path() -> Path:
    return Path(getattr(
        config, "UNDERLYING_STOP_SHADOW_FILE",
        config.PROJECT_FOLDER / "underlying_stop_shadow.csv"))


def stop_level(strike: Any, contract_type: Any, buffer: Any) -> float | None:
    """The underlying price at which the playbook rule would fire.

    A call is stopped BELOW the strike and a put ABOVE it. Getting that
    backwards would produce a log that looks populated and means the
    opposite of what it says, which is worse than no log at all.
    """

    try:
        strike = float(strike)
        buffer = float(buffer)
    except (TypeError, ValueError):
        return None

    kind = str(contract_type).strip().lower()

    if kind == "call":
        return strike - buffer

    if kind == "put":
        return strike + buffer

    return None


def would_fire(underlying_price: Any, strike: Any, contract_type: Any,
               buffer: Any) -> bool | None:
    """Whether the playbook stop is breached. None when it cannot be known.

    None rather than False for a missing price: a stop that could not be
    evaluated has not been passed, and recording it as "did not fire"
    would enter a non-observation into the sample as evidence for holding.
    A default value is a claim.
    """

    level = stop_level(strike, contract_type, buffer)

    if level is None:
        return None

    try:
        price = float(underlying_price)
    except (TypeError, ValueError):
        return None

    if price <= 0:
        return None

    kind = str(contract_type).strip().lower()

    return price < level if kind == "call" else price > level


def premium_return(current_value: Any, entry_debit: Any) -> float | None:
    """Return on the premium, or None. Never 0.0 for unusable input."""

    try:
        value = float(current_value)
        debit = float(entry_debit)
    except (TypeError, ValueError):
        return None

    if debit <= 0 or value < 0:
        return None

    return (value - debit) / debit


def verdict_for(ken: bool | None, premium: bool | None) -> str | None:
    """Which rule fired. None when either side could not be evaluated."""

    if ken is None or premium is None:
        return None

    if ken and premium:
        return AGREE_EXIT

    if ken:
        return KEN_ONLY

    if premium:
        return PREMIUM_ONLY

    return AGREE_HOLD


def observe(position: Any, *, underlying_price: Any, current_value: Any,
            buffer: float, stop_loss_percent: float,
            now: datetime | None = None) -> dict[str, Any] | None:
    """One paired observation, or None if the position cannot be read."""

    import options_contracts as contracts

    try:
        parts = contracts.parse_occ_symbol(position.long_symbol)
    except (ValueError, AttributeError):
        return None

    ken = would_fire(underlying_price, parts.strike, parts.contract_type,
                     buffer)
    ret = premium_return(current_value, getattr(position, "entry_debit", None))
    premium = None if ret is None else ret <= -abs(stop_loss_percent)
    level = stop_level(parts.strike, parts.contract_type, buffer)
    stamp = (now or datetime.now(timezone.utc)).isoformat()

    def num(value: Any, spec: str) -> str:
        return "" if value is None else format(value, spec)

    try:
        price = float(underlying_price)
    except (TypeError, ValueError):
        price = None

    try:
        value = float(current_value)
    except (TypeError, ValueError):
        value = None

    return {
        "timestamp": stamp,
        "underlying": getattr(position, "underlying", ""),
        "long_symbol": position.long_symbol,
        "contract_type": parts.contract_type,
        "strike": f"{parts.strike:.2f}",
        "underlying_price": num(price, ".4f"),
        "ken_stop_level": num(level, ".2f"),
        "ken_would_fire": "" if ken is None else str(ken).lower(),
        "entry_debit": num(getattr(position, "entry_debit", None), ".2f"),
        "current_value": num(value, ".2f"),
        "premium_return": num(ret, ".4f"),
        "premium_stop_percent": f"{stop_loss_percent:.4f}",
        "premium_would_fire": "" if premium is None else str(premium).lower(),
        "verdict": verdict_for(ken, premium) or "",
        # The buffer in force when the row was written. A parameter change
        # splits the population, and an untagged row cannot be assigned to
        # either cohort afterwards.
        "rule_param": f"{float(buffer):.2f}",
    }


def append(rows: list[dict[str, Any]]) -> int:
    """Append observations. Writes this file and no other."""

    rows = [row for row in rows if row]

    if not rows:
        return 0

    path = log_path()
    exists = path.exists()

    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)

        if not exists:
            writer.writeheader()

        for row in rows:
            writer.writerow({key: row.get(key, "") for key in COLUMNS})

    return len(rows)


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}"
              + (f" - {detail}" if detail and not condition else ""))

    print("A call stops BELOW the strike, a put ABOVE it")
    check("call level is strike minus the buffer",
          stop_level(14.5, "call", 0.50) == 14.0,
          str(stop_level(14.5, "call", 0.50)))
    check("put level is strike plus the buffer",
          stop_level(90.0, "put", 0.50) == 90.5,
          str(stop_level(90.0, "put", 0.50)))
    check("an unknown type gives None, not a guessed side",
          stop_level(14.5, "straddle", 0.50) is None)

    print()
    print("Firing is directional")
    check("a call fires when the underlying falls through",
          would_fire(13.90, 14.5, "call", 0.50) is True)
    check("a call does NOT fire above the level",
          would_fire(14.10, 14.5, "call", 0.50) is False)
    check("a put fires when the underlying rises through",
          would_fire(90.60, 90.0, "put", 0.50) is True)
    check("a put does NOT fire below the level",
          would_fire(90.40, 90.0, "put", 0.50) is False)
    check("exactly at the level does not fire -- the test is strict",
          would_fire(14.0, 14.5, "call", 0.50) is False)

    print()
    print("A stop that cannot be evaluated is not a stop that held")
    for bad in (None, "", "n/a", 0, -1):
        check(f"{bad!r} gives None, never False",
              would_fire(bad, 14.5, "call", 0.50) is None)

    print()
    print("Returns are None, never 0.0, for unusable input")
    check("a real return computes",
          abs(premium_return(20.0, 29.0) + 0.310344) < 1e-5,
          str(premium_return(20.0, 29.0)))
    check("a zero debit gives None, not a division",
          premium_return(20.0, 0.0) is None)
    check("garbage gives None", premium_return("x", 29.0) is None)
    check("a worthless contract is -100%, which is real, not missing",
          premium_return(0.0, 29.0) == -1.0, str(premium_return(0.0, 29.0)))

    print()
    print("The verdict pairs both rules at one instant")
    check("both fire", verdict_for(True, True) == AGREE_EXIT)
    check("neither fires", verdict_for(False, False) == AGREE_HOLD)
    check("only the playbook fires", verdict_for(True, False) == KEN_ONLY)
    check("only the premium stop fires",
          verdict_for(False, True) == PREMIUM_ONLY)
    check("an unevaluable side yields no verdict",
          verdict_for(None, False) is None and verdict_for(True, None) is None)

    print()
    print("An observation carries both sides and its cohort")

    class _P:
        underlying = "F"
        long_symbol = "F260918C00014500"
        entry_debit = 29.0

    # F at 13.90 against a 14.50 strike: the playbook stop (14.00) is
    # breached. The premium is at -31%, inside the -35% band, so the live
    # stop is NOT breached. This divergence is the whole point of the log.
    row = observe(_P(), underlying_price=13.90, current_value=20.0,
                  buffer=0.50, stop_loss_percent=0.35,
                  now=datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc))
    check("the playbook stop fired", row["ken_would_fire"] == "true", str(row))
    check("the premium stop did not, at -31% against -35%",
          row["premium_would_fire"] == "false", row["premium_would_fire"])
    check("so the row is KEN_ONLY -- the case worth counting",
          row["verdict"] == KEN_ONLY, row["verdict"])
    check("tagged with the buffer in force", row["rule_param"] == "0.50")
    check("every column is present",
          set(row.keys()) == set(COLUMNS),
          str(set(COLUMNS) ^ set(row.keys())))

    bad = observe(type("B", (), {"underlying": "X", "long_symbol": "junk",
                                 "entry_debit": 1.0})(),
                  underlying_price=10.0, current_value=1.0, buffer=0.50,
                  stop_loss_percent=0.35)
    check("an unparseable symbol is skipped, not half-written", bad is None)

    print()
    print("It cannot touch anything but its own log")
    source = Path(__file__).read_text(encoding="utf-8").split("def _self_test")[0]
    check("no order submission", "submit_order" not in source)
    check("no cancellation", "cancel_order" not in source)
    check("no exit decision returned", "ExitDecision" not in source)
    check("one file opened for writing", source.count('open(path, "a"') == 1)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All underlying-stop-shadow checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure the playbook stop without acting on it")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    print(f"UNDERLYING STOP SHADOW v{VERSION}")
    print(f"  log: {log_path().name}")
    print("  This module observes. It is called from options_manager.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
