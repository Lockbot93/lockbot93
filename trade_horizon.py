"""How long an equity trade is MEANT to be held, and what that changes.

WHY THIS EXISTS

Owner directive, 2026-08-07: "I want Lockbot to be able to take a mix of
day trades, swings and overnight swings." Filed by LOCKBOT as agent_channel
a002c6a0, which found the engine had no holding-horizon concept at all.

Every equity trade LOCKBOT has ever taken has simply ridden its bracket
until one side filled -- about 23 to 25 hours in practice. That is an
accidental overnight hold, not a designed one. Nothing recorded the
intended holding period, so nothing could group results by it, and the
strategy lab's HORIZONS table (day 78 bars / 2% stop, swing 390 bars / 5%
stop) had no counterpart on the live path.

WHAT A HORIZON CHANGES, AND WHAT IT DOES NOT

It changes three things and only three:

  1. the stop distance, and therefore the share count -- a swing trade
     needs room to breathe, and adaptive_brackets already scales shares
     down as the stop widens so the dollars at risk stay put
  2. whether the position must be flat by the close (day only)
  3. how many days it may be held before the engine gives up on it

It changes NOTHING about entry selection. The same signal, the same
ranking, the same regime gate. A horizon is a statement about the exit.

THIS IS PLUMBING, NOT A STRATEGY. LOCKBOT's own words on the item, kept
here because it is the sentence most likely to be forgotten:

    "the 2026-08-06 overnight decomposition found ~98% of return accrues
    close-to-open across 46,970 symbol-days, so overnight holds are the
    empirically favored horizon; conversely every swing entry rule tested
    in the lab is negative, so this is capability plumbing, not a strategy
    endorsement."

Tagging trades by horizon does not make them profitable. It makes them
MEASURABLE by horizon, which the project could not do before. Nobody
should read the existence of a swing option as evidence that swings work.

WHY THE MIX IS A ROTATION AND NOT A JUDGEMENT

Choosing a horizon per setup would mean predicting which setups suit
which holding period, and that is a new entry-side model. CLAUDE.md is
explicit that no such score gets fitted before the evidence exists, and
after seventeen failed entry families the prior on a new one is poor.

So the mix is a fixed rotation. It is a SAMPLING SCHEME, deliberately
uninformative, whose only purpose is to accumulate outcomes under all
three horizons so the question can eventually be answered from data
rather than from argument.

Day trades are rare in the rotation for a hard reason rather than a
preference: under PDT this account gets three round trips per five
business days, so day-horizon capacity is roughly one entry in five to
begin with. A rotation that produced more would simply be blocked, and a
blocked entry teaches nothing.
"""

from __future__ import annotations

from typing import Any

# The three horizons, plus the honest fourth.
DAY = "day"
OVERNIGHT = "overnight"
SWING = "swing"

# For a trade registered before this module existed, or whose tag could
# not be read. Never guess "day": a wrong horizon on a real trade is
# worse than an absent one, because it silently pollutes the very
# per-horizon comparison this exists to enable.
UNKNOWN = "unknown"

HORIZONS = (DAY, OVERNIGHT, SWING)
ALL_TAGS = HORIZONS + (UNKNOWN,)


# What each horizon does to the exit.
#
# stop_scale multiplies the ATR-derived stop distance.
#
# WHY IT IS 1.5 AND NOT THE LAB'S 2.5. The obvious move is to copy the
# lab, which uses 2% at day and 5% at swing -- a factor of 2.5. That was
# tried first and it is wrong, because it double-counts.
#
# The lab's stops are FIXED. The live stop is already ATR-adaptive, so a
# 3%/day stock arrives at a 4.5% stop before any horizon touches it --
# which is almost the lab's SWING width already. Multiplying that by 2.5
# produced an 11.25% stop, and at 1% risk on a $270 account an 11.25%
# stop buys a $24 position: ZERO shares at any real price. Every swing
# entry would have been silently rejected as unsizeable while the tag
# said the feature worked.
#
# The binding constraint at this account size is not how much room a
# swing wants, it is how little room the risk budget can pay for. 1.5x
# is what still sizes. See the self-test that asserts it.
#
# max_hold_days is when the engine gives up and closes at market. It is a
# backstop, not a strategy: without it a swing position whose bracket
# never fills is held forever.
#
# flatten_same_session is what makes a day trade a day trade.
PROFILE: dict[str, dict[str, Any]] = {
    DAY: {
        "stop_scale": 1.0,
        "max_hold_days": 1,
        "flatten_same_session": True,
        "why": "in and out inside one session; PDT-scarce",
    },
    OVERNIGHT: {
        "stop_scale": 1.0,
        "max_hold_days": 3,
        "flatten_same_session": False,
        "why": "the horizon LOCKBOT has always accidentally traded",
    },
    SWING: {
        "stop_scale": 1.5,
        "max_hold_days": 10,
        "flatten_same_session": False,
        "why": "needs room to breathe, but not more than $270 can size",
    },
    UNKNOWN: {
        "stop_scale": 1.0,
        "max_hold_days": 0,          # 0 means no time-based exit at all
        "flatten_same_session": False,
        "why": "untagged; the engine must not act on a horizon it cannot read",
    },
}


