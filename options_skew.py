"""
options_skew.py  --  an entry signal from the options market, not the chart

WHY THIS EXISTS

    options_scanner's docstring says it plainly: it "reuses market_scanner's
    signal engine unchanged -- the same 5-minute EMA/RSI/VWAP/MACD setup,
    the same confidence score, the same regime classifier."

    That signal was measured on 2026-08-05, after the VWAP fix:

        live rule       32.9%   -0.01R   p=0.7055
        random entry    36.7%   +0.10R   p=0.0002

    It loses to buying at random, and the confidence score is degenerate --
    detect_signal requires all five conditions, so every tradable setup
    scores exactly 100 and the ranking carries nothing either.

    So the options book has been a leveraged, double-spread, theta-paying
    expression of a no-information signal. 0-for-9 needs no bad luck to
    explain it. Every execution fix shipped in the week to 2026-08-24 --
    greeks, IV ceiling, side caps, spread gate -- improved the plumbing
    around a picker with nothing in it.

WHAT THIS REPLACES IT WITH

    Option-implied SKEW: the implied volatility of an out-of-the-money PUT
    minus that of an at-the-money CALL. When puts price far above calls,
    the options market is paying up for downside on that name.

    Xing, Zhang & Zhao (2010) and successors find skew negatively predicts
    stock returns cross-sectionally. It clears condition (a) of THE BAR --
    it is not a transformation of the underlying's OHLCV bars, it is a
    different market with different participants.

THE OBJECTION THAT NEARLY KILLS IT, AND THE AUGMENTATION THAT SURVIVES IT

    Muravyev, Pearson & Pollet (Journal of Financial Economics, 2025) asked
    why the signal works. Their answer: option prices embed the STOCK
    BORROW FEE, which was already known to predict returns. Predictability
    "decreases by about two-thirds after returns are adjusted for the
    borrow fees", and unadjusted returns fall similarly when high-fee
    stocks are simply EXCLUDED.

    That artifact lives on the SHORT side and in hard-to-borrow names.
    This account cannot short at all -- no shorting under $2,000 of equity
    -- so it could never have collected the borrow-fee component even if it
    wanted to.

    Hence the augmentation: take the LONG half only, buy calls on the
    LOWEST-skew names, and require easy-to-borrow. That is the same
    exclusion the paper says removes the artifact. What remains is roughly
    a third of a documented effect, which is small.

    It is also strictly more than zero, which is what the current signal
    measured.

WHAT IT WILL NOT DO

    Submit, modify, cancel or price any order. It computes a number and a
    rank. options_scanner remains the only module that submits.

FIVE CONDITIONS LOCKBOT MADE BINDING (2026-08-25)

    1. A stability gate before anything is scored -- one reading of a
       jittery quote is not a signal, which is the same lesson that
       produced OPTIONS_STOP_CONFIRM_CYCLES.
    2. DELTA-matched, not a fixed percentage out of the money: a 7% OTM
       put is a different option on a 20%-vol name than on a 60%-vol one,
       so fixed moneyness silently ranks by volatility instead of skew.
    3. A signal-source tag on every row BEFORE the first entry, so the
       detect_signal cohort and the skew cohort can never be pooled.
    4. The shadow verdict is on UNDERLYING returns, and it gates capital.
    5. An easy-to-borrow check, which decides whether any pass is real or
       the borrow-fee artifact wearing a costume.

USAGE
    python options_skew.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lockbot_config as config
import options_greeks

VERSION = "1.0"

# The put leg is chosen by DELTA, not by distance from the money. Condition
# 2 above: at a fixed 7% OTM, a 60%-vol name's put is nearly at the money
# in risk terms while a 20%-vol name's is far out, so a fixed-moneyness
# "skew" ranks names by volatility with skew as a rounding error.
TARGET_PUT_DELTA = 0.25
ATM_CALL_DELTA = 0.50


def state_path() -> Path:
    return Path(getattr(
        config, "OPTIONS_SKEW_STATE_FILE",
        config.PROJECT_FOLDER / "options_skew_state.json"))


def as_float(value: Any) -> float | None:
    """A number, or None. Never 0.0 for unusable input -- a zero skew is a
    real and very different claim from a skew that could not be read."""

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def skew_value(put_iv: Any, call_iv: Any) -> float | None:
    """OTM put IV minus ATM call IV. None if either side is unreadable.

    POSITIVE means puts are bid up relative to calls -- the options market
    is paying for downside on this name, which the literature associates
    with LOWER subsequent returns. NEGATIVE is the bullish end.
    """

    put, call = as_float(put_iv), as_float(call_iv)

    if put is None or call is None:
        return None

    # An implied volatility of zero or below is not a reading, it is a
    # failed solve. Letting one through would put a fabricated extreme at
    # the top of a cross-sectional rank, which is exactly where it does
    # the most damage.
    if put <= 0 or call <= 0:
        return None

    return put - call


def iv_from_quote(snapshot: Any, *, underlying_price: float, strike: float,
                  days_to_expiration: int, option_type: str
                  ) -> tuple[float | None, str]:
    """Implied volatility for one contract, and where it came from.

    The feed's own number always wins. options_greeks is the fallback for
    the 54% of contracts that arrive without greeks, measured 2026-08-24.
    """

    direct = as_float(getattr(snapshot, "implied_volatility", None))

    if direct is not None and direct > 0:
        return direct, "feed"

    quote = getattr(snapshot, "latest_quote", None)

    if quote is None:
        return None, "none"

    bid = as_float(getattr(quote, "bid_price", None))
    ask = as_float(getattr(quote, "ask_price", None))

    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None, "none"

    if days_to_expiration <= 0:
        return None, "none"

    modelled = options_greeks.implied_volatility(
        0.5 * (bid + ask), underlying_price, strike,
        days_to_expiration / 365.0, options_greeks.DEFAULT_RISK_FREE_RATE,
        0.0, option_type)

    if modelled is None or modelled <= 0:
        return None, "none"

    return float(modelled), "model"


def pick_delta_matched(candidates: list[dict[str, Any]], *, target: float
                       ) -> dict[str, Any] | None:
    """The contract whose |delta| is nearest the target. None if none has one.

    Returns None rather than falling back to moneyness. A moneyness-picked
    leg answers a different question, and silently mixing the two would put
    two different measurements in one column -- the defect class that
    produced the debit-ceiling, entry-limit and exit-valuation bugs.
    """

    usable = [c for c in candidates if as_float(c.get("delta")) is not None]

    if not usable:
        return None

    return min(usable, key=lambda c: abs(abs(float(c["delta"])) - target))


def is_borrowable(asset: Any) -> bool | None:
    """Whether the name is easy to borrow. None when it cannot be read.

    THE MOST IMPORTANT GATE IN THIS MODULE, and the reason is Muravyev,
    Pearson & Pollet (2025): option-signal predictability is largely the
    stock borrow fee showing through, and it concentrates in hard-to-borrow
    names. Excluding them is what separates a real result from the artifact.

    None, not False, when the flag is missing -- and the caller must treat
    None as "do not trade", because an unknown borrow status is exactly the
    case this gate exists to catch.
    """

    flag = getattr(asset, "easy_to_borrow", None)

    if flag is None:
        return None

    return bool(flag)


def load_history() -> dict[str, list[float]]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def save_history(history: dict[str, list[float]]) -> None:
    try:
        state_path().write_text(json.dumps(history, indent=1),
                                encoding="utf-8")
    except OSError:
        pass


def record(history: dict[str, list[float]], symbol: str, value: float,
           *, keep: int = 8) -> dict[str, list[float]]:
    """Append one reading, keeping the last few per symbol."""

    series = list(history.get(symbol, []))
    series.append(round(float(value), 6))
    history[symbol] = series[-keep:]

    return history


def is_stable(history: dict[str, list[float]], symbol: str, *,
              min_readings: int, max_spread: float) -> bool:
    """Has this name's skew held still long enough to be worth acting on?

    Condition 1. These books are wide and jittery -- 16-28% spreads with
    the bid moving 8% between polls seconds apart -- and a skew computed
    from two such quotes inherits all of it. The same reasoning produced
    OPTIONS_STOP_CONFIRM_CYCLES after an EWZ call exited at -8.1% against
    a -35% stop on a single bad print.

    Requires enough readings, all of the SAME SIGN, within a band. Sign
    agreement matters more than closeness: a name flipping between bullish
    and bearish skew is telling you nothing, however small the numbers.
    """

    series = history.get(symbol, [])

    if len(series) < min_readings:
        return False

    recent = series[-min_readings:]

    if not (all(v > 0 for v in recent) or all(v < 0 for v in recent)):
        return False

    return (max(recent) - min(recent)) <= max_spread


def rank_by_skew(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lowest skew first -- the bullish end, and the only end this trades.

    The short half of the published effect is unreachable here: no shorting
    under $2,000 of equity. So the high-skew tail is recorded and never
    acted on, rather than being quietly reinterpreted as a put signal --
    that would be a second, untested rule riding on the first one's
    evidence.
    """

    usable = [r for r in rows if as_float(r.get("skew")) is not None]

    return sorted(usable, key=lambda r: float(r["skew"]))


