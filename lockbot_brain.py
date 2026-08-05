"""
lockbot_brain.py  --  LOCKBOT's natural-language layer  (v1.0)

WHAT THIS IS
    A Claude-powered interface to everything LOCKBOT already knows. It
    reads the state files LOCKBOT writes every cycle -- heartbeats,
    positions, shadow trades, signals, journals -- and lets you ask
    questions about them in plain English, get a nightly analysis, or
    give trading commands out loud instead of remembering which script
    does what.

WHAT IT DOES NOT DO
    It does not participate in the trading cycle. lockbot_controller.py
    never calls this file, and no signal, entry, or exit decision passes
    through a language model. That is deliberate:

      - Non-deterministic. The same setup could get two different
        answers, which destroys the auditability that makes
        lockbot_config.py worth having.
      - Slow. Seconds of latency inside a loop that reacts to prices.
      - No edge. A language model cannot predict prices. Letting one
        invent trades would be an expensive random number generator
        with a confident tone.

    So the rules stay in the config, where they can be read and
    reasoned about. This module explains those rules and executes what
    YOU ask it to.

EXECUTION
    The trading tools here are real. close_position, close_all and
    submit_equity_trade place actual orders against the broker.

    Three things stand between a sentence and a filled order:
      1. You must ask for it. The model never initiates a trade.
      2. Every trade tool calls confirm() and refuses without a typed
         yes. Declining returns a normal tool result, so the model
         explains rather than retries.
      3. The same limits the autonomous path obeys are checked here --
         day-trade count, position caps, kill switch. A command that
         would breach them is refused with the reason.

USAGE
    python lockbot_brain.py --analyze        nightly analysis of the data
    python lockbot_brain.py --chat           interactive session
    python lockbot_brain.py --ask "..."      one question, one answer
    python lockbot_brain.py --self-test      offline checks, no network
    python lockbot_brain.py --chat --read-only    disable every trade tool
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_FOLDER = Path(__file__).resolve().parent

# Claude Fable 5. Thinking is always on and cannot be disabled — passing
# thinking={"type": "disabled"} returns a 400 — which is fine here
# because nothing in this file ever set it. Effort still controls depth.
#
# Fable also requires 30-day data retention and can decline a request
# outright with stop_reason "refusal", so every call site checks that
# before reading content, and the fallback below re-runs a declined
# request on Opus rather than returning nothing.
MODEL = os.getenv("LOCKBOT_MODEL", "claude-fable-5")
MAX_TOKENS = 16000

# Server-side fallback. On a policy decline the API re-runs the same
# request on Anthropic's recommended substitute inside the same call, so
# a refusal degrades to a slightly different answer instead of an error.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# How hard the model thinks, per task. These are not all the same job:
#
#   analyze  Reading 55 resolved trades across several groupings and
#            telling a real pattern from a small-sample accident is the
#            hardest thing this module does. Worth the tokens.
#   chat     Balanced. Most questions are lookups against a snapshot
#            that is already prepared.
#   brief    A 900-character status note. Deep reasoning buys nothing
#            here and costs latency on something you read at a glance.
#
# Thinking shares the max_tokens budget with the response, so the deep
# setting gets a larger ceiling — otherwise a long reasoning pass can
# truncate the answer it was thinking about.
EFFORT_ANALYZE = "xhigh"

# Chat effort is the single biggest lever on how conversational this
# feels. Fable 5 thinks before it emits any text, so effort translates
# almost directly into silence between you finishing a sentence and
# LOCKBOT starting one. At "high" that silence ran long enough to break
# the illusion of a live line — you stop talking, nothing happens, and
# you start wondering whether it heard you.
#
# Most chat turns do not earn it. "How are the options doing" is a
# lookup against a snapshot that is already assembled before the model
# sees it; the reasoning budget was being spent on formatting. Analysis
# keeps xhigh, where depth is the entire point.
#
# Raise it back with LOCKBOT_EFFORT_CHAT=high if answers get shallow.
EFFORT_CHAT = os.getenv("LOCKBOT_EFFORT_CHAT", "medium").strip().lower()
EFFORT_BRIEF = "low"

MAX_TOKENS_ANALYZE = 32000

# Notes the brain keeps for itself between sessions. Without this every
# conversation restarts from zero and rediscovers the same things — the
# volume-ratio inversion would be found again next week and reported as
# news. Plain Markdown so it stays readable and editable by hand.
MEMORY_FILE = PROJECT_FOLDER / "brain_memory.md"

# Set by main(). When True every trading tool refuses before it does
# anything, so --read-only is a hard guarantee rather than a prompt.
READ_ONLY = False

# Speech is opt-in per session. Off by default so nothing on this machine
# starts talking or listening because a script ran.
SPEAK_REPLIES = False
VOICE_INPUT = False
WAKE_WORD = False

# Let it look things up. On by default — most questions worth asking about
# a position eventually need something the state files do not contain.
WEB_SEARCH = True

# How long the conversation stays open after the last thing either of you
# said. Inside this window you can just keep talking; past it, the wake
# word is needed again.
#
# The window exists rather than listening forever because "always on"
# means every word near the machine is transcribed by a cloud service.
# This way the microphone is open only while a conversation is actually
# happening, and closes itself when one stops.
# 45 seconds was too short in practice. A reply worth thinking about
# takes longer than that to think about, and the window closing mid-pause
# forces the wake word again — which is exactly the "say its name every
# time" friction the window existed to remove. Ninety seconds covers a
# considered pause without leaving the microphone open indefinitely.
CONVERSATION_TIMEOUT = int(os.getenv("LOCKBOT_CONVERSATION_TIMEOUT", "90"))

# Said at the end of a turn, these close the conversation immediately
# rather than waiting for the window to lapse.
SIGN_OFFS = {
    "goodbye", "bye", "that's all", "thats all", "that is all",
    "never mind", "nevermind", "thank you", "thanks", "we're done",
    "were done", "go to sleep", "stand down", "dismissed",
}

# Replaced by the CLI with a real prompt. The default refuses, so a
# caller that forgets to wire confirmation cannot silently trade.
def _default_confirm(action: str, detail: str) -> bool:
    print(f"\n[no confirmation handler installed — refusing: {action}]")
    return False


CONFIRM = _default_confirm


# ---------------------------------------------------------------------------
# Reading LOCKBOT's state
#
# Everything here is cheap, deterministic, and free. The model gets a
# prepared snapshot rather than a filesystem, so a question costs one
# API call instead of a dozen tool round trips.
# ---------------------------------------------------------------------------

def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8") or "null")
    except (OSError, json.JSONDecodeError):
        return default


def _read_csv(path: Path, limit: int | None = None) -> list[dict]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []

    return rows[-limit:] if limit else rows


def _file_age_hours(path: Path) -> float | None:
    try:
        age = datetime.now().timestamp() - path.stat().st_mtime
        return round(age / 3600, 2)
    except OSError:
        return None


def _what_is_live(config: Any) -> dict[str, str]:
    """Which paths are submitting orders right now, stated in words.

    WHY THIS IS NOT JUST THE FLAGS

    The flags were already in the snapshot and the answer was still
    wrong. Asked "are options entries paused", with options_shadow_mode
    sitting at true right there in the configuration block, the reply
    was that options were "enabled and actively entering ... flagged as
    shadow-mode paper trades" -- reading shadow mode as a kind of paper
    trading rather than as no orders at all, and then blaming the pause
    on the equity path.

    A flag named shadow_mode does not say what it does, and "is it
    trading right now" is the one question about a trading bot that must
    never be answered by inference. So it is answered here, in the
    snapshot, in a sentence, rather than left to be reconstructed from
    three booleans whose names only make sense if you wrote them.

    Exits are listed separately and deliberately. Pausing entries must
    never read as pausing exits -- an option position with no software
    stop is unprotected capital, and that danger was created once
    already by turning shadow mode on.
    """

    def flag(name: str, default: bool = False) -> bool:
        return bool(getattr(config, name, default))

    options_on = flag("OPTIONS_ENABLED", True)
    options_shadow = flag("OPTIONS_SHADOW_MODE")

    if not options_on:
        options_entries = (
            "OFF — OPTIONS_ENABLED is False. The options scanner does not run."
        )
    elif options_shadow:
        options_entries = (
            "PAUSED — OPTIONS_SHADOW_MODE is True. Candidates are ranked and "
            "written to options_shadow_log.csv, and NO orders are submitted. "
            "This is not paper trading; it is no trading."
        )
    else:
        options_entries = "LIVE — option entry orders are being submitted."

    if flag("EQUITY_ENTRIES_ENABLED", True):
        equity_entries = "LIVE — share entry orders are being submitted."
    else:
        equity_entries = (
            "PAUSED — EQUITY_ENTRIES_ENABLED is False. Setups are still "
            "scanned and shadow-logged, but no share orders are submitted."
        )

    return {
        "equity_entries": equity_entries,
        "options_entries": options_entries,
        "options_exits": (
            "ALWAYS LIVE — options_manager.py evaluates and closes open "
            "option positions on every cycle regardless of shadow mode. It "
            "is the only stop loss options have."
        ),
        "equity_exits": (
            "BROKER-SIDE — the bracket order submitted with the entry is the "
            "sole exit. ENABLE_PAPER_EXITS is False and position_monitor "
            "only alerts."
        ),
        "etf_portfolio": (
            "ENABLED — buy-and-hold, separate from the trading engine."
            if flag("ETF_PORTFOLIO_ENABLED") else "OFF."
        ),
        "money": (
            "PAPER — PAPER_TRADING is True and LIVE_TRADING_ENABLED is "
            "False. No real money is at risk on any path."
            if flag("PAPER_TRADING", True)
            else "REAL MONEY — live trading is enabled."
        ),
    }


def collect_state() -> dict[str, Any]:
    """
    Gather everything LOCKBOT knows into one snapshot.

    Reads files only — no broker calls, so this is fast and works with
    the market closed and the network down.
    """

    import lockbot_config as config

    heartbeat = _read_json(config.HEARTBEAT_FILE, {}) or {}
    scanner_state = _read_json(PROJECT_FOLDER / "scanner_state.json", {}) or {}
    positions = _read_json(config.POSITION_STATE_FILE, {}) or {}
    risk_state = _read_json(config.RISK_STATE_FILE, {}) or {}
    option_positions = _read_json(config.OPTIONS_STATE_FILE, {}) or {}

    pending = _read_csv(config.PENDING_TRADES_FILE)
    completed = _read_csv(config.COMPLETED_TRADES_FILE)
    options_completed = _read_csv(PROJECT_FOLDER / "options_completed_trades.csv")
    shadow = _read_csv(PROJECT_FOLDER / "shadow_trades.csv")

    # signals.csv runs to millions of rows. Aggregate rather than dump:
    # the counts answer "why didn't it trade" far better than raw rows,
    # and the whole file would not fit in context anyway.
    signals = _read_csv(config.SIGNALS_FILE, limit=4000)

    signal_counts = Counter(row.get("signal", "") for row in signals)
    rejection_counts = Counter(
        row.get("approval_reason", "")
        for row in signals
        if row.get("trade_approved", "").strip().lower() not in {"true", "1"}
    )

    shadow_outcomes = Counter(row.get("outcome", "") for row in shadow)
    resolved = [
        row for row in shadow
        if row.get("outcome") in {"TARGET", "STOP"}
    ]

    wins = sum(1 for row in resolved if row.get("outcome") == "TARGET")

    def _r_multiple(row: dict) -> float:
        try:
            return float(row.get("r_multiple") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    module_health = {
        name: {
            "status": data.get("status"),
            "message": data.get("message"),
            "last_heartbeat_utc": data.get("last_heartbeat_at_utc"),
        }
        for name, data in (heartbeat.get("modules") or {}).items()
    }

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "what_is_actually_live": _what_is_live(config),
        "configuration": config.configuration_summary(),
        "module_health": module_health,
        "scanner_state": scanner_state,
        "equity_positions_tracked": positions,
        "option_positions_tracked": option_positions,
        "risk_state": risk_state,
        "pending_equity_trades": pending,
        "completed_equity_trades": completed,
        "completed_option_trades": options_completed,
        "signal_summary": {
            "rows_examined": len(signals),
            "by_signal": dict(signal_counts),
            "top_rejection_reasons": dict(rejection_counts.most_common(12)),
        },
        "shadow_summary": {
            "total_logged": len(shadow),
            "by_outcome": dict(shadow_outcomes),
            "resolved": len(resolved),
            "wins": wins,
            "win_rate_percent": (
                round(wins / len(resolved) * 100, 1) if resolved else None
            ),
            "average_r_multiple": (
                round(sum(_r_multiple(r) for r in resolved) / len(resolved), 3)
                if resolved else None
            ),
        },
        "file_ages_hours": {
            "universe.csv": _file_age_hours(config.UNIVERSE_FILE),
            "signals.csv": _file_age_hours(config.SIGNALS_FILE),
            "heartbeat": _file_age_hours(config.HEARTBEAT_FILE),
        },
        "brain_memory": read_memory(),
    }


def read_memory() -> str:
    """Load the brain's notes from previous sessions."""

    try:
        return MEMORY_FILE.read_text(encoding="utf-8")
    except OSError:
        return "(no notes recorded yet)"


