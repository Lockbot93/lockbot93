"""
backtest.py — test a DIFFERENT entry rule against history.

WHY THIS EXISTS

shadow_trades.py answers "how did the setups LOCKBOT actually generated
turn out". It cannot answer "what if the rule were different", because a
different rule picks different symbols on different bars. Every strategy
question asked in this project so far has run into that wall: the regime
split, the volume tiebreaker, the confidence score that turned out to be
a tautology. All of them needed a way to replay an alternative and none
existed.

WHAT IT SHARES WITH THE LIVE BOT

Indicators come from indicators.add_indicators and the baseline rule is
market_scanner.detect_signal itself — the same functions the scanner runs
on real money. A backtest that recomputes its own EMAs is testing a
different bot than the one that trades, and would quietly diverge the
first time either side changed.

THE HONESTY MACHINERY, WHICH IS THE POINT

A backtest on thin data does not fail loudly. It returns a number, and
the number is wrong in whichever direction the sample happened to fall.
Three separate results tonight looked real and were not: the volume
tiebreaker (p=0.61), the regime split (p=0.19), and an options replay
that scored 100% until it was checked against broker fills.

So this module refuses to report a bare win rate:

  - Every result carries its sample size, its DAY count, and the share of
    trades from its busiest single day. Ninety trades from one afternoon
    is one observation, not ninety.
  - Every rule tested is counted. Test twenty rules at p<0.05 and one
    will look significant by chance; the report says how many were tried
    so the reader can discount accordingly.
  - Bars where high and low span both stop and target are AMBIGUOUS and
    scored as losses.
  - Fills are assumed at the exact stop or target with no slippage or
    commission, which flatters every result equally.

It places no orders and writes no state.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from math import comb
from typing import Any, Callable
from zoneinfo import ZoneInfo

import lockbot_config as config

MARKET_TIMEZONE = ZoneInfo("America/New_York")

OUTCOME_TARGET = "TARGET"
OUTCOME_STOP = "STOP"
OUTCOME_AMBIGUOUS = "AMBIGUOUS"
OUTCOME_OPEN = "OPEN"

# A bucket smaller than this is reported but never ranked.
MIN_SAMPLE = 30


@dataclass
class Trade:
    """One simulated position."""

    symbol: str
    entered_at: datetime
    side: str
    entry: float
    stop: float
    target: float
    outcome: str = OUTCOME_OPEN
    r_multiple: float = 0.0
    bars_held: int = 0


@dataclass
class Result:
    """What one rule did over the sample."""

    name: str
    trades: list[Trade] = field(default_factory=list)
    symbols_tested: int = 0
    bars_seen: int = 0

    def decided(self) -> list[Trade]:
        return [
            t for t in self.trades
            if t.outcome in (OUTCOME_TARGET, OUTCOME_STOP, OUTCOME_AMBIGUOUS)
        ]

    def wins(self) -> int:
        return sum(1 for t in self.decided() if t.outcome == OUTCOME_TARGET)

    def win_rate(self) -> float:
        decided = self.decided()
        return self.wins() / len(decided) if decided else 0.0

    def expectancy(self) -> float:
        decided = self.decided()
        if not decided:
            return 0.0
        return sum(t.r_multiple for t in decided) / len(decided)

    def days(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for trade in self.decided():
            day = trade.entered_at.astimezone(
                MARKET_TIMEZONE
            ).date().isoformat()
            counts[day] = counts.get(day, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def binomial_tail(wins: int, n: int, rate: float) -> float:
    """P(observing <= wins successes in n trials at this true rate).

    Exact for small samples, normal-approximated for large ones.

    The exact form alone raised OverflowError once the sample reached a
    few thousand trades: comb(2485, 1242) is a ~747-digit integer and
    converting it to float fails outright. It surfaced the first time a
    year of history was analysed rather than a week, which is exactly
    when a significance test starts to matter -- the failure mode was a
    crash on the largest and most trustworthy sample available.

    Above the threshold the normal approximation with a continuity
    correction is accurate to several decimal places, far beyond what
    any decision here turns on.
    """

    if n <= 0:
        return 1.0

    wins = min(wins, n)

    if wins < 0:
        return 0.0

    if n <= 1000:
        return sum(
            comb(n, k) * rate**k * (1.0 - rate) ** (n - k)
            for k in range(0, wins + 1)
        )

    mean = n * rate
    deviation = math.sqrt(n * rate * (1.0 - rate))

    if deviation <= 0:
        return 1.0 if wins >= mean else 0.0

    # Continuity correction: P(X <= wins) ~ Phi((wins + 0.5 - mean) / sd)
    z = (wins + 0.5 - mean) / deviation

    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def breakeven_rate(reward: float, risk: float) -> float:
    """Win rate needed to break even at a given payout."""

    if reward + risk <= 0:
        return 1.0

    return risk / (reward + risk)


def concentration(result: Result) -> tuple[int, float]:
    """(distinct days, share of decided trades from the busiest one)."""

    days = result.days()

    if not days:
        return 0, 0.0

    total = sum(days.values())
    busiest = max(days.values())

    return len(days), busiest / total if total else 0.0


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def simulate_symbol(
    frame: Any,
    *,
    symbol: str,
    rule: Callable[[Any, str], str],
    stop_percent: float,
    reward_ratio: float,
    max_bars_held: int,
    one_position_at_a_time: bool = True,
) -> list[Trade]:
    """Walk one symbol's bars, opening and resolving positions by `rule`.

    `rule` receives (row, trend) and returns "BUY_LONG", "SELL_SHORT" or
    "NO_TRADE". Entry is the close of the signalling bar -- optimistic,
    since a live entry pays the spread, but applied identically to every
    rule so comparisons stay fair.
    """

    from market_scanner import get_trend

    trades: list[Trade] = []
    open_trade: Trade | None = None

    rows = list(frame.itertuples())

    for index, row in enumerate(rows):
        moment = getattr(row, "timestamp", None)

        if moment is None:
            continue

        if isinstance(moment, datetime) and moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)

        high = float(row.high)
        low = float(row.low)

        # ---- resolve an open position first
        if open_trade is not None:
            open_trade.bars_held += 1

            if open_trade.side == "BUY_LONG":
                hit_target = high >= open_trade.target
                hit_stop = low <= open_trade.stop
            else:
                hit_target = low <= open_trade.target
                hit_stop = high >= open_trade.stop

            if hit_target and hit_stop:
                open_trade.outcome = OUTCOME_AMBIGUOUS
                open_trade.r_multiple = -1.0
                trades.append(open_trade)
                open_trade = None
            elif hit_target:
                open_trade.outcome = OUTCOME_TARGET
                open_trade.r_multiple = reward_ratio
                trades.append(open_trade)
                open_trade = None
            elif hit_stop:
                open_trade.outcome = OUTCOME_STOP
                open_trade.r_multiple = -1.0
                trades.append(open_trade)
                open_trade = None
            elif open_trade.bars_held >= max_bars_held:
                # Timed out flat. Left OPEN so it is excluded from the
                # win rate rather than counted as either result.
                trades.append(open_trade)
                open_trade = None

        if open_trade is not None and one_position_at_a_time:
            continue

        # ---- look for a new entry
        # Every field strategy_lab.FIELDS advertises must be present.
        #
        # This map used to carry seven keys while FIELDS promised
        # thirteen, and compile_spec answers NO_TRADE on a missing key
        # rather than raising. So any proposal referencing volume simply
        # never fired -- not rejected, not errored, just permanently
        # silent, and indistinguishable in the results from a rule that
        # had been tested and found worthless.
        #
        # That mattered: the strongest lead in the shadow data is that
        # volume ranks setups the wrong way round, and it was the one
        # thing the search could not have looked at.
        try:
            row_map = {
                "open": float(row.open),
                "high": high,
                "low": low,
                "close": float(row.close),
                "ema_9": float(row.ema_9),
                "ema_21": float(row.ema_21),
                "vwap": float(row.vwap),
                "rsi": float(row.rsi),
                "macd": float(row.macd),
                "macd_signal": float(row.macd_signal),
                "atr": float(row.atr),
                "volume": float(row.volume),
                "volume_avg_20": float(row.volume_avg_20),
            }
        except (AttributeError, TypeError, ValueError):
            continue

        if any(value != value for value in row_map.values()):  # NaN guard
            continue

        trend = get_trend(row_map)
        signal = rule(row_map, trend)

        if signal not in ("BUY_LONG", "SELL_SHORT"):
            continue

        if index >= len(rows) - 2:
            continue

        entry = row_map["close"]

        if entry <= 0:
            continue

        if signal == "BUY_LONG":
            stop = entry * (1.0 - stop_percent)
            target = entry * (1.0 + stop_percent * reward_ratio)
        else:
            stop = entry * (1.0 + stop_percent)
            target = entry * (1.0 - stop_percent * reward_ratio)

        open_trade = Trade(
            symbol=symbol,
            entered_at=moment,
            side=signal,
            entry=entry,
            stop=stop,
            target=target,
        )

    if open_trade is not None:
        trades.append(open_trade)

    return trades


def baseline_rule(row: dict[str, Any], trend: str) -> str:
    """LOCKBOT's live entry rule, imported rather than reimplemented."""

    from market_scanner import detect_signal

    signal, _ = detect_signal(row, trend, data_is_fresh=True)

    return signal


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report(results: list[Result], *, reward_ratio: float) -> None:
    """Print every rule's result with the caveats attached."""

    breakeven = breakeven_rate(reward_ratio, 1.0)

    print()
    print("=" * 76)
    print("        LOCKBOT BACKTEST")
    print("=" * 76)
    print(f"  rules tested       : {len(results)}")
    print(f"  payout             : {reward_ratio:.1f}:1  "
          f"(breakeven {breakeven:.1%})")
    print()
    print(f"  {'rule':<24} {'trades':>7} {'days':>5} {'busiest':>8} "
          f"{'win':>7} {'avg R':>7} {'p':>8}")
    print("  " + "-" * 72)

    for result in results:
        decided = result.decided()
        day_count, share = concentration(result)

        if not decided:
            print(f"  {result.name:<24} {0:>7} {day_count:>5} "
                  f"{'-':>8} {'-':>7} {'-':>7} {'-':>8}")
            continue

        p = binomial_tail(result.wins(), len(decided), breakeven)

        print(f"  {result.name:<24} {len(decided):>7} {day_count:>5} "
              f"{share:>7.0%} {result.win_rate():>6.1%} "
              f"{result.expectancy():>+7.2f} {p:>8.4f}")

    print()
    print("  HOW TO READ THIS")
    print("  " + "-" * 72)
    print("  'p' is the chance of a win rate this LOW if the rule were")
    print("  truly breakeven. Small p on a losing rule means the loss is")
    print("  real; it does NOT mean a winning rule is proven.")
    print()

    worst_concentration = max(
        (concentration(r)[1] for r in results if r.decided()), default=0.0
    )
    fewest_days = min(
        (concentration(r)[0] for r in results if r.decided()), default=0
    )

    if worst_concentration >= 0.5:
        print(f"  WARNING: up to {worst_concentration:.0%} of a rule's trades come")
        print("  from a single session. Same-day trades share one market and")
        print("  fail together, so the effective sample is DAYS, not trades.")

    if fewest_days and fewest_days < 20:
        print(f"  WARNING: only {fewest_days} trading day(s) covered. Twenty to")
        print("  forty independent days is the minimum before a difference")
        print("  between rules means anything.")

    if len(results) > 1:
        print()
        print(f"  {len(results)} rules were compared. At p<0.05 roughly one in")
        print("  twenty looks significant by chance, so the best-looking rule")
        print("  here is the one most likely to be a fluke. Anything found is")
        print("  a hypothesis for NEW data, never a rule to deploy.")

    print("=" * 76)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    import pandas as pd

    print("Statistics")

    check("breakeven at 2:1 is 33.3%",
          abs(breakeven_rate(2.0, 1.0) - 1 / 3) < 1e-9)
    check("breakeven at 1:1 is 50%",
          abs(breakeven_rate(1.0, 1.0) - 0.5) < 1e-9)
    check("reproduces the 19/94 shortfall",
          abs(binomial_tail(19, 94, 1 / 3) - 0.00366) < 0.0005,
          f"{binomial_tail(19, 94, 1/3):.5f}")
    check("an empty sample is safe", binomial_tail(0, 0, 0.5) == 1.0)

    # A year of history produces thousands of trades, and the exact form
    # raised OverflowError there -- comb(2485, 1242) has ~747 digits.
    # The crash appeared precisely when the sample became worth trusting.
    big = binomial_tail(856, 2485, 1 / 3)
    check("a large sample does not overflow", 0.0 <= big <= 1.0, str(big))
    check("and 34.4% against 33.3% is unremarkable", big > 0.05,
          f"p(<=856 of 2485) = {big:.4f}")

    check("a huge sample is still bounded",
          0.0 <= binomial_tail(50_000, 150_000, 1 / 3) <= 1.0)

    # The approximation must agree with the exact form where they meet.
    exact = binomial_tail(340, 1000, 1 / 3)
    check("exact and approximate agree at the boundary",
          abs(exact - 0.5) < 0.5, f"{exact:.4f}")
    check("wins above n is clamped", binomial_tail(99, 10, 0.5) == 1.0)
    check("negative wins is zero", binomial_tail(-1, 10, 0.5) == 0.0)

    print()
    print("Simulation")

    def frame_from(prices):
        """Bars that walk through the given closes, high/low +-0.5%."""
        rows = []
        base = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)
        for i, price in enumerate(prices):
            rows.append({
                "timestamp": base + timedelta(minutes=5 * i),
                "open": price,
                "close": price,
                "high": price * 1.005,
                "low": price * 0.995,
                "ema_9": price * 0.99,
                "ema_21": price * 0.98,
                "vwap": price * 0.99,
                "rsi": 60.0,
                "macd": 1.0,
                "macd_signal": 0.5,
                "atr": price * 0.01,
                "volume": 100_000.0,
                "volume_avg_20": 80_000.0,
            })
        return pd.DataFrame(rows)

    # The fixture must carry every field a rule is allowed to reference.
    # It did not, and the gap was invisible: compile_spec answers
    # NO_TRADE on a missing key, so a rule using volume looked tested and
    # worthless rather than never run at all. Asserting the contract here
    # is what stops that drifting apart again.
    try:
        from strategy_lab import FIELDS as ALLOWED_FIELDS

        check("the fixture supplies every field a rule may reference",
              ALLOWED_FIELDS <= set(frame_from([100.0]).columns),
              f"missing {sorted(ALLOWED_FIELDS - set(frame_from([100.0]).columns))}")
    except ImportError:
        check("strategy_lab importable for the field contract", False)

    always_long = lambda row, trend: "BUY_LONG"
    never = lambda row, trend: "NO_TRADE"

    # Rising hard: a long entered at 100 with a 2% stop / 4% target wins.
    rising = frame_from([100.0] + [100.0 * (1.01 ** i) for i in range(1, 12)])
    won = simulate_symbol(
        rising, symbol="UP", rule=always_long, stop_percent=0.02,
        reward_ratio=2.0, max_bars_held=50,
    )
    check("a rising series reaches the target",
          any(t.outcome == OUTCOME_TARGET for t in won),
          str([t.outcome for t in won]))

    falling = frame_from([100.0] + [100.0 * (0.99 ** i) for i in range(1, 12)])
    lost = simulate_symbol(
        falling, symbol="DOWN", rule=always_long, stop_percent=0.02,
        reward_ratio=2.0, max_bars_held=50,
    )
    check("a falling series reaches the stop",
          any(t.outcome == OUTCOME_STOP for t in lost),
          str([t.outcome for t in lost]))

    check("a rule that never fires makes no trades",
          simulate_symbol(rising, symbol="X", rule=never, stop_percent=0.02,
                          reward_ratio=2.0, max_bars_held=50) == [])

    flat = frame_from([100.0] * 12)
    timed_out = simulate_symbol(
        flat, symbol="FLAT", rule=always_long, stop_percent=0.05,
        reward_ratio=2.0, max_bars_held=3,
    )
    check("a flat series times out rather than guessing",
          all(t.outcome == OUTCOME_OPEN for t in timed_out),
          str([t.outcome for t in timed_out]))

    print()
    print("Scoring")

    result = Result(name="test")
    base = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)

    def trade(outcome, r, day_offset=0):
        return Trade(
            symbol="X",
            entered_at=base + timedelta(days=day_offset),
            side="BUY_LONG", entry=100.0, stop=98.0, target=104.0,
            outcome=outcome, r_multiple=r,
        )

    result.trades = [
        trade(OUTCOME_TARGET, 2.0),
        trade(OUTCOME_STOP, -1.0),
        trade(OUTCOME_STOP, -1.0),
        trade(OUTCOME_OPEN, 0.0),
    ]

    check("open trades are excluded from decided", len(result.decided()) == 3)
    check("win rate ignores open trades",
          abs(result.win_rate() - 1 / 3) < 1e-9, str(result.win_rate()))
    check("expectancy is zero at breakeven 2:1",
          abs(result.expectancy()) < 1e-9, str(result.expectancy()))

    days, share = concentration(result)
    check("single-day sample is 100% concentrated",
          days == 1 and abs(share - 1.0) < 1e-9, f"{days} {share}")

    result.trades.append(trade(OUTCOME_TARGET, 2.0, day_offset=1))
    days, share = concentration(result)
    check("adding a second day is detected", days == 2, str(days))
    check("and concentration falls below 100%", share < 1.0, str(share))

    check("ambiguous scores as a loss",
          trade(OUTCOME_AMBIGUOUS, -1.0).r_multiple == -1.0)

    print()
    print("Baseline rule is the live rule")

    from market_scanner import detect_signal

    good = {
        "close": 103.0, "ema_9": 101.0, "ema_21": 98.0, "vwap": 100.0,
        "rsi": 62.0, "macd": 1.2, "macd_signal": 0.4,
    }
    check("baseline agrees with market_scanner.detect_signal",
          baseline_rule(good, "BULLISH")
          == detect_signal(good, "BULLISH", data_is_fresh=True)[0])
    check("and fires on a valid bullish setup",
          baseline_rule(good, "BULLISH") == "BUY_LONG")
    check("and stays out on a neutral trend",
          baseline_rule(good, "NEUTRAL") == "NO_TRADE")

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All backtest checks passed.")
    return 0


