"""
universe.py — LOCKBOT's morning list builder.

WHAT THIS DOES
    Once per morning, asks Alpaca for every US stock it can trade, throws out
    the ones that are too cheap or too thinly traded, ranks what's left by how
    much money actually changes hands, and saves the top N to universe.csv.

WHAT THIS DOES NOT DO
    It does not look at intraday prices, does not generate signals, does not
    evaluate risk, and does not place a single order. It only writes a list.
    Nothing in LOCKBOT reads that list until market_scanner.py is updated to.

USAGE
    python universe.py                 # build/refresh the list
    python universe.py --dry-run       # build it but don't overwrite universe.csv
    python universe.py --top 500       # keep more names
    python universe.py --self-test     # offline logic check, no network needed
    python universe.py --show          # print the saved list and exit

FOR LATER
    market_scanner.py will call load_universe() to get its symbols.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

# --------------------------------------------------------------------------
# Config (pulled from lockbot_config.py when present, with safe fallbacks so
# this file can never crash the rest of LOCKBOT on a missing constant)
# --------------------------------------------------------------------------

# Load .env the same way health_monitor.py and the other modules do
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:  # pragma: no cover - python-dotenv absent in isolated tests
    pass

try:
    import lockbot_config as config  # type: ignore
except Exception:  # pragma: no cover - config is absent in isolated tests
    config = None


def _cfg(name: str, default):
    if config is not None and hasattr(config, name):
        return getattr(config, name)
    return default


# Filters
MIN_PRICE = _cfg("UNIVERSE_MIN_PRICE", 5.00)
MAX_PRICE = _cfg("UNIVERSE_MAX_PRICE", 2000.00)
LOOKBACK_DAYS = _cfg("UNIVERSE_LOOKBACK_DAYS", 20)
MIN_BARS = _cfg("UNIVERSE_MIN_BARS", 15)          # needs real trading history
TOP_N = _cfg("UNIVERSE_TOP_N", 300)               # how many names to keep
MIN_AVG_DOLLAR_VOLUME = _cfg("UNIVERSE_MIN_AVG_DOLLAR_VOLUME", 0)  # 0 = rank only
ALLOWED_EXCHANGES = set(_cfg("UNIVERSE_ALLOWED_EXCHANGES",
                             ["NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"]))
DATA_FEED = str(_cfg("ALPACA_DATA_FEED", "iex")).lower()

# Plumbing
BATCH_SIZE = _cfg("UNIVERSE_BATCH_SIZE", 200)     # symbols per bars request
BATCH_PAUSE_SECONDS = _cfg("UNIVERSE_BATCH_PAUSE_SECONDS", 0.35)
UNIVERSE_FILE = _cfg("UNIVERSE_FILE",
                     os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "universe.csv"))

# Retries — reuse LOCKBOT's existing helper, tolerating either calling style
try:
    from retry_utils import with_retries as _with_retries  # type: ignore
except Exception:  # pragma: no cover
    _with_retries = None


def retry_call(fn, *args, **kwargs):
    """Call fn(*args, **kwargs) through retry_utils, whatever shape it takes.

    Handles both 'runs the callable now' and 'returns a wrapped callable'
    styles, and falls back to a plain call if retry_utils isn't usable.
    """
    if _with_retries is None:
        return fn(*args, **kwargs)
    try:
        result = _with_retries(lambda: fn(*args, **kwargs))
    except TypeError:
        return fn(*args, **kwargs)
    # A wrapper comes back callable; real Alpaca results never do.
    return result() if callable(result) else result


logger = logging.getLogger("universe")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class UniverseRow:
    symbol: str
    exchange: str
    last_close: float
    avg_dollar_volume: float
    avg_share_volume: float
    bars_used: int
    shortable: bool
    easy_to_borrow: bool


CSV_FIELDS = ["symbol", "exchange", "last_close", "avg_dollar_volume",
              "avg_share_volume", "bars_used", "shortable", "easy_to_borrow"]


# --------------------------------------------------------------------------
# Pure filtering logic (no network — this is what --self-test exercises)
# --------------------------------------------------------------------------

def is_clean_symbol(symbol: str) -> bool:
    """Reject warrants, units, preferred shares, and other non-common tickers."""
    if not symbol or len(symbol) > 5:
        return False
    return symbol.isalpha() and symbol.isupper()


# Leveraged, inverse, and volatility products. These move 2-3x a normal stock
# (or opposite to it), so stop distances calibrated on ordinary names get hit
# constantly. Detected by fund name rather than a symbol list, since new ones
# launch all the time.
_LEVERAGE_PATTERNS = [
    re.compile(r"(?:^|[\s\-(])[1-9](?:\.\d)?X(?:$|[\s\-)])", re.I),  # "3X", "1.5X"
    re.compile(r"\bULTRAPRO\b", re.I),
    re.compile(r"\bULTRASHORT\b", re.I),
    re.compile(r"\bLEVERAGED?\b", re.I),
    re.compile(r"\bINVERSE\b", re.I),
    re.compile(r"\bDAILY\b.*\b(?:BULL|BEAR)\b", re.I),
    re.compile(r"\bULTRA\b.*\b(?:SHORT|VIX|PRO)\b", re.I),
    re.compile(r"\bVIX\b", re.I),                                    # volatility decay
    re.compile(r"\bSHORT\b.*\b(?:FUTURES|INDEX|ETF)\b", re.I),
]

EXCLUDE_SYMBOLS = set(_cfg("UNIVERSE_EXCLUDE_SYMBOLS", []))


def is_leveraged_product(name: str) -> bool:
    """True if the fund name looks like a leveraged, inverse, or vol product."""
    if not name:
        return False
    return any(pattern.search(name) for pattern in _LEVERAGE_PATTERNS)


def asset_passes(tradable: bool, exchange: str, status: str, symbol: str,
                 name: str = "") -> bool:
    if not tradable:
        return False
    if str(status).upper().endswith("INACTIVE"):
        return False
    if exchange not in ALLOWED_EXCHANGES:
        return False
    if symbol in EXCLUDE_SYMBOLS:
        return False
    if is_leveraged_product(name):
        return False
    return is_clean_symbol(symbol)


def summarize_bars(bars: Iterable) -> Optional[Dict[str, float]]:
    """Turn a symbol's daily bars into last close + average dollar volume."""
    closes: List[float] = []
    volumes: List[float] = []
    for bar in bars:
        close = float(getattr(bar, "close", 0) or 0)
        volume = float(getattr(bar, "volume", 0) or 0)
        if close <= 0 or volume <= 0:
            continue
        closes.append(close)
        volumes.append(volume)

    if len(closes) < MIN_BARS:
        return None

    avg_share_volume = sum(volumes) / len(volumes)
    avg_dollar_volume = sum(c * v for c, v in zip(closes, volumes)) / len(closes)
    return {
        "last_close": closes[-1],
        "avg_share_volume": avg_share_volume,
        "avg_dollar_volume": avg_dollar_volume,
        "bars_used": len(closes),
    }


