"""
universe_volatility.py  --  LOCKBOT movement filter  (v1.3)

WHAT THIS DOES
--------------
LOCKBOT's brackets ask for a 4% target and a 2% stop. A stock that
barely moves 0.3% on a normal day physically cannot reach that target
in any reasonable amount of time -- it just sits in a position slot
forever (see: VTEB, a municipal bond ETF).

This script removes those stocks from the daily list.

It runs AFTER universe.py, reads universe.csv, measures how much each
name typically moves in a day (ATR), and rewrites universe.csv with
only the names that can actually get where the bracket needs them to go.

It does NOT edit universe.py, market_scanner.py, or anything else.
It does NOT place orders, cancel orders, or touch open positions.
It only reads price history and rewrites one list file.

USAGE
-----
    python universe_volatility.py --self-test    # offline math check, no network
    python universe_volatility.py --dry-run      # show what WOULD be cut, change nothing
    python universe_volatility.py                # actually filter universe.csv
    python universe_volatility.py --show         # print the current filtered list

Run it right after universe.py each morning.
"""

import argparse
import csv
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Defaults -- overridden by lockbot_config.py if the values exist there
# ---------------------------------------------------------------------------

# A stock must typically move at least this much in a day (as a fraction).
# 0.0125 = 1.25%. Reasoning: the take-profit is 4%. At 1.25% of typical
# daily movement, a 4% move is about 3 good days -- reachable. Below that,
# the position slot is effectively dead capital.
DEFAULT_MIN_ATR_PERCENT = 0.0125

# And no more than this -- above ~15% daily range in a $5-$50 stock you are
# usually looking at a pump, a halt risk, or a news blowup, not a setup.
DEFAULT_MAX_ATR_PERCENT = 0.15

# ATR lookback in trading days.
ATR_PERIOD = 14

# If filtering would leave fewer than this many symbols, refuse to write.
# Better to trade a flawed list than an empty one.
MIN_UNIVERSE_SIZE = 40

UNIVERSE_FILE = "universe.csv"
BACKUP_FILE = "universe_prefilter.csv"
REPORT_FILE = "universe_volatility_report.csv"

BATCH_SIZE = 100

KEEP = "KEEP"
TOO_QUIET = "TOO_QUIET"
TOO_WILD = "TOO_WILD"
NO_DATA = "NO_DATA"


# ---------------------------------------------------------------------------
# The math  (pure functions -- no network, fully testable offline)
# ---------------------------------------------------------------------------

def true_ranges(bars):
    """
    True Range for each bar after the first.

    True Range = the largest of:
        today's high - today's low
        |today's high - yesterday's close|
        |today's low  - yesterday's close|

    The last two matter because a stock can gap overnight, and that gap is
    real movement even though it never showed up inside a single bar.

    `bars` is a list of objects (or dicts) with high / low / close,
    oldest first.
    """
    out = []
    for i in range(1, len(bars)):
        high = _field(bars[i], "high")
        low = _field(bars[i], "low")
        prev_close = _field(bars[i - 1], "close")
        if high is None or low is None or prev_close is None:
            continue
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        out.append(tr)
    return out


def calculate_atr(bars, period=ATR_PERIOD):
    """
    Average True Range: the plain average of the last `period` true ranges.

    Returns None if there is not enough history to be meaningful.
    """
    trs = true_ranges(bars)
    if len(trs) < period:
        return None
    window = trs[-period:]
    return sum(window) / len(window)


def atr_percent(bars, period=ATR_PERIOD):
    """
    ATR expressed as a fraction of the most recent close, so a $10 stock
    and a $400 stock can be compared on the same scale.

    0.02 means "this thing typically travels about 2% of its price per day".
    """
    atr = calculate_atr(bars, period=period)
    if atr is None:
        return None
    last_close = _field(bars[-1], "close")
    if not last_close or last_close <= 0:
        return None
    return atr / last_close