def load_history(symbols: list[str], *, days: int) -> dict[str, Any]:
    """Fetch 5-minute bars and attach LOCKBOT's own indicators.

    Indicators come from indicators.add_indicators -- the same function
    market_scanner.py uses live. Recomputing them here would test a
    different bot than the one that trades.
    """

    import os

    import pandas as pd
    from alpaca.data.enums import Adjustment
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from dotenv import load_dotenv

    from indicators import add_indicators

    load_dotenv()

    client = StockHistoricalDataClient(
        os.getenv(config.ALPACA_API_KEY_ENV),
        os.getenv(config.ALPACA_SECRET_KEY_ENV),
    )

    # The free feed is delayed; asking for the live edge fails the whole
    # request rather than returning what it can.
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=days)

    frames: dict[str, Any] = {}

    for batch_start in range(0, len(symbols), 40):
        batch = symbols[batch_start:batch_start + 40]

        try:
            response = client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=batch,
                    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                    start=start,
                    end=end,
                    feed=config.ALPACA_DATA_FEED,
                    # SPLIT-ADJUSTED, and this is not optional.
                    #
                    # Alpaca defaults to RAW. A 3-for-1 split then appears
                    # as a 67% single-day collapse that never happened,
                    # which trips every stop below it and invents a
                    # catastrophic loss for any open long.
                    #
                    # Found 2026-08-05: over a 365-day window, 3 of 40
                    # universe symbols were affected (XLU, BN, HDB), and
                    # over 5 years 7 of 12 index ETFs were -- XLE reads
                    # +17% raw against +182% adjusted. Every backtest run
                    # before this date over a window containing a split
                    # was scoring a price series that never existed.
                    adjustment=Adjustment.ALL,
                )
            )
        except Exception as error:
            print(f"  batch fetch failed: {type(error).__name__}: {error}")
            continue

        for symbol in batch:
            if symbol not in response.data:
                continue

            bars = response[symbol]

            if len(bars) < 60:
                continue

            frame = pd.DataFrame([{
                "timestamp": bar.timestamp,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            } for bar in bars])

            try:
                frames[symbol] = add_indicators(frame).dropna()
            except Exception as error:
                print(f"  {symbol}: indicators failed "
                      f"({type(error).__name__}: {error})")

    return frames


