"""
supervisor.py - Watchdog for momentum_trading_bot.py
Keeps the bot alive 24/7: restarts on crash, backs off on repeated failures,
logs everything, sends Telegram alerts on death/restart.

Run THIS via Task Scheduler - it runs your bot as a child process.
"""

import subprocess
import sys
import time
import os
from datetime import datetime
from pathlib import Path

from heartbeat import send_telegram  # same folder

# ---------- CONFIG ----------
BOT_SCRIPT = Path(__file__).parent / "momentum_trading_bot.py"
PYTHON = sys.executable                      # uses same Python that runs supervisor
MAX_RAPID_CRASHES = 5                        # crashes within window before long cooldown
RAPID_WINDOW_SEC = 600                       # 10 minutes
COOLDOWN_SEC = 1800                          # 30 min pause after crash-loop
RESTART_DELAY_SEC = 15                       # normal delay between restarts
LOG_DIR = Path(__file__).parent / "logs"
KILL_SWITCH = Path(__file__).parent / "STOP" # create a file named STOP to halt everything
# ----------------------------

LOG_DIR.mkdir(exist_ok=True)


def log(msg: str):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "supervisor.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    crash_times = []
    log("Supervisor started.")
    send_telegram("[UP] Supervisor started - bot under watchdog.")

    while True:
        if KILL_SWITCH.exists():
            log("Kill switch (STOP file) detected. Exiting supervisor.")
            send_telegram("[DOWN] Kill switch detected. Supervisor shut down.")
            return

        log(f"Launching bot: {BOT_SCRIPT}")
        start = time.time()
        try:
            proc = subprocess.run(
                [PYTHON, str(BOT_SCRIPT)],
                cwd=str(BOT_SCRIPT.parent),
            )
            code = proc.returncode
        except Exception as e:
            log(f"Failed to launch bot: {e}")
            code = -1

        runtime = time.time() - start
        log(f"Bot exited with code {code} after {runtime:.0f}s.")

        if code == 0:
            # Clean exit - bot chose to stop. Respect it.
            log("Clean exit (code 0). Supervisor stopping.")
            send_telegram("[STOP] Bot exited cleanly. Supervisor stopped.")
            return

        # Crash handling
        now = time.time()
        crash_times = [t for t in crash_times if now - t < RAPID_WINDOW_SEC]
        crash_times.append(now)
        send_telegram(f"[WARN] Bot crashed (exit {code}, ran {runtime:.0f}s). Restarting.")

        if len(crash_times) >= MAX_RAPID_CRASHES:
            log(f"{MAX_RAPID_CRASHES} crashes in {RAPID_WINDOW_SEC}s - cooling down {COOLDOWN_SEC}s.")
            send_telegram(f"[HALT] Crash loop detected. Cooling down {COOLDOWN_SEC//60} min. CHECK LOGS.")
            time.sleep(COOLDOWN_SEC)
            crash_times = []
        else:
            time.sleep(RESTART_DELAY_SEC)


if __name__ == "__main__":
    main()

