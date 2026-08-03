"""
lockbot_hud.py  --  a live heads-up display for LOCKBOT  (v1.0)

WHAT THIS IS
    A full-screen status board showing what LOCKBOT is actually doing:
    equity, open positions, module health, day-trade headroom, and the
    shadow record. Meant to sit on a spare monitor.

HOW IT AVOIDS A WEB SERVER
    The data is baked into the HTML at generation time rather than
    fetched at load time. Browsers block fetch() from file:// URLs, so a
    page that reads a sibling JSON file would need a local HTTP server —
    an open port on the machine that trades, which is not a trade worth
    making for a wallpaper.

    Instead: --watch regenerates the file on an interval and the page
    reloads itself. No server, no port, no dependency.

WHAT IT IS NOT
    Read-only, like everything else outside market_scanner.py. It renders
    state and touches nothing.

USAGE
    python lockbot_hud.py              write lockbot_hud.html once
    python lockbot_hud.py --watch      keep it refreshed (Ctrl+C to stop)
    python lockbot_hud.py --open       write it and open it in a browser
    python lockbot_hud.py --interval 15
    python lockbot_hud.py --self-test  offline checks

    Then press F11 in the browser for full screen.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

PROJECT_FOLDER = Path(__file__).resolve().parent
OUTPUT_FILE = PROJECT_FOLDER / "lockbot_hud.html"

DEFAULT_INTERVAL = 30


# ---------------------------------------------------------------------------
# Status vocabulary
#
# Status colour is RESERVED — these four states and nothing else, never
# recycled as decoration. Every one of them also carries a text label in
# the markup, because colour alone is not an accessible signal and a
# glance across a dark room is exactly the case where hue is least
# reliable.
# ---------------------------------------------------------------------------

STATUS_GOOD = "good"
STATUS_WARN = "warn"
STATUS_CRIT = "crit"
STATUS_IDLE = "idle"


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _signed_money(value: float) -> str:
    """
    Format a signed amount as -$1.36, not $-1.36.

    The naive f"${value:,.2f}" puts the minus inside the currency, which
    reads as a typo at a glance — and a glance is all this display gets.
    """

    sign = "+" if value >= 0 else "-"

    return f"{sign}{_money(abs(value))}"


def fetch_equity_history(points: int = 40) -> list[float]:
    """
    Recent equity values for the sparkline.

    A single series with a title above it, so it needs no legend and no
    axis — the number beside it carries the scale. Returns [] when the
    broker is unreachable, and the sparkline is simply omitted.
    """

    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest

        from rearm_brackets import _client

        history = _client().get_portfolio_history(
            GetPortfolioHistoryRequest(period="1M", timeframe="1D")
        )

        values = [float(v) for v in (history.equity or []) if v]

        return values[-points:]

    except Exception:
        return []


def fetch_live_positions() -> dict[str, dict]:
    """
    Position marks and their live bracket levels, in one broker round trip.

    Current price is what makes the progress track meaningful: knowing a
    position sits 80% of the way to its stop is a different fact from
    knowing the stop exists.
    """

    try:
        from rearm_brackets import _client, flatten_orders, open_orders_with_legs
        from position_filters import equity_positions, option_positions

        client = _client()
        raw = client.get_all_positions()
        orders = flatten_orders(open_orders_with_legs(client))

        result: dict[str, dict] = {}

        for position in equity_positions(raw):
            symbol = str(position.symbol).upper()

            mine = [o for o in orders if str(o.symbol).upper() == symbol]

            stop = next(
                (float(o.stop_price) for o in mine if o.stop_price is not None), None
            )
            target = next(
                (float(o.limit_price) for o in mine if o.limit_price is not None), None
            )

            result[symbol] = {
                "kind": "equity",
                "qty": position.qty,
                "entry": float(position.avg_entry_price),
                "current": float(position.current_price or 0),
                "pl": float(position.unrealized_pl or 0),
                "pl_percent": float(position.unrealized_plpc or 0) * 100,
                "stop": stop,
                "target": target,
                "protected": stop is not None,
            }

        for position in option_positions(raw):
            symbol = str(position.symbol).upper()

            result[symbol] = {
                "kind": "option",
                "qty": position.qty,
                "entry": float(position.avg_entry_price),
                "current": float(position.current_price or 0),
                "pl": float(position.unrealized_pl or 0),
                "pl_percent": float(position.unrealized_plpc or 0) * 100,
                "stop": None,
                "target": None,
                # Options have no broker-side bracket on Alpaca.
                # options_manager.py holds the stop in software.
                "protected": True,
            }

        return result

    except Exception:
        return {}


def track_position(entry: float, current: float, stop: float | None,
                   target: float | None) -> dict | None:
    """
    Where price sits between the stop and the target, as 0..1.

    None when there is no bracket to measure against — an unprotected
    position has no track, which is itself the point.
    """

    if stop is None or target is None or target <= stop:
        return None

    span = target - stop
    position = (current - stop) / span

    return {
        "progress": max(0.0, min(1.0, position)),
        "entry_at": max(0.0, min(1.0, (entry - stop) / span)),
        "to_stop_percent": (current - stop) / current * 100 if current else 0,
        "to_target_percent": (target - current) / current * 100 if current else 0,
    }


def fetch_protection() -> dict[str, bool]:
    """
    Ask the broker which positions actually have a working stop.

    The pending-trades registry records that LOCKBOT *registered* a
    bracket, not that one is *live* — those diverged on 2026-07-29 when
    the legs on NVO and LVS were cancelled while both rows stayed in the
    file. A display that reads the registry alone would have shown two
    unprotected positions as fine, which is worse than showing nothing.

    Returns {} when the broker cannot be reached, which the caller
    renders as "unknown" rather than as "protected".
    """

    try:
        from rearm_brackets import _client, classify_orders, open_orders_with_legs
        from position_filters import equity_positions

        client = _client()

        # nested=True, or an OCO parent hides its stop leg and every
        # protected position reads as unprotected.
        orders = open_orders_with_legs(client)

        return {
            str(position.symbol).upper():
                classify_orders(orders, str(position.symbol))["has_stop"]
            for position in equity_positions(client.get_all_positions())
        }

    except Exception:
        return {}


def build_view(state: dict, protection: dict[str, bool] | None = None) -> dict:
    """
    Reduce the raw state snapshot to exactly what the HUD renders.

    Keeping this separate from the markup means the decisions — what
    counts as a warning, what the headline number is — are testable
    without parsing HTML.
    """

    scanner = state.get("scanner_state", {}) or {}
    risk = state.get("risk_state", {}) or {}
    config = state.get("configuration", {}) or {}
    modules = state.get("module_health", {}) or {}
    shadow = state.get("shadow_summary", {}) or {}
    equity_positions = state.get("equity_positions_tracked", {}) or {}
    option_positions = state.get("option_positions_tracked", {}) or {}
    pending = state.get("pending_equity_trades", []) or []

    equity = float(scanner.get("account_equity", 0.0) or 0.0)
    daily_pnl = float(scanner.get("daily_pnl", 0.0) or 0.0)
    daily_pnl_percent = float(scanner.get("daily_pnl_percent", 0.0) or 0.0) * 100

    # A position with no registered bracket is the condition LOCKBOT is
    # built to never be in, so the HUD calls it out rather than burying
    # it in a colour.
    registered = {
        str(row.get("symbol", "")).upper() for row in pending
    }

    positions = []

    protection = protection if protection is not None else {}

    for symbol, data in equity_positions.items():
        gain = float(data.get("highest_gain_percent", 0.0) or 0.0)
        key = symbol.upper()

        # Three states, not two. "Unknown" is what an unreachable broker
        # gets — it must never collapse into "protected".
        if key in protection:
            guarded = "yes" if protection[key] else "no"
        else:
            guarded = "unknown"

        positions.append(
            {
                "symbol": symbol,
                "kind": "equity",
                "entry": float(data.get("entry_price", 0.0) or 0.0),
                "peak_gain": gain,
                "trailing": bool(data.get("trailing_stop_active")),
                "tracked": key in registered,
                "guarded": guarded,
            }
        )

    for _, data in option_positions.items():
        positions.append(
            {
                "symbol": f"{data.get('underlying', '?')} {data.get('strategy', '')}",
                "kind": "option",
                "entry": float(data.get("entry_debit", 0.0) or 0.0),
                "peak_gain": 0.0,
                "trailing": False,
                "tracked": True,
                # Options never have a broker-side bracket — Alpaca does
                # not offer one. options_manager.py holds the stop in
                # software, so this is accurate rather than alarming.
                "guarded": "software",
            }
        )

    module_rows = []

    for name, data in sorted(modules.items()):
        raw = str(data.get("status", "") or "").upper()

        if raw == "HEALTHY":
            status = STATUS_GOOD
        elif raw in {"DEGRADED", "STARTING"}:
            status = STATUS_WARN
        elif raw == "CRITICAL":
            status = STATUS_CRIT
        else:
            status = STATUS_IDLE

        module_rows.append(
            {
                "name": name.replace("_", " "),
                "status": status,
                "label": raw or "UNKNOWN",
            }
        )

    unguarded = [p for p in positions if p.get("guarded") == "no"]

    if risk.get("kill_switch_active"):
        overall = STATUS_CRIT
        overall_label = "KILL SWITCH ACTIVE"
    elif any(row["status"] == STATUS_CRIT for row in module_rows):
        overall = STATUS_CRIT
        overall_label = "MODULE CRITICAL"
    elif unguarded:
        # The loudest thing this display can say. A position with no stop
        # is the one state the whole architecture exists to prevent.
        overall = STATUS_CRIT
        overall_label = f"{len(unguarded)} POSITION(S) UNPROTECTED"
    elif any(not p["tracked"] for p in positions):
        overall = STATUS_WARN
        overall_label = "POSITION UNTRACKED"
    elif any(row["status"] == STATUS_WARN for row in module_rows):
        overall = STATUS_WARN
        overall_label = "DEGRADED"
    elif scanner.get("market_open"):
        overall = STATUS_GOOD
        overall_label = "TRADING"
    else:
        overall = STATUS_IDLE
        overall_label = "MARKET CLOSED"

    win_rate = shadow.get("win_rate_percent")

    return {
        "equity": equity,
        "equity_text": _money(equity),
        "daily_pnl": daily_pnl,
        "daily_pnl_text": _signed_money(daily_pnl),
        "daily_pnl_percent_text": f"{daily_pnl_percent:+.2f}%",
        "pnl_status": STATUS_GOOD if daily_pnl >= 0 else STATUS_WARN,
        "buying_power_text": _money(float(scanner.get("buying_power", 0.0) or 0.0)),
        "market_open": bool(scanner.get("market_open")),
        "overall": overall,
        "overall_label": overall_label,
        "trades_today": int(risk.get("trades_submitted_today", 0) or 0),
        "max_trades": config.get("max_trades_per_day", 0),
        "max_positions": config.get("max_open_positions", 0),
        "profile": str(config.get("account_profile", "?")).upper(),
        "options_mode": (
            "SHADOW" if config.get("options_shadow_mode") else "LIVE"
        ) if config.get("options_enabled") else "OFF",
        "positions": positions,
        "modules": module_rows,
        "symbols_scanned": int(scanner.get("symbols_scanned", 0) or 0),
        "shadow_resolved": int(shadow.get("resolved", 0) or 0),
        "shadow_win_rate": win_rate,
        "shadow_win_text": f"{win_rate}%" if win_rate is not None else "—",
        "shadow_status": (
            STATUS_IDLE
            if win_rate is None
            else STATUS_GOOD if win_rate >= 33.3 else STATUS_WARN
        ),
        "generated": datetime.now().astimezone().strftime("%H:%M:%S"),
        "generated_date": datetime.now().astimezone().strftime("%a %d %b %Y"),
    }


# ---------------------------------------------------------------------------
# Markup
# ---------------------------------------------------------------------------

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="__INTERVAL__">
<title>LOCKBOT</title>
<style>
  /* Dark by deliberate choice — this is a screensaver, not a document.
     There is no light mode because the design commits to one look. */
  :root {
    --surface:   #05070d;
    --panel:     #0b1018;
    --line:      #16202e;

    /* Ink. Text always wears these, never a status hue — a coloured
       dot beside a label carries the state, the label stays readable. */
    --ink:       #e6f4f8;
    --ink-dim:   #8fa6b4;
    --ink-faint: #4a5c68;

    /* Reserved status palette. Four states, never reused as decoration. */
    --good: #22d3ee;
    --warn: #fbbf24;
    --crit: #f87171;
    --idle: #64748b;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  html, body { height: 100%; }

  body {
    background:
      radial-gradient(ellipse 120% 80% at 50% 0%, #0d1826 0%, transparent 60%),
      radial-gradient(ellipse 100% 60% at 50% 100%, #0a1420 0%, transparent 60%),
      var(--surface);
    color: var(--ink);
    font-family: ui-monospace, "Cascadia Mono", "Consolas", monospace;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    letter-spacing: 0.04em;
  }

  /* Faint scan grid. Low contrast on purpose — it should read as
     texture, never compete with a number. */
  body::before {
    content: "";
    position: fixed; inset: 0;
    background-image:
      linear-gradient(rgba(34,211,238,.045) 1px, transparent 1px),
      linear-gradient(90deg, rgba(34,211,238,.045) 1px, transparent 1px);
    background-size: 44px 44px;
    pointer-events: none;
  }

  body::after {
    content: "";
    position: fixed; left: 0; right: 0; height: 140px;
    background: linear-gradient(180deg, transparent, rgba(34,211,238,.05), transparent);
    animation: sweep 9s linear infinite;
    pointer-events: none;
  }
  @keyframes sweep { from { top: -140px; } to { top: 100%; } }

  .frame {
    position: relative;
    width: min(1500px, 94vw);
    max-height: 94vh;
    padding: 30px 38px;
    border: 1px solid var(--line);
    border-radius: 4px;
    background: linear-gradient(180deg, rgba(11,16,24,.92), rgba(5,7,13,.92));
    box-shadow: 0 0 90px rgba(34,211,238,.06), inset 0 0 90px rgba(0,0,0,.5);
  }

  /* Corner ticks — cheap HUD framing, pure decoration. */
  .frame::before, .frame::after {
    content: ""; position: absolute; width: 22px; height: 22px;
    border-color: var(--good); border-style: solid; opacity: .5;
  }
  .frame::before { top: -1px; left: -1px; border-width: 2px 0 0 2px; }
  .frame::after  { bottom: -1px; right: -1px; border-width: 0 2px 2px 0; }

  header {
    display: flex; align-items: baseline; gap: 18px;
    border-bottom: 1px solid var(--line);
    padding-bottom: 14px; margin-bottom: 22px;
  }
  .wordmark { font-size: 20px; font-weight: 700; letter-spacing: .34em; }
  .wordmark span { color: var(--good); }
  .tagline { color: var(--ink-faint); font-size: 11px; letter-spacing: .2em; }
  .clock { margin-left: auto; text-align: right; color: var(--ink-dim); font-size: 12px; }
  .clock b { display:block; color: var(--ink); font-size: 22px; font-weight: 600; }

  /* Status chip: dot + WORD. Never colour alone. */
  .chip {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 5px 12px; border-radius: 3px; font-size: 11px;
    letter-spacing: .18em; border: 1px solid currentColor;
  }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor;
         box-shadow: 0 0 9px currentColor; animation: pulse 2.4s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }

  .good { color: var(--good); }
  .warn { color: var(--warn); }
  .crit { color: var(--crit); }
  .idle { color: var(--idle); }

  .top { display: grid; grid-template-columns: 1.15fr 1fr; gap: 34px; align-items: center; }

  .hero-label { color: var(--ink-faint); font-size: 11px; letter-spacing: .26em; margin-bottom: 6px; }
  .hero { font-size: clamp(52px, 7.4vw, 96px); font-weight: 300; line-height: 1;
          text-shadow: 0 0 40px rgba(34,211,238,.28); }
  .hero-sub { margin-top: 12px; display: flex; gap: 26px; align-items: baseline; }
  .delta { font-size: 26px; font-weight: 500; }
  .delta-pct { font-size: 15px; color: var(--ink-dim); }

  /* Reactor rings. Decoration, and the only animation that implies
     "running" without claiming a number. */
  .reactor { position: relative; width: 190px; height: 190px; margin: 0 auto; }
  .ring { position: absolute; inset: 0; border-radius: 50%; border: 1px solid transparent; }
  .r1 { border-top-color: var(--good); border-right-color: var(--good);
        opacity:.75; animation: spin 7s linear infinite; }
  .r2 { inset: 22px; border-bottom-color: var(--good); border-left-color: var(--good);
        opacity:.45; animation: spin 11s linear infinite reverse; }
  .r3 { inset: 44px; border-top-color: var(--good); opacity:.3;
        animation: spin 5s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .core { position: absolute; inset: 66px; border-radius: 50%;
          background: radial-gradient(circle, rgba(34,211,238,.34), transparent 70%);
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          text-align: center; }
  .core b { font-size: 27px; font-weight: 600; }
  .core small { font-size: 9px; color: var(--ink-dim); letter-spacing: .16em; }

  .tiles { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin: 26px 0; }
  .tile { border: 1px solid var(--line); border-radius: 3px; padding: 13px 15px;
          background: rgba(255,255,255,.012); }
  .tile .k { color: var(--ink-faint); font-size: 10px; letter-spacing: .18em; margin-bottom: 7px; }
  .tile .v { font-size: 21px; font-weight: 600; }
  .tile .u { color: var(--ink-dim); font-size: 11px; margin-left: 3px; }

  .cols { display: grid; grid-template-columns: 1.5fr 1fr; gap: 26px; }

  /* ---- Guard row -------------------------------------------------------
     The three facts worth interrupting someone for: is the options stop
     armed, how much of the PDT allowance is left, and does the strategy
     actually have an edge. Angular cut corners rather than rounded ones —
     the whole visual language is instrument panel, not web app. */
  .guard { display: grid; grid-template-columns: repeat(3, 1fr);
           gap: 14px; margin: 26px 0 22px; }
  .guard-cell {
    position: relative; padding: 16px 18px;
    background: linear-gradient(180deg, rgba(20,38,50,.55), rgba(10,20,28,.35));
    border: 1px solid rgba(34,229,255,.18);
    clip-path: polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%, 14px 100%, 0 calc(100% - 14px));
  }
  .guard-cell::before {
    content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 2px;
    background: var(--ink-faint);
  }
  .guard-cell .k { display: block; font-size: 9px; letter-spacing: .3em;
                   color: var(--ink-faint); margin-bottom: 8px; }
  .guard-cell .v { display: block; font-size: 20px; letter-spacing: .08em;
                   color: var(--ink); }
  .guard-cell .v em { font-style: normal; font-size: 12px;
                      color: var(--ink-faint); letter-spacing: .1em; }

  .guard-cell.good::before { background: var(--good); box-shadow: 0 0 14px var(--good); }
  .guard-cell.good .v      { color: var(--good); text-shadow: 0 0 18px rgba(34,229,255,.45); }
  .guard-cell.warn::before { background: var(--warn); box-shadow: 0 0 14px var(--warn); }
  .guard-cell.warn .v      { color: var(--warn); }
  .guard-cell.bad::before  { background: var(--bad); box-shadow: 0 0 18px var(--bad); }
  .guard-cell.bad .v       { color: var(--bad); text-shadow: 0 0 20px rgba(255,60,90,.5);
                             animation: alarm 1.1s ease-in-out infinite; }
  @keyframes alarm { 0%,100% { opacity: 1; } 50% { opacity: .45; } }

  /* ---- PDT pips ---- */
  .pips { display: flex; gap: 6px; margin-top: 12px; }
  .pips .pip { width: 100%; height: 4px; background: rgba(120,150,170,.22);
               clip-path: polygon(0 0, 100% 0, calc(100% - 3px) 100%, 0 100%); }
  .pips .pip.on { background: var(--warn); box-shadow: 0 0 10px var(--warn); }

  /* ---- Edge gauge ----
     Breakeven is a tick on the scale, not a sentence beneath it. Sitting
     left of that tick is the entire strategic problem, visible without
     reading a number. */
  .gauge { position: relative; height: 6px; margin-top: 12px;
           background: rgba(120,150,170,.16); overflow: visible; }
  .gauge-fill { position: absolute; left: 0; top: 0; bottom: 0;
                transition: width .6s ease; }
  .gauge-fill.good { background: var(--good); box-shadow: 0 0 12px var(--good); }
  .gauge-fill.bad  { background: var(--bad);  box-shadow: 0 0 12px var(--bad); }
  .gauge-mark { position: absolute; top: -4px; bottom: -4px; width: 2px;
                background: var(--ink); opacity: .85; }
  .gauge-mark::after { content: ""; position: absolute; top: -4px; left: -2px;
                       border-left: 3px solid transparent;
                       border-right: 3px solid transparent;
                       border-top: 4px solid var(--ink); }
  .gauge-empty { font-size: 10px; letter-spacing: .25em; color: var(--ink-faint); }

  .positions { margin-top: 4px; }

  /* ---- Radar sweep behind the orb ----
     Ties the speech display to the instrument language of the rest of the
     panel. Purely decorative — it carries no data and is not pretending to. */
  .orb-wrap { position: relative; width: 200px; height: 200px; margin: 14px auto 0; }
  .orb-wrap::before {
    content: ""; position: absolute; inset: 8px; border-radius: 50%;
    border: 1px dashed rgba(34,229,255,.12);
  }
  .orb-wrap::after {
    content: ""; position: absolute; inset: 8px; border-radius: 50%;
    background: conic-gradient(from 0deg, rgba(34,229,255,.16), transparent 70deg);
    animation: sweep-radar 4.5s linear infinite;
  }
  @keyframes sweep-radar { to { transform: rotate(360deg); } }
  .orb-wrap .wave { position: relative; z-index: 2; margin: 0; }

  /* Reticle ticks around the orb, every 30 degrees. */
  .orb-wrap .tick { position: absolute; left: 50%; top: 50%; width: 1px;
                    height: 50%; transform-origin: top center;
                    background: linear-gradient(180deg, rgba(34,229,255,.30) 0 6px, transparent 6px); }

  /* Corner brackets on the frame. Pure decoration, but it is the single
     cheapest thing that makes a page read as an instrument rather than a
     document. */
  .frame::before, .frame::after {
    content: ""; position: fixed; width: 46px; height: 46px;
    border-color: rgba(34,229,255,.5); pointer-events: none; z-index: 5;
  }
  .frame::before { top: 16px; left: 16px; border-top: 2px solid; border-left: 2px solid; }
  .frame::after { bottom: 16px; right: 16px; border-bottom: 2px solid; border-right: 2px solid; }
  h2 { font-size: 10px; letter-spacing: .26em; color: var(--ink-faint);
       margin-bottom: 11px; font-weight: 500; }

  /* Position cards with a stop-to-target track. The track is the point:
     "80% of the way to its stop" is information a price alone is not. */
  .pos { padding: 12px 14px; border: 1px solid var(--line);
         border-left: 2px solid var(--good); border-radius: 3px;
         margin-bottom: 10px; background: rgba(255,255,255,.014); }
  .pos-head { display: flex; align-items: baseline; gap: 10px; }
  .pos-head .sym { font-weight: 700; font-size: 15px; letter-spacing: .06em; }
  .pos-head .kind { font-size: 9px; color: var(--ink-faint);
                    letter-spacing: .18em; text-transform: uppercase; }
  .pos-head .pl { margin-left: auto; font-size: 13px; font-weight: 600; }
  .pos-prices { display: flex; gap: 18px; margin: 7px 0 10px;
                font-size: 11px; color: var(--ink-dim); }
  .pos-prices b { color: var(--ink); font-weight: 600; }

  .track { position: relative; height: 6px; border-radius: 3px;
           background: linear-gradient(90deg,
             rgba(248,113,113,.30), rgba(100,116,139,.18), rgba(34,211,238,.30));
           overflow: visible; }
  .track-fill { position: absolute; inset: 0 auto 0 0; border-radius: 3px;
                background: rgba(34,211,238,.22); }
  .track-entry { position: absolute; top: -3px; width: 1px; height: 12px;
                 background: var(--ink-faint); }
  .track-now { position: absolute; top: -4px; width: 3px; height: 14px;
               border-radius: 2px; background: var(--ink);
               box-shadow: 0 0 8px var(--good); transform: translateX(-1px); }
  .track-ends { display: flex; justify-content: space-between; margin-top: 6px;
                font-size: 9.5px; letter-spacing: .1em; }
  .track-ends .muted { color: var(--ink-faint); }

  .note { font-size: 10px; letter-spacing: .12em; color: var(--ink-dim);
          text-transform: uppercase; }
  .note.crit { color: var(--crit); }

  .spark { width: 100%; height: 44px; display: block; margin-top: 10px; }

  .panel { border: 1px solid var(--line); border-radius: 3px; padding: 13px 15px;
           background: rgba(255,255,255,.012); margin-bottom: 12px; }
  .kv { display: flex; font-size: 11px; padding: 4px 0; }
  .kv .k { color: var(--ink-faint); letter-spacing: .1em; }
  .kv .v { margin-left: auto; color: var(--ink); font-weight: 600; }

  .pos-legacy { display: flex; align-items: center; gap: 14px; padding: 11px 13px;
         border: 1px solid var(--line); border-left: 2px solid var(--good);
         border-radius: 3px; margin-bottom: 8px; background: rgba(255,255,255,.012); }
  .pos.untracked { border-left-color: var(--warn); }
  .pos.unguarded { border-left-color: var(--crit); background: rgba(248,113,113,.07); }
  .pos.unguarded .right { color: var(--crit); }
  .pos .sym { font-weight: 700; font-size: 15px; min-width: 108px; }
  .pos .meta { color: var(--ink-dim); font-size: 11px; }
  .pos .right { margin-left: auto; text-align: right; font-size: 11px; }

  .mod { display: flex; align-items: center; gap: 10px; padding: 7px 0;
         border-bottom: 1px solid rgba(22,32,46,.55); font-size: 11px; }
  .mod .nm { color: var(--ink-dim); }
  .mod .st { margin-left: auto; letter-spacing: .14em; font-size: 10px; }

  .empty { color: var(--ink-faint); font-size: 12px; padding: 13px 0; }

  footer { margin-top: 22px; padding-top: 13px; border-top: 1px solid var(--line);
           display: flex; gap: 22px; color: var(--ink-faint); font-size: 10px;
           letter-spacing: .14em; }
  footer .right { margin-left: auto; }

  @media (max-width: 1080px) {
    .top { grid-template-columns: 1fr; }
    .reactor { display: none; }
    .tiles { grid-template-columns: repeat(2, 1fr); }
    .cols { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="frame">

  <header>
    <div class="wordmark">LOCK<span>BOT</span></div>
    <div class="tagline">AUTONOMOUS TRADING SYSTEM</div>
    <div class="clock"><b>__TIME__</b>__DATE__</div>
  </header>

  <div class="top">
    <div>
      <div class="hero-label">ACCOUNT EQUITY</div>
      <div class="hero">__EQUITY__</div>
      <div class="hero-sub">
        <span class="delta __PNL_STATUS__">__PNL__</span>
        <span class="delta-pct">__PNL_PCT__ today</span>
        <span class="chip __OVERALL__"><i class="dot"></i>__OVERALL_LABEL__</span>
      </div>
    </div>
    <div class="reactor">
      <div class="ring r1"></div><div class="ring r2"></div><div class="ring r3"></div>
      <div class="core __OVERALL__">
        <b>__POS_COUNT__</b><small>OPEN</small>
      </div>
    </div>
  </div>

  __SPARKLINE__

  <!-- Everything below is something you would ACT on. Universe size,
       incident counts, buying power, open hypotheses and the profile name
       were all removed: true, occasionally interesting, and never the
       reason anyone looks at a HUD mid-session. -->

  <div class="guard">
    <div class="guard-cell __OPTIONS_STOP_STATUS__">
      <span class="k">OPTIONS STOP</span>
      <span class="v">__OPTIONS_STOP__</span>
    </div>
    <div class="guard-cell">
      <span class="k">DAY TRADES · PDT</span>
      <span class="v">__TRADES__<em>/__MAX_TRADES__</em></span>
      <div class="pips">__PDT_PIPS__</div>
    </div>
    <div class="guard-cell __SHADOW_STATUS__">
      <span class="k">EDGE vs BREAKEVEN</span>
      <span class="v">__SHADOW_WIN__<em> / 33.3%</em></span>
      __EDGE_BAR__
    </div>
  </div>

  <section class="positions">
    <h2>OPEN POSITIONS</h2>
    __POSITIONS__
  </section>

  <footer>
    <span class="right">__SHADOW_N__ RESOLVED &nbsp;·&nbsp; F11 FULL SCREEN</span>
  </footer>

</div>
</body>
</html>"""