def run_rules(
    frames: dict[str, Any],
    rules: dict[str, Callable[[Any, str], str]],
    *,
    stop_percent: float,
    reward_ratio: float,
    max_bars_held: int,
) -> list[Result]:
    """Run every rule over every symbol's history."""

    results = []

    for name, rule in rules.items():
        result = Result(name=name, symbols_tested=len(frames))

        for symbol, frame in frames.items():
            result.bars_seen += len(frame)
            result.trades.extend(simulate_symbol(
                frame,
                symbol=symbol,
                rule=rule,
                stop_percent=stop_percent,
                reward_ratio=reward_ratio,
                max_bars_held=max_bars_held,
            ))

        results.append(result)

    return results


def main() -> int:
    """Backtest the live rule, plus its inverse as a control."""

    from universe import load_universe

    days = 5

    for argument in sys.argv[1:]:
        if argument.startswith("--days="):
            days = int(argument.split("=", 1)[1])

    symbols = load_universe(config.UNIVERSE_FILE)

    if not symbols:
        print("universe.csv is empty. Run build_universe.py first.")
        return 1

    print(f"Loading {days} day(s) of 5-minute bars for "
          f"{len(symbols)} symbol(s)…")

    frames = load_history(symbols, days=days)

    if not frames:
        print("No usable history was returned.")
        return 1

    print(f"  {len(frames)} symbol(s) with enough bars.")

    def inverted(row: dict[str, Any], trend: str) -> str:
        """The live rule's opposite. A control, not a proposal.

        If the baseline is genuinely losing then its inverse should look
        like it wins -- which is a sanity check on the harness, not a
        strategy. Shorting is also unavailable under $2,000 of equity, so
        this can be measured but not traded.
        """

        signal = baseline_rule(row, trend)

        if signal == "BUY_LONG":
            return "SELL_SHORT"

        if signal == "SELL_SHORT":
            return "BUY_LONG"

        return "NO_TRADE"

    stop_percent = getattr(config, "BRACKET_STOP_LOSS_PERCENT", 0.02)
    reward_ratio = getattr(config, "ATR_REWARD_RATIO", 2.0)

    results = run_rules(
        frames,
        {"baseline (live rule)": baseline_rule, "inverted (control)": inverted},
        stop_percent=stop_percent,
        reward_ratio=reward_ratio,
        max_bars_held=78,  # one full session of 5-minute bars
    )

    report(results, reward_ratio=reward_ratio)

    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    sys.exit(main())
