"""
Central configuration — Bybit 0DTE BTC Synthetic Straddle (Session 3 Only).

Single daily session: 14:00–18:00 UTC, Mon–Fri.
Position: 0.5 BTC spot (margin) + 2 × 0.5 BTC ITM puts per straddle.
Compound sizing: 60 % of current equity, no cap.
"""
from __future__ import annotations

import os
from datetime import time

from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────── Bybit API ───────────────────────────

BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET: str = os.getenv("BYBIT_API_SECRET", "")

TESTNET: bool = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
DEMO: bool = os.getenv("BYBIT_DEMO", "false").lower() == "true"
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

# ─────────────────────────── Instrument ──────────────────────────

SPOT_SYMBOL = "BTCUSDT"
SPOT_CATEGORY = "spot"
SPOT_LEVERAGE: int = 10          # margin leverage for spot
BASE_COIN = "BTC"
SETTLE_COIN = "USDT"
ACCOUNT_TYPE = "UNIFIED"

# ─────────────────────────── Position ────────────────────────────

QTY_PER_LEG: float = 0.5       # BTC per leg per straddle
NUM_PUTS: int = 2               # put contracts per straddle (2 × 0.5 BTC)

# ─────────────────────────── Sizing (Compound) ──────────────────

INITIAL_CAPITAL_USD: float = 7_900.0
ALLOC_PCT: float = 0.80        # 80 % of current equity

# ──────────────────────── Option Filters ─────────────────────────

MAX_BID_ASK_SPREAD_PCT: float = 0.10   # skip puts with spread > 10 % of mid
MIN_OPEN_INTEREST: float = 0.0

# ────────────────────────── Session ──────────────────────────────

SESSION_ENTRY_UTC: time = time(14, 0)
SESSION_CLOSE_UTC: time = time(18, 0)
REPORT_UTC: time = time(19, 0)
WEEKLY_REPORT_UTC: time = time(20, 0)
ALLOWED_WEEKDAYS: set[int] = {0, 1, 2, 3, 4}  # Mon–Fri

# ───────────────────── Exit ───────────────────────────────────────
# No take-profit — all positions hold until 18:00 UTC hard close.

# ──────────────────── Execution Settings ─────────────────────────

# Spot: GTC limit orders at bid/ask for maker rebate
SPOT_CHASE_INTERVAL_SEC: float = 3.0
SPOT_CHASE_MAX_ATTEMPTS: int = 15
SPOT_TICK_SIZE: float = 0.10       # BTCUSDT spot tick size on Bybit

# Options: maker-only chase — 50% gap-narrowing toward ask + fair-value cap
# (no taker fallback; bails on deadline → session is skipped)
OPTION_CHASE_INTERVAL_SEC: float = 3.0
OPTION_CHASE_MAX_ATTEMPTS: int = 50  # legacy, unused by new chase logic
OPTION_CHASE_DEADLINE_SEC: float = float(
    os.getenv("OPTION_CHASE_DEADLINE_SEC", "900.0")
)  # 15 min total chase time before giving up

# Each retry narrows the remaining bid-ask gap by this fraction.
# 0.5 = halve the gap toward (ask − 1 tick).
OPTION_CHASE_GAP_NARROW_PCT: float = float(
    os.getenv("OPTION_CHASE_GAP_NARROW_PCT", "0.5")
)

# Hard ceiling on slippage vs mark price.
# Buy will never post above mark × this; sell will never post below mark / this.
OPTION_CHASE_MAX_SLIPPAGE_FACTOR: float = float(
    os.getenv("OPTION_CHASE_MAX_SLIPPAGE_FACTOR", "1.15")
)

# Pre-entry spread sanity gate — skip session if put (ask − bid) / mid exceeds this.
# 0.30 = skip if spread is wider than 30% of mid.
OPTION_MAX_ENTRY_SPREAD_PCT: float = float(
    os.getenv("OPTION_MAX_ENTRY_SPREAD_PCT", "0.30")
)

OPTION_TICK_SIZE: float = 5.0

# ──────────────────── Risk Management ────────────────────────────

MAX_DAILY_LOSS_PCT: float | None = None      # disabled — no daily loss halt
CIRCUIT_BREAKER_API_ERRORS: int = 5
CIRCUIT_BREAKER_COOLDOWN_SEC: float = 300.0

# ───────────────────────── Telegram ──────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_REPORT_BOT_TOKEN: str = os.getenv("TELEGRAM_REPORT_BOT_TOKEN", "")
TELEGRAM_REPORT_CHAT_ID: str = os.getenv("TELEGRAM_REPORT_CHAT_ID", "")
TELEGRAM_ENABLED: bool = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ───────────────────────── Logging ───────────────────────────────

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = "logs/algo.log"
LOG_JSON: bool = True

# ───────────────────── State Persistence ─────────────────────────

STATE_DIR: str = "state"
EQUITY_FILE: str = "state/equity.json"
POSITIONS_FILE: str = "state/positions.json"
TRADE_LOG_FILE: str = "state/trade_log.csv"
VOLUME_FILE: str = "state/monthly_volumes.csv"