def append_memory(note: str) -> str:
    """Append one dated note. Never rewrites what is already there."""

    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    entry = f"\n- **{stamp}** — {note.strip()}\n"

    header = ""

    if not MEMORY_FILE.exists():
        header = (
            "# LOCKBOT brain notes\n\n"
            "Findings worth carrying between sessions. Written by\n"
            "lockbot_brain.py; safe to edit or prune by hand.\n"
        )

    with MEMORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(header + entry)

    return f"Noted: {note.strip()}"


def shadow_breakdown(group_by: str) -> dict[str, Any]:
    """
    Win rate and average R for resolved shadow trades, grouped by a field.

    This is the analysis that matters most right now: it is how the
    inverted volume-ratio ranking was found. Grouping is done here in
    plain Python rather than asked of the model, because arithmetic
    over 150 rows should not be a language task.
    """

    rows = _read_csv(PROJECT_FOLDER / "shadow_trades.csv")
    resolved = [r for r in rows if r.get("outcome") in {"TARGET", "STOP"}]

    if not resolved:
        return {"error": "No resolved shadow trades yet."}

    if group_by == "volume_ratio":
        def keyer(row: dict) -> str:
            try:
                value = float(row.get("volume_ratio") or 0)
            except (TypeError, ValueError):
                return "unknown"
            if value < 1.25:
                return "1.10-1.25"
            if value < 1.75:
                return "1.25-1.75"
            return "1.75+"
    else:
        def keyer(row: dict) -> str:
            return str(row.get(group_by, "unknown") or "unknown")

    groups: dict[str, list[dict]] = {}

    for row in resolved:
        groups.setdefault(keyer(row), []).append(row)

    result = {}

    for name, group in sorted(groups.items()):
        wins = sum(1 for r in group if r.get("outcome") == "TARGET")

        def _r(row: dict) -> float:
            try:
                return float(row.get("r_multiple") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        result[name] = {
            "trades": len(group),
            "wins": wins,
            "win_rate_percent": round(wins / len(group) * 100, 1),
            "average_r": round(sum(_r(r) for r in group) / len(group), 3),
        }

    # 2:1 reward:risk means anything under 33.3% loses money.
    result["_breakeven_win_rate_percent"] = 33.3
    result["_note"] = (
        "Groups with fewer than 10 trades are noise — read them with that "
        "in mind."
    )

    return result


# ---------------------------------------------------------------------------
# Broker access shared by the trading tools
# ---------------------------------------------------------------------------

def _trading_client():
    from alpaca.trading.client import TradingClient
    from dotenv import load_dotenv

    import lockbot_config as config

    load_dotenv(PROJECT_FOLDER / ".env")

    api_key = os.getenv(config.ALPACA_API_KEY_ENV)
    secret_key = os.getenv(config.ALPACA_SECRET_KEY_ENV)

    if not api_key or not secret_key:
        raise RuntimeError("Alpaca API keys were not found in the .env file.")

    return TradingClient(api_key, secret_key, paper=config.PAPER_TRADING)


def _guard(action: str, detail: str) -> str | None:
    """
    Run every check that stands between a request and an order.

    Returns None when the trade may proceed, or the refusal text to hand
    back to the model.
    """

    import lockbot_config as config

    if READ_ONLY:
        return (
            "REFUSED: this session is read-only. Trading tools are disabled. "
            "Restart without --read-only to place orders."
        )

    risk_state = _read_json(config.RISK_STATE_FILE, {}) or {}

    if risk_state.get("kill_switch_active"):
        return (
            "REFUSED: the LOCKBOT kill switch is active "
            f"({risk_state.get('kill_switch_reason') or 'no reason recorded'}). "
            "Clear it in risk_state.json before trading."
        )

    if not CONFIRM(action, detail):
        return (
            "REFUSED: the user declined this action. Do not retry it or "
            "propose a variation unless they ask again."
        )

    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def build_tools() -> list:
    """Build the tool list. Imported lazily so --self-test needs no SDK."""

    from anthropic import beta_tool

    @beta_tool
    def get_broker_snapshot() -> str:
        """Read live account equity, buying power, open positions and open
        orders directly from the broker. Use when the question is about
        what is true right now rather than what LOCKBOT last recorded."""

        from position_filters import equity_positions, option_positions

        client = _trading_client()
        account = client.get_account()
        all_positions = client.get_all_positions()
        orders = client.get_orders()
        clock = client.get_clock()

        return json.dumps(
            {
                "equity": float(account.equity),
                "last_equity": float(account.last_equity),
                "buying_power": float(account.buying_power),
                "cash": float(account.cash) if account.cash else None,
                "options_trading_level": getattr(
                    account, "options_trading_level", None
                ),
                "market_open": clock.is_open,
                "next_open": str(clock.next_open),
                "equity_positions": [
                    {
                        "symbol": p.symbol,
                        "qty": p.qty,
                        "avg_entry": float(p.avg_entry_price),
                        "market_value": float(p.market_value),
                        "unrealized_pl": float(p.unrealized_pl),
                    }
                    for p in equity_positions(all_positions)
                ],
                "option_positions": [
                    {
                        "symbol": p.symbol,
                        "qty": p.qty,
                        "avg_entry": float(p.avg_entry_price),
                        "unrealized_pl": float(p.unrealized_pl),
                    }
                    for p in option_positions(all_positions)
                ],
                "open_orders": [
                    {
                        "symbol": o.symbol,
                        "side": str(getattr(o.side, "value", o.side)),
                        "qty": o.qty,
                        "status": str(getattr(o.status, "value", o.status)),
                    }
                    for o in orders
                ],
            },
            indent=2,
        )

    @beta_tool
    def get_shadow_breakdown(group_by: str) -> str:
        """Win rate and average R for resolved shadow trades, grouped by a
        field. This is the strongest evidence available about whether the
        strategy has an edge.

        Args:
            group_by: One of "regime", "side", "confidence", "volume_ratio",
                or "taken".
        """

        allowed = {"regime", "side", "confidence", "volume_ratio", "taken"}

        if group_by not in allowed:
            return f"group_by must be one of {sorted(allowed)}."

        return json.dumps(shadow_breakdown(group_by), indent=2)

    @beta_tool
    def get_day_trade_count() -> str:
        """Count same-day round trips in the last 7 days. The pattern-day-
        trader limit is 3 per 5 business days under $25,000, and options
        round trips count toward it."""

        from day_trade_tracker import get_day_trade_count as counter

        result = counter(_trading_client())

        return json.dumps(
            {
                "round_trips": result.total,
                "by_day": result.by_day,
                "detail": result.detail,
            },
            indent=2,
        )

    @beta_tool
    def close_position(symbol: str) -> str:
        """PLACES A REAL ORDER. Close one open position at the broker,
        cancelling any orders holding its shares first.

        Args:
            symbol: The position to close, e.g. "NVO".
        """

        symbol = symbol.strip().upper()

        refusal = _guard("CLOSE POSITION", f"Close the entire {symbol} position")

        if refusal:
            return refusal

        client = _trading_client()

        held = {p.symbol.upper() for p in client.get_all_positions()}

        if symbol not in held:
            return f"No open position in {symbol}. Nothing was sent."

        # Cancel the position's own orders FIRST. A protected position has
        # its shares held by the bracket, so a bare close_position fails
        # with "insufficient qty available (requested: 1, available: 0)" —
        # which is exactly what happened once the brackets were re-armed.
        # The stop is doing its job by holding the shares; it has to be
        # stood down before they can be sold deliberately.
        cancelled = 0

        try:
            for order in client.get_orders():
                if str(order.symbol).upper() != symbol:
                    continue

                try:
                    client.cancel_order_by_id(order.id)
                    cancelled += 1
                except Exception:
                    pass

        except Exception as error:
            return f"Could not read open orders for {symbol}: {error}"

        # Cancellation is not instant; the quantity is released a moment
        # later. Retry rather than failing on the first attempt.
        last_error = None

        for attempt in range(6):
            if attempt:
                time.sleep(1.5)

            try:
                client.close_position(symbol, close_options=None)

                return (
                    f"{symbol}: cancelled {cancelled} order(s), then submitted "
                    "the close. It fills at the next opportunity; if the market "
                    "is closed it queues for the open."
                )

            except Exception as error:
                last_error = error

        return (
            f"{symbol}: cancelled {cancelled} order(s) but the close was still "
            f"refused after 6 attempts — {last_error}. The position is now "
            "UNPROTECTED; re-arm it or close it by hand."
        )

    @beta_tool
    def close_all_positions() -> str:
        """PLACES REAL ORDERS. Cancel every open order, then liquidate every
        open position. This is the emergency flatten."""

        client = _trading_client()
        positions = client.get_all_positions()

        if not positions:
            return "Account is already flat. Nothing was sent."

        summary = ", ".join(f"{p.symbol} x{p.qty}" for p in positions)

        refusal = _guard(
            "CLOSE EVERYTHING",
            f"Cancel all orders and liquidate {len(positions)} position(s): {summary}",
        )

        if refusal:
            return refusal

        client.close_all_positions(cancel_orders=True)

        remaining = client.get_all_positions()

        return (
            f"Liquidation submitted for {len(positions)} position(s). "
            f"{len(remaining)} still showing open — if the market is closed "
            "that is expected, the orders are queued for the open."
        )

    @beta_tool
    def submit_equity_trade(symbol: str, quantity: int, side: str) -> str:
        """PLACES A REAL ORDER. Submit a plain market order for shares.

        This is a manual order and deliberately carries no bracket. The
        autonomous path in market_scanner.py attaches stop and target legs
        at entry; a position opened here has neither, so it needs watching
        or a manual exit.

        Args:
            symbol: Stock ticker, e.g. "EWZ".
            quantity: Whole number of shares, must be positive.
            side: "buy" or "sell".
        """

        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        import lockbot_config as config
        from day_trade_tracker import day_trade_limit_reached
        from position_filters import equity_positions

        symbol = symbol.strip().upper()
        side = side.strip().lower()

        if side not in {"buy", "sell"}:
            return 'side must be "buy" or "sell".'

        if quantity <= 0:
            return "quantity must be a positive whole number of shares."

        client = _trading_client()

        blocked, reason = day_trade_limit_reached(
            client, config.MAX_DAY_TRADES_PER_5_DAYS
        )

        if blocked:
            return f"REFUSED: {reason}"

        open_count = len(equity_positions(client.get_all_positions()))

        if side == "buy" and open_count >= config.MAX_OPEN_POSITIONS:
            return (
                f"REFUSED: {open_count} equity positions are already open, at "
                f"the MAX_OPEN_POSITIONS limit of {config.MAX_OPEN_POSITIONS}."
            )

        refusal = _guard(
            "SUBMIT ORDER",
            f"{side.upper()} {quantity} share(s) of {symbol} at market "
            "(no stop loss, no take profit)",
        )

        if refusal:
            return refusal

        order = client.submit_order(
            order_data=MarketOrderRequest(
                symbol=symbol,
                qty=quantity,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        )

        return (
            f"Order {order.id} submitted: {side} {quantity} {symbol}. "
            "No bracket is attached — this position has no automatic stop."
        )

    @beta_tool
    def read_project_file(filename: str, tail_lines: int = 200) -> str:
        """Read a file from the LOCKBOT folder — a log, a config, a data file,
        or the source of any module. Use it when a question needs detail the
        state snapshot does not carry, such as what the controller logged at a
        particular time or how a specific rule is implemented.

        Args:
            filename: Name relative to the LOCKBOT folder, e.g.
                "lockbot_controller.log" or "market_scanner.py".
            tail_lines: Return only the last N lines. Logs run to tens of
                thousands of lines, so the default is a tail.
        """

        # Resolve and confine to the project folder. The model chooses this
        # path, so "../../.." must not reach the rest of the disk.
        try:
            target = (PROJECT_FOLDER / filename).resolve()
            target.relative_to(PROJECT_FOLDER.resolve())
        except (ValueError, OSError):
            return f"Refused: {filename} is outside the LOCKBOT folder."

        if not target.is_file():
            return f"{filename} does not exist."

        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as error:
            return f"Could not read {filename}: {error}"

        total = len(lines)
        selected = lines[-max(1, tail_lines):]
        body = "\n".join(selected)

        # Keep a runaway file from swallowing the context window.
        if len(body) > 60000:
            body = body[-60000:]

        header = f"{filename} — {total} lines total, showing last {len(selected)}\n\n"

        return header + body

    @beta_tool
    def remember(note: str) -> str:
        """Save one finding to the brain's long-term notes so future sessions
        start with it instead of rediscovering it.

        Record durable conclusions — a measured pattern, a bug found, a
        decision the user made and why. Do not record the current equity or
        anything else that is already in the state snapshot and changes
        every cycle.

        Args:
            note: One sentence, specific and self-contained.
        """

        return append_memory(note)

    @beta_tool
    def get_process_status() -> str:
        """Check what LOCKBOT processes are running: whether the controller is
        up, how long for, whether its log is fresh, and any brain, HUD or
        Telegram sessions that never exited."""

        from lockbot_process import status

        return json.dumps(status(), indent=2, default=str)

    @beta_tool
    def propose_strategy(
        name: str,
        rationale: str,
        conditions_json: str,
        trend: str = "ANY",
        side: str = "BUY_LONG",
    ) -> str:
        """PROPOSE AND BACKTEST an entry rule. Places no orders.

        The rule is DATA, not code. Give conditions as a JSON list of
        objects, each with "left", "op" and "right":

          [{"left": "close", "op": "<", "right": "ema_9"},
           {"left": "rsi", "op": "between", "right": [30, 45]}]

        Fields available: close, open, high, low, ema_9, ema_21, vwap,
        rsi, macd, macd_signal, atr, volume, volume_avg_20.
        Operators: > < >= <= between outside.
        "right" may be another field name or a number; between/outside
        take two numbers.

        The rule is compiled and backtested over real history, and the
        result — including failure — is recorded. Read the scorecard
        first with strategy_scorecard: a rule that looks good means one
        thing after three proposals and something else after fifty.

        Be honest in the rationale. A rule you cannot state a reason for
        is a curve fit, and the validator rejects proposals without one.

        Args:
            name: Short name for the rule.
            rationale: Why this might work. Required.
            conditions_json: JSON list of condition objects.
            trend: BULLISH, BEARISH or ANY.
            side: BUY_LONG or SELL_SHORT.
        """

        import json as _json

        import strategy_lab

        try:
            conditions = _json.loads(conditions_json)
        except _json.JSONDecodeError as error:
            return f"conditions_json is not valid JSON: {error}"

        spec = {
            "name": name,
            "rationale": rationale,
            "trend": trend,
            "side": side,
            "conditions": conditions,
        }

        ok, why = strategy_lab.validate_spec(spec)

        if not ok:
            strategy_lab.record_proposal(spec, None, "REJECTED")
            return f"REJECTED: {why}"

        try:
            import backtest
            import lockbot_config as _cfg
            from universe import load_universe

            symbols = load_universe(_cfg.UNIVERSE_FILE)
            frames = backtest.load_history(symbols, days=5)
        except Exception as error:
            return f"Could not load history: {type(error).__name__}: {error}"

        if not frames:
            return "No usable history; cannot judge the proposal."

        verdict, result = strategy_lab.evaluate(spec, frames)
        strategy_lab.record_proposal(spec, result, verdict)

        return (
            f"{strategy_lab.describe_spec(spec)}\n\n"
            f"VERDICT: {verdict}\n"
            f"  trades {result.get('trades')} over {result.get('days')} day(s), "
            f"{result.get('busiest_share', 0):.0%} from the busiest\n"
            f"  win rate {result.get('win_rate', 0):.1%} against a "
            f"{result.get('breakeven', 0):.1%} breakeven\n"
            f"  expectancy {result.get('expectancy_r', 0):+.3f} R\n\n"
            "Recorded. This does not deploy anything."
        )

    @beta_tool
    def recommend_change(
        setting: str,
        value: str,
        rationale: str,
        evidence: str,
        sample_size: int,
    ) -> str:
        """RECOMMEND a setting change. Applies nothing.

        This is how you turn something you have learned into something
        the user can act on. It records the proposal with its evidence;
        they decide.

        Use it when the data supports a change, not when a change feels
        sensible. State the sample size honestly — a recommendation from
        twelve observations is recorded as THIN and should say so.

        Do not recommend a change you cannot cite evidence for. The
        volume-ratio split looked convincing across 55 setups and was
        chance at p=0.61; a loop that acted on findings that strong would
        have retuned the ranking around noise.

        Args:
            setting: A remotely changeable setting (see list_settings).
            value: The proposed value.
            rationale: Why, in one sentence.
            evidence: What was measured, and over how much data.
            sample_size: Number of observations behind it.
        """

        import recommendations

        ok, message = recommendations.propose(
            setting, value, rationale, evidence, sample_size
        )

        return message

    @beta_tool
    def list_recommendations() -> str:
        """Show pending setting recommendations and how past ones went.

        Read this before recommending something new — a proposer whose
        dismissed ideas are forgotten looks better than it is.
        """

        import recommendations

        return recommendations.report()

    @beta_tool
    def strategy_scorecard() -> str:
        """How the strategy proposer has actually performed: how many
        rules it has proposed, and how many looked promising.

        Read this before trusting any single result. If twenty rules have
        been proposed and one looks good, that one is what chance looks
        like.
        """

        import strategy_lab

        return strategy_lab.generator_scorecard()

    @beta_tool
    def list_settings() -> str:
        """List the settings that can be changed while LOCKBOT is running,
        their allowed ranges, and which are currently overridden.

        Use this before change_setting when unsure of a name or a bound.
        """

        from runtime_settings import describe

        return describe()

    @beta_tool
    def change_setting(name: str, value: str) -> str:
        """CHANGES A LIVE RISK SETTING. Requires confirmation.

        Writes to a runtime overrides file that lockbot_config.py applies
        at import. It does NOT edit any code. The change takes effect on
        the next cycle, because components are spawned fresh.

        Only names on the allowlist in runtime_settings.py are accepted,
        each within a fixed range. PAPER_TRADING and LIVE_TRADING_ENABLED
        are NOT on that list and cannot be changed here — moving between
        fake and real money requires someone at the keyboard.

        If a request falls outside the bounds, say so and stop. Do not
        suggest editing the file directly to get around it.

        Args:
            name: The setting, e.g. OPTIONS_MAX_SPREAD_PERCENT.
            value: The new value. "off"/"false" work for switches.
        """

        from runtime_settings import set_override, validate

        name = name.strip().upper()

        ok, why = validate(name, value)

        if not ok:
            return f"REFUSED: {why}"

        refusal = _guard(
            "CHANGE SETTING",
            f"{name} -> {value}. Alters live risk behaviour from the next "
            "cycle.",
        )

        if refusal:
            return refusal

        ok, message = set_override(name, value, who="assistant")

        return message if ok else f"REFUSED: {message}"

    @beta_tool
    def reset_setting(name: str) -> str:
        """Remove a runtime override so the value in lockbot_config.py
        applies again. Requires confirmation.

        Args:
            name: The setting to reset.
        """

        from runtime_settings import clear_override

        name = name.strip().upper()

        refusal = _guard(
            "RESET SETTING", f"{name} back to its file default."
        )

        if refusal:
            return refusal

        ok, message = clear_override(name, who="assistant")

        return message if ok else f"REFUSED: {message}"

    @beta_tool
    def control_lockbot(action: str) -> str:
        """CONTROLS THE RUNNING SYSTEM. Start, stop or restart the LOCKBOT
        controller, or clear out stale sessions.

        Stopping the controller also stops options_manager.py, which is the
        ONLY stop loss open option positions have — Alpaca provides no bracket
        for contracts. The stop is refused while options are open unless the
        user explicitly insists.

        Args:
            action: One of "start", "stop", "restart", "cleanup".
        """

        from lockbot_process import (
            cleanup_sessions,
            restart_controller,
            start_controller,
            stop_controller,
        )

        action = action.strip().lower()

        if action not in {"start", "stop", "restart", "cleanup"}:
            return 'action must be "start", "stop", "restart" or "cleanup".'

        # Cleanup only ends read-only sessions, so it needs confirming but
        # carries none of the stop-loss risk the others do.
        detail = {
            "start": "Start the LOCKBOT controller",
            "stop": "STOP the controller — trading and options stops both end",
            "restart": "Restart the controller",
            "cleanup": "End stale brain/HUD/Telegram sessions (no positions affected)",
        }[action]

        refusal = _guard(f"{action.upper()} LOCKBOT", detail)

        if refusal:
            return refusal

        if action == "start":
            return start_controller()

        if action == "stop":
            return stop_controller()

        if action == "restart":
            return restart_controller()

        return cleanup_sessions()

    @beta_tool
    def run_lockbot_component(name: str) -> str:
        """Run one LOCKBOT component once, right now, and report the result.

        Useful for forcing a scan, rebuilding the universe, resolving shadow
        trades, or re-arming brackets without waiting for the next cycle.

        Args:
            name: One of scanner, manager, monitor, health, options,
                options_scan, universe, volatility, shadow, rearm.
        """

        from lockbot_process import COMPONENTS, run_component

        if name.strip().lower() not in COMPONENTS:
            return f"Unknown component. Choose from: {', '.join(sorted(COMPONENTS))}"

        refusal = _guard("RUN COMPONENT", f"Run {name} once now")

        if refusal:
            return refusal

        return run_component(name)

    tools = [
        get_broker_snapshot,
        get_shadow_breakdown,
        get_day_trade_count,
        get_process_status,
        read_project_file,
        remember,
        propose_strategy,
        strategy_scorecard,
        recommend_change,
        list_recommendations,
        list_settings,
        change_setting,
        reset_setting,
        control_lockbot,
        run_lockbot_component,
        close_position,
        close_all_positions,
        submit_equity_trade,
    ]

    # Web search runs on Anthropic's servers — there is no function here to
    # implement, just a declaration. It is what turns "what LOCKBOT
    # recorded" into "what is true in the world": why a stock moved, what
    # an earnings date is, what a rule actually says.
    #
    # Off in a read-only remote session, where the point is answering from
    # LOCKBOT's own data rather than the internet.
    if WEB_SEARCH:
        tools.append({"type": "web_search_20260209", "name": "web_search"})

    return tools


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are LOCKBOT's operator interface — the natural-language
layer over an autonomous algorithmic trading bot running an Alpaca paper
account.

You are talking to the person who built and owns LOCKBOT. They are technical
and they are risking their own money, so be direct and factual. Never
congratulate them on a question.

GROUNDING
Every claim you make about LOCKBOT must come from the state snapshot or a
tool result. If the data does not answer the question, say so plainly rather
than reasoning from what a trading bot usually does. Never invent a number.

WHAT YOU CAN DO
You have these interfaces. Answer questions about your own capabilities from
this list — do not guess, and do not assume a channel is unavailable just
because it is not the one currently in use.

- Text chat in a terminal.
- SPEECH INPUT. lockbot_voice.py captures the microphone and transcribes it.
  Launch with `lockbot_brain.py --chat --listen` for push-to-talk: the user
  presses Enter, speaks, and you receive the transcript as an ordinary
  message. So yes, you can be spoken to.
- SPEECH OUTPUT. Replies are read aloud with a Microsoft neural voice
  (`--voice`, implied by `--listen`).
- Phone briefings pushed through Pushover (`--brief`).
- Telegram, read-only, for questions from a phone.
- Long-term notes in brain_memory.md that persist between sessions.
- WEB SEARCH. You can look things up — why a stock moved, an earnings date,
  what a term means. Use it when the answer is not in LOCKBOT's own files, and
  say where a fact came from. Quoted prices from search are delayed; the broker
  snapshot is the authority on anything about this account.
- READING ANY FILE in the LOCKBOT folder, including logs and your own source.
  Reach for it when someone asks what happened at a particular time, or how a
  rule is actually implemented rather than how it is described.

The session you are in right now may have only some of these switched on. If
someone asks whether you can hear them and voice input is not active in this
session, say the capability exists and name the flag that enables it, rather
than saying you cannot.

WHAT YOU KNOW ABOUT THE EVIDENCE
As of 2026-07-29 the shadow data showed a 27.8% win rate against a 33.3%
breakeven at 2:1 reward-to-risk, and the volume-ratio tiebreaker used to rank
setups was inverted — the higher-volume half performed worse. Treat the
strategy as unproven-to-negative, not as working. If the user asks whether it
is profitable, answer from the data, including sample size.

TRADING
You have tools that place real orders. Never use them unless the user asks
for that specific action in this conversation. Do not offer to trade, do not
propose trades, and do not treat analysis as a reason to act. If a tool
returns REFUSED, relay the reason and stop — do not retry or work around it.

STYLE
You are talking with someone, not filing a report. Write the way a sharp
colleague talks: contractions, ordinary connectives, a natural rhythm. Answer
first, detail after.

That does not mean padding. Skip the throat-clearing — no "Great question",
no restating what was asked, no summarising what you just said. Warmth comes
from how you say it, not from extra words.

Follow the thread of the conversation. If they asked about NVO a minute ago
and now say "and the other one?", that is LVS — do not make them repeat
themselves. Refer back to what has already been established rather than
re-explaining it.

Have a view. When the data points somewhere, say so plainly rather than
laying out options and retreating. If a question rests on a wrong premise,
say so in a sentence and then answer it anyway.

Reserve tables for genuinely tabular data. Prose is usually better.

Deliver what was asked at the scope asked."""


# The JARVIS register. Set LOCKBOT_PERSONA=plain in .env to turn it off.
#
# The character is not the "sir" — it is the composure. He delivers bad
# news at exactly the same volume as good news, and that steadiness is
# worth something in a trading tool: a system that sounds alarmed makes
# you act alarmed, which is how people abandon a strategy mid-measurement.
#
# What must NOT change is the honesty. A butler who tells you what you
# want to hear is useless, and a trading assistant that does it is
# dangerous. Composed, not agreeable.
PERSONA = os.getenv("LOCKBOT_PERSONA", "jarvis").strip().lower()

JARVIS_PERSONA = """

MANNER
Address the user as "sir", but sparingly — once in an exchange, not in
every sentence, and never twice in one reply.

Be composed. Deliver a loss in the same register as a gain: level, precise,
unhurried. Never exclaim, never console, never dramatise. "Both positions
are without a stop loss at present" carries more weight than any warning
you could shout.

Understatement over emphasis. Dry where dryness is earned — a small
observation about the situation is welcome; a joke at the user's expense
is not. If something is genuinely bad, say it plainly and let the plainness
do the work.

Anticipate. If a number implies an obvious next question, answer it before
it is asked. If the user is about to do something the data argues against,
say so once, clearly, and then do as instructed.

None of this softens the facts. You are candid first and composed second —
if the strategy is losing money, say the strategy is losing money. A
pleasant manner attached to a flattering account of the numbers would be
worse than no manner at all.
"""


HEARING_STYLE = """

WHAT YOU RECEIVE MAY BE IMPERFECT
Some messages arrive through speech recognition. It drops words, mangles
tickers, and sometimes cuts a sentence short. Read what you are given as a
transcript of someone talking, not as text they typed.

Interpret charitably using context. "N V O" and "in vo" and "envy oh" are
NVO. "L B S" is probably LVS. "rearm the brackets" and "re-arm my brackets"
are the same request. If the account holds two positions and someone asks
about "the other one", they mean the one you did not just discuss.

Act on a reasonable reading rather than stalling on an imperfect one. A
fragment like "what about the stop" is answerable — answer it.

Only ask when the reading actually changes what you would do, and when you
do, say what you think you heard rather than asking them to repeat
themselves: "did you mean NVO's stop, or the options one?" is useful.
"Sorry, I did not catch that" is not.

Never mention transcription quality unless they raise it. They know what
they said."""


VOICE_STYLE = """
YOU ARE BEING HEARD, NOT READ
This reply will be spoken aloud, so write for the ear.

- No markdown at all. No headers, bullets, tables, asterisks or backticks —
  they are read out as punctuation or silently mangled.
- Short sentences. A listener cannot re-read a clause they lost.
- Say numbers the way a person says them: "two hundred forty nine dollars",
  "down about half a percent", "roughly a third". Exact cents are for the
  screen.
- Spell out tickers as letters — "N V O", "L V S" — they are unintelligible
  otherwise.
- Two or three sentences unless genuinely asked for more. Anything past about
  forty words gets cut off mid-thought when spoken.
- If the full answer is long, say the short version and offer the rest:
  "there's more detail if you want it."
"""


ANALYST_PROMPT = """Analyze LOCKBOT's current state and recent performance.

Cover, in this order:

1. OPERATIONAL — is anything broken, stale, or degraded? Module health,
   file freshness, positions that don't reconcile.
2. EVIDENCE — what do the shadow trades say about whether the strategy has
   an edge? Use get_shadow_breakdown on more than one grouping. Always give
   sample sizes and say when a group is too small to read.
3. THE ONE THING — the single most important thing the user should look at
   next, and why.

Be specific and quantitative. If the data is too thin to support a
conclusion, say that instead of manufacturing one. Do not recommend trades."""


def _client():
    from anthropic import Anthropic
    from dotenv import load_dotenv

    load_dotenv(PROJECT_FOLDER / ".env")

    api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")

    if not api_key:
        raise RuntimeError(
            "No Claude API key found. Set CLAUDE_API_KEY in the .env file."
        )

    # The SDK retries 429/5xx/529 with backoff. The default of 2 attempts
    # is thin during an API capacity crunch — a 529 burst would surface as
    # a dead session and lose the question you just asked. Six costs
    # nothing when things are healthy.
    return Anthropic(api_key=api_key, max_retries=6)


def _system_prompt() -> str:
    """Assemble the system prompt for this session's persona and channel."""

    prompt = SYSTEM_PROMPT

    if PERSONA == "jarvis":
        prompt += JARVIS_PERSONA

    # Hearing guidance applies whenever the mic is open, whether or not
    # replies are spoken.
    if VOICE_INPUT or WAKE_WORD:
        prompt += HEARING_STYLE

    if SPEAK_REPLIES:
        prompt += "\n" + VOICE_STYLE

    return prompt


def _state_block(state: dict) -> dict:
    """The state snapshot as a cached system block.

    Marked cacheable because it is identical across every turn of a
    session — repeated questions then cost a fraction of the first.
    """

    return {
        "type": "text",
        "text": (
            "Current LOCKBOT state snapshot (JSON):\n\n"
            + json.dumps(state, indent=2, default=str)
        ),
        "cache_control": {"type": "ephemeral"},
    }


def analyze() -> str:
    """Run one analysis pass over LOCKBOT's data and print the result."""

    client = _client()
    state = collect_state()
    tools = build_tools()

    print("Collecting state and analyzing...\n")

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS_ANALYZE,
        output_config={"effort": EFFORT_ANALYZE},
        system=[{"type": "text", "text": _system_prompt()}, _state_block(state)],
        tools=tools,
        messages=[{"role": "user", "content": ANALYST_PROMPT}],
    )

    final = ""

    for message in runner:
        if message.stop_reason == "refusal":
            return "Claude declined this request."

        for block in message.content:
            if block.type == "text" and block.text.strip():
                final = block.text

    print(final)
    return final


BRIEF_PROMPT = """Write a short briefing for the phone. Hard limit 900
characters — it is a push notification, not a report.

Cover only what changed and what needs attention: anything broken or stale,
open positions and where they stand, and the one thing worth knowing today.
Skip anything routine. If nothing needs attention, say so in a sentence
rather than inventing content.

Plain sentences, no headers, no bullets, no markdown."""


def brief(send: bool = True) -> str:
    """Write a short briefing and push it to the phone."""

    client = _client()
    state = collect_state()

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        output_config={"effort": EFFORT_BRIEF},
        system=[{"type": "text", "text": _system_prompt()}, _state_block(state)],
        messages=[{"role": "user", "content": BRIEF_PROMPT}],
    )

    if response.stop_reason == "refusal":
        return "Claude declined to write the briefing."

    text = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()

    print(text)

    if send and text:
        try:
            from notifications import send_notification

            send_notification(title="LOCKBOT Briefing", message=text[:1000])
            print("\n[pushed to phone]")

        except Exception as error:
            print(f"\n[push failed] {type(error).__name__}: {error}")

    return text