_LIVE_STYLE = """
  /* Voice reactivity. The reactor is the focal point, so it carries the
     state — colour plus speed, because motion reads from across a room
     where a hue shift alone does not. */
  body[data-voice="waiting"]  .ring { border-color: transparent; }
  body[data-voice="waiting"]  .r1 { border-top-color: var(--idle); border-right-color: var(--idle); }
  body[data-voice="waiting"]  .r2 { border-bottom-color: var(--idle); border-left-color: var(--idle); }
  body[data-voice="waiting"]  .r3 { border-top-color: var(--idle); }

  body[data-voice="listening"] .r1 { animation-duration: 1.6s; border-top-color: var(--good); border-right-color: var(--good); }
  body[data-voice="listening"] .r2 { animation-duration: 2.4s; }
  body[data-voice="listening"] .r3 { animation-duration: 1.1s; }
  body[data-voice="listening"] .core { animation: breathe 1.1s ease-in-out infinite; }

  body[data-voice="thinking"] .r1 { animation-duration: .6s; border-top-color: var(--warn); border-right-color: var(--warn); }
  body[data-voice="thinking"] .r2 { animation-duration: .9s; border-bottom-color: var(--warn); border-left-color: var(--warn); }
  body[data-voice="thinking"] .r3 { animation-duration: .45s; border-top-color: var(--warn); }

  body[data-voice="speaking"] .r1 { animation-duration: 3s; }
  body[data-voice="speaking"] .core { animation: breathe .5s ease-in-out infinite; }

  @keyframes breathe {
    0%,100% { transform: scale(1);    opacity: .85; }
    50%     { transform: scale(1.12); opacity: 1; }
  }

  /* Speech orb.
     A circle whose radius is modulated by the WordBoundary timings
     lockbot_voice.py publishes, so it deforms on the words actually being
     spoken. Always visible — it breathes slowly when idle rather than
     appearing and vanishing, which reads as a system that is on. */
  .wave { display: block; margin: 14px auto 0; width: 200px; height: 200px; }

  .voice-line { text-align: center; margin-top: 10px; font-size: 10px;
                letter-spacing: .22em; color: var(--ink-faint); min-height: 14px; }
  .voice-line.active { color: var(--good); }

  .heard { text-align: center; margin-top: 6px; font-size: 12px;
           color: var(--ink-dim); min-height: 16px; font-style: italic; }
"""

