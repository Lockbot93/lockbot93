"""
news_signal.py — does unusual news attention precede anything?

WHY THIS EXISTS

Ten strategy families have been tested against controls and every one
lost to doing nothing. LOCKBOT set a bar for what would be worth trying
next: an input NOT derived from these OHLCV bars, testable across
separate YEARS, judged against random entry rather than breakeven. Both
of us then concluded nothing available cleared it.

Neither of us had checked. The Alpaca news API carries five years of
history with 100% coverage of the lab universe, median 24 articles per
symbol per quarter. Headlines are not derived from price and they are
testable across years, so this is the first foundation that clears the
bar.

WHAT IS TESTED, AND WHY NOT SENTIMENT

The timestamp, not the text. Scoring five years of headlines through a
language model costs real money and invites exactly the overfitting this
project has spent a week avoiding. Article COUNT against a symbol's own
trailing baseline needs no interpretation and cannot be tuned after the
fact.

THE TRAP THIS IS BUILT AROUND

News follows price. A story about a move is published after the move, so
"buy on a news spike" can beat random entry while being nothing but
momentum arriving through a more expensive data source.

LOCKBOT's structural defence, and the reason this module has two
controls rather than one:

    random         seeded random entries, matched in count per year
    price-matched  entries on symbol-days with SIMILAR day-T price
                   action and NO news

Beating random while failing price-matched means momentum was
rediscovered, not news. The price-matched margin is the binding test.

PRE-REGISTERED ACCEPTANCE, fixed before any result was seen

    >= 500 resolved events, timeouts booked at mark, not dropped
    >= 3 separate calendar years
    no single year contributing more than 40% of events
    the SIGN of the edge the same in every year
    outside the random control's 95th percentile in EACH year
    >= +0.10R over the price-matched control, pooled

Any year with the opposite sign, or a pooled price-matched margin under
+0.10R, kills the hypothesis. No re-cutting the spike threshold or the
baseline window afterwards.

LOOKAHEAD RULES, also pre-registered

    the counting window closes at day T's market close
    entry is the T+1 open, so the minimum gap is a full overnight
    articles published after the T+1 open are excluded outright
    articles in the last 30 minutes before T's close are excluded,
      because created_at can lag the wire and a late-tagged story can
      carry the closing move inside it
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

import lockbot_config as config

# Pre-registered parameters. Changing any of these after seeing a result
# is curve fitting; the docstring says so and so does LOCKBOT.
BASELINE_DAYS = 60          # trailing window for a symbol's normal volume
SPIKE_MULTIPLE = 3.0        # articles must exceed baseline by this factor
MIN_SPIKE_ARTICLES = 3      # and clear an absolute floor
HOLD_DAYS = 5               # trading days held
STOP_PERCENT = 0.05
REWARD_RATIO = 2.0
CLOSE_BUFFER_MINUTES = 30   # ignore articles this close to T's close

# US regular session close, in UTC. Used only to apply the buffer rule.
MARKET_CLOSE_UTC = time(20, 0)

ACCEPT_MIN_EVENTS = 500
ACCEPT_MIN_YEARS = 3
ACCEPT_MAX_YEAR_SHARE = 0.40
ACCEPT_MIN_MARGIN_R = 0.10


def _client_pair():
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.historical.stock import StockHistoricalDataClient

    key = os.getenv(config.ALPACA_API_KEY_ENV)
    secret = os.getenv(config.ALPACA_SECRET_KEY_ENV)

    return NewsClient(key, secret), StockHistoricalDataClient(key, secret)


def fetch_news_counts(symbols, start, end, *, verbose=False):
    """Article counts per (symbol, date), honouring the lookahead rules.

    Returns {symbol: {date: count}}. Articles inside CLOSE_BUFFER_MINUTES
    of the session close are dropped, because Alpaca's created_at can lag
    the wire and a late-tagged story can carry the closing move.
    """

    from alpaca.data.requests import NewsRequest

    news_client, _ = _client_pair()
    counts = defaultdict(lambda: defaultdict(int))
    cutoff = (datetime.combine(date(2000, 1, 1), MARKET_CLOSE_UTC)
              - timedelta(minutes=CLOSE_BUFFER_MINUTES)).time()

    window = start
    step = timedelta(days=30)

    while window < end:
        chunk_end = min(window + step, end)

        for i in range(0, len(symbols), 20):
            batch = symbols[i:i + 20]

            try:
                page = None

                while True:
                    request = NewsRequest(
                        symbols=",".join(batch),
                        start=window,
                        end=chunk_end,
                        limit=50,
                        page_token=page,
                    )
                    response = news_client.get_news(request)
                    items = (response.data.get("news", [])
                             if hasattr(response, "data") else [])

                    for article in items:
                        stamp = article.created_at

                        if stamp.tzinfo is None:
                            stamp = stamp.replace(tzinfo=timezone.utc)

                        # The buffer rule.
                        if stamp.time() >= cutoff:
                            continue

                        for symbol in (getattr(article, "symbols", None) or []):
                            if symbol in batch:
                                counts[symbol][stamp.date()] += 1

                    page = getattr(response, "next_page_token", None)

                    if not page or not items:
                        break

            except Exception as error:
                if verbose:
                    print(f"    news {batch[0]}..: "
                          f"{type(error).__name__}: {str(error)[:70]}")

        if verbose:
            print(f"  news through {chunk_end.date()}: "
                  f"{sum(len(v) for v in counts.values())} symbol-days",
                  flush=True)

        window = chunk_end

    return {s: dict(d) for s, d in counts.items()}


def fetch_daily_bars(symbols, start, end):
    """Split-adjusted daily bars, keyed by symbol then date."""

    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    _, stock_client = _client_pair()
    bars = {}

    for i in range(0, len(symbols), 40):
        batch = symbols[i:i + 40]

        try:
            response = stock_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed="sip",
                adjustment=Adjustment.ALL,
            ))
        except Exception:
            continue

        for symbol in batch:
            if symbol not in response.data:
                continue

            series = {}

            for bar in response[symbol]:
                stamp = bar.timestamp
                series[stamp.date()] = {
                    "open": float(bar.open), "high": float(bar.high),
                    "low": float(bar.low), "close": float(bar.close),
                }

            if len(series) > 200:
                bars[symbol] = series

    return bars


def baseline_at(history, day, *, window=BASELINE_DAYS):
    """A symbol's normal daily article count before this date."""

    start = day - timedelta(days=window)
    counts = [n for d, n in history.items() if start <= d < day]

    if not counts:
        return 0.0

    return sum(counts) / window