def price_passes(last_close: float) -> bool:
    return MIN_PRICE <= last_close <= MAX_PRICE


def select_universe(rows: List[UniverseRow], top_n: int = TOP_N) -> List[UniverseRow]:
    """Apply the dollar-volume floor (if any) and keep the most liquid top_n."""
    kept = [r for r in rows if r.avg_dollar_volume >= MIN_AVG_DOLLAR_VOLUME]
    kept.sort(key=lambda r: r.avg_dollar_volume, reverse=True)
    return kept[:top_n]


def percentiles(values: List[float]) -> Dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def pct(p: float) -> float:
        idx = min(len(ordered) - 1, max(0, int(round(p / 100.0 * (len(ordered) - 1)))))
        return ordered[idx]

    return {"p10": pct(10), "p50": pct(50), "p90": pct(90),
            "min": ordered[0], "max": ordered[-1]}


# --------------------------------------------------------------------------
# Alpaca access (imported lazily so --self-test works with no SDK installed)
# --------------------------------------------------------------------------

def _credentials():
    key = (_cfg("ALPACA_API_KEY", None) or _cfg("APCA_API_KEY_ID", None)
           or os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID"))
    secret = (_cfg("ALPACA_SECRET_KEY", None) or _cfg("APCA_API_SECRET_KEY", None)
              or os.environ.get("ALPACA_SECRET_KEY")
              or os.environ.get("APCA_API_SECRET_KEY"))
    if not key or not secret:
        raise RuntimeError(
            "Alpaca credentials not found. Expected ALPACA_API_KEY / "
            "ALPACA_SECRET_KEY in lockbot_config.py or the environment."
        )
    return key, secret


def fetch_tradable_assets() -> List[UniverseRow]:
    """Ask Alpaca for every active, tradable US equity and keep the clean ones."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.trading.enums import AssetClass, AssetStatus

    key, secret = _credentials()
    client = TradingClient(key, secret, paper=True)
    request = GetAssetsRequest(status=AssetStatus.ACTIVE,
                               asset_class=AssetClass.US_EQUITY)

    assets = retry_call(client.get_all_assets, request)
    logger.info("Alpaca returned %d active US equities", len(assets))

    rows: List[UniverseRow] = []
    leveraged_dropped: List[str] = []
    for asset in assets:
        symbol = getattr(asset, "symbol", "") or ""
        name = getattr(asset, "name", "") or ""
        exchange = str(getattr(asset, "exchange", "") or "").replace("AssetExchange.", "")
        if is_leveraged_product(name) and is_clean_symbol(symbol):
            leveraged_dropped.append(symbol)
        if not asset_passes(bool(getattr(asset, "tradable", False)),
                            exchange,
                            str(getattr(asset, "status", "")),
                            symbol,
                            name):
            continue
        rows.append(UniverseRow(
            symbol=symbol,
            exchange=exchange,
            last_close=0.0,
            avg_dollar_volume=0.0,
            avg_share_volume=0.0,
            bars_used=0,
            shortable=bool(getattr(asset, "shortable", False)),
            easy_to_borrow=bool(getattr(asset, "easy_to_borrow", False)),
        ))

    logger.info("Excluded %d leveraged/inverse/volatility products (e.g. %s)",
                len(leveraged_dropped), ", ".join(sorted(leveraged_dropped)[:8]))
    logger.info("%d symbols passed the tradability/name filters", len(rows))
    return rows


def fetch_daily_bars(symbols: List[str]) -> Dict[str, List]:
    """Daily bars for a batch of symbols, in chunks, respecting rate limits."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret = _credentials()
    client = StockHistoricalDataClient(key, secret)

    end = datetime.now(timezone.utc) - timedelta(minutes=20)  # avoid the SIP delay window
    start = end - timedelta(days=LOOKBACK_DAYS * 2 + 10)      # calendar days -> trading days

    out: Dict[str, List] = {}
    total_batches = (len(symbols) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(symbols), BATCH_SIZE):
        chunk = symbols[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        logger.info("Fetching daily bars, batch %d/%d (%d symbols)",
                    batch_num, total_batches, len(chunk))

        kwargs = dict(symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                      start=start, end=end)
        try:
            from alpaca.data.enums import DataFeed
            kwargs["feed"] = DataFeed.SIP if DATA_FEED == "sip" else DataFeed.IEX
        except Exception:
            pass  # older SDKs default the feed themselves

        try:
            bar_set = retry_call(client.get_stock_bars, StockBarsRequest(**kwargs))
        except Exception as exc:
            logger.error("Batch %d failed, skipping it: %s", batch_num, exc)
            continue

        data = getattr(bar_set, "data", {}) or {}
        for symbol, bars in data.items():
            out[symbol] = list(bars)[-LOOKBACK_DAYS:]

        time.sleep(BATCH_PAUSE_SECONDS)

    logger.info("Got bar history for %d symbols", len(out))
    return out


# --------------------------------------------------------------------------
# Build / save / load
# --------------------------------------------------------------------------

def build_universe(top_n: int = TOP_N) -> List[UniverseRow]:
    candidates = fetch_tradable_assets()
    bars_by_symbol = fetch_daily_bars([r.symbol for r in candidates])

    priced: List[UniverseRow] = []
    for row in candidates:
        summary = summarize_bars(bars_by_symbol.get(row.symbol, []))
        if summary is None:
            continue
        if not price_passes(summary["last_close"]):
            continue
        row.last_close = round(summary["last_close"], 2)
        row.avg_share_volume = round(summary["avg_share_volume"], 0)
        row.avg_dollar_volume = round(summary["avg_dollar_volume"], 0)
        row.bars_used = int(summary["bars_used"])
        priced.append(row)

    logger.info("%d symbols had usable history and passed the price filter",
                len(priced))

    if priced:
        stats = percentiles([r.avg_dollar_volume for r in priced])
        logger.info(
            "Avg daily dollar volume across candidates (feed=%s) — "
            "min $%.0f | p10 $%.0f | median $%.0f | p90 $%.0f | max $%.0f",
            DATA_FEED, stats["min"], stats["p10"], stats["p50"],
            stats["p90"], stats["max"])

    selected = select_universe(priced, top_n=top_n)
    logger.info("Keeping the %d most liquid names", len(selected))
    return selected


def save_universe(rows: List[UniverseRow], path: str = UNIVERSE_FILE) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    logger.info("Wrote %d symbols to %s", len(rows), path)


def load_universe(path: str = UNIVERSE_FILE,
                  require_shortable: bool = False) -> List[str]:
    """Called by market_scanner.py later. Returns just the symbols."""
    if not os.path.exists(path):
        logger.warning("No universe file at %s — run universe.py first", path)
        return []
    symbols: List[str] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if require_shortable and row.get("shortable", "").lower() != "true":
                continue
            symbols.append(row["symbol"])
    return symbols


def universe_age_hours(path: str = UNIVERSE_FILE) -> Optional[float]:
    if not os.path.exists(path):
        return None
    mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime).total_seconds() / 3600.0


def needs_refresh(path: str = UNIVERSE_FILE, max_age_hours: float = 20.0) -> bool:
    age = universe_age_hours(path)
    return age is None or age > max_age_hours


# --------------------------------------------------------------------------
# Self-test (offline — verifies the filtering logic with fake data)
# --------------------------------------------------------------------------

class _FakeBar:
    def __init__(self, close, volume):
        self.close = close
        self.volume = volume


def _self_test() -> int:
    failures = []

    def check(label, condition):
        if not condition:
            failures.append(label)
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")

    print("Symbol cleanliness:")
    check("plain ticker accepted", is_clean_symbol("AAPL"))
    check("warrant rejected", not is_clean_symbol("ABC.WS"))
    check("6-letter rejected", not is_clean_symbol("ABCDEF"))
    check("empty rejected", not is_clean_symbol(""))
    check("lowercase rejected", not is_clean_symbol("aapl"))

    print("Asset filters:")
    check("non-tradable rejected", not asset_passes(False, "NASDAQ", "ACTIVE", "AAPL"))
    check("OTC rejected", not asset_passes(True, "OTC", "ACTIVE", "AAPL"))
    check("good asset accepted", asset_passes(True, "NASDAQ", "ACTIVE", "AAPL"))

    print("Leveraged / inverse detection:")
    should_drop = [
        "Direxion Daily Semiconductor Bull 3X Shares",
        "Direxion Daily Small Cap Bear 3X Shares",
        "ProShares UltraPro QQQ",
        "ProShares UltraPro Short QQQ",
        "ProShares UltraShort S&P500",
        "ProShares Ultra VIX Short-Term Futures ETF",
        "iPath Series B S&P 500 VIX Short-Term Futures ETN",
        "Leveraged Gold Miners ETF",
        "MicroSectors FANG+ Index 3X Leveraged ETN",
        "GraniteShares 2x Long NVDA Daily ETF",
    ]
    should_keep = [
        "Apple Inc. Common Stock",
        "SPDR S&P 500 ETF Trust",
        "Invesco QQQ Trust, Series 1",
        "Build-A-Bear Workshop, Inc.",
        "Ultra Clean Holdings, Inc.",
        "Bear Creek Mining Corporation",
        "iShares Russell 2000 ETF",
        "Vanguard Total Stock Market ETF",
        "Bullfrog AI Holdings, Inc.",
        "Daily Journal Corporation",
    ]
    for fund in should_drop:
        check(f"drops: {fund[:42]}", is_leveraged_product(fund))
    for fund in should_keep:
        check(f"keeps: {fund[:42]}", not is_leveraged_product(fund))
    check("leveraged name blocked at asset filter",
          not asset_passes(True, "NASDAQ", "ACTIVE", "TQQQ", "ProShares UltraPro QQQ"))
    check("ordinary name allowed at asset filter",
          asset_passes(True, "NASDAQ", "ACTIVE", "AAPL", "Apple Inc. Common Stock"))
    check("missing name doesn't crash filter",
          asset_passes(True, "NASDAQ", "ACTIVE", "AAPL", ""))

    print("Bar summarizing:")
    enough = [_FakeBar(100.0, 1_000_000) for _ in range(MIN_BARS)]
    summary = summarize_bars(enough)
    check("enough bars summarized", summary is not None)
    check("dollar volume math correct",
          summary and abs(summary["avg_dollar_volume"] - 100_000_000) < 1)
    check("too few bars rejected", summarize_bars(enough[:MIN_BARS - 1]) is None)
    check("zero-volume bars ignored",
          summarize_bars([_FakeBar(100.0, 0)] * (MIN_BARS + 5)) is None)

    print("Price filter:")
    check("penny stock rejected", not price_passes(1.50))
    check("normal price accepted", price_passes(50.0))
    check("absurd price rejected", not price_passes(MAX_PRICE + 1))

    print("Selection and ranking:")
    fake_rows = [
        UniverseRow(f"SYM{i}", "NASDAQ", 50.0, float(i) * 1_000_000,
                    1000.0, MIN_BARS, True, True)
        for i in range(1, 11)
    ]
    top3 = select_universe(fake_rows, top_n=3)
    check("keeps requested count", len(top3) == 3)
    check("ranked most liquid first", top3[0].symbol == "SYM10")
    check("least liquid excluded", "SYM1" not in [r.symbol for r in top3])

    print("Save / load round trip:")
    tmp = os.path.join("/tmp", "universe_selftest.csv")
    save_universe(top3, tmp)
    loaded = load_universe(tmp)
    check("round trip preserved order", loaded == ["SYM10", "SYM9", "SYM8"])
    mixed = list(top3)
    mixed[0].shortable = False
    save_universe(mixed, tmp)
    check("shortable filter works",
          load_universe(tmp, require_shortable=True) == ["SYM9", "SYM8"])
    check("missing file returns empty", load_universe("/tmp/does_not_exist.csv") == [])
    os.remove(tmp)

    print("Freshness:")
    check("missing file needs refresh", needs_refresh("/tmp/does_not_exist.csv"))

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {failures}")
        return 1
    print("All self-tests passed.")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Build LOCKBOT's tradable universe")
    parser.add_argument("--top", type=int, default=TOP_N,
                        help=f"how many symbols to keep (default {TOP_N})")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and print, but don't write universe.csv")
    parser.add_argument("--self-test", action="store_true",
                        help="offline logic check, no network or credentials needed")
    parser.add_argument("--show", action="store_true",
                        help="print the saved universe and exit")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.show:
        symbols = load_universe()
        age = universe_age_hours()
        print(f"{len(symbols)} symbols"
              + (f", last built {age:.1f}h ago" if age is not None else ""))
        for i in range(0, len(symbols), 12):
            print("  " + " ".join(symbols[i:i + 12]))
        return 0

    try:
        rows = build_universe(top_n=args.top)
    except Exception as exc:
        logger.error("Universe build failed: %s", exc)
        return 1

    if not rows:
        logger.error("Universe came back empty — not overwriting the existing file")
        return 1

    print(f"\nTop 20 by average daily dollar volume (feed={DATA_FEED}):")
    print(f"  {'SYMBOL':<8}{'PRICE':>10}{'AVG $ VOL':>18}  SHORTABLE")
    for row in rows[:20]:
        print(f"  {row.symbol:<8}{row.last_close:>10.2f}"
              f"{row.avg_dollar_volume:>18,.0f}  {row.shortable}")
    shortable_count = sum(1 for r in rows if r.shortable)
    print(f"\n{len(rows)} symbols selected, {shortable_count} of them shortable.")

    if args.dry_run:
        print("Dry run — universe.csv not written.")
        return 0

    save_universe(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())