_LIVE_SCRIPT = """
<script>
// Poll the loopback server. One request a second is nothing, and it keeps
// the display honest — a reload would restart every animation.
const LABELS = {
  idle:      "",
  waiting:   "LISTENING FOR WAKE WORD",
  listening: "LISTENING",
  thinking:  "THINKING",
  speaking:  "SPEAKING"
};

let lastEquity = null;

// ---------------------------------------------------------------------------
// Speech waveform
//
// lockbot_voice.py publishes edge-tts WordBoundary events: for every word,
// when it starts and how long it lasts, in seconds from the beginning of the
// utterance. Those are the synthesiser's own timings for the audio actually
// playing, so a peak here is a word being said rather than a decorative
// pulse. `at` is when playback began, which is what anchors the playhead.
//
// The canvas runs on requestAnimationFrame rather than the one-second poll —
// speech moves far faster than the numbers do, and driving it off the poll
// would produce a bar chart that lurched once a second.
// ---------------------------------------------------------------------------
let speech = { words: [], duration: 0, startedAt: 0, active: false };

function updateSpeech(v) {
  const speaking = v && v.state === "speaking";

  if (speaking && Array.isArray(v.words) && v.words.length) {
    // Only re-anchor on a genuinely new utterance, otherwise every poll
    // would restart the playhead and the wave would never advance.
    if (v.at !== speech.startedAt) {
      speech = {
        words: v.words,
        duration: v.duration || 0,
        startedAt: v.at,
        active: true,
        localStart: performance.now() / 1000
      };
    }
  } else if (!speaking) {
    speech.active = false;
  }
}

// Amplitude at a moment: how loudly, roughly, is it speaking right now.
// A word contributes a smooth hump across its own duration, scaled a little
// by length so "unaffected" reads bigger than "is". Silence between words
// falls to zero, which is what makes it look like speech rather than a
// oscillator.
function amplitudeAt(t) {
  let total = 0;

  for (let i = 0; i < speech.words.length; i++) {
    const [start, dur, chars] = speech.words[i];

    if (t < start - 0.05 || t > start + dur + 0.05) continue;

    const span = Math.max(dur, 0.06);
    const phase = (t - start) / span;
    if (phase < -0.2 || phase > 1.2) continue;

    const hump = Math.sin(Math.max(0, Math.min(1, phase)) * Math.PI);
    total += hump * (0.55 + Math.min(chars, 12) / 24);
  }

  return Math.min(1, total);
}

// Smoothed level, so the orb swells and settles rather than snapping
// between frames. Speech amplitude is spiky at word boundaries; a circle
// that tracked it exactly would flicker instead of breathe.
let level = 0;

function drawWave() {
  const canvas = document.getElementById("wave");

  if (canvas) {
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    const cx = w / 2;
    const cy = h / 2;
    ctx.clearRect(0, 0, w, h);

    const now = performance.now() / 1000;
    const elapsed = speech.active ? now - speech.localStart : 0;
    const live = speech.active && elapsed <= speech.duration + 0.4;

    const target = live && elapsed >= 0 ? amplitudeAt(elapsed) : 0;
    // Rises quickly, falls slowly — speech attacks fast and decays.
    level += (target - level) * (target > level ? 0.35 : 0.08);

    const base = Math.min(w, h) * 0.26;
    const swell = base * (1 + level * 0.30);
    const colour = live ? "34,229,255" : "120,150,170";

    // Two faint echo rings that expand with the voice, giving the orb
    // somewhere to push into.
    for (let ring = 1; ring <= 2; ring++) {
      ctx.beginPath();
      ctx.strokeStyle = `rgba(${colour},${(live ? 0.20 : 0.08) / ring})`;
      ctx.lineWidth = 1;
      ctx.arc(cx, cy, swell + ring * (10 + level * 26), 0, Math.PI * 2);
      ctx.stroke();
    }

    // The orb itself. Radius is modulated around the circumference so it
    // deforms rather than merely scaling — three lobes turning at
    // different rates keeps it from looking mechanical.
    ctx.beginPath();
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = `rgba(${colour},${live ? 0.95 : 0.5})`;
    ctx.shadowBlur = live ? 22 : 6;
    ctx.shadowColor = `rgba(${colour},.85)`;

    const STEPS = 180;

    for (let i = 0; i <= STEPS; i++) {
      const a = (i / STEPS) * Math.PI * 2;

      const wobble =
        Math.sin(a * 3 + now * 2.1) * 0.55 +
        Math.sin(a * 5 - now * 1.4) * 0.30 +
        Math.sin(a * 8 + now * 3.2) * 0.15;

      // Idle keeps a slow breath so the circle is never a dead outline.
      const idle = 0.045 * Math.sin(now * 1.6);
      const r = swell * (1 + level * wobble * 0.28 + idle);

      const x = cx + Math.cos(a) * r;
      const y = cy + Math.sin(a) * r;

      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }

    ctx.closePath();
    ctx.stroke();

    // Filled centre, brightening as it speaks.
    const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, swell);
    glow.addColorStop(0, `rgba(${colour},${0.16 + level * 0.34})`);
    glow.addColorStop(1, `rgba(${colour},0)`);
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(cx, cy, swell, 0, Math.PI * 2);
    ctx.fill();

    ctx.shadowBlur = 0;
  }

  requestAnimationFrame(drawWave);
}

requestAnimationFrame(drawWave);

async function tick() {
  try {
    const res = await fetch("/state", { cache: "no-store" });
    const s = await res.json();

    const voice = (s.voice && s.voice.state) || "idle";
    document.body.dataset.voice = voice;
    updateSpeech(s.voice);

    const line = document.getElementById("voiceLine");
    line.textContent = LABELS[voice] || "";
    line.className = "voice-line" + (voice === "idle" ? "" : " active");

    const heard = document.getElementById("heard");
    heard.textContent = (voice === "speaking" || voice === "thinking")
      ? (s.voice.detail || "") : "";

    // Numbers that move, updated in place.
    document.getElementById("equity").textContent = s.equity_text;
    document.getElementById("pnl").textContent = s.daily_pnl_text;
    document.getElementById("pnl").className = "delta " + s.pnl_status;
    document.getElementById("pnlPct").textContent = s.daily_pnl_percent_text + " today";

    const chip = document.getElementById("overall");
    chip.className = "chip " + s.overall;
    chip.innerHTML = '<i class="dot"></i>' + s.overall_label;

    const core = document.getElementById("core");
    core.className = "core " + s.overall;

    if (lastEquity !== null && s.equity_text !== lastEquity) {
      const hero = document.getElementById("equity");
      hero.style.textShadow = "0 0 60px rgba(34,229,255,.7)";
      setTimeout(() => { hero.style.textShadow = ""; }, 600);
    }
    lastEquity = s.equity_text;

  } catch (e) {
    document.body.dataset.voice = "idle";
  }
}

tick();
setInterval(tick, 1000);
</script>
"""