def classify(atr_pct, min_pct, max_pct):
    """Decide what to do with a symbol given its measured movement."""
    if atr_pct is None:
        return NO_DATA
    if atr_pct < min_pct:
        return TOO_QUIET
    if atr_pct > max_pct:
        return TOO_WILD
    return KEEP


def suggested_min_atr(take_profit_percent, days_to_target=3.0):
    """
    A defensible starting threshold rather than a number pulled from the air.

    If the target is 4% and you want it reachable within about 3 normal
    days of movement, the stock needs to travel roughly 4% / 3 per day.
    """
    if not take_profit_percent or take_profit_percent <= 0:
        return DEFAULT_MIN_ATR_PERCENT
    return take_profit_percent / float(days_to_target)


def _field(bar, name):
    """Read a field off either an object (Alpaca bar) or a plain dict."""
    if isinstance(bar, dict):
        value = bar.get(name)
    else:
        value = getattr(bar, name, None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# universe.csv handling -- preserve the file's existing shape exactly
# ---------------------------------------------------------------------------

def load_universe_csv(path=UNIVERSE_FILE):
    """
    Returns (fieldnames, rows). Rows keep their original order and columns
    so the rewritten file is byte-compatible with whatever universe.py wrote
    and whatever load_universe() expects.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} not found. Run universe.py first to build the list."
        )
    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        rows = [row for row in reader]
    if not fieldnames:
        raise ValueError(f"{path} has no header row -- refusing to touch it.")
    return fieldnames, rows


def symbol_of(row):
    """universe.csv might name the column 'symbol', 'Symbol', or 'ticker'."""
    for key in ("symbol", "Symbol", "SYMBOL", "ticker", "Ticker"):
        if key in row and row[key]:
            return str(row[key]).strip().upper()
    # Fall back to the first column's value.
    for value in row.values():
        if value:
            return str(value).strip().upper()
    return None


def write_universe_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def write_report(path, records):
    """Full detail lands here, separate from universe.csv so nothing breaks."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "atr_percent", "atr_dollars",
                         "last_close", "bars_used", "decision"])
        for rec in records:
            writer.writerow([
                rec["symbol"],
                "" if rec["atr_pct"] is None else round(rec["atr_pct"] * 100, 3),
                "" if rec["atr"] is None else round(rec["atr"], 4),
                "" if rec["last_close"] is None else round(rec["last_close"], 4),
                rec["bars"],
                rec["decision"],
            ])


# ---------------------------------------------------------------------------
# Config + network wiring (mirrors how the other LOCKBOT modules do it)
# ---------------------------------------------------------------------------

def load_thresholds():
    """Pull thresholds from lockbot_config.py if present, else use defaults."""
    min_pct = DEFAULT_MIN_ATR_PERCENT
    max_pct = DEFAULT_MAX_ATR_PERCENT
    note = "defaults (lockbot_config.py values not found)"
    try:
        import lockbot_config as cfg
        found = []
        if hasattr(cfg, "UNIVERSE_MIN_ATR_PERCENT"):
            min_pct = float(cfg.UNIVERSE_MIN_ATR_PERCENT)
            found.append("min")
        elif hasattr(cfg, "TAKE_PROFIT_PERCENT"):
            min_pct = suggested_min_atr(float(cfg.TAKE_PROFIT_PERCENT))
            found.append("min (derived from TAKE_PROFIT_PERCENT)")
        if hasattr(cfg, "UNIVERSE_MAX_ATR_PERCENT"):
            max_pct = float(cfg.UNIVERSE_MAX_ATR_PERCENT)
            found.append("max")
        if found:
            note = "from lockbot_config.py: " + ", ".join(found)
    except Exception as exc:  # config missing or broken -- never fatal
        note = f"defaults (could not read lockbot_config.py: {exc})"
    return min_pct, max_pct, note


