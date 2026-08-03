"""
options_probe.py  -  v1.1

READ-ONLY. Answers ONE question before any options code gets written:

    Can a $250-$350 account actually trade options on LOCKBOT's universe,
    and would the buy/sell spread eat the whole profit?

WHAT IT DOES
  1. Checks whether the Alpaca paper account is approved for options, and at
     what level.
  2. For each symbol in universe.csv, finds the nearest at-the-money contract
     in a chosen expiration window.
  3. Pulls the live bid/ask for those contracts.
  4. Reports, per symbol: what one contract costs, how wide the spread is,
     roughly how much the option would gain if the stock hit its adaptive
     bracket target, and whether the spread eats that gain.
  5. Gives a verdict per symbol and a summary across the whole universe.

WHAT IT DOES NOT DO
  - Places no orders of any kind.
  - Writes to no existing LOCKBOT file.
  - Touches no config, no universe.csv, no state files.
  It only reads. The only file it can create is an optional CSV report,
  and only when --save is passed.

FLAGS
  python options_probe.py --self-test          offline checks, no network
  python options_probe.py                      probe the real account
  python options_probe.py --symbols T,VZ,F     probe just a few names
  python options_probe.py --liquid             probe heavily traded big names
  python options_probe.py --equity 350         use a different account size
  python options_probe.py --puts               price puts instead of calls
  python options_probe.py --days 21 45         expiration window in days
  python options_probe.py --show-all           list every symbol, not a sample
  python options_probe.py --save               write options_probe_report.csv
"""

import argparse
import csv
import os
import statistics
import sys
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Optional imports. Everything below must be importable for --self-test to run
# on a machine with no alpaca-py and no dotenv, so nothing here is required at
# import time.
# ---------------------------------------------------------------------------

try:
    from dotenv import load_dotenv
    HAVE_DOTENV = True
except Exception:
    HAVE_DOTENV = False

ALPACA_IMPORT_ERROR = None
HAVE_ALPACA_OPTIONS = False

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOptionContractsRequest
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionLatestQuoteRequest
    HAVE_ALPACA_OPTIONS = True
except Exception as exc:  # pragma: no cover - depends on installed version
    ALPACA_IMPORT_ERROR = str(exc)

# Stock price lookup. Needed because the at-the-money picker cannot choose a
# strike without knowing what the stock costs. universe.csv supplies this for
# universe names; anything typed in by hand (--symbols, --liquid) has to be
# looked up. Optional so --self-test still runs with no alpaca-py installed.
HAVE_STOCK_DATA = False
STOCK_IMPORT_ERROR = None
try:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest
    HAVE_STOCK_DATA = True
except Exception as exc:  # pragma: no cover - depends on installed version
    STOCK_IMPORT_ERROR = str(exc)

# ContractType / AssetStatus moved around between alpaca-py versions, so they
# are looked up separately and treated as optional.
CONTRACT_TYPE = None
try:
    from alpaca.trading.enums import ContractType
    CONTRACT_TYPE = ContractType
except Exception:
    pass

# ---------------------------------------------------------------------------
# Defaults. Overridden by lockbot_config.py when the value exists there.
# ---------------------------------------------------------------------------

DEFAULT_EQUITY = 250.0
DEFAULT_POSITION_CAP_PERCENT = 0.40      # MAX_POSITION_VALUE_PERCENT
DEFAULT_TARGET_PERCENT = 0.04            # TAKE_PROFIT_PERCENT (fixed-bracket fallback)
DEFAULT_ATR_STOP_MULTIPLIER = 1.5
DEFAULT_ATR_REWARD_RATIO = 2.0
DEFAULT_ATR_MIN_STOP = 0.015
DEFAULT_ATR_MAX_STOP = 0.060

DEFAULT_DAYS_MIN = 21
DEFAULT_DAYS_MAX = 45

# The most heavily traded options in the US market. These are NOT candidates
# for the $250 account - most are far too expensive. The point of running the
# probe against them is to find out what spreads look like when the options are
# actually liquid, and therefore what account size an options layer would need.
LIQUID_NAMES = [
    "SPY", "QQQ", "IWM", "DIA",              # index ETFs
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",  # mega caps
    "AMD", "INTC", "MU", "PLTR", "SOFI",     # active mid-price names
    "GLD", "SLV", "TLT", "XLF", "EEM",       # liquid sector/commodity ETFs
]

