"""
LOCKBOT Central Configuration v1.1

This module is the single source of truth for shared LOCKBOT settings.
Every module below imports its shared settings from here instead of
defining its own local copy — that was the root cause of several bugs
found during the v1.0 audit (mismatched risk limits, mismatched stop
percentages, and a journal-filename mismatch that silently broke
performance reporting).

Unit convention: every *_PERCENT constant below is a FRACTION
(0.02 means 2%), matching how these values are used directly in the
trading math throughout the codebase, e.g.:
    risk_dollars = account_equity * MAX_RISK_PER_TRADE_PERCENT

Important:
- PAPER_TRADING should remain True during validation.
- ENABLE_PAPER_EXITS should remain False. Bracket orders (submitted by
  market_scanner.py) are LOCKBOT's sole exit mechanism. position_monitor.py
  is monitoring/alerting only and must never submit its own exit order
  while this remains the design — see position_monitor.py's docstring.
- LIVE_TRADING_ENABLED should remain False until paper testing is complete.
"""

from __future__ import annotations

from pathlib import Path


# ============================================================
# Project identity
# ============================================================

LOCKBOT_PROJECT_VERSION = "0.9"
LOCKBOT_CONFIG_VERSION = "1.1"

PROJECT_FOLDER = Path(__file__).resolve().parent


# ============================================================
# Trading environment
# ============================================================

PAPER_TRADING = True
LIVE_TRADING_ENABLED = False

# Bracket orders are LOCKBOT's sole exit mechanism. Keep this False —
# position_monitor.py must stay monitoring/alerting only. See its
# module docstring for the full rationale.
ENABLE_PAPER_EXITS = False

ALPACA_API_KEY_ENV = "ALPACA_API_KEY"
ALPACA_SECRET_KEY_ENV = "ALPACA_SECRET_KEY"


# ============================================================
# Market universe
# ============================================================

SYMBOLS = ["SPY", "QQQ"]


# ============================================================
# Scheduling
# ============================================================

SCAN_INTERVAL_SECONDS = 300
POSITION_MONITOR_INTERVAL_SECONDS = 300
TRADE_MANAGER_INTERVAL_SECONDS = 300
HEALTH_MONITOR_INTERVAL_SECONDS = 300
PREMARKET_CHECK_SECONDS = 60


# ============================================================
# Signal / entry thresholds
# ============================================================

MIN_SIGNAL_CONFIDENCE = 80
MIN_VOLUME_RATIO = 1.10


# ============================================================
# Bracket order exit legs
# This is LOCKBOT's authoritative, sole exit mechanism.
# ============================================================

BRACKET_STOP_LOSS_PERCENT = 0.02
BRACKET_TAKE_PROFIT_PERCENT = 0.04


# ============================================================
# Position-monitor informational thresholds
# position_monitor.py uses these ONLY to decide when to alert —
# it never submits its own exit order while bracket orders remain
# the sole exit mechanism (see ENABLE_PAPER_EXITS above).
# ============================================================

BREAK_EVEN_TRIGGER_PERCENT = 0.005
TRAILING_STOP_TRIGGER_PERCENT = 0.01
TRAILING_STOP_DISTANCE_PERCENT = 0.005
MONITOR_STOP_LOSS_PERCENT = -0.005


# ============================================================
# Position sizing
# ============================================================

MAX_RISK_PER_TRADE_PERCENT = 0.01
MAX_POSITION_VALUE_PERCENT = 0.10


# ============================================================
# Risk controls
# These remain conservative during paper validation.
# ============================================================

MAX_OPEN_POSITIONS = 1
MAX_TRADES_PER_DAY = 4
MAX_TOTAL_EXPOSURE_PERCENT = 0.20
MAX_DAILY_LOSS_PERCENT = 0.02


# ============================================================
# Heartbeat thresholds
# ============================================================

HEARTBEAT_WARNING_MINUTES = 10
HEARTBEAT_CRITICAL_MINUTES = 20

CONTINUOUS_MODULES = {"CONTROLLER"}

SCHEDULED_MODULES = {
    "MARKET_SCANNER",
    "POSITION_MONITOR",
    "TRADE_MANAGER",
}

ON_DEMAND_MODULES = {"HEALTH_MONITOR"}


# ============================================================
# Notifications
# ============================================================

NOTIFY_ON_STARTUP = True
NOTIFY_ON_SHUTDOWN = True
NOTIFY_ON_TRADE_SIGNAL = True
NOTIFY_ON_ORDER_SUBMISSION = True
NOTIFY_ON_EXIT_SIGNAL = True
NOTIFY_ON_CRITICAL_ERROR = True
NOTIFY_ON_HEARTBEAT_DEGRADED = True


# ============================================================
# Data files
# These paths match what trade_journal.py and trade_grader.py
# actually read and write. (v1.0 pointed COMPLETED_TRADES_FILE at
# "lockbot_trade_journal.csv" — a file nothing ever wrote to. Fixed.)
# ============================================================