def is_valid(horizon: Any) -> bool:
    return str(horizon or "").strip().lower() in ALL_TAGS


def normalise(horizon: Any) -> str:
    """Any input to a tag this module recognises. Unreadable becomes UNKNOWN."""

    tag = str(horizon or "").strip().lower()

    return tag if tag in ALL_TAGS else UNKNOWN


def profile(horizon: Any) -> dict[str, Any]:
    return PROFILE[normalise(horizon)]


def flattens_before_close(horizon: Any) -> bool:
    return bool(profile(horizon)["flatten_same_session"])


def max_hold_days(horizon: Any) -> int:
    return int(profile(horizon)["max_hold_days"])


def choose(policy: str, *, sequence: int) -> str:
    """Which horizon the next entry gets.

    `policy` is one of the three horizon names -- meaning every entry gets
    that one -- or "mixed" for the rotation.

    `sequence` is a counter the caller supplies, so this function stays
    pure and testable. Any monotonically increasing integer works; the
    scanner passes the number of entries submitted so far.

    An unrecognised policy falls back to OVERNIGHT rather than raising.
    This is called on the order-submission path, and a typo in config
    must not be able to stop LOCKBOT trading; it should quietly produce
    the horizon the engine has always actually traded.
    """

    policy = str(policy or "").strip().lower()

    if policy in HORIZONS:
        return policy

    if policy != "mixed":
        return OVERNIGHT

    if not MIX_ROTATION:
        return OVERNIGHT

    return MIX_ROTATION[int(sequence) % len(MIX_ROTATION)]


# The rotation. Day appears once in five because PDT allows roughly one
# entry in five to be a day trade at this account size -- see the module
# docstring. Overridden from config at import time below.
MIX_ROTATION: tuple[str, ...] = (OVERNIGHT, SWING, DAY, OVERNIGHT, SWING)


def bracket_settings_for(horizon: Any, base: dict[str, Any]) -> dict[str, Any]:
    """Scale a bracket settings dict for a horizon. Never mutates `base`.

    Only the stop moves. The reward ratio, the risk budget and the
    position cap are all horizon-independent on purpose: widening the stop
    already reduces the share count to hold dollars-at-risk constant, and
    scaling the risk budget as well would compound two effects that were
    each decided separately.

    The min/max stop bounds scale with the multiplier, because a swing
    stop capped at the day-horizon ceiling would not actually be a swing
    stop -- it would be a day stop with a longer name. That was the whole
    failure mode: a horizon that changes the label and not the behaviour.
    """

    scaled = dict(base or {})
    scale = float(profile(horizon)["stop_scale"])

    if scale == 1.0:
        return scaled

    for key in ("stop_multiplier", "min_stop", "max_stop"):
        if key in scaled:
            try:
                scaled[key] = float(scaled[key]) * scale
            except (TypeError, ValueError):
                pass

    return scaled


def describe() -> str:
    lines = ["HORIZONS", ""]

    for tag in ALL_TAGS:
        p = PROFILE[tag]
        hold = p["max_hold_days"]
        lines.append(
            f"  {tag:<10} stop x{p['stop_scale']:<4} "
            f"hold {'none' if not hold else str(hold) + 'd':<5} "
            f"{'flatten at close' if p['flatten_same_session'] else '':<17} "
            f"{p['why']}"
        )

    lines.append("")
    lines.append(f"  mixed rotation: {' -> '.join(MIX_ROTATION)}")

    return "\n".join(lines)


# Config override, applied at import so every module sees one rotation.
try:  # pragma: no cover - config is normally importable
    import lockbot_config as _config

    _rotation = getattr(_config, "EQUITY_HORIZON_MIX", None)

    if _rotation:
        _cleaned = tuple(
            normalise(h) for h in _rotation if normalise(h) in HORIZONS
        )

        if _cleaned:
            MIX_ROTATION = _cleaned
except Exception:
    pass