def ask(
    question: str,
    state: dict | None = None,
    history: list[dict] | None = None,
) -> str:
    """Answer one question about LOCKBOT.

    `history` is prior turns in message format, oldest first. Without it
    this is a standing start every time, which is how the Telegram
    channel behaved for its whole existence -- "what about the other
    one?" had no other one. chat() below has always kept its own running
    list, so the same assistant was coherent at the keyboard and
    amnesiac on the phone.

    The caller owns the history and its bounds. See
    conversation_memory.py for why it is capped by both count and age.
    """

    client = _client()
    state = state if state is not None else collect_state()

    messages = list(history or [])
    messages.append({"role": "user", "content": question})

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        output_config={"effort": EFFORT_CHAT},
        system=[{"type": "text", "text": _system_prompt()}, _state_block(state)],
        tools=build_tools(),
        messages=messages,
    )

    final = ""

    for message in runner:
        if message.stop_reason == "refusal":
            return "Claude declined this request."

        for block in message.content:
            if block.type == "text" and block.text.strip():
                final = block.text

    return final


def chat() -> None:
    """Interactive session. State is collected once and cached."""

    client = _client()
    state = collect_state()
    tools = build_tools()
    system = [{"type": "text", "text": _system_prompt()}, _state_block(state)]

    equity = state.get("scanner_state", {}).get("account_equity", 0.0)

    print("=" * 60)
    print("LOCKBOT BRAIN")
    print("=" * 60)
    print(f"Model      : {MODEL}")
    print(f"Mode       : {'READ-ONLY' if READ_ONLY else 'TRADING ENABLED'}")
    print(f"Equity     : ${equity:,.2f}")
    print(f"Speech out : {'on' if SPEAK_REPLIES else 'off'}")
    if WAKE_WORD:
        from lockbot_voice import WAKE_PHRASES

        print(f"Speech in  : voice activated — say \"{WAKE_PHRASES[0]}\"")
    else:
        print(f"Speech in  : {'push-to-talk (press Enter)' if VOICE_INPUT else 'off'}")
    print("Trading    : every order asks for typed confirmation")
    print("Exit       : Ctrl+C, or type 'exit'")
    print("=" * 60)

    if SPEAK_REPLIES:
        print("(speech is non-blocking — press Enter any time to cut it off)")
        _say("Lockbot online.")

    messages: list[dict] = []

    # Conversation state. `conversing` means the window is open and the
    # wake word is not needed; `last_exchange` is when either of you last
    # said something.
    conversing = False
    last_exchange = 0.0

    while True:
        try:
            # NOTE: do not silence speech here.
            #
            # This is where _hush() used to live, and it was wrong. The
            # reply is spoken asynchronously at the bottom of the loop,
            # so control returned here immediately and cut the audio off
            # a fraction of a second after it began — you heard the first
            # syllable and nothing else. That was the "choppiness".
            #
            # Speech is now silenced only once the user actually starts a
            # turn: after the wake word fires, or after Enter is pressed.
            # Interrupting still feels instant, but an uninterrupted reply
            # is allowed to finish.

            if WAKE_WORD:
                from lockbot_voice import WAKE_PHRASES, listen, wait_until_quiet

                # Inside the conversation window, just keep talking. The
                # wake word is only needed to START a conversation, not to
                # continue one — saying a name before every sentence is
                # not how people talk.
                if conversing and (time.monotonic() - last_exchange) < CONVERSATION_TIMEOUT:
                    # Never open the mic while it is still talking, or it
                    # transcribes its own reply as your next question.
                    wait_until_quiet()

                    remaining = int(
                        CONVERSATION_TIMEOUT - (time.monotonic() - last_exchange)
                    )
                    print(
                        f"\n[listening — {remaining}s left, or type instead]"
                    )

                    heard, source = _listen_or_type(
                        lambda: listen(timeout=CONVERSATION_TIMEOUT)
                    )

                    if not heard:
                        conversing = False
                        print("[conversation closed — say the wake word to start again]")
                        continue

                    if source == "voice":
                        print(f"you> {heard}")

                    question = heard

                else:
                    if conversing:
                        print("[conversation timed out]")
                        conversing = False

                    print(
                        f"\n[say '{WAKE_PHRASES[0]}' — or just start typing "
                        "— Ctrl+C to quit]"
                    )

                    question, source = _await_wake_or_typing()

                    if not question:
                        continue

                    if source == "voice":
                        print(f"you> {question}")
                        conversing = True
                    else:
                        # Typing is a one-off turn. Opening a voice window
                        # after a typed question would leave the mic live
                        # for someone who chose not to use it.
                        conversing = False

                # An explicit sign-off ends it now rather than waiting for
                # the window to lapse.
                if question.strip().lower().rstrip(".!") in SIGN_OFFS:
                    _say("Very good, sir.")
                    print("[conversation closed]")
                    conversing = False
                    last_exchange = 0.0
                    continue

            elif VOICE_INPUT:
                # Push-to-talk. Nothing is recorded until you ask for it,
                # and Enter alone is the trigger so the machine is never
                # sitting there listening on its own.
                prompt = input("\n[Enter] to speak, or type> ").strip()

                # The user is back — anything still playing is stale now.
                _hush()

                if prompt:
                    question = prompt
                else:
                    from lockbot_voice import listen

                    heard = listen()

                    if not heard:
                        continue

                    print(f"you (voice)> {heard}")
                    question = heard
            else:
                question = input("\nyou> ").strip()
                _hush()

        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return

        if not question:
            continue

        if question.lower() in {"exit", "quit"}:
            print("Bye.")
            return

        messages.append({"role": "user", "content": question})

        try:
            _voice_state("thinking", question[:120])

            runner = client.beta.messages.tool_runner(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                output_config={"effort": EFFORT_CHAT},
                system=system,
                tools=tools,
                messages=messages,
            )

            reply = ""

            for message in runner:
                if message.stop_reason == "refusal":
                    reply = "Claude declined this request."
                    break

                messages.append({"role": "assistant", "content": message.content})

                tool_response = runner.generate_tool_call_response()

                if tool_response is not None:
                    messages.append(tool_response)

                for block in message.content:
                    if block.type == "text" and block.text.strip():
                        reply = block.text

            print(f"\nlockbot> {reply}")
            _say(reply)

            # The window is measured from the end of the exchange, so a
            # long answer does not eat the time you have to respond to it.
            last_exchange = time.monotonic()

        except Exception as error:
            name = type(error).__name__

            # An exhausted credit balance is a billing state, not a fault,
            # and retrying will never clear it. Say so plainly instead of
            # printing a 400 that reads like a code defect -- and say the
            # part that actually matters, which is that nothing about
            # trading depends on this API. The scanner, the risk gates and
            # options_manager's software stop are pure Python against
            # Alpaca; only conversation and the nightly learning pass go
            # through Anthropic. Someone reading a raw 400 at the open
            # could reasonably assume their stop loss had just died.
            if "credit balance is too low" in str(error).lower():
                print(
                    "\nlockbot> The Anthropic credit balance is empty, so I "
                    "can't answer questions until it's topped up at "
                    "console.anthropic.com under Plans & Billing.\n"
                    "         Trading is NOT affected — the scanner, the "
                    "risk limits and the options stop loss don't use this "
                    "API at all.\n"
                    "         Telegram /status still works too; it reads "
                    "the account directly."
                )
                _say(
                    "The Anthropic credit balance is empty, so I cannot "
                    "answer questions until it is topped up. Trading is "
                    "unaffected — the stop loss does not use this API."
                )

                # Drop the unanswered turn; the history must not carry a
                # user message that never got a reply.
                if messages and messages[-1].get("role") == "user":
                    messages.pop()

                continue

            # Overload and rate-limit are Anthropic-side capacity, not a
            # fault here. Say that in words rather than showing a raw
            # traceback, and keep the question so it can be retried by
            # pressing Enter instead of asking again.
            if "Overloaded" in name or "RateLimit" in name or "529" in str(error):
                print(
                    "\nlockbot> The API is overloaded right now — that's on "
                    "Anthropic's side, not yours. I kept your question; say "
                    "'retry' and I'll send it again."
                )
                _say("The API is busy. Say retry and I'll try again.")

                # Drop the unanswered turn so the history stays valid.
                if messages and messages[-1].get("role") == "user":
                    last_question = messages.pop()

                    if question.strip().lower() in {"retry", "try again"}:
                        messages.append(last_question)

                continue

            print(f"\n[error] {name}: {error}")