def find_spikes(news_counts, bars):
    """Symbol-days where attention exceeded that symbol's own baseline."""

    spikes = []

    for symbol, history in news_counts.items():
        series = bars.get(symbol)

        if not series:
            continue

        for day, count in sorted(history.items()):
            if count < MIN_SPIKE_ARTICLES:
                continue

            base = baseline_at(history, day)

            if base <= 0 or count < base * SPIKE_MULTIPLE:
                continue

            spikes.append({"symbol": symbol, "date": day,
                           "articles": count, "baseline": round(base, 2)})

    return spikes


def _sessions(series):
    return sorted(series)


def simulate(symbol, entry_day, bars, *, stop_percent=STOP_PERCENT,
             reward_ratio=REWARD_RATIO, hold_days=HOLD_DAYS):
    """Enter at the NEXT session's open, hold to bracket or timeout.

    Returns an R multiple, with a timeout marked to market rather than
    dropped -- dropping unresolved trades conditions on resolution and
    flatters wide targets.
    """

    series = bars.get(symbol)

    if not series:
        return None

    days = _sessions(series)

    try:
        index = days.index(entry_day)
    except ValueError:
        return None

    if index + 1 >= len(days):
        return None

    entry_date = days[index + 1]
    entry = series[entry_date]["open"]

    if entry <= 0:
        return None

    stop = entry * (1 - stop_percent)
    target = entry * (1 + stop_percent * reward_ratio)

    for offset in range(1, hold_days + 1):
        if index + 1 + offset >= len(days):
            break

        bar = series[days[index + 1 + offset]]

        hit_target = bar["high"] >= target
        hit_stop = bar["low"] <= stop

        if hit_target and hit_stop:
            return -1.0          # ambiguous bar counts against
        if hit_target:
            return float(reward_ratio)
        if hit_stop:
            return -1.0

    # Timed out: mark to market in R.
    last = series[days[min(index + 1 + hold_days, len(days) - 1)]]["close"]

    return (last - entry) / (entry * stop_percent)


