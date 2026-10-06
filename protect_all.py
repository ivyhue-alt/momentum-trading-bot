import os, certifi
os.environ.setdefault('SSL_CERT_FILE', certifi.where())
from heartbeat import _load_env
_load_env()
from momentum_trading_bot import load_keys
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import StopLimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

k, s = load_keys()
c = TradingClient(k, s, paper=True)

open_sells = set()
for o in c.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)):
    if str(o.side).lower().endswith("sell"):
        open_sells.add(o.symbol)

protected, skipped = [], []
for p in c.get_all_positions():
    if p.symbol in open_sells:
        skipped.append(p.symbol)
        continue
    sym = p.symbol.replace("USD", "/USD") if "/" not in p.symbol else p.symbol
    qty = float(p.qty) * 0.99  # extra margin for unknown precision limits
    entry = float(p.avg_entry_price)
    stop = round(entry * 0.965, 8)
    limit = round(stop * 0.995, 8)
    try:
        o = c.submit_order(StopLimitOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, stop_price=stop, limit_price=limit,
        ))
        protected.append((sym, qty, stop, o.id))
    except Exception as e:
        print(f"FAILED to protect {sym}: {e}")

print("PROTECTED:")
for sym, qty, stop, oid in protected:
    print(f"  {sym} qty={qty} stop={stop} order={oid}")
print("ALREADY HAD A STOP (skipped):", skipped)
