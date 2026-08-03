"""
signal_quality.py  --  ranking setups when every one of them scores 100

THE PROBLEM THIS SOLVES
    market_scanner.py ranked approved setups like this:

        approved.sort(key=lambda r: (r["score"], r["volume_ratio"]),
                      reverse=True)

    Every approved setup scores exactly 100 — all 157 in the shadow log.
    That is not a coincidence, it is arithmetic. The confidence score
    awards 20 points each for trend direction, price above the 9 EMA,
    MACD above its signal, RSI in band, and price above VWAP. Those are
    the SAME five conditions that produce a BUY_LONG signal. A setup
    cannot have a signal without scoring 100, and cannot score below 100
    while having one.

    So the score is not a measure of quality. It is the entry condition
    restated, and it can never separate two tradeable setups.

    With the primary sort key constant, the tiebreaker becomes the whole
    ranking — and the tiebreaker was volume_ratio, which the shadow data
    shows degrading monotonically:

        volume 1.10-1.25   37.5% win rate   +0.125 R   (8 trades)
        volume 1.25-1.75   26.7% win rate   -0.20 R    (15 trades)
        volume 1.75+       25.0% win rate   -0.25 R    (32 trades)

    LOCKBOT was choosing which setups to trade by the one measure its
    own data suggests points the wrong way.

WHAT THIS DOES NOT CLAIM
    It does not claim to improve returns, and the weights below are not
    fitted to anything. Fitting four weights to 55 resolved trades would
    produce a number that describes 2026-07-28 and nothing else.

    What it does is make ranking two things it was not: NON-ARBITRARY
    (a real ordering rather than a constant plus a bad tiebreak) and
    MEASURABLE (every component is logged per setup, so shadow_trades.py
    can eventually say which ones carry information and which don't).

    The honest position is that ranking is currently unmeasured. This
    turns it into an experiment instead of an accident.

WHY THESE FOUR COMPONENTS
    Each was chosen to be independent of the entry condition — measuring
    something the five entry checks do not already cover. A component
    collinear with entry would just rebuild the constant-100 problem.

      trend_strength   ADX. The entry rules never look at it.
      momentum         MACD histogram size. Entry checks only the SIGN
                       of the crossover; how wide the gap is, is new.
      conviction       Spread between +DI and -DI. Unused at entry.
      restraint        Distance from VWAP. Entry requires price to be on
                       the right side of it; being far past it means
                       chasing an extended move, which is different
                       information from being on the right side.

    Everything is normalized by ATR so a $9 stock and a $400 stock
    produce comparable numbers.

USAGE
    python signal_quality.py --self-test     offline checks, no network
"""

from __future__ import annotations

import math
import sys
from typing import Any


# Component weights. These are PRIORS, not fitted values — deliberately
# equal across the four evidence-neutral components, because there is no
# data yet that justifies preferring one over another.
#
# volume_ratio is weighted ZERO. It is still computed and logged on every
# setup so it stays measurable, but the only evidence available says it
# ranks backwards, and a factor with negative evidence should not steer
# live decisions while it is being re-measured.
DEFAULT_WEIGHTS = {
    "trend_strength": 1.0,
    "momentum": 1.0,
    "conviction": 1.0,
    "restraint": 1.0,
    "volume_ratio": 0.0,
}

# ADX below this is noise, above this is a strong trend. Standard
# readings: under 20 is rangebound, over 25 is trending, over 40 is
# very strong.
ADX_FLOOR = 15.0
ADX_CEILING = 40.0

# MACD histogram measured in ATR units. A gap of one full ATR between
# MACD and its signal line is a large move by any standard.
MOMENTUM_CEILING_ATR = 0.35

# Distance from VWAP in ATR units at which a setup is considered fully
# extended and scores zero for restraint.
EXTENSION_CEILING_ATR = 2.0

# Volume ratio that maps to 1.0, when weighted above zero.
VOLUME_CEILING = 2.5


def _finite(value: Any) -> float | None:
    """Return a usable float, or None for missing/NaN/infinite values."""

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    return result if math.isfinite(result) else None


def _clamp(value: float) -> float:
    """Constrain a component to 0..1."""

    return max(0.0, min(1.0, value))


def _scale(value: float, floor: float, ceiling: float) -> float:
    """Map value from the floor..ceiling range onto 0..1."""

    if ceiling <= floor:
        return 0.0

    return _clamp((value - floor) / (ceiling - floor))