def _sparkline(values: list[float], width: int = 240, height: int = 44) -> str:
    """
    A single-series equity trace as inline SVG.

    One series, so no legend — the label above it names the thing. No
    axis either; the number beside it carries the scale. Two points or
    fewer is not a trend and renders nothing.
    """

    if not values or len(values) < 3:
        return ""

    low = min(values)
    high = max(values)
    span = (high - low) or 1.0

    step = width / (len(values) - 1)

    points = " ".join(
        f"{i * step:.1f},{height - ((v - low) / span) * (height - 6) - 3:.1f}"
        for i, v in enumerate(values)
    )

    rising = values[-1] >= values[0]
    colour = "var(--good)" if rising else "var(--warn)"
    last_x = width
    last_y = height - ((values[-1] - low) / span) * (height - 6) - 3

    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<polyline points="{points}" fill="none" stroke="{colour}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{last_x - 2:.1f}" cy="{last_y:.1f}" r="2.5" fill="{colour}"/>'
        f"</svg>"
    )


def _position_card(position: dict) -> str:
    """One position, with a track showing where price sits stop-to-target."""

    guarded = position["protected"]
    classes = "pos" if guarded else "pos unguarded"

    pl = position["pl"]
    pl_class = "good" if pl >= 0 else "warn"
    pl_text = f"{'+' if pl >= 0 else '-'}${abs(pl):,.2f}"

    header = (
        f'<div class="pos-head">'
        f'<span class="sym">{html.escape(position["symbol"])}</span>'
        f'<span class="kind">{position["kind"]}</span>'
        f'<span class="pl {pl_class}">{pl_text} '
        f'({position["pl_percent"]:+.2f}%)</span>'
        f"</div>"
    )

    prices = (
        f'<div class="pos-prices">'
        f'<span>entry <b>{position["entry"]:.2f}</b></span>'
        f'<span>now <b>{position["current"]:.2f}</b></span>'
        f"</div>"
    )

    track = position.get("track")

    if track:
        pct = track["progress"] * 100
        entry_pct = track["entry_at"] * 100

        body = (
            f'<div class="track">'
            f'<div class="track-fill" style="width:{pct:.1f}%"></div>'
            f'<div class="track-entry" style="left:{entry_pct:.1f}%"></div>'
            f'<div class="track-now" style="left:{pct:.1f}%"></div>'
            f"</div>"
            f'<div class="track-ends">'
            f'<span class="crit">stop {position["stop"]:.2f}</span>'
            f'<span class="muted">{track["to_stop_percent"]:.1f}% away</span>'
            f'<span class="good">target {position["target"]:.2f}</span>'
            f"</div>"
        )
    elif position["kind"] == "option":
        body = '<div class="note">software stop — options_manager holds it</div>'
    else:
        body = '<div class="note crit">NO STOP LOSS AT THE BROKER</div>'

    return f'<div class="{classes}">{header}{prices}{body}</div>'