def _self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))
            failures.append(name)

    print("\nTAGS")
    check("the three horizons are valid", all(is_valid(h) for h in HORIZONS))
    check("unknown is valid too", is_valid(UNKNOWN))
    check("nonsense normalises to unknown", normalise("swimg") == UNKNOWN)
    check("None normalises to unknown", normalise(None) == UNKNOWN)
    check("case and spacing are forgiven", normalise("  SWING ") == SWING)
    check("an unreadable tag is NEVER guessed as day",
          normalise("") != DAY and normalise(None) != DAY)

    print("\nWHAT EACH HORIZON DOES")
    check("only day flattens at the close",
          flattens_before_close(DAY)
          and not flattens_before_close(OVERNIGHT)
          and not flattens_before_close(SWING))
    check("unknown never flattens", not flattens_before_close(UNKNOWN))
    check("unknown has no time exit at all", max_hold_days(UNKNOWN) == 0)
    check("swing may be held longest",
          max_hold_days(SWING) > max_hold_days(OVERNIGHT) > max_hold_days(DAY))

    print("\nPOLICY")
    check("a named policy always returns itself",
          all(choose(h, sequence=n) == h for h in HORIZONS for n in range(5)))
    check("an unknown policy falls back to overnight, not to day",
          choose("nonsense", sequence=0) == OVERNIGHT)
    check("an empty policy falls back to overnight",
          choose("", sequence=3) == OVERNIGHT)

    drawn = [choose("mixed", sequence=n) for n in range(len(MIX_ROTATION) * 3)]
    check("mixed produces every horizon", set(drawn) == set(HORIZONS), str(set(drawn)))
    check("mixed is deterministic",
          drawn == [choose("mixed", sequence=n)
                    for n in range(len(MIX_ROTATION) * 3)])
    check("mixed cycles",
          choose("mixed", sequence=0) == choose("mixed", sequence=len(MIX_ROTATION)))

    day_share = drawn.count(DAY) / len(drawn)
    check("day trades are the scarce third, as PDT requires",
          day_share <= 0.25, f"day share {day_share:.0%}")
    check("mixed never emits unknown", UNKNOWN not in drawn)

    print("\nBRACKET SCALING")
    base = {"stop_multiplier": 1.5, "min_stop": 0.015, "max_stop": 0.06,
            "reward_ratio": 2.0, "max_risk_percent": 0.01,
            "max_position_percent": 0.40}

    day_settings = bracket_settings_for(DAY, base)
    swing_settings = bracket_settings_for(SWING, base)

    check("day leaves the bracket alone", day_settings == base)
    check("swing widens the stop",
          swing_settings["stop_multiplier"] > base["stop_multiplier"])
    check("swing widens the CEILING too, or it is a day stop with a new name",
          swing_settings["max_stop"] > base["max_stop"])
    check("swing widens the floor as well",
          swing_settings["min_stop"] > base["min_stop"])
    check("the reward ratio is untouched",
          swing_settings["reward_ratio"] == base["reward_ratio"])
    check("the risk budget is untouched",
          swing_settings["max_risk_percent"] == base["max_risk_percent"])
    check("the position cap is untouched",
          swing_settings["max_position_percent"] == base["max_position_percent"])
    check("the caller's dict is never mutated",
          base["stop_multiplier"] == 1.5)

    print("\nA SWING MUST STILL BE SIZEABLE ON A $270 ACCOUNT")

    # The check that matters, and the one whose absence let a 2.5x scale
    # through: a wider stop buys fewer shares, and below one share the
    # trade is rejected outright. A horizon that can never be sized is
    # not a feature, it is a tag on something that never happens.
    try:
        from adaptive_brackets import compute_bracket, load_settings

        live = load_settings()

        sized = {
            horizon: compute_bracket(
                symbol="TEST", price=30.0, equity=270.0, atr_pct=0.03,
                side="long",
                settings=bracket_settings_for(horizon, live),
            )
            for horizon in HORIZONS
        }

        for horizon, bracket in sized.items():
            print(f"        {horizon:<10} stop {bracket['stop_percent']*100:5.2f}%"
                  f"  shares {bracket.get('shares', 0)}")

        check("a swing sizes to at least one share at $270",
              int(sized[SWING].get("shares", 0)) >= 1,
              f"swing stop {sized[SWING]['stop_percent']:.4f} -> "
              f"{sized[SWING].get('shares')} shares")
        check("a day trade sizes too",
              int(sized[DAY].get("shares", 0)) >= 1)
        check("and the swing stop really is the wider one",
              sized[SWING]["stop_percent"] > sized[DAY]["stop_percent"])
        check("while risking no more dollars than the day trade",
              sized[SWING]["stop_percent"] * sized[SWING]["shares"] * 30.0
              <= sized[DAY]["stop_percent"] * sized[DAY]["shares"] * 30.0 + 0.01,
              "a wider stop must buy fewer shares, not risk more money")
    except ImportError:
        print("        (adaptive_brackets unavailable — skipped)")

    print("\nIT SURVIVES RUBBISH")
    check("a missing settings dict does not raise",
          isinstance(bracket_settings_for(SWING, {}), dict))
    check("None settings does not raise",
          isinstance(bracket_settings_for(SWING, None), dict))
    check("non-numeric settings are left alone",
          bracket_settings_for(SWING, {"stop_multiplier": "wide"})
          ["stop_multiplier"] == "wide")
    check("an unknown horizon scales nothing",
          bracket_settings_for("nonsense", base) == base)

    print()

    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All trade-horizon checks passed.")

    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    print(describe())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