# An at-the-money option moves roughly 50 cents for every dollar the stock
# moves. Used only to estimate the option's gain at the stock's target. This is
# an approximation, not a pricing model, and it is stated as such in the report.
ATM_DELTA = 0.50

CONTRACT_MULTIPLIER = 100                # one contract covers 100 shares

WIDE_SPREAD_PERCENT = 10.0               # spread wider than this is a warning
UNIVERSE_FILE = "universe.csv"
MOVEMENT_FILE = "universe_volatility_report.csv"
REPORT_FILE = "options_probe_report.csv"

OK = "OK"
WIDE = "WIDE_SPREAD"
SPREAD_KILLS = "SPREAD_EATS_PROFIT"
TOO_EXPENSIVE = "TOO_EXPENSIVE"
ZERO_BID = "CANNOT_SELL"
NO_QUOTE = "NO_QUOTE"
NO_OPTIONS = "NO_OPTIONS"


# ---------------------------------------------------------------------------
# Config loading (same pattern as adaptive_brackets.py)
# ---------------------------------------------------------------------------

def _load_config_module():
    try:
        import lockbot_config
        return lockbot_config
    except Exception:
        return None


def config_value(name, default):
    """Read a value from lockbot_config.py if it exists there, else default."""
    module = _load_config_module()
    if module is None:
        return default
    value = getattr(module, name, None)
    if value is None:
        return default
    return value


# ---------------------------------------------------------------------------
# Pure helpers (all covered by --self-test)
# ---------------------------------------------------------------------------

def spread_percent(bid, ask):
    """Buy/sell gap as a percent of the midpoint. None if unusable."""
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return ((ask - bid) / mid) * 100.0


def contract_cost(ask):
    """Dollar cost of buying one contract at the ask."""
    if ask is None or ask <= 0:
        return None
    return ask * CONTRACT_MULTIPLIER


def adaptive_target_percent(daily_move_percent, settings=None):
    """
    Stock target the adaptive brackets would use, as a percent.
    Mirrors adaptive_brackets.py: stop = multiplier x daily move, clamped,
    then target = reward ratio x stop.
    """
    settings = settings or {}
    multiplier = settings.get("stop_multiplier", DEFAULT_ATR_STOP_MULTIPLIER)
    ratio = settings.get("reward_ratio", DEFAULT_ATR_REWARD_RATIO)
    floor = settings.get("min_stop", DEFAULT_ATR_MIN_STOP) * 100.0
    ceiling = settings.get("max_stop", DEFAULT_ATR_MAX_STOP) * 100.0

    if daily_move_percent is None or daily_move_percent <= 0:
        return DEFAULT_TARGET_PERCENT * 100.0

    stop = daily_move_percent * multiplier
    stop = max(floor, min(ceiling, stop))
    return stop * ratio


def estimated_option_gain_percent(stock_price, option_mid, stock_target_percent,
                                  delta=ATM_DELTA):
    """
    Rough estimate of how much the option gains, in percent of premium, if the
    stock reaches its target. Approximation only: assumes a constant delta and
    ignores time decay, so real results will be worse.
    """
    if not stock_price or not option_mid or option_mid <= 0:
        return None
    if stock_target_percent is None or stock_target_percent <= 0:
        return None
    dollar_move = stock_price * (stock_target_percent / 100.0)
    option_dollar_gain = dollar_move * delta
    return (option_dollar_gain / option_mid) * 100.0


def classify(cost, budget, spread_pct, estimated_gain_pct, bid):
    """Turn the numbers into one verdict word."""
    if bid is not None and bid <= 0:
        return ZERO_BID
    if spread_pct is None or cost is None:
        return NO_QUOTE
    if budget is not None and cost > budget:
        return TOO_EXPENSIVE
    if estimated_gain_pct is not None and spread_pct >= estimated_gain_pct:
        return SPREAD_KILLS
    if spread_pct > WIDE_SPREAD_PERCENT:
        return WIDE
    return OK


def pick_atm_contract(contracts, stock_price):
    """
    Pick the contract whose strike is closest to the current stock price.
    Ties break toward the earlier expiration, then the lower strike, so the
    choice is stable rather than dependent on list order.
    """
    usable = []
    for contract in contracts:
        strike = getattr(contract, "strike_price", None)
        if strike is None and isinstance(contract, dict):
            strike = contract.get("strike_price")
        if strike is None:
            continue
        try:
            strike = float(strike)
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        usable.append((strike, contract))

    if not usable or not stock_price:
        return None

    def sort_key(item):
        strike, contract = item
        expiry = getattr(contract, "expiration_date", None)
        if expiry is None and isinstance(contract, dict):
            expiry = contract.get("expiration_date")
        return (abs(strike - stock_price), str(expiry), strike)

    usable.sort(key=sort_key)
    return usable[0][1]