def render(
    view: dict,
    interval: int = DEFAULT_INTERVAL,
    live: bool = False,
) -> str:
    """Turn the view into a self-contained HTML page."""

    if view["positions"]:
        # Positions arrive from the broker with live marks and bracket
        # levels; the older file-derived shape is still tolerated so the
        # static page and the self-test keep working.
        cards = []

        for position in view["positions"]:
            if "current" in position:
                cards.append(_position_card(position))
                continue

            # File-only shape: no broker mark, so no track. The
            # protection note still has to appear — it is the single most
            # important thing this display says.
            guarded = position.get("guarded", "unknown")

            if guarded == "no":
                note = '<div class="note crit">NO STOP LOSS</div>'
                classes = "pos unguarded"
            elif guarded == "software":
                note = '<div class="note">software stop</div>'
                classes = "pos"
            elif guarded == "unknown":
                note = '<div class="note">protection unknown</div>'
                classes = "pos untracked"
            else:
                note = '<div class="note">bracket live</div>'
                classes = "pos"

            cards.append(
                f'<div class="{classes}">'
                f'<div class="pos-head">'
                f'<span class="sym">{html.escape(str(position["symbol"]))}</span>'
                f"</div>"
                f'<div class="pos-prices"><span>entry '
                f'<b>{position.get("entry", 0):.2f}</b></span></div>'
                f"{note}</div>"
            )

        position_html = "".join(cards)
    else:
        position_html = '<div class="empty">No open positions.</div>'

    if view["modules"]:
        module_html = ""

        for module in view["modules"]:
            module_html += (
                f'<div class="mod {module["status"]}">'
                f'<i class="dot"></i>'
                f'<span class="nm">{html.escape(module["name"])}</span>'
                f'<span class="st">{html.escape(module["label"])}</span>'
                f"</div>"
            )
    else:
        module_html = '<div class="empty">No module heartbeats.</div>'

    # Optional panels — absent when the view came from files alone.
    incidents = view.get("incidents") or {}
    learning = view.get("learning") or {}

    # The options stop, promoted out of the module list.
    #
    # Alpaca has no broker-side stop for options, so options_manager.py
    # running every cycle IS the stop loss for every open contract. Buried
    # as one row among five modules it read like any other service; on a
    # HUD you glance at, the question "is my downside protected right now"
    # deserves to be the thing you cannot miss.
    #
    # It only earns that prominence when contracts are actually open --
    # otherwise it is a status about nothing.
    option_count = sum(
        1 for position in view["positions"] if position.get("kind") == "option"
    )

    stop_module = next(
        (
            module
            for module in view["modules"]
            if "OPTIONS" in module["name"].upper()
            and "MANAGER" in module["name"].upper()
        ),
        None,
    )

    if not option_count:
        options_stop_text = "NO CONTRACTS"
        options_stop_status = "idle"
    elif stop_module is None:
        options_stop_text = "UNKNOWN"
        options_stop_status = "bad"
    elif stop_module["status"] in ("good", "ok", "healthy"):
        options_stop_text = f"ARMED · {option_count}"
        options_stop_status = "good"
    elif stop_module["status"] in ("warn", "degraded"):
        options_stop_text = "DEGRADED"
        options_stop_status = "warn"
    else:
        options_stop_text = "NOT RUNNING"
        options_stop_status = "bad"

    # ---- PDT pips -------------------------------------------------------
    # Three round trips per five business days under $25k. A fraction like
    # "1/3" is read; three pips are SEEN. The limit is small enough that
    # one glyph per trade is honest rather than a bar chart of nothing.
    used = min(int(view["trades_today"]), int(view["max_trades"]))
    pip_html = "".join(
        f'<i class="pip{" on" if n < used else ""}"></i>'
        for n in range(int(view["max_trades"]))
    )

    # ---- Edge gauge -----------------------------------------------------
    # Win rate against the 33.3% breakeven, with breakeven drawn as a tick
    # rather than written underneath. The scale tops out at 60% because
    # nothing above that is plausible here and a 0-100 axis would squash
    # the only region that matters into the left third.
    SCALE = 60.0
    win_rate = view.get("shadow_win_rate")

    if win_rate is None:
        edge_html = '<div class="gauge"><div class="gauge-empty">NO DATA</div></div>'
    else:
        fill = max(0.0, min(float(win_rate), SCALE)) / SCALE * 100
        mark = 33.3 / SCALE * 100
        tone = "good" if float(win_rate) >= 33.3 else "bad"
        edge_html = (
            f'<div class="gauge">'
            f'<div class="gauge-fill {tone}" style="width:{fill:.1f}%"></div>'
            f'<div class="gauge-mark" style="left:{mark:.1f}%"></div>'
            f"</div>"
        )

    replacements = {
        "__PDT_PIPS__": pip_html,
        "__EDGE_BAR__": edge_html,
        "__OPTIONS_STOP__": options_stop_text,
        "__OPTIONS_STOP_STATUS__": options_stop_status,
        "__INTERVAL__": str(interval),
        "__TIME__": view["generated"],
        "__DATE__": view["generated_date"],
        "__EQUITY__": view["equity_text"],
        "__PNL__": view["daily_pnl_text"],
        "__PNL_PCT__": view["daily_pnl_percent_text"],
        "__PNL_STATUS__": view["pnl_status"],
        "__OVERALL__": view["overall"],
        "__OVERALL_LABEL__": view["overall_label"],
        "__POS_COUNT__": str(len(view["positions"])),
        "__BUYING_POWER__": view["buying_power_text"],
        "__TRADES__": str(view["trades_today"]),
        "__MAX_TRADES__": str(view["max_trades"]),
        "__SCANNED__": str(view["symbols_scanned"]),
        "__OPTIONS_MODE__": view["options_mode"],
        "__SHADOW_WIN__": view["shadow_win_text"],
        "__SHADOW_N__": str(view["shadow_resolved"]),
        "__SHADOW_STATUS__": view["shadow_status"],
        "__POSITIONS__": position_html,
        "__MODULES__": module_html,
        "__PROFILE__": view["profile"],
        "__MAX_POS__": str(view["max_positions"]),
        "__SPARKLINE__": _sparkline(view.get("equity_history") or []),
        "__INCIDENTS__": (
            f"{incidents.get('recurring', 0)} recurring"
            if incidents.get("recurring")
            else str(incidents.get("distinct", 0))
        ),
        "__INCIDENT_STATUS__": (
            STATUS_WARN if incidents.get("recurring") else STATUS_IDLE
        ),
        "__HYPOTHESES__": str(learning.get("open_hypotheses", 0)),
    }

    output = _TEMPLATE

    for token, value in replacements.items():
        output = output.replace(token, value)

    if live:
        # Live mode updates in place, so the meta refresh has to go —
        # reloading the page would restart every animation mid-sentence.
        output = output.replace(
            f'<meta http-equiv="refresh" content="{interval}">', ""
        )
        output = output.replace("</style>", _LIVE_STYLE + "\n</style>")
        output = output.replace("</body>", _LIVE_SCRIPT + "\n</body>")

        # Hooks for the script to update without rebuilding the page.
        output = output.replace(
            '<div class="hero">', '<div class="hero" id="equity">'
        )
        output = output.replace(
            f'<span class="delta {view["pnl_status"]}">',
            f'<span class="delta {view["pnl_status"]}" id="pnl">',
        )
        output = output.replace(
            '<span class="delta-pct">', '<span class="delta-pct" id="pnlPct">'
        )
        output = output.replace(
            f'<span class="chip {view["overall"]}">',
            f'<span class="chip {view["overall"]}" id="overall">',
        )
        output = output.replace(
            f'<div class="core {view["overall"]}">',
            f'<div class="core {view["overall"]}" id="core">',
        )
        # Insert the speech indicators just above the stat tiles. A plain
        # insertion before a known marker, rather than reshaping the DOM —
        # the fragile version of this had unbalanced quotes and invented
        # closing tags.
        # The anchor must be a string that actually appears in the template.
        #
        # This previously inserted before '  <div class="tiles">', which does
        # not exist -- the template has a .tiles CSS rule but no element
        # using it. str.replace() on a missing needle is a silent no-op, so
        # the speech indicators were never rendered at all and nothing ever
        # said so. The bars were "working" for weeks in the sense that the
        # CSS was correct and the markup was absent.
        #
        # The anchor must also still be present at THIS point in render().
        # __SPARKLINE__ looked like a good marker and was not: it is a
        # placeholder already substituted by the time this runs, so it
        # failed exactly as silently as the previous attempt.
        #
        # '  <div class="guard">' is literal markup rather than a
        # placeholder, so nothing rewrites it. The orb lands directly above
        # the guard row. The check below turns any future mismatch into a
        # visible failure -- and it earned its keep immediately: trimming
        # the layout removed the previous anchor and this printed a warning
        # instead of the display quietly disappearing for a third time.
        anchor = '  <div class="guard">'

        voice_block = (
            '  <div class="orb-wrap">'
            + "".join(
                f'<i class="tick" style="transform:translate(-50%,0) '
                f'rotate({angle}deg)"></i>'
                for angle in range(0, 360, 30)
            )
            + '<canvas class="wave" id="wave" width="400" height="400">'
            "</canvas></div>\n"
            '  <div class="voice-line" id="voiceLine"></div>\n'
            '  <div class="heard" id="heard"></div>\n\n'
            + anchor
        )

        if anchor not in output:
            print(
                "HUD WARNING: speech-indicator anchor not found; the "
                "waveform will not render. The template changed."
            )
        else:
            output = output.replace(anchor, voice_block, 1)

    return output