def _say(text: str, block: bool = False) -> None:
    """
    Speak a reply when the session asked for voice. Never fatal.

    Non-blocking by default. Speech runs an order of magnitude slower than
    reading — a reply you scan in two seconds takes thirty to hear — and
    blocking the prompt for those thirty seconds makes the session look
    frozen. It did exactly that before this: 41 seconds per answer with no
    way to type or interrupt.

    One-shot commands pass block=True, because there the process exits and
    would cut its own audio off.
    """

    if not SPEAK_REPLIES or not text:
        return

    try:
        import lockbot_voice

        if block:
            lockbot_voice.speak(text)
        else:
            lockbot_voice.speak_async(text)

    except Exception as error:
        print(f"[voice unavailable] {type(error).__name__}: {error}")


def _listen_or_type(listen_fn) -> tuple[str, str]:
    """
    Capture the next turn from the microphone OR the keyboard.

    Used mid-conversation, where the window is already open. The mic
    thread cannot be cancelled once speech_recognition is blocking on it,
    so a typed line simply wins and the thread is abandoned — it times
    out on its own a few seconds later and nothing reads its result.
    """

    import threading

    import msvcrt

    result: dict = {}

    def capture() -> None:
        try:
            result["heard"] = listen_fn()
        except Exception:
            result["heard"] = None

    listener = threading.Thread(target=capture, daemon=True)
    listener.start()

    while True:
        if "heard" in result:
            heard = result["heard"]
            return (heard, "voice") if heard else ("", "none")

        if msvcrt.kbhit():
            first = msvcrt.getwche()
            rest = input().strip()

            return ((first + rest).strip(), "typed")

        time.sleep(0.05)


