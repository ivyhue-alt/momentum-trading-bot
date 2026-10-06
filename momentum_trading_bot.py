"""
momentum_trading_bot.py - Standalone autonomous crypto momentum bot (Alpaca paper).

Architecture decisions (learned from live MCP session logs):
- SL lives AT THE BROKER as a stop-limit order ? position is protected even if
  this bot/PC dies. TP is monitored IN CODE ? avoids Alpaca's quantity
  reservation rejection (can't have two full-size sell orders open at once).
- State persisted to state.json every cycle ? restart-safe.
- No LLM anywhere in the loop. Zero token cost.
- Crashes raise normally ? supervisor.py sees nonzero exit and restarts.
  Clean shutdown (STOP file) exits 0 ? supervisor stops.

Run via supervisor.py, not directly (except for testing).
"""

import json
import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, StopLimitOrderRequest, GetOrdersRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

from heartbeat import heartbeat, alert_trade, send_telegram

# ---------------- CONFIG ----------------
BASE = Path(__file__).parent
STATE_FILE = BASE / "state.json"
KILL_SWITCH = BASE / "STOP"
LOG_DIR = BASE / "logs"

SYMBOLS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "ADA/USD", "AVAX/USD",
    "XRP/USD", "LINK/USD", "DOT/USD", "UNI/USD", "AAVE/USD", "LTC/USD",
    "BCH/USD", "SHIB/USD", "PEPE/USD", "BONK/USD", "TRUMP/USD", "WIF/USD",
    "ARB/USD", "LDO/USD", "ONDO/USD", "RENDER/USD", "POL/USD", "GRT/USD",
    "FIL/USD", "BAT/USD", "CRV/USD", "SUSHI/USD", "YFI/USD", "XTZ/USD",
    "SKY/USD", "HYPE/USD", "PAXG/USD",
]  # 33 pairs

CYCLE_MINUTES = 60          # scan interval
BARS_HOURS = 100            # history pulled per scan (1Hour bars)
EMA_FAST, EMA_SLOW = 12, 26
RSI_PERIOD = 14
RSI_MIN, RSI_MAX = 55, 75   # momentum confirmation window (avoid overbought)

MAX_POSITIONS = 5
POSITION_PCT = 0.10         # 10% of equity per position
TAKE_PROFIT_PCT = 0.06      # +6% -- the "full target" exit
LOCK_IN_TRIGGER_PCT = 0.02  # once up 2%, start protecting that gain
TRAIL_PCT = 0.015           # exit if price falls 1.5% from its peak after triggering
STOP_LOSS_PCT = 0.035       # -3.5% (stop trigger)
STOP_LIMIT_BUFFER = 0.005   # limit sits 0.5% below stop trigger
MIN_NOTIONAL = 25.0
# -----------------------------------------

LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bot")


# ---------------- helpers ----------------
def load_keys():
    """Read Alpaca keys from .env (same loader pattern as heartbeat)."""
    import os
    key = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not sec:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")
    return key, sec