def generate(interval: int = DEFAULT_INTERVAL) -> Path:
    """Write the HUD once from current state."""

    # Same assembly as the served page, so the static file and the live
    # one can never disagree about what is true.
    OUTPUT_FILE.write_text(
        render(build_full_view(interval), interval), encoding="utf-8"
    )

    return OUTPUT_FILE


# ---------------------------------------------------------------------------
# Live mode
#
# The reactive display needs the page to poll, and a file:// page cannot
# fetch a sibling file — browsers treat local files as opaque origins. So
# live mode runs a tiny HTTP server.
#
# The boundary, deliberately narrow:
#   - bound to 127.0.0.1, NOT 0.0.0.0, so nothing outside this machine can
#     reach it even on the same network
#   - GET only; POST and everything else are refused
#   - exactly two paths, both read-only
#   - accepts no input, runs no command, places no order
#
# It is a window onto state, not a control surface. The Telegram bot got a
# different answer for a different reason: it needed to be reachable from
# outside, and the safe way to do that was to poll outward rather than
# listen inward.
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = int(os.getenv("LOCKBOT_HUD_PORT", "8770"))


def build_full_view(interval: int) -> dict:
    """
    The complete view: files, broker marks, incidents and hypotheses.

    One assembly point so the served page and the static file cannot
    drift apart.
    """

    from lockbot_brain import collect_state

    state = collect_state()
    live = fetch_live_positions()

    view = build_view(state, {s: d["protected"] for s, d in live.items()})

    # Replace the file-derived positions with broker truth, which carries
    # the current mark and the live bracket levels.
    positions = []

    for symbol, data in sorted(live.items(), key=lambda kv: kv[1]["kind"]):
        track = track_position(
            data["entry"], data["current"], data["stop"], data["target"]
        )

        label = symbol

        if data["kind"] == "option":
            # OCC symbols are unreadable. Show the underlying and strike.
            try:
                from options_contracts import parse_occ_symbol

                parts = parse_occ_symbol(symbol)
                label = (
                    f"{parts.underlying} {parts.strike:g} "
                    f"{parts.contract_type.upper()[0]} "
                    f"{parts.expiration.strftime('%d%b').upper()}"
                )
            except Exception:
                pass

        positions.append(
            {
                "symbol": label,
                "raw": symbol,
                "kind": data["kind"],
                "qty": data["qty"],
                "entry": data["entry"],
                "current": data["current"],
                "pl": data["pl"],
                "pl_percent": data["pl_percent"],
                "stop": data["stop"],
                "target": data["target"],
                "protected": data["protected"],
                "track": track,
            }
        )

    view["positions"] = positions
    view["equity_history"] = fetch_equity_history()

    try:
        from lockbot_incidents import collect as collect_incidents

        incidents = collect_incidents(days=3, include_broker=False)
        view["incidents"] = {
            "distinct": incidents["distinct_incidents"],
            "occurrences": incidents["total_occurrences"],
            "recurring": len(incidents["recurring"]),
            "worst": (
                incidents["incidents"][0]["category"]
                if incidents["incidents"] else None
            ),
        }
    except Exception:
        view["incidents"] = {"distinct": 0, "occurrences": 0, "recurring": 0,
                             "worst": None}

    try:
        from lockbot_learn import load_log, open_hypotheses

        log = load_log()
        view["learning"] = {
            "open_hypotheses": len(open_hypotheses(log)),
            "passes": len([e for e in log if e.get("type") == "pass"]),
        }
    except Exception:
        view["learning"] = {"open_hypotheses": 0, "passes": 0}

    # Unprotected is recomputed here because the broker, not the file, is
    # the authority on whether a stop is live.
    unguarded = [p for p in positions if not p["protected"]]

    if unguarded and view["overall"] not in {STATUS_CRIT}:
        view["overall"] = STATUS_CRIT
        view["overall_label"] = f"{len(unguarded)} POSITION(S) UNPROTECTED"

    view["interval"] = interval

    return view