def _await_wake_or_typing() -> tuple[str, str]:
    """
    Wait for either the wake word or the keyboard, whichever comes first.

    Speech should never be the ONLY way in. Voice is wrong when someone
    else is in the room, when a ticker will not survive transcription, or
    when you simply want to type — so the keyboard stays live the entire
    time the microphone is.

    Returns (text, source) where source is "voice", "typed" or "none".
    """

    import threading

    import msvcrt

    from lockbot_voice import listen, wait_for_wake_word, wait_until_quiet

    cancel = threading.Event()
    result: dict = {}

    def wake() -> None:
        phrase = wait_for_wake_word(cancel=cancel)

        if phrase and not cancel.is_set():
            result["phrase"] = phrase

    listener = threading.Thread(target=wake, daemon=True)
    listener.start()

    try:
        while True:
            if "phrase" in result:
                _hush()
                print(f"[woken: {result['phrase']}]")
                _say("Yes?")
                wait_until_quiet()

                heard = listen()

                return (heard, "voice") if heard else ("", "none")

            if not listener.is_alive() and "phrase" not in result:
                # Recogniser stopped on its own — fall back to typing.
                return (input("\nyou> ").strip(), "typed")

            # A keypress means they would rather type. Stop listening and
            # hand the line over, keeping the character they already hit.
            if msvcrt.kbhit():
                first = msvcrt.getwche()
                cancel.set()

                rest = input().strip()

                return ((first + rest).strip(), "typed")

            time.sleep(0.05)

    finally:
        cancel.set()