def compute_components(row: Any, is_long: bool) -> dict[str, float]:
    """
    Score one setup's components, each on 0..1.

    `row` is a row of the indicator DataFrame — anything supporting
    row["close"] style access. A missing or unusable input scores 0.5
    rather than 0: an absent measurement is not evidence of a bad setup,
    and scoring it zero would silently push every symbol with a data gap
    to the bottom of the ranking.
    """

    def field(name: str) -> float | None:
        try:
            return _finite(row[name])
        except (KeyError, TypeError, IndexError):
            return None

    close = field("close")
    atr = field("atr")

    # ATR is the denominator for the scale-free components. Without it,
    # those cannot be computed meaningfully.
    atr_usable = atr is not None and atr > 0

    components: dict[str, float] = {}

    # --- trend strength -------------------------------------------------
    adx = field("adx")
    components["trend_strength"] = (
        _scale(adx, ADX_FLOOR, ADX_CEILING) if adx is not None else 0.5
    )

    # --- momentum -------------------------------------------------------
    macd = field("macd")
    macd_signal = field("macd_signal")

    if macd is not None and macd_signal is not None and atr_usable:
        histogram = macd - macd_signal

        # A histogram pointing against the trade direction is not
        # momentum in our favour, whatever its size.
        aligned = histogram if is_long else -histogram
        components["momentum"] = _scale(
            max(aligned, 0.0) / atr, 0.0, MOMENTUM_CEILING_ATR
        )
    else:
        components["momentum"] = 0.5

    # --- directional conviction -----------------------------------------
    plus_di = field("plus_di")
    minus_di = field("minus_di")

    if plus_di is not None and minus_di is not None:
        total = plus_di + minus_di

        if total > 0:
            spread = (plus_di - minus_di) if is_long else (minus_di - plus_di)
            components["conviction"] = _clamp(spread / total)
        else:
            components["conviction"] = 0.5
    else:
        components["conviction"] = 0.5

    # --- restraint ------------------------------------------------------
    # Entry already required price to be on the correct side of VWAP.
    # This asks HOW FAR past it — a setup two ATR beyond VWAP is being
    # chased, and has less room left to reach its target.
    vwap = field("vwap")

    if close is not None and vwap is not None and atr_usable:
        beyond = (close - vwap) if is_long else (vwap - close)
        extension = max(beyond, 0.0) / atr
        components["restraint"] = 1.0 - _scale(
            extension, 0.0, EXTENSION_CEILING_ATR
        )
    else:
        components["restraint"] = 0.5

    # --- volume ---------------------------------------------------------
    volume = field("volume")
    volume_average = field("volume_avg_20")

    if volume is not None and volume_average is not None and volume_average > 0:
        components["volume_ratio"] = _scale(
            volume / volume_average, 1.0, VOLUME_CEILING
        )
    else:
        components["volume_ratio"] = 0.5

    return components


def compute_quality(
    row: Any,
    is_long: bool,
    weights: dict[str, float] | None = None,
) -> tuple[float, dict[str, float]]:
    """
    Return (quality 0..100, components).

    Quality is the weighted average of the components, rescaled to 0-100
    so it reads on the same axis as the old confidence score without
    being confusable with it.
    """

    weights = weights or DEFAULT_WEIGHTS
    components = compute_components(row, is_long)

    total_weight = sum(
        weight for name, weight in weights.items() if name in components
    )

    if total_weight <= 0:
        return 50.0, components

    weighted = sum(
        components[name] * weight
        for name, weight in weights.items()
        if name in components
    )

    return round(weighted / total_weight * 100, 2), components


