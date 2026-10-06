import os, certifi, time
os.environ.setdefault('SSL_CERT_FILE', certifi.where())
from heartbeat import _load_env
_load_env()
from momentum_trading_bot import load_keys
from alpaca.trading.client import TradingClient

k, s = load_keys()
c = TradingClient(k, s, paper=True)

print("Cancelling all open orders...")
try:
    c.cancel_orders()
    time.sleep(3)
    print("  orders cancelled.")
except Exception as e:
    print(f"  cancel error: {e}")

print("Closing all positions...")
try:
    # Alpaca's built-in: liquidates every position at market.
    closed = c.close_all_positions(cancel_orders=True)
    print(f"  close_all_positions submitted for {len(closed) if closed else 0} positions.")
except Exception as e:
    print(f"  close_all error: {e}")

time.sleep(5)
print("\n--- FINAL STATE ---")
a = c.get_account()
print(f"Equity: ${float(a.equity):,.2f}  Cash: ${float(a.cash):,.2f}")
pos = c.get_all_positions()
if not pos:
    print("All positions closed. Flat.")
else:
    print("Remaining (may still be settling):")
    for p in pos:
        print(f"  {p.symbol} qty={p.qty} value=${float(p.market_value):.2f}")
