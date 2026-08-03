from trade_manager import build_paper_trading_client

client = build_paper_trading_client()
result = client.close_position("SPY")
print(f"Close order submitted: {result.id} — status: {result.status}")