def read_universe(path=UNIVERSE_FILE):
    """Return [(symbol, last_close)] from universe.csv."""
    rows = []
    with open(path, "r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            try:
                last_close = float(row.get("last_close") or 0) or None
            except (TypeError, ValueError):
                last_close = None
            rows.append((symbol, last_close))
    return rows


def read_movement_table(path=MOVEMENT_FILE):
    """
    Return {symbol: daily_move_percent} from universe_volatility_report.csv.
    Missing file is not an error; the fixed 4% target is used instead.
    """
    table = {}
    if not os.path.exists(path):
        return table
    try:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                symbol = (row.get("symbol") or "").strip().upper()
                if not symbol:
                    continue
                raw = (row.get("atr_percent")
                       or row.get("daily_move_percent")
                       or row.get("atr_pct"))
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if value <= 0:
                    continue
                # Accept either 2.87 or 0.0287 for the same thing.
                if value < 1.0:
                    value *= 100.0
                table[symbol] = value
    except Exception:
        return {}
    return table


# ---------------------------------------------------------------------------
# Network work. Every call is wrapped so a failure explains itself.
# ---------------------------------------------------------------------------

def build_clients():
    if not HAVE_ALPACA_OPTIONS:
        raise RuntimeError(
            "This alpaca-py install does not expose the options endpoints.\n"
            "  Import error: %s\n"
            "  Fix: pip install --upgrade alpaca-py" % ALPACA_IMPORT_ERROR
        )
    if HAVE_DOTENV:
        load_dotenv()

    key = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "No API keys found. Expected ALPACA_API_KEY / ALPACA_SECRET_KEY "
            "in .env (same names the other LOCKBOT modules use)."
        )

    trading = TradingClient(key, secret, paper=True)
    data = OptionHistoricalDataClient(key, secret)

    stock = None
    if HAVE_STOCK_DATA:
        try:
            stock = StockHistoricalDataClient(key, secret)
        except Exception as exc:  # pragma: no cover
            print("  WARNING: could not build the stock price client (%s)." % exc)
    else:
        print("  WARNING: this alpaca-py install has no stock data client (%s)."
              % STOCK_IMPORT_ERROR)
        print("           Symbols not listed in universe.csv cannot be priced.")

    return trading, data, stock


def merge_prices(pairs, looked_up):
    """Combine (symbol, price) pairs with a looked-up price dict.

    universe.csv prices win when present; looked-up prices fill the gaps.
    Pure function so --self-test covers it without any network.
    """
    out = {}
    for symbol, price in pairs:
        if price:
            out[symbol] = price
    for symbol, price in (looked_up or {}).items():
        if price and symbol not in out:
            out[symbol] = price
    return out


def fetch_stock_prices(stock_client, symbols):
    """Latest traded price per symbol. Returns {} rather than raising, so a
    price-lookup problem degrades the report instead of killing the run."""
    if stock_client is None or not symbols:
        return {}

    prices = {}
    batch_size = 100
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start:start + batch_size]
        try:
            request = StockLatestTradeRequest(symbol_or_symbols=batch)
            response = stock_client.get_stock_latest_trade(request)
        except Exception as exc:
            print("  WARNING: stock price lookup failed (%s)." % exc)
            continue

        if callable(response):
            print("  WARNING: price lookup returned a function, not data. "
                  "Something wrapped the call without running it.")
            continue

        items = response
        if hasattr(response, "data") and isinstance(response.data, dict):
            items = response.data
        if not isinstance(items, dict):
            print("  WARNING: unexpected price response shape %s." % type(items))
            continue

        for symbol, trade in items.items():
            price = getattr(trade, "price", None)
            if price is None and isinstance(trade, dict):
                price = trade.get("price")
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price > 0:
                prices[str(symbol).upper()] = price

    return prices


def describe_account_options(trading):
    """Read the account's options permission level. Read-only."""
    account = trading.get_account()
    level = None
    for attribute in ("options_trading_level", "options_approved_level"):
        value = getattr(account, attribute, None)
        if value is not None:
            level = value
            break
    try:
        level = int(level) if level is not None else None
    except (TypeError, ValueError):
        pass

    buying_power = getattr(account, "options_buying_power", None)
    try:
        equity = float(getattr(account, "equity", 0) or 0)
    except (TypeError, ValueError):
        equity = 0.0

    return {
        "level": level,
        "options_buying_power": buying_power,
        "equity": equity,
    }


