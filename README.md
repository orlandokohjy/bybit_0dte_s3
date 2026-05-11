# Bybit 0DTE BTC Synthetic Straddle — Multi-Session

Automated trading bot that executes a **long-gamma synthetic straddle** strategy on BTC 0DTE (zero-days-to-expiration) options via the **Bybit V5 API**.

## Strategy Overview

The bot runs **two sessions per trading day** (10 trades per week) and constructs synthetic long straddles:

| Leg | Instrument | Direction | Purpose |
|-----|-----------|-----------|---------|
| Spot | BTCUSDT spot (10× margin) | Long | Delta-one BTC exposure |
| Puts | 2 × `qty_per_leg` BTC ITM 0DTE puts (USDT-settled) | Long | Downside protection + long gamma |

A synthetic straddle replicates the payoff of being long both a call and a put — profiting from **large moves in either direction** while the premium paid (put cost + margin) is the max loss.

### Schedule

| Session | Window (UTC) | Days (UTC) | qty_per_leg | Notes |
|---|---|---|---|---|
| afternoon (1st entry) | 13:30–15:30 | Mon–Fri | 0.25 BTC | Targets next-day 08:00 UTC expiry |
| morning (2nd entry) | 01:00–02:00 | Tue–Sat | 0.25 BTC | Targets same-day 08:00 UTC expiry |

Both sessions of one trading day share the **same 08:00 UTC option expiry** — e.g. Monday afternoon (13:30 UTC) and Tuesday morning (01:00 UTC) both target Tuesday's 08:00 UTC expiry, and roll up into the **Tuesday trading-day report**.

The position is held continuously within each session window (no take-profit; hard close at the close time).

### Reports

* **Daily report** — chained off the **morning close** (Tue–Sat ~02:00 UTC). Aggregates both sessions of the trading day. Risk Metrics & Edge sections removed per the live config.
* **Weekly report** — chained off the **Saturday morning close** (~02:00 UTC). Covers the full Mon→Sat trading-week window.
* No standalone DAILY SUMMARY message — replaced by the daily report.

### Execution Details

- **Spot orders** use **GTC limit orders** posted at the bid/ask for **maker status** and trading rebates. Orders chase the book (cancel and re-post at updated bid/ask every 1 second) until filled.
- **Option orders** use a **hybrid maker/taker** strategy: first 5 attempts post at the bid (buy) / ask (sell) for **maker rebates**; if not filled, remaining attempts escalate to the ask (buy) / bid (sell) to guarantee execution. Chase interval: 2 seconds.
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
├── reporting/
│   └── daily_report.py         # Quant metrics & Telegram daily report
├── utils/
│   ├── logging_config.py       # Structured logging (structlog)
│   ├── time_utils.py           # UTC/SGT time helpers
│   └── volume_tracker.py       # Monthly spot volume tracking
├── test_run.py                 # Manual test: entry → hold → close on demand
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

### 3. Bybit account prerequisites

The bot auto-configures the following on startup, but your account must meet these prerequisites:

- **Unified Trading Account (UTA)** enabled
- **Net equity ≥ 1,000 USDC** (required for Portfolio Margin)
- **BTC enabled as collateral** in UTA settings (required to sell spot BTC)
- **No Hedge Mode positions** (Portfolio Margin requires One-Way mode)

The bot will automatically:
1. Switch to **Portfolio Margin** mode (lower maintenance margin for our hedge)
2. Enable **Spot Hedging** (spot included in stress testing)
3. Set **10× spot margin leverage**

### 4. Run (production)

```bash
python main.py
```

### 5. Manual test run (Demo)

Run a single entry → hold → close cycle on demand, without waiting for the scheduler:

```bash
# In .env, set:
#   BYBIT_DEMO=true
#   DRY_RUN=false

# Execute with 60-second hold (default)
python test_run.py

# Or close immediately after entry (execution test only)
HOLD_SECONDS=0 python test_run.py
```

This connects to Bybit Demo (real market data, simulated fills), runs the full algo cycle, and prints the performance report. Use this to verify everything works before going live.

