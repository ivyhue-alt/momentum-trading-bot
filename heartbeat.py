"""
heartbeat.py - Telegram alerts + cycle heartbeat.
Import into both supervisor.py and momentum_trading_bot.py.

Setup (one-time, 3 minutes):
1. In Telegram, message @BotFather ? /newbot ? copy the token.
2. Message your new bot anything, then visit:
   https://api.telegram.org/bot<TOKEN>/getUpdates  ? copy "chat":{"id": ...}
3. Put both values in .env (see .env.example).

If .env values are missing, all functions no-op silently - bot still runs.
"""

import os
import json
import urllib.request, ssl, certifi
from datetime import datetime
from pathlib import Path

# Lightweight .env loader (no extra dependency needed)
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(message: str) -> bool:
    """Send a message. Returns True on success, False otherwise. Never raises."""
    if not TOKEN or not CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        data = json.dumps({"chat_id": CHAT_ID, "text": message}).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context(cafile=certifi.where())) as resp:
            return resp.status == 200
    except Exception:
        return False


def heartbeat(cycle_num: int, positions: int = 0, equity: float | None = None):
    """Call once per bot cycle. Sends a compact status ping."""
    eq = f" | equity ${equity:,.2f}" if equity is not None else ""
    send_telegram(
        f"[BEAT] Cycle {cycle_num} @ {datetime.now().strftime('%H:%M')} "
        f"| {positions} open{eq}"
    )


def alert_trade(side: str, symbol: str, qty, price=None):
    """Call when a trade fires."""
    p = f" @ ${price}" if price else ""
    emoji = "[UP]" if side.lower() == "buy" else "[DOWN]"
    send_telegram(f"{emoji} {side.upper()} {qty} {symbol}{p}")