def fetch_contracts(trading, symbols, days_min, days_max, want_puts=False):
    """
    Get active contracts for these underlyings in the expiration window.
    Returns {underlying: [contract, ...]}.
    """
    start = date.today() + timedelta(days=days_min)
    end = date.today() + timedelta(days=days_max)

    kwargs = {
        "underlying_symbols": list(symbols),
        "expiration_date_gte": start,
        "expiration_date_lte": end,
        "limit": 10000,
    }
    if CONTRACT_TYPE is not None:
        kwargs["type"] = CONTRACT_TYPE.PUT if want_puts else CONTRACT_TYPE.CALL

    grouped = {}
    page_token = None
    pages = 0

    while True:
        if page_token:
            kwargs["page_token"] = page_token
        request = GetOptionContractsRequest(**kwargs)
        response = trading.get_option_contracts(request)

        contracts = getattr(response, "option_contracts", None)
        if contracts is None and isinstance(response, dict):
            contracts = response.get("option_contracts")
        contracts = contracts or []

        for contract in contracts:
            underlying = getattr(contract, "underlying_symbol", None)
            if underlying is None and isinstance(contract, dict):
                underlying = contract.get("underlying_symbol")
            if not underlying:
                continue
            grouped.setdefault(str(underlying).upper(), []).append(contract)

        page_token = getattr(response, "next_page_token", None)
        if page_token is None and isinstance(response, dict):
            page_token = response.get("next_page_token")

        pages += 1
        if not page_token or pages >= 20:
            break

    return grouped


def fetch_quotes(data_client, contract_symbols):
    """Latest bid/ask for a list of contract symbols. Returns {symbol: (bid, ask)}."""
    out = {}
    batch_size = 100
    for index in range(0, len(contract_symbols), batch_size):
        batch = contract_symbols[index:index + batch_size]
        try:
            request = OptionLatestQuoteRequest(symbol_or_symbols=batch)
            response = data_client.get_option_latest_quote(request)
        except Exception as exc:
            print("  ! quote request failed for %d contracts: %s"
                  % (len(batch), exc))
            continue

        items = response.items() if hasattr(response, "items") else []
        for symbol, quote in items:
            bid = getattr(quote, "bid_price", None)
            ask = getattr(quote, "ask_price", None)
            try:
                bid = float(bid) if bid is not None else None
                ask = float(ask) if ask is not None else None
            except (TypeError, ValueError):
                bid, ask = None, None
            out[symbol] = (bid, ask)
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def money(value):
    return "-" if value is None else "$%.2f" % value


def pct(value):
    return "-" if value is None else "%.1f%%" % value


def print_report(rows, budget, equity, show_all, want_puts):
    kind = "PUT" if want_puts else "CALL"
    print("")
    print("=" * 78)
    print("OPTIONS VIABILITY PROBE  -  %s contracts" % kind)
    print("=" * 78)
    print("Account size used      : %s" % money(equity))
    print("Per-position budget    : %s  (40%% cap, same as the stock side)" % money(budget))
    print("")

    verdicts = {}
    for row in rows:
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1

    header = ("%-7s %9s %9s %8s %9s %9s  %s"
              % ("SYMBOL", "STOCK", "CONTRACT", "SPREAD", "STK TGT", "OPT GAIN", "VERDICT"))
    print(header)
    print("-" * 78)

    shown = rows if show_all else rows[:15]
    for row in shown:
        print("%-7s %9s %9s %8s %9s %9s  %s" % (
            row["symbol"],
            money(row["stock_price"]),
            money(row["cost"]),
            pct(row["spread_pct"]),
            pct(row["stock_target_pct"]),
            pct(row["est_gain_pct"]),
            row["verdict"],
        ))
    if not show_all and len(rows) > len(shown):
        print("... %d more (use --show-all to list every symbol)"
              % (len(rows) - len(shown)))

    print("")
    print("VERDICT COUNTS")
    for name in (OK, WIDE, SPREAD_KILLS, TOO_EXPENSIVE, ZERO_BID, NO_QUOTE, NO_OPTIONS):
        if verdicts.get(name):
            print("  %-20s %d" % (name, verdicts[name]))

    priced = [r for r in rows if r["cost"] is not None]
    affordable = [r for r in priced if r["cost"] <= budget] if budget else []
    spreads = [r["spread_pct"] for r in rows if r["spread_pct"] is not None]

    print("")
    print("THE NUMBERS THAT DECIDE THIS")
    if priced:
        costs = sorted(r["cost"] for r in priced)
        print("  Cheapest contract    : %s" % money(costs[0]))
        print("  Median contract      : %s" % money(statistics.median(costs)))
        print("  Most expensive       : %s" % money(costs[-1]))
        print("  Fit the %s budget : %d of %d priced"
              % (money(budget).rjust(7), len(affordable), len(priced)))
    else:
        print("  No contracts could be priced.")
    if spreads:
        print("  Median spread        : %s of the premium"
              % pct(statistics.median(spreads)))
        print("  Widest spread        : %s" % pct(max(spreads)))

    usable = [r for r in rows if r["verdict"] == OK]
    print("")
    print("BOTTOM LINE: %d of %d symbols look usable for options right now."
          % (len(usable), len(rows)))
    print("")
    print("Caveats, so these numbers are not read as more than they are:")
    print("  - Option gain is a rough estimate using a constant 0.50 delta and")
    print("    ignoring time decay. Real outcomes will be worse, not better.")
    print("  - Quotes on a free data plan may be indicative rather than live")
    print("    exchange quotes, so real spreads can be different.")
    print("  - Spread shown is the full round trip: buy at ask, sell at bid.")
    print("  - No orders were placed. This script only reads.")
    print("")