## Key Parameters (config.py)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SPOT_LEVERAGE` | 10 | Spot cross-margin leverage |
| `AFTERNOON_QTY_PER_LEG` | 0.25 BTC | qty/leg for afternoon session (13:30–15:30 UTC) |
| `MORNING_QTY_PER_LEG` | 0.25 BTC | qty/leg for morning session (01:00–02:00 UTC) |
| `NUM_PUTS` | 2 | Put contracts per straddle |
| `NUM_STRADDLES_OVERRIDE` | 1 | Force exactly this many straddles per session. `0` = compound sizing |
| `ALLOC_PCT` | 0.80 | Compound mode: % of current equity per session (only used when override = 0) |
| `INITIAL_CAPITAL_USD` | 8,000 | Starting equity for compound tracking |
| `SESSIONS` | (afternoon, morning) | Multi-session list — see config.py |
| `MAX_DAILY_LOSS_PCT` | None | Daily loss halt disabled |
| `RESET_STATE_ON_BOOT` | false | One-shot wipe of state files (auto-disables after running) |

## Margin Methodology

The algo operates within a **Bybit Unified Trading Account (UTA)** and uses two distinct margin products:

### Spot Margin (Cross-Margin)

The long BTC leg uses **spot cross-margin** at 10× leverage:

```
Spot margin required = (QTY_PER_LEG × spot_price × num_straddles) / SPOT_LEVERAGE

Example: 0.5 BTC × $69,000 × 1 straddle / 10× = $3,450 margin
         Notional exposure: $34,500
         Margin locked:     $3,450
```

- **Product**: Bybit Spot Margin (not perpetual futures)
- **Leverage**: Set via `/v5/spot-margin-trade/set-leverage` (range 2–10×)
- **Order flag**: `isLeverage=1` on buy orders tells Bybit to use borrowed USDT
- **Collateral**: USDT in the UTA; BTC must be enabled as collateral asset
- **Interest**: Bybit charges hourly interest on borrowed USDT (accrues while position is open)
- **Rebate**: Spot limit orders earn maker rebates (the reason we use spot instead of perps)

### Option Margin

The put legs are **bought options** (long puts) — these require **no margin**, only the premium paid upfront:

```
Option cost = NUM_PUTS × QTY_PER_LEG × put_premium × num_straddles

Example: 2 puts × 0.5 BTC × $2,040 × 1 straddle = $2,040
```

- **Product**: USDT-settled BTC options on Bybit
- **Margin**: Zero — long options are fully paid, no liquidation risk on the option leg
- **Settlement**: Cash-settled in USDT at 08:00 UTC daily

### Total Capital Per Trade

The total capital deployed per session is the sum of both legs:

```
Total capital = spot_margin + option_premium_cost

Example: $3,450 (spot margin) + $2,040 (2 puts) = $5,490 per straddle
```

A **pre-flight capital check** runs before any orders are placed:
1. Computes exact margin + premium cost per straddle
2. Adds a 5% slippage buffer on the option premium
3. Calculates max straddles that fit within 60% of current equity
4. Only proceeds if at least 1 complete straddle can be funded

This prevents orphaned positions (e.g. buying spot but not having enough for the puts).

### Portfolio Margin Mode (Recommended)

The algo automatically switches the Bybit UTA to **Portfolio Margin** mode on startup. This is the optimal margin mode for a hedged strategy like ours.

#### Why Portfolio Margin?

Bybit UTA supports three margin modes: Isolated, Cross (default), and Portfolio Margin. Here is how each evaluates our position (Long BTC Spot + Long BTC Puts):

| Mode | Margin Calculation | Hedge Recognition | Our Strategy |
|------|-------------------|-------------------|--------------|
| **Isolated** | Per-position | None | **Eliminated** — doesn't support Spot Margin or Options |
| **Cross** | Sum of individual position margins | None | Works, but treats spot and puts as unrelated |
| **Portfolio** | Stress testing across entire portfolio | **Yes** — offsets hedged risk | **Optimal** — recognises our spot+puts hedge |

Under **Cross Margin**, Bybit calculates maintenance margin for each position independently and sums them. It doesn't know that our puts protect our spot downside.

Under **Portfolio Margin**, Bybit runs **stress testing** — simulating large price moves and IV shocks — and evaluates the net P&L of the entire portfolio:

