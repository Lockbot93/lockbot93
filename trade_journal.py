"""
LOCKBOT Trade Journal v0.2

Permanently records structured trading events and completed trades.

This module does not submit, modify, replace, or cancel orders.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from trade_grader import grade_trade


PROJECT_FOLDER = Path(__file__).resolve().parent

TRADE_JOURNAL_FILE = PROJECT_FOLDER / "trade_journal.jsonl"
COMPLETED_TRADES_FILE = PROJECT_FOLDER / "completed_trades.csv"

JOURNAL_FILE = COMPLETED_TRADES_FILE

TRADE_JOURNAL_VERSION = "0.2"

COMPLETED_TRADE_COLUMNS = [
    "trade_id",
    "strategy_version",
    "symbol",
    "side",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "quantity",
    "position_value",
    "account_equity_at_entry",
    "initial_risk_dollars",
    "stop_loss_percent",
    "take_profit_percent",
    "profit_loss",
    "return_percent",
    "holding_minutes",
    "exit_reason",
    "market_regime",
    "confidence",
    "paper_trade",
    "journal_version",
]


@dataclass
class JournalEntry:
    """One permanent LOCKBOT operational journal event."""

    timestamp: str
    event_type: str
    symbol: str
    action: str
    status: str
    reason: str

    quantity: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    profit_loss: float = 0.0

    confidence: int = 0
    market_regime: str = ""
    rsi: float = 0.0
    macd: float = 0.0
    ema_alignment: bool = False

    estimated_risk_dollars: float = 0.0
    estimated_risk_percent: float = 0.0

    order_id: str = ""
    notes: str = ""

    journal_version: str = TRADE_JOURNAL_VERSION


def current_timestamp() -> str:
    """Return the current local timestamp."""

    return datetime.now().astimezone().isoformat(
        timespec="seconds"
    )


def normalize_text(value: Any) -> str:
    """Convert a value into clean journal text."""

    if value is None:
        return ""

    raw_value = getattr(value, "value", value)
    return str(raw_value).strip()


def initialize_trade_journal() -> Path:
    """
    Create both journal files when needed.

    Returns the completed-trades CSV path because trade_manager.py
    uses this function together with JOURNAL_FILE.
    """

    try:
        TRADE_JOURNAL_FILE.touch(exist_ok=True)

        if not COMPLETED_TRADES_FILE.exists():
            with COMPLETED_TRADES_FILE.open(
                mode="w",
                newline="",
                encoding="utf-8",
            ) as completed_file:
                writer = csv.DictWriter(
                    completed_file,
                    fieldnames=COMPLETED_TRADE_COLUMNS,
                )
                writer.writeheader()
                completed_file.flush()
                os.fsync(completed_file.fileno())
        else:
            with COMPLETED_TRADES_FILE.open(
                mode="r",
                newline="",
                encoding="utf-8-sig",
            ) as completed_file:
                reader = csv.reader(completed_file)
                header = next(reader, [])

            if header != COMPLETED_TRADE_COLUMNS:
                raise RuntimeError(
                    f"{COMPLETED_TRADES_FILE.name} has an "
                    "unexpected header. Back up the file before "
                    "changing or recreating it."
                )

    except OSError as error:
        raise RuntimeError(
            "Could not initialize LOCKBOT trade journals: "
            f"{error}"
        ) from error

    return COMPLETED_TRADES_FILE


def create_journal_entry(
    *,
    event_type: str,
    symbol: str = "",
    action: str = "",
    status: str = "",
    reason: str = "",
    quantity: float = 0.0,
    entry_price: float = 0.0,
    exit_price: float = 0.0,
    profit_loss: float = 0.0,
    confidence: int = 0,
    market_regime: str = "",
    rsi: float = 0.0,
    macd: float = 0.0,
    ema_alignment: bool = False,
    estimated_risk_dollars: float = 0.0,
    estimated_risk_percent: float = 0.0,
    order_id: str = "",
    notes: str = "",
) -> JournalEntry:
    """Create one validated operational journal entry."""

    normalized_event_type = normalize_text(
        event_type
    ).upper()

    if not normalized_event_type:
        raise ValueError(
            "event_type cannot be blank."
        )

    if not 0 <= int(confidence) <= 100:
        raise ValueError(
            "confidence must be between 0 and 100."
        )

    return JournalEntry(
        timestamp=current_timestamp(),
        event_type=normalized_event_type,
        symbol=normalize_text(symbol).upper(),
        action=normalize_text(action).upper(),
        status=normalize_text(status).upper(),
        reason=normalize_text(reason),
        quantity=float(quantity),
        entry_price=float(entry_price),
        exit_price=float(exit_price),
        profit_loss=float(profit_loss),
        confidence=int(confidence),
        market_regime=normalize_text(
            market_regime
        ).upper(),
        rsi=float(rsi),
        macd=float(macd),
        ema_alignment=bool(ema_alignment),
        estimated_risk_dollars=float(
            estimated_risk_dollars
        ),
        estimated_risk_percent=float(
            estimated_risk_percent
        ),
        order_id=normalize_text(order_id),
        notes=normalize_text(notes),
    )


def append_journal_entry(entry: JournalEntry) -> None:
    """Permanently append one operational event."""

    journal_data = asdict(entry)

    serialized_entry = json.dumps(
        journal_data,
        ensure_ascii=False,
    )

    try:
        with TRADE_JOURNAL_FILE.open(
            mode="a",
            encoding="utf-8",
        ) as journal_file:
            journal_file.write(
                serialized_entry + "\n"
            )
            journal_file.flush()
            os.fsync(journal_file.fileno())

    except OSError as error:
        raise RuntimeError(
            f"Could not write to "
            f"{TRADE_JOURNAL_FILE.name}: "
            f"{error}"
        ) from error


def record_event(
    *,
    event_type: str,
    symbol: str = "",
    action: str = "",
    status: str = "",
    reason: str = "",
    quantity: float = 0.0,
    entry_price: float = 0.0,
    exit_price: float = 0.0,
    profit_loss: float = 0.0,
    confidence: int = 0,
    market_regime: str = "",
    rsi: float = 0.0,
    macd: float = 0.0,
    ema_alignment: bool = False,
    estimated_risk_dollars: float = 0.0,
    estimated_risk_percent: float = 0.0,
    order_id: str = "",
    notes: str = "",
) -> JournalEntry:
    """Create and permanently record one operational event."""

    initialize_trade_journal()

    entry = create_journal_entry(
        event_type=event_type,
        symbol=symbol,
        action=action,
        status=status,
        reason=reason,
        quantity=quantity,
        entry_price=entry_price,
        exit_price=exit_price,
        profit_loss=profit_loss,
        confidence=confidence,
        market_regime=market_regime,
        rsi=rsi,
        macd=macd,
        ema_alignment=ema_alignment,
        estimated_risk_dollars=(
            estimated_risk_dollars
        ),
        estimated_risk_percent=(
            estimated_risk_percent
        ),
        order_id=order_id,
        notes=notes,
    )

    append_journal_entry(entry)

    return entry


def _calculate_completed_trade_metrics(
    *,
    side: str,
    entry_time: datetime,
    exit_time: datetime,
    entry_price: float,
    exit_price: float,
    quantity: float,
) -> tuple[float, float, float]:
    """Calculate realized P/L, return percentage, and holding time."""

    normalized_side = normalize_text(side).upper()

    if normalized_side == "LONG":
        price_change = exit_price - entry_price
    elif normalized_side == "SHORT":
        price_change = entry_price - exit_price
    else:
        raise ValueError(
            "side must be LONG or SHORT."
        )

    profit_loss = round(
        price_change * quantity,
        2,
    )

    return_percent = round(
        (price_change / entry_price) * 100,
        4,
    )

    holding_minutes = round(
        (exit_time - entry_time).total_seconds()
        / 60,
        2,
    )

    return (
        profit_loss,
        return_percent,
        holding_minutes,
    )


def record_completed_trade(
    *,
    trade_id: str,
    strategy_version: str,
    symbol: str,
    side: str,
    entry_time: datetime,
    exit_time: datetime,
    entry_price: float,
    exit_price: float,
    quantity: float,
    position_value: float,
    account_equity_at_entry: float,
    initial_risk_dollars: float,
    stop_loss_percent: float,
    take_profit_percent: float,
    exit_reason: str,
    market_regime: str,
    confidence: int,
    paper_trade: bool = True,
) -> dict[str, Any]:
    """
    Permanently record one completed trade in completed_trades.csv.

    This signature matches trade_manager.py v0.5.
    """

    normalized_trade_id = normalize_text(trade_id)
    normalized_symbol = normalize_text(symbol).upper()
    normalized_side = normalize_text(side).upper()

    if not normalized_trade_id:
        raise ValueError(
            "trade_id cannot be blank."
        )

    if not normalize_text(strategy_version):
        raise ValueError(
            "strategy_version cannot be blank."
        )

    if not normalized_symbol:
        raise ValueError(
            "symbol cannot be blank."
        )

    if normalized_side not in {"LONG", "SHORT"}:
        raise ValueError(
            "side must be LONG or SHORT."
        )

    if entry_time.tzinfo is None or exit_time.tzinfo is None:
        raise ValueError(
            "entry_time and exit_time must be timezone-aware."
        )

    if exit_time < entry_time:
        raise ValueError(
            "exit_time cannot be earlier than entry_time."
        )

    numeric_values = {
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": quantity,
        "position_value": position_value,
        "account_equity_at_entry": account_equity_at_entry,
    }

    for field_name, value in numeric_values.items():
        if float(value) <= 0:
            raise ValueError(
                f"{field_name} must be greater than zero."
            )

    if float(initial_risk_dollars) < 0:
        raise ValueError(
            "initial_risk_dollars cannot be negative."
        )

    if not 0 < float(stop_loss_percent) < 1:
        raise ValueError(
            "stop_loss_percent must be between 0 and 1."
        )

    if not 0 < float(take_profit_percent) < 1:
        raise ValueError(
            "take_profit_percent must be between 0 and 1."
        )

    if not 0 <= int(confidence) <= 100:
        raise ValueError(
            "confidence must be between 0 and 100."
        )

    (
        profit_loss,
        return_percent,
        holding_minutes,
    ) = _calculate_completed_trade_metrics(
        side=normalized_side,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=float(entry_price),
        exit_price=float(exit_price),
        quantity=float(quantity),
    )

    initialize_trade_journal()

    with COMPLETED_TRADES_FILE.open(
        mode="r",
        newline="",
        encoding="utf-8-sig",
    ) as completed_file:
        reader = csv.DictReader(completed_file)

        if reader.fieldnames != COMPLETED_TRADE_COLUMNS:
            raise RuntimeError(
                f"{COMPLETED_TRADES_FILE.name} has an "
                "unexpected header."
            )

        if any(
            normalize_text(row.get("trade_id"))
            == normalized_trade_id
            for row in reader
        ):
            raise ValueError(
                f"trade_id {normalized_trade_id} "
                "is already recorded."
            )

    row = {
        "trade_id": normalized_trade_id,
        "strategy_version": normalize_text(
            strategy_version
        ),
        "symbol": normalized_symbol,
        "side": normalized_side,
        "entry_time": entry_time.isoformat(
            timespec="seconds"
        ),
        "exit_time": exit_time.isoformat(
            timespec="seconds"
        ),
        "entry_price": round(float(entry_price), 4),
        "exit_price": round(float(exit_price), 4),
        "quantity": float(quantity),
        "position_value": round(
            float(position_value),
            2,
        ),
        "account_equity_at_entry": round(
            float(account_equity_at_entry),
            2,
        ),
        "initial_risk_dollars": round(
            float(initial_risk_dollars),
            2,
        ),
        "stop_loss_percent": float(
            stop_loss_percent
        ),
        "take_profit_percent": float(
            take_profit_percent
        ),
        "profit_loss": profit_loss,
        "return_percent": return_percent,
        "holding_minutes": holding_minutes,
        "exit_reason": normalize_text(
            exit_reason
        ).upper(),
        "market_regime": normalize_text(
            market_regime
        ).upper(),
        "confidence": int(confidence),
        "paper_trade": bool(paper_trade),
        "journal_version": TRADE_JOURNAL_VERSION,
    }

    try:
        with COMPLETED_TRADES_FILE.open(
            mode="a",
            newline="",
            encoding="utf-8",
        ) as completed_file:
            writer = csv.DictWriter(
                completed_file,
                fieldnames=COMPLETED_TRADE_COLUMNS,
            )
            writer.writerow(row)
            completed_file.flush()
            os.fsync(completed_file.fileno())

    except OSError as error:
        raise RuntimeError(
            f"Could not write to "
            f"{COMPLETED_TRADES_FILE.name}: "
            f"{error}"
        ) from error
    
    r_multiple, trade_grade = grade_trade(
        profit_loss,
        float(initial_risk_dollars),
    )
    record_event(
        event_type="TRADE_COMPLETED",
        symbol=normalized_symbol,
        action=normalized_side,
        status="RECORDED",
        reason=normalize_text(exit_reason),
        quantity=float(quantity),
        entry_price=float(entry_price),
        exit_price=float(exit_price),
        profit_loss=profit_loss,
        confidence=int(confidence),
        market_regime=market_regime,
        estimated_risk_dollars=float(
            initial_risk_dollars
        ),
        order_id=normalized_trade_id,
        notes=(
            f"Strategy v{normalize_text(strategy_version)}; "
            f"return {return_percent:.4f}%; "
            f"held {holding_minutes:.2f} minutes; "
            f"R-multiple {r_multiple:.2f}R; "
            f"grade {trade_grade}."
        ),
    )

    return row


def load_journal_entries() -> list[dict[str, Any]]:
    """Load every valid operational journal entry."""

    if not TRADE_JOURNAL_FILE.exists():
        return []

    entries: list[dict[str, Any]] = []

    try:
        with TRADE_JOURNAL_FILE.open(
            mode="r",
            encoding="utf-8",
        ) as journal_file:
            for line_number, line in enumerate(
                journal_file,
                start=1,
            ):
                stripped_line = line.strip()

                if not stripped_line:
                    continue

                try:
                    entry = json.loads(
                        stripped_line
                    )

                except json.JSONDecodeError as error:
                    print(
                        f"WARNING: Skipped invalid "
                        f"journal line {line_number}: "
                        f"{error}"
                    )
                    continue

                if isinstance(entry, dict):
                    entries.append(entry)

    except OSError as error:
        raise RuntimeError(
            f"Could not read "
            f"{TRADE_JOURNAL_FILE.name}: "
            f"{error}"
        ) from error

    return entries


def get_recent_entries(
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the newest operational journal entries."""

    if limit <= 0:
        return []

    entries = load_journal_entries()
    return entries[-limit:]