def save_report(rows, path=REPORT_FILE):
    fields = ["symbol", "stock_price", "contract_symbol", "strike", "expiration",
              "bid", "ask", "mid", "cost", "spread_pct", "daily_move_pct",
              "stock_target_pct", "est_gain_pct", "verdict"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fields})
    print("Detail written to %s" % path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(symbols=None, equity=None, want_puts=False, days_min=DEFAULT_DAYS_MIN,
        days_max=DEFAULT_DAYS_MAX, show_all=False, save=False, liquid=False):
    trading, data_client, stock_client = build_clients()

    print("Checking account options permission ...")
    info = describe_account_options(trading)
    level = info["level"]
    print("  Options trading level : %s" % (level if level is not None else "unknown"))
    print("  Options buying power  : %s" % (info["options_buying_power"] or "-"))
    print("  Account equity        : %s" % money(info["equity"]))

    if level in (0, None):
        print("")
        print("  >> This account is not approved to trade options (level 0 or")
        print("     unreported). Everything below is still useful as pricing")
        print("     research, but no options order would go through until the")
        print("     paper account has options enabled in the Alpaca dashboard.")

    if equity is None:
        equity = info["equity"] or float(config_value("STARTING_EQUITY", DEFAULT_EQUITY))

    cap = float(config_value("MAX_POSITION_VALUE_PERCENT", DEFAULT_POSITION_CAP_PERCENT))
    budget = equity * cap

    bracket_settings = {
        "stop_multiplier": float(config_value("ATR_STOP_MULTIPLIER", DEFAULT_ATR_STOP_MULTIPLIER)),
        "reward_ratio": float(config_value("ATR_REWARD_RATIO", DEFAULT_ATR_REWARD_RATIO)),
        "min_stop": float(config_value("ATR_MIN_STOP_PERCENT", DEFAULT_ATR_MIN_STOP)),
        "max_stop": float(config_value("ATR_MAX_STOP_PERCENT", DEFAULT_ATR_MAX_STOP)),
    }

    if liquid:
        universe = [(s, None) for s in LIQUID_NAMES]
        print("")
        print("Using the built-in liquid-names list (%d symbols)." % len(universe))
        print("These are reference names, not candidates for this account size.")
    elif symbols:
        universe = [(s.strip().upper(), None) for s in symbols if s.strip()]
        print("")
        print("Using %d symbol(s) from the command line." % len(universe))
    else:
        universe = read_universe()
        print("")
        print("Loaded %d symbols from %s" % (len(universe), UNIVERSE_FILE))

    movement = read_movement_table()
    if movement:
        print("Loaded daily movement for %d symbols from %s"
              % (len(movement), MOVEMENT_FILE))
    else:
        print("No movement file found - falling back to a flat 4%% stock target.")

    symbol_list = [s for s, _ in universe]

    # BUG FIX (v1.1): the at-the-money picker needs a stock price. universe.csv
    # supplies one; --symbols and --liquid do not. Without this lookup those
    # runs found contracts and then priced zero of them, reporting NO_OPTIONS
    # for names that clearly have options.
    missing = [s for s, p in universe if not p]
    looked_up = {}
    if missing:
        print("Looking up the current price for %d symbol(s) ..." % len(missing))
        looked_up = fetch_stock_prices(stock_client, missing)
        print("  Got prices for %d of %d." % (len(looked_up), len(missing)))
        still_missing = [s for s in missing if s not in looked_up]
        if still_missing:
            print("  No price for: %s" % ", ".join(still_missing[:10]))
            print("  Those will report NO_OPTIONS because no strike can be chosen.")

    price_by_symbol = merge_prices(universe, looked_up)

    print("")
    print("Fetching contracts expiring %d-%d days out ..." % (days_min, days_max))
    grouped = fetch_contracts(trading, symbol_list, days_min, days_max, want_puts)
    print("  Found contracts for %d of %d symbols." % (len(grouped), len(symbol_list)))

    chosen = {}
    for symbol in symbol_list:
        stock_price = price_by_symbol.get(symbol)
        contracts = grouped.get(symbol, [])
        if not contracts or not stock_price:
            chosen[symbol] = None
            continue
        chosen[symbol] = pick_atm_contract(contracts, stock_price)

    contract_symbols = []
    for symbol, contract in chosen.items():
        if contract is None:
            continue
        name = getattr(contract, "symbol", None)
        if name is None and isinstance(contract, dict):
            name = contract.get("symbol")
        if name:
            contract_symbols.append(str(name))

    print("Fetching quotes for %d at-the-money contracts ..." % len(contract_symbols))
    quotes = fetch_quotes(data_client, contract_symbols)
    print("  Got quotes for %d." % len(quotes))

    rows = []
    for symbol, _ in universe:
        contract = chosen.get(symbol)
        stock_price = price_by_symbol.get(symbol)
        daily_move = movement.get(symbol)
        stock_target = adaptive_target_percent(daily_move, bracket_settings)

        if contract is None:
            rows.append({
                "symbol": symbol, "stock_price": stock_price,
                "contract_symbol": None, "strike": None, "expiration": None,
                "bid": None, "ask": None, "mid": None, "cost": None,
                "spread_pct": None, "daily_move_pct": daily_move,
                "stock_target_pct": stock_target, "est_gain_pct": None,
                "verdict": NO_OPTIONS,
            })
            continue

        name = getattr(contract, "symbol", None) or (
            contract.get("symbol") if isinstance(contract, dict) else None)
        strike = getattr(contract, "strike_price", None) or (
            contract.get("strike_price") if isinstance(contract, dict) else None)
        expiry = getattr(contract, "expiration_date", None) or (
            contract.get("expiration_date") if isinstance(contract, dict) else None)

        bid, ask = quotes.get(str(name), (None, None))
        mid = ((bid + ask) / 2.0) if (bid and ask and ask >= bid) else None
        spread = spread_percent(bid, ask)
        cost = contract_cost(ask)
        gain = estimated_option_gain_percent(stock_price, mid, stock_target)
        verdict = classify(cost, budget, spread, gain, bid)

        rows.append({
            "symbol": symbol, "stock_price": stock_price,
            "contract_symbol": name,
            "strike": float(strike) if strike else None,
            "expiration": str(expiry) if expiry else None,
            "bid": bid, "ask": ask, "mid": mid, "cost": cost,
            "spread_pct": spread, "daily_move_pct": daily_move,
            "stock_target_pct": stock_target, "est_gain_pct": gain,
            "verdict": verdict,
        })

    order = {OK: 0, WIDE: 1, SPREAD_KILLS: 2, TOO_EXPENSIVE: 3,
             ZERO_BID: 4, NO_QUOTE: 5, NO_OPTIONS: 6}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), r["cost"] or 1e9))

    print_report(rows, budget, equity, show_all, want_puts)
    if save:
        save_report(rows)
    return rows