def _live_payload(interval: int) -> dict:
    """Everything the page needs, in one response."""

    try:
        from lockbot_voice import get_voice_state

        voice = get_voice_state()
    except Exception:
        voice = {"state": "idle", "detail": "", "age": None}

    view = build_full_view(interval)
    view["voice"] = voice

    return view


def serve(interval: int = DEFAULT_INTERVAL, open_browser: bool = False) -> int:
    """Serve the live HUD on loopback."""

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    cache: dict = {"at": 0.0, "view": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # a request per second would drown the console

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]

            if path in ("/", "/index.html"):
                self._send(render(_live_payload(interval), interval, live=True)
                           .encode("utf-8"), "text/html; charset=utf-8")
                return

            if path == "/state":
                # The account snapshot is rebuilt at most once a second;
                # the voice state is always current, because that is the
                # part the display animates on.
                now = time.time()

                if cache["view"] is None or now - cache["at"] > 1.0:
                    cache["view"] = _live_payload(interval)
                    cache["at"] = now
                else:
                    try:
                        from lockbot_voice import get_voice_state

                        cache["view"]["voice"] = get_voice_state()
                    except Exception:
                        pass

                self._send(
                    json.dumps(cache["view"], default=str).encode("utf-8"),
                    "application/json",
                )
                return

            self.send_error(404)

        def do_POST(self):
            self.send_error(405, "This server is read-only.")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"

    print(f"Live HUD on {url}")
    print("  loopback only, read-only, no commands accepted")
    print("  open it and press F11.  Ctrl+C to stop.\n")

    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()

    return 0


def watch(interval: int = DEFAULT_INTERVAL) -> int:
    """Keep regenerating so the page always shows current state."""

    print(f"Writing {OUTPUT_FILE.name} every {interval}s. Ctrl+C to stop.")
    print("Open it in a browser and press F11.\n")

    while True:
        try:
            generate(interval)
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"  [{stamp}] refreshed", end="\r", flush=True)
            time.sleep(interval)

        except KeyboardInterrupt:
            print("\nStopped.")
            return 0

        except Exception as error:
            print(f"\n[error] {type(error).__name__}: {error}")
            time.sleep(interval)