def load_weights() -> dict[str, float]:
    """Read weights from lockbot_config.py, falling back to the defaults."""

    try:
        import lockbot_config as config

        configured = getattr(config, "SIGNAL_QUALITY_WEIGHTS", None)

        if isinstance(configured, dict) and configured:
            return {**DEFAULT_WEIGHTS, **configured}

    except Exception:
        pass

    return dict(DEFAULT_WEIGHTS)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    """Offline checks. Pure arithmetic — no config, no network."""

    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    def make(**overrides) -> dict:
        row = {
            "close": 100.0,
            "vwap": 100.0,
            "atr": 2.0,
            "adx": 27.5,
            "macd": 0.35,
            "macd_signal": 0.0,
            "plus_di": 30.0,
            "minus_di": 10.0,
            "volume": 1_500_000.0,
            "volume_avg_20": 1_000_000.0,
        }
        row.update(overrides)
        return row

    print("Component bounds")

    components = compute_components(make(), is_long=True)

    for name, value in components.items():
        check(f"{name} within 0..1", 0.0 <= value <= 1.0, str(value))

    print()
    print("Trend strength")

    check(
        "weak ADX scores low",
        compute_components(make(adx=12), True)["trend_strength"] == 0.0,
    )
    check(
        "strong ADX scores high",
        compute_components(make(adx=45), True)["trend_strength"] == 1.0,
    )
    check(
        "mid ADX scores mid",
        0.3 < compute_components(make(adx=27.5), True)["trend_strength"] < 0.7,
    )

    print()
    print("Momentum")

    strong = compute_components(make(macd=0.7, macd_signal=0.0), True)["momentum"]
    weak = compute_components(make(macd=0.05, macd_signal=0.0), True)["momentum"]
    check("wider histogram scores higher", strong > weak, f"{strong} vs {weak}")

    # A bullish histogram must not reward a short, and vice versa.
    against = compute_components(make(macd=0.7, macd_signal=0.0), is_long=False)
    check("histogram against the trade scores zero", against["momentum"] == 0.0)

    print()
    print("Conviction")

    long_conviction = compute_components(make(), is_long=True)["conviction"]
    short_conviction = compute_components(make(), is_long=False)["conviction"]
    check("+DI dominance favours longs", long_conviction > short_conviction)

    # The scale runs 0 (no directional edge) to 1 (total dominance), so
    # +DI 30 against -DI 10 landing on 0.50 is correct, not neutral.
    balanced = compute_components(make(plus_di=20, minus_di=20), True)["conviction"]
    dominant = compute_components(make(plus_di=38, minus_di=2), True)["conviction"]

    check("conviction rises with dominance",
          balanced < long_conviction < dominant,
          f"{balanced} < {long_conviction} < {dominant}")
    check("evenly matched DI scores zero", balanced == 0.0, str(balanced))
    check("near-total dominance approaches one", dominant > 0.85, str(dominant))

    flat = compute_components(make(plus_di=0.0, minus_di=0.0), True)
    check("zero DI is neutral, not zero", flat["conviction"] == 0.5)

    print()
    print("Restraint")

    at_vwap = compute_components(make(close=100.0, vwap=100.0), True)["restraint"]
    extended = compute_components(make(close=105.0, vwap=100.0), True)["restraint"]
    check("at VWAP scores full restraint", at_vwap == 1.0, str(at_vwap))
    check("extended scores lower", extended < at_vwap, f"{extended} vs {at_vwap}")
    check(
        "very extended bottoms out",
        compute_components(make(close=110.0, vwap=100.0), True)["restraint"] == 0.0,
    )

    # Below VWAP on a long was already excluded by the entry rules, but
    # the maths must not produce a value above 1.
    below = compute_components(make(close=95.0, vwap=100.0), True)["restraint"]
    check("below VWAP does not exceed 1", below <= 1.0, str(below))

    print()
    print("Missing data")

    for missing in ("adx", "atr", "vwap", "macd", "plus_di", "volume_avg_20"):
        row = make()
        del row[missing]
        result = compute_components(row, True)
        check(
            f"missing {missing} stays in bounds",
            all(0.0 <= v <= 1.0 for v in result.values()),
            str(result),
        )

    empty = compute_components({}, True)
    check(
        "an empty row is neutral, not zero",
        all(v == 0.5 for v in empty.values()),
        str(empty),
    )

    nan_row = make(adx=float("nan"), atr=float("inf"))
    result = compute_components(nan_row, True)
    check(
        "NaN and infinity are handled",
        all(0.0 <= v <= 1.0 for v in result.values()),
        str(result),
    )

    check("zero ATR does not divide by zero", bool(compute_components(make(atr=0.0), True)))

    print()
    print("Quality score")

    quality, components = compute_quality(make(), True)
    check("quality within 0..100", 0.0 <= quality <= 100.0, str(quality))
    check("components returned alongside", len(components) == 5)

    good = compute_quality(make(adx=45, macd=0.9, plus_di=40, minus_di=5), True)[0]
    poor = compute_quality(
        make(adx=12, macd=0.01, plus_di=20, minus_di=19, close=110.0), True
    )[0]
    check("a strong setup outranks a weak one", good > poor, f"{good} vs {poor}")

    # The defect this module exists to fix: two setups that both score
    # 100 on the old confidence scale must be separable here.
    first = compute_quality(make(adx=38, macd=0.6), True)[0]
    second = compute_quality(make(adx=18, macd=0.1), True)[0]
    check(
        "two 100-confidence setups get different quality",
        first != second,
        f"{first} vs {second}",
    )

    # Volume carries zero weight by default, so changing it alone must
    # not move the ranking.
    base = compute_quality(make(volume=1_000_000), True)[0]
    surged = compute_quality(make(volume=9_000_000), True)[0]
    check(
        "volume does not move quality at weight zero",
        base == surged,
        f"{base} vs {surged}",
    )

    # ...but it must still be measured and logged.
    check(
        "volume is still computed for measurement",
        compute_quality(make(volume=9_000_000), True)[1]["volume_ratio"] > 0.5,
    )

    weighted = compute_quality(
        make(volume=9_000_000), True, {**DEFAULT_WEIGHTS, "volume_ratio": 1.0}
    )[0]
    check(
        "volume moves quality once weighted",
        weighted != base,
        f"{weighted} vs {base}",
    )

    check(
        "all-zero weights are survivable",
        compute_quality(make(), True, {k: 0.0 for k in DEFAULT_WEIGHTS})[0] == 50.0,
    )

    print()
    print("Scale independence")

    # A cheap stock and an expensive one with identical shapes should
    # score identically — that is the point of normalizing by ATR.
    cheap = compute_quality(make(close=9.0, vwap=9.0, atr=0.18, macd=0.063), True)[0]
    pricey = compute_quality(
        make(close=450.0, vwap=450.0, atr=9.0, macd=3.15), True
    )[0]
    check("price level does not change quality", abs(cheap - pricey) < 0.01,
          f"{cheap} vs {pricey}")

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All signal-quality checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(__doc__)
    print("Run with --self-test for the offline logic check.")