# ---------------------------------------------------------------------------
# Offline self-test
# ---------------------------------------------------------------------------

class FakeContract:
    def __init__(self, symbol, strike, expiration, underlying="X"):
        self.symbol = symbol
        self.strike_price = strike
        self.expiration_date = expiration
        self.underlying_symbol = underlying


def self_test():
    passed = 0
    failed = 0

    def check(label, condition):
        nonlocal passed, failed
        if condition:
            passed += 1
            print("  PASS  %s" % label)
        else:
            failed += 1
            print("  FAIL  %s" % label)

    print("options_probe.py self-test (offline, no network, no orders)")
    print("-" * 62)

    # spread_percent
    check("spread 1.00/1.10 is about 9.5%",
          abs(spread_percent(1.00, 1.10) - 9.5238) < 0.01)
    check("tight spread 2.00/2.02 is about 1%",
          abs(spread_percent(2.00, 2.02) - 0.995) < 0.01)
    check("zero bid gives None", spread_percent(0.0, 0.50) is None)
    check("missing ask gives None", spread_percent(1.0, None) is None)
    check("crossed quote gives None", spread_percent(1.20, 1.00) is None)
    check("negative values give None", spread_percent(-1.0, 1.0) is None)

    # contract_cost
    check("a $1.25 ask costs $125", contract_cost(1.25) == 125.0)
    check("zero ask has no cost", contract_cost(0.0) is None)
    check("missing ask has no cost", contract_cost(None) is None)

    # adaptive_target_percent
    settings = {"stop_multiplier": 1.5, "reward_ratio": 2.0,
                "min_stop": 0.015, "max_stop": 0.060}
    check("T at 2.87%/day gives an 8.61% target",
          abs(adaptive_target_percent(2.87, settings) - 8.61) < 0.01)
    check("a quiet 0.5%/day name is floored at a 3% target",
          abs(adaptive_target_percent(0.5, settings) - 3.0) < 0.01)
    check("a wild 10%/day name is capped at a 12% target",
          abs(adaptive_target_percent(10.0, settings) - 12.0) < 0.01)
    check("no movement data falls back to 4%",
          abs(adaptive_target_percent(None, settings) - 4.0) < 0.01)

    # estimated_option_gain_percent
    gain = estimated_option_gain_percent(30.0, 1.50, 8.61)
    check("a $30 stock moving 8.61% roughly doubles a $1.50 option",
          gain is not None and 85 < gain < 90)
    check("no option price gives no estimate",
          estimated_option_gain_percent(30.0, 0.0, 8.0) is None)
    check("no stock price gives no estimate",
          estimated_option_gain_percent(None, 1.5, 8.0) is None)

    # classify
    check("cheap, tight, profitable is OK",
          classify(120.0, 100.0 * 2, 3.0, 80.0, 0.60) == OK)
    check("over budget is flagged expensive",
          classify(450.0, 100.0, 3.0, 80.0, 0.60) == TOO_EXPENSIVE)
    check("spread bigger than the expected gain is flagged",
          classify(90.0, 100.0, 95.0, 80.0, 0.60) == SPREAD_KILLS)
    check("wide but survivable spread is a warning",
          classify(90.0, 100.0, 25.0, 80.0, 0.60) == WIDE)
    check("no bid means you cannot get out",
          classify(90.0, 100.0, None, 80.0, 0.0) == ZERO_BID)
    check("missing quote is reported as such",
          classify(None, 100.0, None, 80.0, 0.60) == NO_QUOTE)
    check("budget check runs before the spread check",
          classify(500.0, 100.0, 95.0, 10.0, 0.60) == TOO_EXPENSIVE)

    # pick_atm_contract
    contracts = [
        FakeContract("X1", 25.0, "2026-09-18"),
        FakeContract("X2", 30.0, "2026-09-18"),
        FakeContract("X3", 35.0, "2026-09-18"),
    ]
    picked = pick_atm_contract(contracts, 29.20)
    check("closest strike to $29.20 is the $30 strike",
          picked is not None and picked.symbol == "X2")
    picked = pick_atm_contract(contracts, 24.00)
    check("closest strike to $24.00 is the $25 strike",
          picked is not None and picked.symbol == "X1")
    check("no contracts gives None", pick_atm_contract([], 30.0) is None)
    check("no stock price gives None", pick_atm_contract(contracts, None) is None)

    dict_contracts = [{"symbol": "D1", "strike_price": "30.0",
                       "expiration_date": "2026-09-18"}]
    picked = pick_atm_contract(dict_contracts, 29.5)
    check("dict-shaped contracts also work",
          picked is not None and picked["symbol"] == "D1")

    bad = [FakeContract("B1", None, "2026-09-18"),
           FakeContract("B2", "not-a-number", "2026-09-18")]
    check("unparseable strikes are skipped, not crashed on",
          pick_atm_contract(bad, 30.0) is None)

    tie = [FakeContract("T_LATE", 30.0, "2026-12-18"),
           FakeContract("T_EARLY", 30.0, "2026-09-18")]
    picked = pick_atm_contract(tie, 30.0)
    check("equal strikes break toward the nearer expiration",
          picked is not None and picked.symbol == "T_EARLY")

    # movement table units
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "move.csv")
        with open(path, "w", newline="", encoding="utf-8") as handle:
            handle.write("symbol,atr_percent\nT,2.87\nVZ,0.0242\nBAD,abc\nZ,0\n")
        table = read_movement_table(path)
        check("percent-form movement reads as 2.87", abs(table.get("T", 0) - 2.87) < 0.01)
        check("fraction-form movement converts to 2.42",
              abs(table.get("VZ", 0) - 2.42) < 0.01)
        check("unparseable movement row is skipped", "BAD" not in table)
        check("zero movement row is skipped", "Z" not in table)
        check("missing movement file returns empty, not an error",
              read_movement_table(os.path.join(folder, "nope.csv")) == {})

        upath = os.path.join(folder, "uni.csv")
        with open(upath, "w", newline="", encoding="utf-8") as handle:
            handle.write("symbol,last_close\nT,24.41\n,50.0\nVZ,\n")
        rows = read_universe(upath)
        check("universe read keeps real rows and drops blank symbols",
              len(rows) == 2 and rows[0] == ("T", 24.41))
        check("blank price becomes None", rows[1][1] is None)

    # ---- v1.1: price merging and the liquid list -------------------------
    pairs = [("VZ", 47.36), ("SPY", None), ("T", None)]
    merged = merge_prices(pairs, {"SPY": 739.09, "T": 24.41})
    check("merge keeps the universe.csv price", merged["VZ"] == 47.36)
    check("merge fills a missing price", merged["SPY"] == 739.09)
    check("merge fills every gap", len(merged) == 3)

    merged = merge_prices([("VZ", 47.36)], {"VZ": 99.99})
    check("universe.csv price wins over lookup", merged["VZ"] == 47.36)

    check("merge tolerates no lookup", merge_prices(pairs, None)["VZ"] == 47.36)
    check("merge drops zero prices",
          "X" not in merge_prices([("X", 0)], {"X": 0}))
    check("merge with nothing gives nothing", merge_prices([], {}) == {})

    check("liquid list is not empty", len(LIQUID_NAMES) > 10)
    check("liquid list is all uppercase",
          all(s == s.upper() for s in LIQUID_NAMES))
    check("liquid list has no duplicates",
          len(LIQUID_NAMES) == len(set(LIQUID_NAMES)))
    check("liquid list includes SPY", "SPY" in LIQUID_NAMES)

    check("price lookup with no client returns empty",
          fetch_stock_prices(None, ["SPY"]) == {})
    check("price lookup with no symbols returns empty",
          fetch_stock_prices(object(), []) == {})

    print("-" * 62)
    print("%d passed, %d failed" % (passed, failed))
    return failed == 0


