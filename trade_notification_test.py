"""LOCKBOT notification system test."""

from notification_manager import (
    send_completed_trade_notification,
)

sample_trade = {
    "trade_id": "TEST-001",
    "strategy_version": "0.9",
    "symbol": "SPY",
    "side": "LONG",
    "entry_time": "2026-07-19T09:35:00",
    "exit_time": "2026-07-19T10:12:00",
    "entry_price": 650.25,
    "exit_price": 653.82,
    "quantity": 10,
    "position_value": 6502.50,
    "account_equity_at_entry": 100000.00,
    "initial_risk_dollars": 100.00,
    "stop_loss_percent": 0.01,
    "take_profit_percent": 0.02,
    "gross_pnl": 35.70,
    "return_percent": 0.55,
    "holding_minutes": 37,
    "exit_reason": "TAKE_PROFIT",
    "market_regime": "BULLISH",
    "confidence": 94,
    "paper_trade": True,
}

print("=" * 50)
print("LOCKBOT NOTIFICATION TEST")
print("=" * 50)

success = send_completed_trade_notification(
    sample_trade
)

print()
print(f"Notification Sent: {success}")
print("=" * 50)