def print_journal_entry(
    entry: dict[str, Any],
) -> None:
    """Display one operational journal entry."""

    print("-" * 58)
    print(
        f"Time       : "
        f"{entry.get('timestamp', '')}"
    )
    print(
        f"Event      : "
        f"{entry.get('event_type', '')}"
    )
    print(
        f"Symbol     : "
        f"{entry.get('symbol', '') or 'N/A'}"
    )
    print(
        f"Action     : "
        f"{entry.get('action', '') or 'N/A'}"
    )
    print(
        f"Status     : "
        f"{entry.get('status', '') or 'N/A'}"
    )
    print(
        f"Reason     : "
        f"{entry.get('reason', '') or 'N/A'}"
    )
    print(
        f"Quantity   : "
        f"{entry.get('quantity', 0.0)}"
    )
    print(
        f"Entry Price: "
        f"${float(entry.get('entry_price', 0.0)):,.2f}"
    )
    print(
        f"Exit Price : "
        f"${float(entry.get('exit_price', 0.0)):,.2f}"
    )
    print(
        f"Profit/Loss: "
        f"${float(entry.get('profit_loss', 0.0)):,.2f}"
    )
    print(
        f"Confidence : "
        f"{entry.get('confidence', 0)}"
    )
    print(
        f"Regime     : "
        f"{entry.get('market_regime', '') or 'N/A'}"
    )
    print(
        f"Order ID   : "
        f"{entry.get('order_id', '') or 'N/A'}"
    )
    print(
        f"Notes      : "
        f"{entry.get('notes', '') or 'N/A'}"
    )