POSITION_STATE_FILE = PROJECT_FOLDER / "position_state.json"
PENDING_TRADES_FILE = PROJECT_FOLDER / "lockbot_pending_trades.csv"
COMPLETED_TRADES_FILE = PROJECT_FOLDER / "completed_trades.csv"
HEARTBEAT_FILE = PROJECT_FOLDER / "lockbot_heartbeat.json"
RISK_STATE_FILE = PROJECT_FOLDER / "risk_state.json"
NOTIFICATION_STATE_FILE = PROJECT_FOLDER / "notification_state.json"


# ============================================================
# Validation
# ============================================================

def validate_configuration() -> None:
    """Raise an error when a shared configuration value is unsafe."""

    if LIVE_TRADING_ENABLED and PAPER_TRADING:
        raise ValueError(
            "LIVE_TRADING_ENABLED and PAPER_TRADING cannot both be True."
        )

    if LIVE_TRADING_ENABLED and not ENABLE_PAPER_EXITS:
        raise ValueError(
            "Live trading cannot be enabled while exits remain disabled."
        )

    if not SYMBOLS:
        raise ValueError("At least one trading symbol is required.")

    if len(set(SYMBOLS)) != len(SYMBOLS):
        raise ValueError("SYMBOLS contains duplicate entries.")

    for name, value in (
        ("SCAN_INTERVAL_SECONDS", SCAN_INTERVAL_SECONDS),
        ("POSITION_MONITOR_INTERVAL_SECONDS", POSITION_MONITOR_INTERVAL_SECONDS),
        ("TRADE_MANAGER_INTERVAL_SECONDS", TRADE_MANAGER_INTERVAL_SECONDS),
        ("HEALTH_MONITOR_INTERVAL_SECONDS", HEALTH_MONITOR_INTERVAL_SECONDS),
    ):
        if value < 60:
            raise ValueError(f"{name} must be at least 60 seconds.")

    if HEARTBEAT_WARNING_MINUTES <= 0:
        raise ValueError("HEARTBEAT_WARNING_MINUTES must be greater than zero.")

    if HEARTBEAT_CRITICAL_MINUTES <= HEARTBEAT_WARNING_MINUTES:
        raise ValueError(
            "HEARTBEAT_CRITICAL_MINUTES must be greater than "
            "HEARTBEAT_WARNING_MINUTES."
        )

    if MONITOR_STOP_LOSS_PERCENT >= 0:
        raise ValueError("MONITOR_STOP_LOSS_PERCENT must be negative.")

    if BREAK_EVEN_TRIGGER_PERCENT <= 0:
        raise ValueError("BREAK_EVEN_TRIGGER_PERCENT must be greater than zero.")

    if TRAILING_STOP_TRIGGER_PERCENT <= 0:
        raise ValueError("TRAILING_STOP_TRIGGER_PERCENT must be greater than zero.")

    if TRAILING_STOP_DISTANCE_PERCENT <= 0:
        raise ValueError("TRAILING_STOP_DISTANCE_PERCENT must be greater than zero.")

    if not 0 <= MIN_SIGNAL_CONFIDENCE <= 100:
        raise ValueError("MIN_SIGNAL_CONFIDENCE must be between 0 and 100.")

    if BRACKET_STOP_LOSS_PERCENT <= 0 or BRACKET_TAKE_PROFIT_PERCENT <= 0:
        raise ValueError(
            "BRACKET_STOP_LOSS_PERCENT and BRACKET_TAKE_PROFIT_PERCENT "
            "must both be greater than zero."
        )

    if MAX_OPEN_POSITIONS <= 0:
        raise ValueError("MAX_OPEN_POSITIONS must be greater than zero.")

    if MAX_TRADES_PER_DAY <= 0:
        raise ValueError("MAX_TRADES_PER_DAY must be greater than zero.")


def configuration_summary() -> dict[str, object]:
    """Return a compact configuration summary for diagnostics."""

    return {
        "project_version": LOCKBOT_PROJECT_VERSION,
        "config_version": LOCKBOT_CONFIG_VERSION,
        "paper_trading": PAPER_TRADING,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "paper_exits_enabled": ENABLE_PAPER_EXITS,
        "symbols": SYMBOLS,
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "position_monitor_interval_seconds": POSITION_MONITOR_INTERVAL_SECONDS,
        "trade_manager_interval_seconds": TRADE_MANAGER_INTERVAL_SECONDS,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "max_daily_loss_percent": MAX_DAILY_LOSS_PERCENT,
        "bracket_stop_loss_percent": BRACKET_STOP_LOSS_PERCENT,
        "bracket_take_profit_percent": BRACKET_TAKE_PROFIT_PERCENT,
        "heartbeat_warning_minutes": HEARTBEAT_WARNING_MINUTES,
        "heartbeat_critical_minutes": HEARTBEAT_CRITICAL_MINUTES,
    }


if __name__ == "__main__":
    validate_configuration()

    print("=" * 60)
    print("LOCKBOT CENTRAL CONFIGURATION")
    print("=" * 60)

    for key, value in configuration_summary().items():
        print(f"{key:<34}: {value}")

    print("=" * 60)
    print("Status                            : READY")