def _self_test() -> int:
    """Offline checks against synthetic state. No broker, no network."""

    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    def state(**overrides) -> dict:
        base = {
            "scanner_state": {
                "account_equity": 249.47,
                "daily_pnl": -1.36,
                "daily_pnl_percent": -0.0054,
                "buying_power": 148.95,
                "market_open": False,
                "symbols_scanned": 47,
            },
            "risk_state": {"trades_submitted_today": 2, "kill_switch_active": False},
            "configuration": {
                "max_trades_per_day": 10,
                "max_open_positions": 2,
                "account_profile": "small",
                "options_enabled": True,
                "options_shadow_mode": False,
            },
            "module_health": {"MARKET_SCANNER": {"status": "HEALTHY"}},
            "shadow_summary": {"resolved": 55, "win_rate_percent": 27.3},
            "equity_positions_tracked": {
                "NVO": {"entry_price": 51.36, "highest_gain_percent": 1.19,
                        "trailing_stop_active": True}
            },
            "option_positions_tracked": {},
            "pending_equity_trades": [{"symbol": "NVO"}],
        }
        base.update(overrides)
        return base

    print("View building")

    view = build_view(state())
    check("equity formatted", view["equity_text"] == "$249.47", view["equity_text"])
    check("negative P&L keeps its sign", view["daily_pnl_text"] == "-$1.36",
          view["daily_pnl_text"])
    check("negative P&L is a warning", view["pnl_status"] == STATUS_WARN)
    check("percentage signed", view["daily_pnl_percent_text"] == "-0.54%",
          view["daily_pnl_percent_text"])
    check("position counted", len(view["positions"]) == 1)
    check("registered position is tracked", view["positions"][0]["tracked"])
    check("closed market reads idle", view["overall"] == STATUS_IDLE, view["overall"])

    print()
    print("Status precedence")

    killed = build_view(state(risk_state={"kill_switch_active": True,
                                          "kill_switch_reason": "x"}))
    check("kill switch outranks everything", killed["overall"] == STATUS_CRIT)
    check("and is labelled", "KILL" in killed["overall_label"])

    critical = build_view(state(module_health={"X": {"status": "CRITICAL"}}))
    check("critical module escalates", critical["overall"] == STATUS_CRIT)

    untracked = build_view(state(pending_equity_trades=[]))
    check("unregistered position warns", untracked["overall"] == STATUS_WARN,
          untracked["overall"])
    check("and is named", "UNTRACKED" in untracked["overall_label"])
    check("and marks the position", not untracked["positions"][0]["tracked"])

    print()
    print("Protection")

    unguarded = build_view(state(), protection={"NVO": False})
    check("no stop is CRITICAL, not a warning", unguarded["overall"] == STATUS_CRIT,
          unguarded["overall"])
    check("and says how many", "UNPROTECTED" in unguarded["overall_label"],
          unguarded["overall_label"])
    check("and marks the position", unguarded["positions"][0]["guarded"] == "no")

    guarded = build_view(state(), protection={"NVO": True})
    check("a live stop is fine", guarded["overall"] == STATUS_IDLE, guarded["overall"])

    # The failure that motivated this: registered in the file, no bracket
    # at the broker. The registry alone must not read as protected.
    unknown = build_view(state(), protection={})
    check("an unreachable broker is unknown, not protected",
          unknown["positions"][0]["guarded"] == "unknown")
    check("and does not claim safety", unknown["overall"] != STATUS_GOOD)

    markup = render(build_view(state(), protection={"NVO": False}))
    check("unprotected renders in words", "NO STOP LOSS" in markup)
    check("and gets its own styling", "unguarded" in markup)

    trading = build_view(state(scanner_state={**state()["scanner_state"],
                                              "market_open": True}))
    check("open market reads good", trading["overall"] == STATUS_GOOD)

    print()
    print("Shadow framing")

    check("below breakeven warns", build_view(state())["shadow_status"] == STATUS_WARN)
    above = build_view(state(shadow_summary={"resolved": 60, "win_rate_percent": 41.0}))
    check("above breakeven is good", above["shadow_status"] == STATUS_GOOD)
    none = build_view(state(shadow_summary={"resolved": 0, "win_rate_percent": None}))
    check("no data is idle, not bad", none["shadow_status"] == STATUS_IDLE)
    check("and renders a dash", none["shadow_win_text"] == "—")

    print()
    print("Rendering")

    markup = render(build_view(state()))
    check("no placeholder survives", "__" not in markup.replace("__", "", 0) or
          all(t not in markup for t in ("__EQUITY__", "__PNL__", "__MODULES__")))
    check("equity appears", "$249.47" in markup)
    check("self-contained (no external fetch)",
          "http://" not in markup and "https://" not in markup)
    check("has a refresh directive", "http-equiv=\"refresh\"" in markup)

    # Status must never be colour alone — every indicator ships with a word.
    #
    # The per-module list is gone from the layout (the overall chip
    # aggregates it, and the watchdog alerts independently), so this checks
    # the indicators that remain rather than the string that used to be
    # there. Deleting the check would have quietly dropped the principle
    # along with the markup.
    # Assert the principle, not a vocabulary. The label is "MARKET CLOSED"
    # when idle and something else when trading, so listing expected words
    # would only test which words I happened to think of.
    chip_text = re.search(
        r'class="chip[^"]*"[^>]*>(?:<i[^>]*></i>)?([^<]*)<', markup
    )
    check(
        "overall status carries a word, not just a colour",
        bool(chip_text) and bool(chip_text.group(1).strip()),
        repr(chip_text.group(1)) if chip_text else "no chip found",
    )

    print()
    print("Widgets carry real values, not decoration")

    # PDT pips: one per allowed round trip, lit for each one used.
    pips = render(build_view(state(risk_state={
        "trades_submitted_today": 2, "kill_switch_active": False,
    })), live=True)
    check("pips are rendered", 'class="pip' in pips)
    check("two of three are lit", pips.count('class="pip on"') == 2,
          str(pips.count('class="pip on"')))

    none_used = render(build_view(state(risk_state={
        "trades_submitted_today": 0, "kill_switch_active": False,
    })), live=True)
    check("none lit at zero trades", none_used.count('class="pip on"') == 0)

    # Edge gauge: fill scaled to a 60% axis, breakeven drawn as a tick.
    losing = render(build_view(state(shadow_summary={
        "resolved": 94, "win_rate_percent": 20.2,
    })), live=True)
    check("the gauge renders", 'class="gauge"' in losing)
    check("a losing edge is red", 'gauge-fill bad' in losing)
    check("breakeven is marked on the scale", 'gauge-mark' in losing)

    winning = render(build_view(state(shadow_summary={
        "resolved": 94, "win_rate_percent": 45.0,
    })), live=True)
    check("a winning edge is green", 'gauge-fill good' in winning)

    blank = render(build_view(state(shadow_summary={})), live=True)
    check("no data says so rather than drawing an empty bar",
          "NO DATA" in blank)

    check("the orb has its reticle", 'class="tick"' in losing)
    check("and its radar wrapper", 'class="orb-wrap"' in losing)

    print()

    armed = render(build_view(state()), live=True)
    check("the options stop states itself in words", "OPTIONS STOP" in armed)
    check(
        "and gives a readable value",
        any(v in armed for v in ("ARMED", "NOT RUNNING", "DEGRADED",
                                 "UNKNOWN", "NO CONTRACTS")),
    )
    check("overall status carries a text label",
          build_view(state())["overall_label"] in markup)

    empty = render(build_view(state(equity_positions_tracked={},
                                    pending_equity_trades=[])))
    check("empty state renders", "No open positions." in empty)

    hostile = render(build_view(state(
        equity_positions_tracked={"<script>x</script>": {"entry_price": 1.0}},
        pending_equity_trades=[])))
    check("symbols are escaped", "<script>x</script>" not in hostile)

    print()
    print("Speech waveform is actually in the markup")

    # The bug this guards: the insertion anchored on a marker that was not
    # in the template, str.replace() did nothing, and the speech display
    # was absent for weeks while the CSS for it looked perfectly correct.
    # Checking the CSS or the script is not enough -- the ELEMENT has to
    # be there.
    live_html = render(build_view(state()), live=True)

    check("the canvas element is rendered", 'id="wave"' in live_html)
    check("with the wave class the CSS targets", 'class="wave"' in live_html)
    check("the voice line is rendered", 'id="voiceLine"' in live_html)
    check("the heard line is rendered", 'id="heard"' in live_html)
    check("the drawing code is included", "drawWave" in live_html)
    check("it reads word timings", "amplitudeAt" in live_html)
    check(
        "the old fixed-cycle bars are gone",
        'class="bars"' not in live_html,
    )

    # The static render deliberately omits it. That file is written by
    # --watch and has no polling script, so drawWave never runs and a
    # canvas there would be dead markup showing a frozen line. Asserting
    # the absence keeps the two paths honest about differing on purpose.
    static_html = render(build_view(state()))
    check("static render omits the canvas", 'id="wave"' not in static_html)
    check("because it has no live script", "drawWave" not in static_html)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All HUD checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LOCKBOT heads-up display.")
    parser.add_argument("--serve", action="store_true",
                        help="live HUD that reacts to LOCKBOT's voice")
    parser.add_argument("--watch", action="store_true", help="keep it refreshed")
    parser.add_argument("--open", action="store_true", help="open it in a browser")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help="seconds between refreshes")
    parser.add_argument("--self-test", action="store_true", help="offline checks")

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.serve:
        return serve(args.interval, open_browser=args.open)

    path = generate(args.interval)
    print(f"Wrote {path}")

    if args.open:
        webbrowser.open(path.as_uri())

    if args.watch:
        return watch(args.interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