```
Stress scenario (BTC drops 10%):
  Cross Margin sees:  Spot loss = -$3,450  |  Put margin = $0  →  total risk = $3,450
  Portfolio Margin:   Spot loss = -$3,450  |  Put gain ≈ +$3,000  →  net risk ≈ $450
```

The result: **maintenance margin is significantly lower** because the stress-test maximum loss of a hedged portfolio is much smaller than the sum of individual position risks.

#### What is identical across Cross and Portfolio Margin

- **Spot margin borrowing** — same formula, same leverage (10×), same interest rate
- **Option premium** — same cost (long puts always require only the premium, zero margin)
- **Execution** — same order types, same API calls
- **Borrowing interest** — same hourly rate on borrowed USDT

#### What improves with Portfolio Margin

- **Maintenance margin** — reduced via stress-test netting of our hedge
- **Liquidation risk** — further from liquidation threshold
- **Capital efficiency** — more free equity available as buffer

#### Setup (automatic)

The algo calls two API endpoints on startup:

1. `POST /v5/account/set-margin-mode` → `PORTFOLIO_MARGIN`
2. `POST /v5/account/set-hedging-mode` → `ON` (includes spot in stress testing)

**Requirements**: Net equity ≥ 1,000 USDC equivalent, no Hedge Mode positions.

If the account is already in Portfolio Margin mode, the calls are idempotent (no-op with a warning log).

### Borrowing Interest

Bybit charges **hourly interest** on borrowed USDT (the leveraged portion of our spot buy). The rate is floating, based on the USDT lending pool utilisation. For our 4-hour session hold, expect ~4 hours of interest. This is a real cost factored into the trade P&L.

Interest = Borrowed USDT × Hourly Rate × 4 hours

Current rates are visible on Bybit's [Margin Data page](https://www.bybit.com/announcement-info/fullstock-leverage-uta/) or via the `/v5/spot-margin-trade/data` API.

## Execution Algorithm

### Entry Sequence (per session)

1. **Refresh 0DTE option chain** — fetch all USDT-settled puts for the trading day's 08:00 UTC expiry
2. **Select ITM put** — scan from nearest ITM strike upward, pick first with bid/ask spread < 10%
3. **Pre-flight sizing** — compute capital for `NUM_STRADDLES_OVERRIDE` straddles (or compound sizing if override = 0)
4. **Buy spot** (GTC limit at bid for maker rebate):
   - Post limit buy at current bid price
   - Wait up to 1 second for fill
   - If not filled, cancel and re-post at updated bid
   - Chase up to 15 attempts
5. **Buy puts** (GTC limit at bid for maker rebate):
   - Post limit buy at current bid price
   - Wait up to 2 seconds for fill
   - If not filled, cancel and re-post at updated bid
   - Chase up to 15 attempts per put leg
   - 2 put legs per straddle, each QTY_PER_LEG BTC
6. If any leg fails, all previously filled legs are unwound immediately

### Exit Sequence (per session close)

1. **Sell spot first** — GTC limit at ask, same chase logic as entry
2. **Sell puts** — GTC limit at ask, same chase logic as entry
3. Log trade, update equity. If this is the **morning close** (LAST_CLOSE_SESSION_NAME), the daily report is sent immediately. On Saturday, the weekly report is also chained.

### Order Types

| Leg | Order Type | Time in Force | Rationale |
|-----|-----------|---------------|-----------|
| Spot buy | Limit | GTC | Post at bid → maker rebate |
| Spot sell | Limit | GTC | Post at ask → maker rebate |
| Put buy | Limit | GTC | Post at bid → maker rebate |
| Put sell | Limit | GTC | Post at ask → maker rebate |

## Risk Controls

- **Pre-flight capital check** — ensures enough funds for *complete* straddles (spot + puts) before placing any orders
- **Daily loss limit** — currently disabled (`None`); set `MAX_DAILY_LOSS_PCT` in config to re-enable
- **API circuit breaker** — pauses trading after 5 consecutive API errors (5-minute cooldown)
- **Atomic entry/exit** — if any leg fails, all other legs are unwound immediately
- **Maker execution** — all orders (spot + options) post at bid/ask with GTC limits for maker rebate; chase logic re-posts at updated prices
