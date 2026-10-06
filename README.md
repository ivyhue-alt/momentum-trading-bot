# momentum-trading-bot

An unattended crypto momentum bot on Alpaca paper trading, built to survive its own failures: broker-side stop losses, state reconciliation against broker records, watchdog supervision, and a kill switch that actually stops everything.

> **Paper trading only. This is a systems-reliability project, not a profitability claim.** Not investment advice and not a recommendation of any strategy. The strategy is deliberately simple; the interesting part is the operational layer around it.

## The problem this solves

A process that holds positions and runs unattended cannot be allowed to disagree with reality. Every failure mode is a chance for the bot's beliefs and the broker's records to diverge: a crash between order and confirmation, a stop loss filling while the process is down, a partial fill, a manual trade placed outside the bot. Naive bots track their own state and act on it. That works until the first crash.

## Design decisions

**The stop loss lives at the broker, not in the code.** It's submitted as a GTC stop-limit order the moment a position opens. If this process dies, if the PC reboots, if the network drops — the position is still protected. A stop loss monitored in code is only as reliable as the process monitoring it, which is exactly the thing you cannot count on.

**Take profit is monitored in code, and that asymmetry is forced.** Alpaca reserves the full quantity for an open sell order, so a second full-size sell is rejected. Only one can live at the broker. Protection is the one that must survive a crash, so protection got the slot.

**The broker is the source of truth; state.json is a cache.** Every cycle begins with reconciliation: positions gone from the broker are dropped from state, positions the broker holds that aren't tracked are adopted and given a stop. Untracked positions are checked for an existing resting sell order before placing a new one, since a duplicate would try to reserve already-reserved coins and fail.

**Real quantities are read back from the broker before selling.** Filled quantity and sellable balance differ after fees and precision. Selling the number the order confirmation reported produces intermittent rejections; selling what the broker says is actually held does not. Sell size is also floored to 6 decimals at 99.9% of holdings for the same reason.

**Dust is dropped, not retried.** A position below the minimum notional can't be meaningfully sold. It's removed from tracking rather than crashing a cycle or looping on a rejected order.

**Clean exit code 0 means stop; anything else means restart.** The supervisor respects the difference. A kill switch that the watchdog fights isn't a kill switch — creating a `STOP` file exits the bot with 0, and the supervisor sees that and stops too.

**Crash-loop detection with cooldown.** Five crashes inside ten minutes triggers a thirty-minute pause and an alert. Infinite restart hides the bug; bounded restart surfaces it.

**Sleep is sliced.** The hour between cycles is slept in 30-second increments so the kill switch is honored in seconds rather than up to an hour.

**No LLM in the loop.** Every decision is deterministic. Zero inference cost, zero latency, reproducible behavior.

## EMA implementation

The original implementation passed a single 52-bar window to both the fast (12) and slow (26) EMA, and seeded each from a single price. Both problems compress the fast/slow separation. Corrected to seed from the SMA of the first `period` values and give each EMA its own lookback.

Measured on a 200-bar synthetic series, the spread between fast and slow moved from `+1.8910` to `+1.9817` — a 4.8% difference. Real, but smaller than expected; worth fixing for correctness rather than because it transformed the signal.

## Strategy

Intentionally plain: EMA(12) above EMA(26), RSI between 55 and 75 to confirm momentum without buying an overbought top, and price above its level five bars back. Ranked by five-bar momentum, capped at 5 concurrent positions, 10% of equity each, across 33 crypto pairs on hourly bars.

Exits: +6% take profit, -3.5% broker-side stop, and a trailing lock-in that activates once unrealized gain reaches 2% and exits on a 1.5% pullback from the peak — banking a real gain rather than waiting for a target that may never arrive.

## Components

| File | Role |
|---|---|
| `momentum_trading_bot.py` | Main loop: reconcile, manage positions, scan, enter |
| `supervisor.py` | Watchdog. Runs the bot as a child process, handles crashes and cooldown |
| `heartbeat.py` | Telegram alerts and per-cycle status. No-ops silently if unconfigured |
| `check_positions.py`, `close_all.py`, `protect_all.py` | Manual operator tools |
| `TradingBotSupervisor.xml` | Windows Task Scheduler definition |

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env        # Alpaca paper keys; Telegram optional
python supervisor.py        # not the bot directly
```

Create a file named `STOP` in the project directory to shut down cleanly.

## Known limitations

- **Brief unprotected window on exit.** Taking profit requires cancelling the resting broker stop before market-selling. Between cancel and fill the position has no stop. This is a consequence of Alpaca's quantity reservation, not an oversight — the alternative is a rejected order.
- **Symbol conversion is naive.** `p.symbol.replace("USD", "/USD")` works for all 33 configured pairs but would mangle any symbol containing "USD" internally.
- **`ALPACA_PAPER` in `.env.example` is unused** — the client hardcodes `paper=True`. Live trading is not a config flip, by design.
- **No test suite.** The pure functions (`ema`, `rsi`, `signal`) are the obvious first targets since they need no broker connection.
- **Single broker, single strategy, Windows-scheduled.** Not portable without a container.
- **Strategy parameters were not walk-forward validated.** Thresholds are reasonable defaults, not optimized or out-of-sample tested. Treat the strategy as a placeholder for the operational machinery.

## What I'd do differently

Write the pure functions with tests first. The operational layer was built to survive crashes, and it does, but a process that runs without crashing is not the same as one that computes correctly. Unit tests on the signal functions are the next addition.