def retry_call(func, *args, **kwargs):
    """
    Deliberately does NOT use retry_utils.with_retries.

    v1.0 wrapped this call in with_retries, which is a decorator: it returns
    a wrapped function rather than a result. The script stored that function
    object, found no .data on it, and reported "0 symbols" without raising a
    single error. Two network requests a day do not justify that risk, so
    this is a plain local retry with no outside dependency.
    """
    import time
    last_error = None
    for attempt in range(3):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                wait = 2 ** attempt
                print(f"    request failed ({exc}) -- retrying in {wait}s")
                time.sleep(wait)
    raise last_error


def extract_bars(response):
    """
    Pull {symbol: [bars]} out of whatever alpaca-py handed back.

    Different versions return a BarSet, a plain dict, or something with a
    dataframe. If none of those shapes fit, this raises instead of quietly
    returning nothing -- silence is what cost us the last run.
    """
    if response is None:
        raise RuntimeError("the price request returned nothing at all")

    if callable(response):
        raise RuntimeError(
            "the price request returned a function instead of data "
            "(a retry wrapper was not called)"
        )

    # Shape 1: BarSet-style object with a .data dict
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return {str(k).upper(): list(v) for k, v in data.items()}

    # Shape 2: already a plain dict of symbol -> bars
    if isinstance(response, dict):
        return {str(k).upper(): list(v) for k, v in response.items()}

    # Shape 3: subscriptable BarSet -- try iterating its keys
    try:
        keys = list(response.keys())
        return {str(k).upper(): list(response[k]) for k in keys}
    except Exception:
        pass

    raise RuntimeError(
        f"could not read price data out of a {type(response).__name__} object"
    )


def make_client():
    """Load .env, verify keys exist, return a data client."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    from alpaca.data.historical import StockHistoricalDataClient

    api_key = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
    secret_key = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError(
            "Alpaca keys not found in .env "
            "(expected ALPACA_API_KEY / ALPACA_SECRET_KEY)."
        )
    return StockHistoricalDataClient(api_key, secret_key)


def fetch_daily_bars(symbols, lookback_days=60, verbose=False):
    """
    One batched daily-bars request per 100 symbols. Returns {symbol: [bars]}.

    Raises loudly if a batch comes back empty rather than reporting zeros.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = make_client()

    # Daily bars only need a date. Ending "yesterday" avoids the free-feed
    # restriction on very recent data, which returns an empty set rather
    # than an error.
    end = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    start = end - timedelta(days=lookback_days)

    out = {}
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        print(f"  fetching daily bars {i + 1}-{i + len(batch)} of {len(symbols)}...")

        def _do(extra):
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                **extra,
            )
            return client.get_stock_bars(req)

        response = retry_call(_do, {})
        if verbose:
            print(f"    response type: {type(response).__name__}")

        batch_data = extract_bars(response)

        if not batch_data:
            raise RuntimeError(
                f"Alpaca returned no price data for any of {len(batch)} symbols "
                f"between {start} and {end}.\n"
                f"    First few requested: {', '.join(batch[:5])}\n"
                f"    Likely causes: those are not valid ticker symbols, or the "
                f"data plan does not cover this request.\n"
                f"    Run: python universe_volatility.py --diagnose"
            )

        out.update(batch_data)

    return out


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def evaluate(rows, bars_by_symbol, min_pct, max_pct):
    """Score every row in the universe. Returns (records, kept_rows)."""
    records = []
    kept_rows = []
    for row in rows:
        symbol = symbol_of(row)
        if not symbol:
            continue
        bars = bars_by_symbol.get(symbol, [])
        pct = atr_percent(bars)
        atr = calculate_atr(bars)
        last_close = _field(bars[-1], "close") if bars else None
        decision = classify(pct, min_pct, max_pct)
        records.append({
            "symbol": symbol,
            "atr_pct": pct,
            "atr": atr,
            "last_close": last_close,
            "bars": len(bars),
            "decision": decision,
        })
        if decision == KEEP:
            kept_rows.append(row)
    return records, kept_rows