def main():
    parser = argparse.ArgumentParser(
        description="Read-only options viability probe. Places no orders.")
    parser.add_argument("--self-test", action="store_true",
                        help="run offline checks and exit")
    parser.add_argument("--symbols", type=str, default=None,
                        help="comma-separated symbols instead of universe.csv")
    parser.add_argument("--liquid", action="store_true",
                        help="probe the built-in list of heavily traded names "
                             "to see what tight option spreads look like")
    parser.add_argument("--equity", type=float, default=None,
                        help="account size to budget against")
    parser.add_argument("--puts", action="store_true",
                        help="price puts instead of calls")
    parser.add_argument("--days", type=int, nargs=2,
                        metavar=("MIN", "MAX"),
                        default=[DEFAULT_DAYS_MIN, DEFAULT_DAYS_MAX],
                        help="expiration window in days from today")
    parser.add_argument("--show-all", action="store_true",
                        help="list every symbol instead of the first 15")
    parser.add_argument("--save", action="store_true",
                        help="write options_probe_report.csv")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    symbols = args.symbols.split(",") if args.symbols else None
    try:
        run(symbols=symbols, equity=args.equity, want_puts=args.puts,
            days_min=args.days[0], days_max=args.days[1],
            show_all=args.show_all, save=args.save, liquid=args.liquid)
    except Exception as exc:
        print("")
        print("PROBE FAILED: %s" % exc)
        sys.exit(1)


if __name__ == "__main__":
    main()