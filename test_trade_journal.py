from trade_journal import log_trade

log_trade(
    symbol="SPY",
    action="SELL",
    entry_price=751.37,
    exit_price=746.80,
    quantity=1,
    pnl=-4.57,
    reason="STOP_LOSS",
)

print("Trade journal updated successfully.")