def print_distribution(records, min_pct, max_pct):
    """
    Show how the whole list is spread out, so a threshold can be picked from
    the real numbers instead of from a guess.
    """
    bands = [
        (0.000, 0.005), (0.005, 0.010), (0.010, 0.0125), (0.0125, 0.015),
        (0.015, 0.020), (0.020, 0.025), (0.025, 0.030), (0.030, 0.040),
        (0.040, 0.050), (0.050, 0.070), (0.070, 0.100), (0.100, 99.0),
    ]
    measured = [r for r in records if r["atr_pct"] is not None]
    if not measured:
        return

    print()
    print("  How the whole list is spread out (daily movement):")
    for low, high in bands:
        count = sum(1 for r in measured if low <= r["atr_pct"] < high)
        if not count:
            continue
        label = f"{low * 100:5.2f}-{high * 100:5.2f}%" if high < 90 \
            else f"{low * 100:5.2f}%+     "
        inside = low >= min_pct and high <= max_pct
        marker = "keep" if inside else "    "
        print(f"    {label}  {'#' * min(count, 40):<40} {count:>3}  {marker}")


def print_summary(records, min_pct, max_pct, note, show_all=False):
    counts = {}
    for rec in records:
        counts[rec["decision"]] = counts.get(rec["decision"], 0) + 1

    print()
    print("=" * 62)
    print("MOVEMENT FILTER RESULTS")
    print("=" * 62)
    print(f"Thresholds: keep {min_pct * 100:.2f}% to {max_pct * 100:.2f}% "
          f"average daily movement")
    print(f"Source: {note}")
    print()
    print(f"  Checked:    {len(records)}")
    print(f"  KEEP:       {counts.get(KEEP, 0)}")
    print(f"  TOO_QUIET:  {counts.get(TOO_QUIET, 0)}   (cannot reach a 4% target)")
    print(f"  TOO_WILD:   {counts.get(TOO_WILD, 0)}")
    print(f"  NO_DATA:    {counts.get(NO_DATA, 0)}   (not enough price history)")

    print_distribution(records, min_pct, max_pct)

    quiet = sorted(
        [r for r in records if r["decision"] == TOO_QUIET],
        key=lambda r: r["atr_pct"],
    )
    if quiet:
        print()
        print("  Quietest names being removed:")
        for rec in quiet[:12]:
            print(f"    {rec['symbol']:<6} {rec['atr_pct'] * 100:5.2f}% per day")
        if len(quiet) > 12:
            print(f"    ... and {len(quiet) - 12} more")

    kept = sorted(
        [r for r in records if r["decision"] == KEEP],
        key=lambda r: r["atr_pct"],
        reverse=True,
    )
    if kept and not show_all:
        print()
        print("  Most active names being kept:")
        for rec in kept[:10]:
            print(f"    {rec['symbol']:<6} {rec['atr_pct'] * 100:5.2f}% per day")
        print("  (run with --list to see all of them)")
    elif kept:
        print()
        print(f"  All {len(kept)} names being kept, most active first:")
        cells = [f"{r['symbol']:<6}{r['atr_pct'] * 100:5.2f}%" for r in kept]
        for i in range(0, len(cells), 4):
            print("    " + "   ".join(cells[i:i + 4]))
    print("=" * 62)


def parse_threshold(value):
    """
    Accept either '2.5' (meaning 2.5%) or '0.025' (meaning the same thing).
    Anything above 1 is read as a percentage, which is how people type it.
    """
    if value is None:
        return None
    number = float(value)
    return number / 100.0 if number > 1 else number


