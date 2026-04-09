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
ALLOC_PCT: float = 0.60        # 60 % of current equity

# ──────────────────────── Option Filters ─────────────────────────

MAX_BID_ASK_SPREAD_PCT: float = 0.10   # skip puts with spread > 10 % of mid
MIN_OPEN_INTEREST: float = 0.0

# ────────────────────────── Session ──────────────────────────────

SESSION_ENTRY_UTC: time = time(14, 0)
SESSION_CLOSE_UTC: time = time(18, 0)
REPORT_UTC: time = time(19, 0)
ALLOWED_WEEKDAYS: set[int] = {0, 1, 2, 3, 4}  # Mon–Fri

# ───────────────────── Exit ───────────────────────────────────────
# No take-profit — all positions hold until 18:00 UTC hard close.

# ──────────────────── Execution Settings ─────────────────────────

# Spot: GTC limit orders at bid/ask for maker rebate
SPOT_CHASE_INTERVAL_SEC: float = 1.0
SPOT_CHASE_MAX_ATTEMPTS: int = 15
SPOT_TICK_SIZE: float = 0.10       # BTCUSDT spot tick size on Bybit

# Options: GTC limit — escalating maker (bid → ask-1tick, never cross spread)
OPTION_CHASE_INTERVAL_SEC: float = 2.0
OPTION_CHASE_MAX_ATTEMPTS: int = 15
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