def ema(values, period):
    """Standard EMA. Seeded with the SMA of the first `period` values rather
    than a single price - seeding off one bar leaves the average dominated by
    that bar for roughly `period` steps. Computed over the full series passed
    in, so fast and slow must each receive enough history to converge."""
    if len(values) < period:
        return sum(values) / len(values)
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"cycle": 0, "positions": {}}  # positions: symbol -> {qty, entry, tp, sl_order_id}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------- core ----------------
class Bot:
    def __init__(self):
        key, sec = load_keys()
        self.trade = TradingClient(key, sec, paper=True)
        self.data = CryptoHistoricalDataClient()  # crypto data needs no keys
        self.state = load_state()

    # ---- reconciliation: trust the broker, not the file ----
    def reconcile(self):
        live = {p.symbol.replace("USD", "/USD") if "/" not in p.symbol else p.symbol: p
                for p in self.trade.get_all_positions()}
        tracked = self.state["positions"]

        # What open SELL orders already exist at the broker, by symbol?
        # A position with an existing stop must NOT get a second one (it would
        # try to reserve already-reserved coins and fail).
        existing_sell = {}
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            for o in self.trade.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)):
                if str(o.side).lower().endswith("sell"):
                    existing_sell[o.symbol.replace("USD", "/USD") if "/" not in o.symbol else o.symbol] = str(o.id)
        except Exception as e:
            log.warning(f"Could not list open orders: {e}")

        # Positions closed while we were down (SL filled) -> forget them
        for sym in list(tracked):
            if sym not in live:
                log.info(f"Reconcile: {sym} gone at broker (SL filled or manual close). Removing.")
                send_telegram(f"[INFO] {sym} closed while bot was down.")
                del tracked[sym]

        # Positions at broker we don't track (manual/MCP trades) -> adopt, protect
        for sym, p in live.items():
            if sym not in tracked and sym in SYMBOLS:
                entry = float(p.avg_entry_price)
                qty = float(p.qty)
                if sym in existing_sell:
                    log.info(f"Reconcile: adopting {sym} qty={qty}; existing stop found, reusing it.")
                    sl_id = existing_sell[sym]
                else:
                    log.info(f"Reconcile: adopting untracked position {sym} qty={qty}")
                    sl_id = self.place_stop_loss(sym, qty, entry)
                tracked[sym] = {
                    "qty": qty, "entry": entry,
                    "tp": entry * (1 + TAKE_PROFIT_PCT),
                    "sl_order_id": sl_id,
                }
        save_state(self.state)

    def get_bars(self, symbols):
        # Per-symbol fetch: Alpaca's multi-symbol crypto request silently drops
        # all but the first symbol, so each must be fetched individually.
        # Explicit start time required or the endpoint returns 0 bars.
        from datetime import datetime, timedelta, timezone
        start = datetime.now(timezone.utc) - timedelta(hours=BARS_HOURS * 2)
        out = {}
        for sym in symbols:
            try:
                req = CryptoBarsRequest(
                    symbol_or_symbols=[sym],
                    timeframe=TimeFrame.Hour,
                    start=start,
                    limit=BARS_HOURS,
                )
                bars = self.data.get_crypto_bars(req).data.get(sym, [])
                if bars:
                    out[sym] = bars
            except Exception as e:
                log.warning(f"bars fetch failed for {sym}: {e}")
        return out

    def signal(self, closes):
        """True if momentum entry conditions met."""
        if len(closes) < EMA_SLOW + 5:
            return False
        # Each EMA gets its own lookback. Passing one 52-bar window to both
        # compressed the fast/slow spread by ~5% on a synthetic series.
        fast = ema(closes[-EMA_FAST * 4:], EMA_FAST)
        slow = ema(closes[-EMA_SLOW * 4:], EMA_SLOW)
        r = rsi(closes, RSI_PERIOD)
        uptrend = closes[-1] > closes[-6]  # higher than 5 bars ago
        return fast > slow and RSI_MIN <= r <= RSI_MAX and uptrend

    def place_stop_loss(self, symbol, qty, entry):
        stop = round(entry * (1 - STOP_LOSS_PCT), 6)
        limit = round(stop * (1 - STOP_LIMIT_BUFFER), 6)
        order = self.trade.submit_order(StopLimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=stop, limit_price=limit,
        ))
        log.info(f"SL placed {symbol}: stop {stop} limit {limit} (id {order.id})")
        return str(order.id)

    def enter(self, symbol, equity):
        notional = round(equity * POSITION_PCT, 2)
        if notional < MIN_NOTIONAL:
            return
        order = self.trade.submit_order(MarketOrderRequest(
            symbol=symbol, notional=notional, side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        ))
        # wait for fill, then read the ACTUAL settled position from the broker
        # (filled_qty can differ slightly from sellable balance after fees/precision)
        for _ in range(20):
            time.sleep(2)
            o = self.trade.get_order_by_id(order.id)
            if o.filled_qty and float(o.filled_qty) > 0 and o.filled_avg_price:
                entry = float(o.filled_avg_price)
                time.sleep(2)  # let the position settle
                qty = float(o.filled_qty)
                try:
                    pos = self.trade.get_open_position(symbol.replace("/", ""))
                    qty = float(pos.qty)  # broker truth
                except Exception:
                    pass
                sl_id = self.place_stop_loss(symbol, qty, entry)
                self.state["positions"][symbol] = {
                    "qty": qty, "entry": entry,
                    "tp": entry * (1 + TAKE_PROFIT_PCT),
                    "sl_order_id": sl_id,
                }
                save_state(self.state)
                alert_trade("buy", symbol, qty, entry)
                log.info(f"ENTER {symbol} qty={qty} entry={entry}")
                return
        log.warning(f"{symbol} buy not confirmed filled in 40s - will reconcile next cycle.")

    def exit_take_profit(self, symbol, pos):
        """Exit a position. Reads the REAL quantity from the broker (state can
        be stale if a stop-loss already sold the position). Cancels any resting
        sell order first, then market-sells what actually exists."""
        # Get the true held quantity from the broker.
        try:
            bpos = self.trade.get_open_position(symbol.replace("/", ""))
            real_qty = float(bpos.qty)
        except Exception:
            # No position at broker at all -> it's already gone. Forget it.
            log.info(f"{symbol} not held at broker; removing stale state entry.")
            self.state["positions"].pop(symbol, None)
            save_state(self.state)
            return

        # Dust: too small to sell meaningfully. Drop from tracking, don't crash.
        if real_qty <= 0 or float(bpos.market_value) < MIN_NOTIONAL:
            log.info(f"{symbol} is dust (qty={real_qty}); removing from tracking.")
            self.state["positions"].pop(symbol, None)
            save_state(self.state)
            return

        # Cancel any resting sell order (the broker stop) so the coins are free.
        try:
            if pos.get("sl_order_id"):
                self.trade.cancel_order_by_id(pos["sl_order_id"])
                time.sleep(1)
        except Exception as e:
            log.warning(f"Cancel SL for {symbol} failed ({e}) - may have just filled. Re-checking.")
            try:
                bpos = self.trade.get_open_position(symbol.replace("/", ""))
                real_qty = float(bpos.qty)
            except Exception:
                self.state["positions"].pop(symbol, None)
                save_state(self.state)
                return

        sell_qty = int(real_qty * 0.999 * 1e6) / 1e6
        self.trade.submit_order(MarketOrderRequest(
            symbol=symbol, qty=sell_qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
        ))
        alert_trade("sell", symbol, sell_qty)
        log.info(f"EXIT {symbol} qty={sell_qty}")
        self.state["positions"].pop(symbol, None)
        save_state(self.state)

    def cycle(self):
        self.state["cycle"] += 1
        n = self.state["cycle"]
        log.info(f"--- Cycle {n} ---")

        self.reconcile()

        acct = self.trade.get_account()
        equity = float(acct.equity)

        bars = self.get_bars(SYMBOLS)

        # 1) Manage open positions (TP check + trailing lock-in)
        for sym in list(self.state["positions"]):
            pos = self.state["positions"][sym]
            sym_bars = bars.get(sym, [])
            if not sym_bars:
                continue
            price = float(sym_bars[-1].close)
            entry = pos["entry"]

            # Full +6% target reached: take it, no further checks needed.
            if price >= pos["tp"]:
                self.exit_take_profit(sym, pos)
                continue

            # Trailing lock-in: once unrealized gain has reached
            # LOCK_IN_TRIGGER_PCT at any point, track the peak price and
            # exit if price pulls back TRAIL_PCT from that peak -- this
            # banks a real gain instead of waiting for the full TP target
            # (which may never come) or riding a reversal into a loss.
            peak = max(pos.get("peak", entry), price)
            pos["peak"] = peak
            peak_gain = peak / entry - 1
            if peak_gain >= LOCK_IN_TRIGGER_PCT:
                trail_stop = peak * (1 - TRAIL_PCT)
                if price <= trail_stop and price > entry:
                    log.info(f"{sym} trailing lock-in: peak gain {peak_gain*100:.2f}%, "
                             f"price pulled back to {price}, exiting to bank profit.")
                    self.exit_take_profit(sym, pos)

        # 2) Scan for entries
        open_count = len(self.state["positions"])
        if open_count < MAX_POSITIONS:
            candidates = []
            for sym in SYMBOLS:
                if sym in self.state["positions"]:
                    continue
                sym_bars = bars.get(sym, [])
                closes = [float(b.close) for b in sym_bars]
                if closes and self.signal(closes):
                    momentum = closes[-1] / closes[-6] - 1
                    candidates.append((momentum, sym))
            candidates.sort(reverse=True)
            log.info(f"Scan: {len(candidates)} signal(s); {open_count} open; equity ${equity:,.2f}")
            for _, sym in candidates[: MAX_POSITIONS - open_count]:
                self.enter(sym, equity)
        else:
            log.info(f"Scan skipped: at max {MAX_POSITIONS} positions; equity ${equity:,.2f}")

        save_state(self.state)
        log.info(f"Cycle {n} complete. {len(self.state['positions'])} open. Sleeping {CYCLE_MINUTES}m.")
        heartbeat(n, positions=len(self.state["positions"]), equity=equity)

    def run(self):
        send_telegram("[BOT] Bot started.")
        log.info(f"Bot started. Tracking {len(SYMBOLS)} pairs, "
                 f"{len(self.state['positions'])} open positions in state.")
        while True:
            if KILL_SWITCH.exists():
                log.info("STOP file found. Clean shutdown.")
                send_telegram("[BOT] Bot shut down via STOP file.")
                sys.exit(0)
            self.cycle()
            # sleep in 30s slices so STOP is honored quickly
            for _ in range(CYCLE_MINUTES * 2):
                if KILL_SWITCH.exists():
                    break
                time.sleep(30)


if __name__ == "__main__":
    try:
        Bot().run()
    except Exception:
        # Under pythonw there is no console, so an uncaught traceback vanishes.
        # Write it to bot.log, then exit non-zero so supervisor.py restarts us.
        # (sys.exit(0) on the STOP file is SystemExit and is not caught here.)
        log.exception("Unhandled exception - bot exiting with code 1.")
        logging.shutdown()
        sys.exit(1)