def run(dry_run=False, min_override=None, max_override=None, show_all=False):
    min_pct, max_pct, note = load_thresholds()
    if min_override is not None:
        min_pct = min_override
        note = "command line override"
    if max_override is not None:
        max_pct = max_override
        note = "command line override"

    fieldnames, rows = load_universe_csv()
    symbols = [s for s in (symbol_of(r) for r in rows) if s]
    print(f"Loaded {len(symbols)} symbols from {UNIVERSE_FILE}")
    print(f"  columns: {', '.join(fieldnames)}")
    print(f"  first 10: {', '.join(symbols[:10])}")
    if not all(s.isalpha() and 1 <= len(s) <= 5 for s in symbols[:10]):
        print("  WARNING: those do not look like ticker symbols -- the wrong "
              "column may be getting read.")

    bars_by_symbol = fetch_daily_bars(symbols)
    print(f"Got price history for {len(bars_by_symbol)} symbols")

    records, kept_rows = evaluate(rows, bars_by_symbol, min_pct, max_pct)
    print_summary(records, min_pct, max_pct, note, show_all=show_all)

    write_report(REPORT_FILE, records)
    print(f"\nFull detail written to {REPORT_FILE}")

    if dry_run:
        print(f"\nDRY RUN -- {UNIVERSE_FILE} was NOT changed.")
        print(f"Would have kept {len(kept_rows)} of {len(rows)} symbols.")
        return 0

    if len(kept_rows) < MIN_UNIVERSE_SIZE:
        print(f"\nREFUSING TO WRITE: only {len(kept_rows)} symbols would survive, "
              f"which is below the safety floor of {MIN_UNIVERSE_SIZE}.")
        print(f"{UNIVERSE_FILE} left untouched. Lower the minimum threshold "
              f"and try again.")
        return 1

    shutil.copyfile(UNIVERSE_FILE, BACKUP_FILE)
    write_universe_csv(UNIVERSE_FILE, fieldnames, kept_rows)
    print(f"\nOriginal list backed up to {BACKUP_FILE}")
    print(f"{UNIVERSE_FILE} rewritten: {len(rows)} -> {len(kept_rows)} symbols")
    print("\nNOTE: this changes what LOCKBOT will SHOP FOR from here on.")
    print("It does not touch positions you already hold.")
    return 0


def diagnose():
    """
    Isolate the price fetch: three known-good symbols, nothing else moving.
    If this works and the full run does not, the problem is the symbol list.
    If this fails too, the problem is the data connection.
    """
    print("DIAGNOSTIC -- fetching daily bars for 3 known symbols")
    print("-" * 62)

    try:
        fieldnames, rows = load_universe_csv()
        symbols = [s for s in (symbol_of(r) for r in rows) if s]
        print(f"universe.csv columns: {', '.join(fieldnames)}")
        print(f"universe.csv first 10 symbols read: {', '.join(symbols[:10])}")
        print()
    except Exception as exc:
        print(f"could not read universe.csv: {exc}\n")

    test_symbols = ["SPY", "AAPL", "CELH"]
    print(f"requesting: {', '.join(test_symbols)}")
    try:
        bars = fetch_daily_bars(test_symbols, verbose=True)
    except Exception as exc:
        print(f"\nFETCH FAILED: {exc}")
        return 1

    print()
    for symbol in test_symbols:
        series = bars.get(symbol, [])
        if not series:
            print(f"  {symbol:<6} no bars returned")
            continue
        pct = atr_percent(series)
        last = _field(series[-1], "close")
        newest = getattr(series[-1], "timestamp", "?")
        print(f"  {symbol:<6} {len(series)} bars, last close "
              f"{last}, newest {newest}, "
              f"{'no ATR yet' if pct is None else f'{pct * 100:.2f}% per day'}")

    print()
    print("Bars above means the data connection is working. If the full run "
          "still returns nothing, the symbol list is the problem.")
    return 0


def show():
    try:
        fieldnames, rows = load_universe_csv()
    except FileNotFoundError as exc:
        print(exc)
        return 1
    symbols = [s for s in (symbol_of(r) for r in rows) if s]
    print(f"{UNIVERSE_FILE}: {len(symbols)} symbols")
    for i in range(0, len(symbols), 10):
        print("  " + "  ".join(f"{s:<6}" for s in symbols[i:i + 10]))
    return 0


# ---------------------------------------------------------------------------
# Offline self-test -- no network, no files, no Alpaca
# ---------------------------------------------------------------------------