def print_recent_entries(
    limit: int = 10,
) -> None:
    """Display the newest operational journal entries."""

    entries = get_recent_entries(
        limit=limit
    )

    print("=" * 58)
    print(
        f"          LOCKBOT TRADE JOURNAL "
        f"v{TRADE_JOURNAL_VERSION}"
    )
    print("=" * 58)
    print(
        f"Event File   : "
        f"{TRADE_JOURNAL_FILE.name}"
    )
    print(
        f"Completed CSV: "
        f"{COMPLETED_TRADES_FILE.name}"
    )
    print(
        f"Total Events : "
        f"{len(load_journal_entries())}"
    )
    print(
        f"Showing      : "
        f"{len(entries)}"
    )

    if not entries:
        print("Event journal is currently empty.")
        print("=" * 58)
        return

    for entry in entries:
        print_journal_entry(entry)

    print("=" * 58)


def run_self_test() -> None:
    """Initialize both journals and record one safe test event."""

    initialize_trade_journal()

    test_entry = record_event(
        event_type="SYSTEM_TEST",
        symbol="SPY",
        action="NONE",
        status="SUCCESS",
        reason=(
            "Trade Journal standalone test completed."
        ),
        notes=(
            "This is not a broker order or paper trade."
        ),
    )

    print(
        "Test journal entry recorded successfully."
    )
    print(
        f"Timestamp: {test_entry.timestamp}"
    )
    print()

    print_recent_entries(
        limit=5
    )


if __name__ == "__main__":
    run_self_test()