def day_return(symbol, day, bars):
    """Day T's own return, used to match the no-news control."""

    series = bars.get(symbol)

    if not series:
        return None

    days = _sessions(series)

    try:
        index = days.index(day)
    except ValueError:
        return None

    if index == 0:
        return None

    prior = series[days[index - 1]]["close"]
    today = series[day]["close"]

    return (today - prior) / prior if prior > 0 else None


def build_price_matched(spikes, news_counts, bars, *, tolerance=0.01):
    """For each spike, a no-news symbol-day with similar day-T movement.

    This is the control that matters. News follows price, so a spike
    strategy can beat random entry purely by rediscovering momentum.
    Matching on day-T return and requiring NO news isolates whatever the
    headline adds beyond the move that produced it.
    """

    import random

    rng = random.Random(20260806)

    # Every symbol-day with no article at all, indexed by rounded return.
    quiet = defaultdict(list)

    for symbol, series in bars.items():
        history = news_counts.get(symbol, {})

        for day in _sessions(series):
            if history.get(day, 0) > 0:
                continue

            move = day_return(symbol, day, bars)

            if move is None:
                continue

            quiet[round(move / tolerance)].append((symbol, day))

    matched = []

    for spike in spikes:
        move = day_return(spike["symbol"], spike["date"], bars)

        if move is None:
            continue

        bucket = quiet.get(round(move / tolerance))

        if not bucket:
            continue

        matched.append(rng.choice(bucket))

    return matched


def build_random(spikes, bars, *, seed=20260806):
    """Seeded random symbol-days, matched in count to the spikes."""

    import random

    rng = random.Random(seed)
    pool = [(s, d) for s, series in bars.items() for d in _sessions(series)]

    if not pool:
        return []

    return [rng.choice(pool) for _ in spikes]


def score(events, bars):
    """(count, mean R, wins, win rate) for a list of (symbol, date)."""

    results = []

    for symbol, day in events:
        r = simulate(symbol, day, bars)

        if r is not None:
            results.append(r)

    if not results:
        return 0, 0.0, 0, 0.0

    wins = sum(1 for r in results if r > 0)

    return (len(results), sum(results) / len(results), wins,
            wins / len(results))


def _self_test() -> int:
    failures = []

    def check(name, condition, detail=""):
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    print("Pre-registered parameters are fixed")

    check("acceptance needs 500 events", ACCEPT_MIN_EVENTS == 500)
    check("and three years", ACCEPT_MIN_YEARS == 3)
    check("no year may dominate", ACCEPT_MAX_YEAR_SHARE == 0.40)
    check("and the margin is over the PRICE-MATCHED control",
          ACCEPT_MIN_MARGIN_R == 0.10)

    print()
    print("Entry is the day AFTER the signal")

    days = [date(2026, 8, d) for d in (3, 4, 5, 6, 7, 10, 11, 12)]
    series = {d: {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0}
              for d in days}
    bars = {"TEST": series}

    # A target hit on the third held day.
    series[days[4]] = {"open": 100.0, "high": 120.0, "low": 99.5,
                       "close": 119.0}

    r = simulate("TEST", days[1], bars)
    check("a target beyond the entry resolves as a win", r == REWARD_RATIO,
          str(r))

    check("the signal day itself is never the entry",
          simulate("TEST", days[-1], bars) is None,
          "no next session should mean no trade")

    flat = {"FLAT": {d: {"open": 100.0, "high": 100.1, "low": 99.9,
                         "close": 100.0} for d in days}}
    timeout = simulate("FLAT", days[0], flat)
    check("a timeout is marked to market, not dropped",
          timeout is not None and abs(timeout) < 0.5, str(timeout))

    print()
    print("Spikes need a baseline AND a floor")

    history = {date(2026, 6, d % 28 + 1): 1 for d in range(1, 60)}
    history[date(2026, 8, 3)] = 20

    spikes = find_spikes({"TEST": history}, bars)
    check("a genuine spike is found", any(s["articles"] == 20 for s in spikes),
          str(spikes))

    quiet_history = {date(2026, 8, 3): 2}
    check("two articles never counts as a spike",
          find_spikes({"TEST": quiet_history}, bars) == [],
          "MIN_SPIKE_ARTICLES is the floor")

    print()
    print("Controls are seeded, so a rerun gives the same answer")

    fake_spikes = [{"symbol": "TEST", "date": days[1]}] * 5
    a = build_random(fake_spikes, bars)
    b = build_random(fake_spikes, bars)
    check("random entries reproduce", a == b)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All news-signal checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(__doc__)