def self_test():
    passed = []
    failed = []

    def check(name, condition):
        (passed if condition else failed).append(name)
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}")

    print("Running offline self-test (no network, no account access)")
    print("-" * 62)

    def make_bars(closes, range_fraction):
        """Build bars where each day's high/low span `range_fraction` of price."""
        bars = []
        for close in closes:
            half = close * range_fraction / 2.0
            bars.append({"high": close + half, "low": close - half, "close": close})
        return bars

    # 1. True range picks up an overnight gap, not just the intraday range.
    gapped = [
        {"high": 100.0, "low": 99.0, "close": 99.5},
        {"high": 110.0, "low": 109.0, "close": 109.5},
    ]
    trs = true_ranges(gapped)
    check("true range accounts for overnight gaps",
          len(trs) == 1 and abs(trs[0] - 10.5) < 1e-9)

    # 2. ATR math on a clean, hand-checkable series.
    flat = [{"high": 51.0, "low": 49.0, "close": 50.0} for _ in range(20)]
    atr = calculate_atr(flat)
    check("ATR of a steady $2 range is $2", atr is not None and abs(atr - 2.0) < 1e-9)
    check("ATR percent of that at $50 is 4%",
          abs(atr_percent(flat) - 0.04) < 1e-9)

    # 3. A bond-ETF-like name gets cut.
    bond = make_bars([49.70 + (i % 3) * 0.02 for i in range(20)], 0.003)
    bond_pct = atr_percent(bond)
    check("bond-ETF-like name measures under 1% per day", bond_pct < 0.01)
    check("bond-ETF-like name is classified TOO_QUIET",
          classify(bond_pct, DEFAULT_MIN_ATR_PERCENT, DEFAULT_MAX_ATR_PERCENT)
          == TOO_QUIET)

    # 4. A normal mover survives.
    mover = make_bars([29.0 + (i % 5) * 0.4 for i in range(20)], 0.03)
    mover_pct = atr_percent(mover)
    check("normal mover is classified KEEP",
          classify(mover_pct, DEFAULT_MIN_ATR_PERCENT, DEFAULT_MAX_ATR_PERCENT)
          == KEEP)

    # 5. A blowup gets cut too.
    wild = make_bars([12.0 + (i % 4) * 3.0 for i in range(20)], 0.40)
    check("extreme mover is classified TOO_WILD",
          classify(atr_percent(wild), DEFAULT_MIN_ATR_PERCENT,
                   DEFAULT_MAX_ATR_PERCENT) == TOO_WILD)

    # 6. Not enough history is NO_DATA, never a silent keep.
    short = make_bars([20.0] * 5, 0.03)
    check("too little history returns no measurement", atr_percent(short) is None)
    check("too little history is classified NO_DATA",
          classify(atr_percent(short), DEFAULT_MIN_ATR_PERCENT,
                   DEFAULT_MAX_ATR_PERCENT) == NO_DATA)
    check("a symbol with no bars at all is NO_DATA",
          classify(atr_percent([]), DEFAULT_MIN_ATR_PERCENT,
                   DEFAULT_MAX_ATR_PERCENT) == NO_DATA)

    # 7. Threshold derivation from the take-profit.
    check("4% target over 3 days suggests ~1.33% minimum",
          abs(suggested_min_atr(0.04) - 0.013333) < 1e-5)

    # 8. Bad input never crashes the math.
    junk = [{"high": None, "low": 1.0, "close": "abc"} for _ in range(20)]
    try:
        result = atr_percent(junk)
        check("malformed bars return None instead of crashing", result is None)
    except Exception as exc:
        check(f"malformed bars return None instead of crashing ({exc})", False)

    # 8b. The v1.0 silent-failure bug: a retry wrapper returning a function
    #     instead of data must raise, not report zero.
    def fake_wrapper():
        return None

    try:
        extract_bars(fake_wrapper)
        check("a function instead of data raises an error", False)
    except RuntimeError:
        check("a function instead of data raises an error", True)

    try:
        extract_bars(None)
        check("an empty response raises an error", False)
    except RuntimeError:
        check("an empty response raises an error", True)

    class FakeBarSet:
        def __init__(self, data):
            self.data = data

    check("reads a BarSet-style response",
          extract_bars(FakeBarSet({"celh": [1, 2]})) == {"CELH": [1, 2]})
    check("reads a plain dict response",
          extract_bars({"nok": [1]}) == {"NOK": [1]})

    # 9. Symbol column detection across naming styles.
    check("finds 'symbol' column", symbol_of({"symbol": "celh"}) == "CELH")
    check("finds 'Ticker' column", symbol_of({"Ticker": "vteb"}) == "VTEB")
    check("falls back to first column",
          symbol_of({"whatever": "nok", "x": "1"}) == "NOK")

    # 10. CSV round-trip keeps columns and order intact.
    tmp_in = "_selftest_universe.csv"
    original_fields = ["symbol", "avg_dollar_volume", "last_price", "shortable"]
    original_rows = [
        {"symbol": "CELH", "avg_dollar_volume": "9000000",
         "last_price": "29.19", "shortable": "True"},
        {"symbol": "VTEB", "avg_dollar_volume": "8000000",
         "last_price": "49.74", "shortable": "True"},
        {"symbol": "NOK", "avg_dollar_volume": "7000000",
         "last_price": "9.27", "shortable": "True"},
    ]
    try:
        write_universe_csv(tmp_in, original_fields, original_rows)
        fields_back, rows_back = load_universe_csv(tmp_in)
        check("rewritten CSV keeps the exact same columns",
              fields_back == original_fields)
        check("rewritten CSV keeps every row in order",
              [symbol_of(r) for r in rows_back] == ["CELH", "VTEB", "NOK"])

        # 11. Filtering drops the quiet name and preserves the others' columns.
        bars_map = {"CELH": mover, "VTEB": bond, "NOK": mover}
        records, kept = evaluate(rows_back, bars_map,
                                 DEFAULT_MIN_ATR_PERCENT, DEFAULT_MAX_ATR_PERCENT)
        kept_symbols = [symbol_of(r) for r in kept]
        check("the quiet bond ETF is filtered out", "VTEB" not in kept_symbols)
        check("the active names are retained", kept_symbols == ["CELH", "NOK"])
        check("kept rows still carry all original fields",
              all(set(r.keys()) == set(original_fields) for r in kept))

        # 12. Safety floor blocks a near-empty universe.
        all_quiet = {"CELH": bond, "VTEB": bond, "NOK": bond}
        _, kept_none = evaluate(rows_back, all_quiet,
                                DEFAULT_MIN_ATR_PERCENT, DEFAULT_MAX_ATR_PERCENT)
        check("safety floor would block writing a near-empty list",
              len(kept_none) < MIN_UNIVERSE_SIZE)
    finally:
        for path in (tmp_in,):
            if os.path.exists(path):
                os.remove(path)

    print("-" * 62)
    print(f"{len(passed)} passed, {len(failed)} failed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    print("All checks passed. Safe to run against the real universe.csv.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Remove stocks that don't move enough to reach LOCKBOT's target."
    )
    parser.add_argument("--self-test", action="store_true",
                        help="offline math check, touches nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be cut without changing universe.csv")
    parser.add_argument("--show", action="store_true",
                        help="print the current universe.csv symbols")
    parser.add_argument("--diagnose", action="store_true",
                        help="test the price fetch on 3 symbols only")
    parser.add_argument("--min", dest="min_pct", default=None,
                        help="minimum daily movement, e.g. 1.25 for 1.25%%")
    parser.add_argument("--max", dest="max_pct", default=None,
                        help="maximum daily movement, e.g. 3 for 3%%")
    parser.add_argument("--list", dest="show_all", action="store_true",
                        help="print every surviving symbol, not just the top 10")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.diagnose:
        return diagnose()
    if args.show:
        return show()
    return run(dry_run=args.dry_run,
               min_override=parse_threshold(args.min_pct),
               max_override=parse_threshold(args.max_pct),
               show_all=args.show_all)


if __name__ == "__main__":
    sys.exit(main())