def observation(symbol: str, *, skew: float | None, put_iv: float | None,
                call_iv: float | None, put_delta: float | None,
                call_delta: float | None, iv_source: str,
                easy_to_borrow: bool | None, stable: bool,
                now: datetime | None = None) -> dict[str, Any]:
    """One row, tagged with everything a later verdict needs to split on."""

    def num(value: Any, spec: str) -> str:
        return "" if value is None else format(float(value), spec)

    return {
        "timestamp": (now or datetime.now(timezone.utc)).isoformat(),
        "underlying": symbol,
        "skew": num(skew, ".6f"),
        "put_iv": num(put_iv, ".6f"),
        "call_iv": num(call_iv, ".6f"),
        "put_delta": num(put_delta, ".4f"),
        "call_delta": num(call_delta, ".4f"),
        "iv_source": iv_source,
        # Condition 5. Blank when unreadable, and unreadable must not be
        # read as tradable by anything downstream.
        "easy_to_borrow": ("" if easy_to_borrow is None
                           else str(easy_to_borrow).lower()),
        "stable": str(bool(stable)).lower(),
        # Condition 3. Present from the first row so the detect_signal
        # cohort and the skew cohort can never be pooled by a later reader.
        "signal_source": "skew",
    }


def tradable(row: dict[str, Any]) -> bool:
    """Whether this observation may drive capital. Conservative by design.

    Every unknown is a refusal. An unreadable borrow flag, an unstable
    reading, or a missing skew all mean no -- the whole point of the
    borrow-fee exclusion is defeated if a name passes because its status
    could not be determined.
    """

    if as_float(row.get("skew")) is None:
        return False

    if (row.get("stable") or "").strip().lower() != "true":
        return False

    if (row.get("easy_to_borrow") or "").strip().lower() != "true":
        return False

    return True


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}"
              + (f" - {detail}" if detail and not condition else ""))

    print("Skew is put IV minus call IV, and its sign has a meaning")
    # Compared with a tolerance, not ==. 0.40 - 0.30 is 0.10000000000000003
    # in binary floating point; asserting exact equality tests the format
    # of a float rather than the behaviour of the function.
    check("puts bid over calls is POSITIVE, the bearish end",
          abs(skew_value(0.40, 0.30) - 0.10) < 1e-12,
          str(skew_value(0.40, 0.30)))
    check("calls bid over puts is NEGATIVE, the bullish end",
          abs(skew_value(0.30, 0.40) + 0.10) < 1e-12)
    check("a missing side gives None, never 0.0",
          skew_value(None, 0.30) is None and skew_value(0.30, None) is None)
    check("a non-positive IV is a failed solve, not a reading",
          skew_value(0.0, 0.30) is None and skew_value(0.30, -0.1) is None)
    check("garbage gives None", skew_value("x", 0.30) is None)

    print()
    print("The put leg is matched on DELTA, never on moneyness")
    chain = [{"symbol": "a", "delta": -0.10}, {"symbol": "b", "delta": -0.27},
             {"symbol": "c", "delta": -0.55}]
    check("nearest the 0.25 target wins",
          pick_delta_matched(chain, target=TARGET_PUT_DELTA)["symbol"] == "b")
    check("the ATM call is the one nearest 0.50",
          pick_delta_matched([{"symbol": "x", "delta": 0.31},
                              {"symbol": "y", "delta": 0.48}],
                             target=ATM_CALL_DELTA)["symbol"] == "y")
    # A moneyness fallback would answer a different question in the same
    # column -- the one-quantity-two-places defect, in a measurement.
    check("no delta anywhere gives None, not a moneyness fallback",
          pick_delta_matched([{"symbol": "a"}, {"symbol": "b", "delta": None}],
                             target=0.25) is None)
    check("an empty chain gives None",
          pick_delta_matched([], target=0.25) is None)

    print()
    print("Borrow status: the gate that separates signal from artifact")

    class _A:
        def __init__(self, flag):
            self.easy_to_borrow = flag

    check("easy to borrow reads True", is_borrowable(_A(True)) is True)
    check("hard to borrow reads False", is_borrowable(_A(False)) is False)
    check("a missing flag is None, NOT False",
          is_borrowable(object()) is None)

    print()
    print("Stability: one reading of a jittery book is not a signal")
    hist: dict[str, list[float]] = {}
    for value in (-0.04, -0.05, -0.045):
        hist = record(hist, "NOK", value)
    check("three same-signed readings in a tight band are stable",
          is_stable(hist, "NOK", min_readings=3, max_spread=0.05))
    check("two readings are not enough",
          not is_stable(hist, "NOK", min_readings=4, max_spread=0.05))

    flip: dict[str, list[float]] = {}
    for value in (-0.04, 0.02, -0.03):
        flip = record(flip, "X", value)
    check("a name that flips sign is NOT stable, however small the numbers",
          not is_stable(flip, "X", min_readings=3, max_spread=0.10))

    wide: dict[str, list[float]] = {}
    for value in (-0.01, -0.20, -0.02):
        wide = record(wide, "Y", value)
    check("same sign but a wide band is not stable",
          not is_stable(wide, "Y", min_readings=3, max_spread=0.05))
    check("an unseen symbol is not stable",
          not is_stable(hist, "NEVER_SEEN", min_readings=3, max_spread=0.5))
    check("history keeps only the recent tail",
          len(record({"Z": [0.0] * 20}, "Z", 0.1, keep=8)["Z"]) == 8)

    print()
    print("Ranking runs from bullish to bearish, and trades one end")
    ranked = rank_by_skew([{"underlying": "BAC", "skew": "0.061"},
                           {"underlying": "NOK", "skew": "-0.048"},
                           {"underlying": "F", "skew": "0.004"},
                           {"underlying": "BAD", "skew": ""}])
    check("lowest skew is first", ranked[0]["underlying"] == "NOK",
          str([r["underlying"] for r in ranked]))
    check("highest skew is last", ranked[-1]["underlying"] == "BAC")
    check("an unreadable skew is dropped, not sorted to an end",
          len(ranked) == 3 and all(r["underlying"] != "BAD" for r in ranked))

    print()
    print("Every unknown is a refusal")
    good = observation("NOK", skew=-0.048, put_iv=0.574, call_iv=0.622,
                       put_delta=-0.26, call_delta=0.49, iv_source="feed",
                       easy_to_borrow=True, stable=True)
    check("a complete, stable, borrowable row is tradable", tradable(good))
    check("it is tagged with its signal source from the first row",
          good["signal_source"] == "skew")

    check("an unstable row is refused",
          not tradable({**good, "stable": "false"}))
    check("a hard-to-borrow row is refused",
          not tradable({**good, "easy_to_borrow": "false"}))
    # The exclusion exists to remove the borrow-fee artifact. A name that
    # passes because its status could not be read defeats it entirely.
    check("an UNKNOWN borrow status is refused, not waved through",
          not tradable({**good, "easy_to_borrow": ""}))
    check("a row with no skew is refused",
          not tradable({**good, "skew": ""}))

    print()
    print("It cannot place or price an order")
    source = Path(__file__).read_text(encoding="utf-8").split("def _self_test")[0]
    check("no order submission", "submit_order" not in source)
    check("no cancellation", "cancel_order" not in source)
    check("no limit pricing", "limit_price" not in source)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All options-skew checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Option-implied skew as an entry signal")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    print(f"OPTIONS SKEW v{VERSION}")
    print(f"  state: {state_path().name}")
    print("  This module computes and ranks. options_scanner submits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