def _voice_state(state: str, detail: str = "") -> None:
    """Publish what the brain is doing, for the HUD. Never fatal."""

    try:
        from lockbot_voice import set_voice_state

        set_voice_state(state, detail)

    except Exception:
        pass


def _hush() -> None:
    """Stop any speech still playing. Called when the user starts a turn."""

    if not SPEAK_REPLIES:
        return

    try:
        from lockbot_voice import stop_speaking

        stop_speaking()

    except Exception:
        pass


def _cli_confirm(action: str, detail: str) -> bool:
    """
    Ask the operator before anything reaches the broker.

    Confirmation is always TYPED, even in a voice session. Speech
    recognition mishears, and "yes" is far too close to several things a
    microphone might invent. A misheard word should never be able to
    place an order.
    """

    print()
    print("!" * 60)
    print(f"CONFIRM: {action}")
    print(f"  {detail}")
    print("!" * 60)

    _say(f"Confirm: {detail}. Type yes to approve.")

    try:
        answer = input("Type 'yes' to place this order: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nDeclined.")
        return False

    approved = answer == "yes"
    print("Approved." if approved else "Declined.")

    return approved


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    """Offline checks. No network, no API key, no SDK required."""

    global READ_ONLY, CONFIRM

    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    print("State collection")

    state = collect_state()

    for key in (
        "configuration",
        "what_is_actually_live",
        "module_health",
        "shadow_summary",
        "signal_summary",
        "risk_state",
    ):
        check(f"snapshot has {key}", key in state)

    # "Is it trading right now" must be answerable by reading, never by
    # inferring from flag names. It was inferred once and got it wrong.
    live = state["what_is_actually_live"]

    for key in ("equity_entries", "options_entries", "options_exits",
                "equity_exits", "money"):
        check(f"live status covers {key}", key in live)

    check(
        "every live status says PAUSED, LIVE, OFF or names the mechanism",
        all(
            any(word in text for word in
                ("PAUSED", "LIVE", "OFF", "PAPER", "REAL MONEY",
                 "BROKER-SIDE", "ENABLED"))
            for text in live.values()
        ),
        str(live),
    )

    check(
        "exits are reported separately from entries",
        "options_exits" in live and "ALWAYS LIVE" in live["options_exits"],
        live.get("options_exits", ""),
    )

    import lockbot_config as _cfg

    if getattr(_cfg, "OPTIONS_SHADOW_MODE", False):
        check(
            "shadow mode is described as no trading, not paper trading",
            "no trading" in live["options_entries"].lower(),
            live["options_entries"],
        )

    check(
        "snapshot serializes to JSON",
        isinstance(json.dumps(state, default=str), str),
    )

    shadow = state["shadow_summary"]
    check("shadow summary counts rows", shadow["total_logged"] >= 0)

    if shadow["resolved"]:
        check(
            "win rate is a percentage",
            0 <= shadow["win_rate_percent"] <= 100,
            str(shadow["win_rate_percent"]),
        )

    print()
    print("Shadow breakdown")

    for field in ("regime", "volume_ratio", "taken"):
        result = shadow_breakdown(field)
        check(f"groups by {field}", isinstance(result, dict) and bool(result))

    volume = shadow_breakdown("volume_ratio")

    if "_breakeven_win_rate_percent" in volume:
        check(
            "breakeven rate reflects 2:1 brackets",
            volume["_breakeven_win_rate_percent"] == 33.3,
        )

    print()
    print("Trading guardrails")

    original_confirm = CONFIRM

    READ_ONLY = True
    CONFIRM = lambda *_: True  # noqa: E731 - would approve if reached
    refusal = _guard("TEST", "test")
    check("read-only blocks before confirmation", refusal is not None and "read-only" in refusal)

    READ_ONLY = False
    CONFIRM = lambda *_: False  # noqa: E731
    refusal = _guard("TEST", "test")
    check("a declined confirmation refuses", refusal is not None and "declined" in refusal)

    CONFIRM = lambda *_: True  # noqa: E731
    refusal = _guard("TEST", "test")
    check(
        "an approved confirmation permits",
        refusal is None or "kill switch" in refusal,
        str(refusal),
    )

    check(
        "default confirm handler refuses",
        _default_confirm("TEST", "test") is False,
    )

    CONFIRM = original_confirm
    READ_ONLY = False

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All brain checks passed.")
    return 0


