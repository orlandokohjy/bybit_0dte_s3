# Bybit 0DTE BTC Synthetic Straddle — Session 3

Automated trading bot that executes a **long-gamma synthetic straddle** strategy on BTC 0DTE (zero-days-to-expiration) options via the **Bybit V5 API**.

## Strategy Overview

The bot runs a single daily session (14:00–18:00 UTC, Monday to Friday) and constructs synthetic long straddles:

| Leg | Instrument | Direction | Purpose |
|-----|-----------|-----------|---------|
| Spot | BTCUSDT spot (10× margin) | Long | Delta-one BTC exposure |
| Puts | 2 × 0.5 BTC ITM 0DTE puts (USDT-settled) | Long | Downside protection + long gamma |

A synthetic straddle replicates the payoff of being long both a call and a put — profiting from **large moves in either direction** while the premium paid (put cost + margin) is the max loss.

### Daily Workflow

1. **14:00 UTC** — Algo sizes the position based on 60% of current equity (compound growth), runs a pre-flight capital check to ensure enough funds for complete straddles, then enters:
   - Buys BTC spot on margin (Post-Only limit orders for maker rebate)
   - Buys 2 ITM put options per straddle (aggressive limit chase)
2. **18:00 UTC** — Hard close: sells all spot then sells all puts. No early exit.

### Execution Details

- **Spot orders** use **Post-Only limit orders** posted at the bid/ask to guarantee **maker status** and earn trading rebates. Orders chase the book (re-post at updated bid/ask every 1 second) until filled.
- **Option orders** use aggressive IOC (Immediate-or-Cancel) limit orders with price chasing to ensure fills.
- A **pre-flight capital check** verifies sufficient funds for all legs (spot margin + put premiums with 5% slippage buffer) before placing any trades, preventing orphaned positions.

## Project Structure

```
bybit_0dte_s3/
├── main.py                     # Entry point — orchestrates the daily session
├── config.py                   # All tuneable parameters and environment variables
├── requirements.txt
├── .env.example
├── core/
│   ├── exchange.py             # Bybit V5 REST + WebSocket wrapper
│   ├── portfolio.py            # Local equity tracking, straddle state, trade log
│   ├── notifier.py             # Telegram notifications
│   └── scheduler.py            # APScheduler — daily entry/close triggers
├── data/
│   ├── market_data.py          # Spot and option market data (REST + WS)
│   └── option_chain.py         # 0DTE option chain filter (USDT-settled only)
├── strategy/
│   ├── straddle_builder.py     # Atomic straddle entry and exit
│   ├── position_sizer.py       # Compound sizing + pre-flight capital check
│   ├── option_selector.py      # Nearest ITM put selection
│   └── exit_manager.py         # Hard close at session end
├── risk/
│   └── risk_manager.py         # Daily loss limit, circuit breaker
├── utils/
│   ├── logging_config.py       # Structured logging (structlog)
│   ├── time_utils.py           # UTC/SGT time helpers
│   └── volume_tracker.py       # Monthly spot volume tracking
├── state/                      # Runtime state (equity, positions, trade log)
└── logs/                       # Structured JSON logs
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your Bybit API credentials
```

| Variable | Description |
|----------|-------------|
| `BYBIT_API_KEY` | Bybit V5 API key |
| `BYBIT_API_SECRET` | Bybit V5 API secret |
| `BYBIT_DEMO` | `true` for demo trading account |
| `DRY_RUN` | `true` to simulate orders without executing |
| `TELEGRAM_BOT_TOKEN` | Optional — Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | Optional — Telegram chat ID for alerts |

### 3. Ensure spot margin is activated

The bot uses 10× spot margin leverage. Make sure spot margin trading is enabled on your Bybit Unified Trading Account.

### 4. Run

```bash
python main.py
```

## Key Parameters (config.py)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SPOT_LEVERAGE` | 10 | Spot cross-margin leverage |
| `QTY_PER_LEG` | 0.5 BTC | BTC per leg per straddle |
| `NUM_PUTS` | 2 | Put contracts per straddle |
| `ALLOC_PCT` | 0.60 | 60% of equity allocated per session |
| `INITIAL_CAPITAL_USD` | 10,000 | Starting equity for compound tracking |
| `SESSION_ENTRY_UTC` | 14:00 | Daily entry time |
| `SESSION_CLOSE_UTC` | 18:00 | Daily hard close time |
| `MAX_DAILY_LOSS_PCT` | 0.10 | Halt trading if daily loss exceeds 10% |

## Risk Controls

- **Pre-flight capital check** — ensures enough funds for *complete* straddles (spot + puts) before placing any orders
- **Daily loss limit** — halts the session if daily P&L drops below -10% of equity
- **API circuit breaker** — pauses trading after 5 consecutive API errors (5-minute cooldown)
- **Atomic entry/exit** — if any leg fails, all other legs are unwound immediately
- **Post-Only enforcement** — spot orders are rejected (not executed as taker) if they would cross the spread
