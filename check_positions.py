import os, certifi
os.environ.setdefault('SSL_CERT_FILE', certifi.where())
from heartbeat import _load_env
_load_env()
from momentum_trading_bot import load_keys
from alpaca.trading.client import TradingClient

k, s = load_keys()
c = TradingClient(k, s, paper=True)
a = c.get_account()
print(f"EQUITY: ${float(a.equity):,.2f}  CASH: ${float(a.cash):,.2f}")
print("-" * 70)
for p in c.get_all_positions():
    print(f"{p.symbol:10} qty={p.qty:>20} entry=${float(p.avg_entry_price):.6f} "
          f"now=${float(p.current_price):.6f} P/L=${float(p.unrealized_pl):+.2f} "
          f"({float(p.unrealized_plpc)*100:+.2f}%) value=${float(p.market_value):.2f}")