def main() -> int:
    global READ_ONLY, CONFIRM, SPEAK_REPLIES, VOICE_INPUT, WAKE_WORD

    parser = argparse.ArgumentParser(description="LOCKBOT's natural-language layer.")
    parser.add_argument("--analyze", action="store_true", help="run one analysis pass")
    parser.add_argument("--chat", action="store_true", help="interactive session")
    parser.add_argument("--ask", metavar="QUESTION", help="ask one question")
    parser.add_argument(
        "--brief",
        action="store_true",
        help="short briefing, pushed to your phone",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="with --brief, print the briefing without sending it",
    )
    parser.add_argument("--self-test", action="store_true", help="offline checks")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="disable every trading tool for this session",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="speak replies out loud",
    )
    parser.add_argument(
        "--listen",
        action="store_true",
        help="push-to-talk voice input (implies --voice)",
    )
    parser.add_argument(
        "--wake",
        action="store_true",
        help='voice activated — just say "LockBot" (implies --listen)',
    )

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    READ_ONLY = args.read_only
    SPEAK_REPLIES = args.voice or args.listen or args.wake
    VOICE_INPUT = args.listen or args.wake
    WAKE_WORD = args.wake
    CONFIRM = _cli_confirm

    if args.analyze:
        analyze()
        return 0

    if args.brief:
        # Blocking here: this process exits straight after, and a
        # background thread would have its audio cut off mid-sentence.
        _say(brief(send=not args.no_push), block=True)
        return 0

    if args.ask:
        print(ask(args.ask))
        return 0

    if args.chat:
        chat()
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
