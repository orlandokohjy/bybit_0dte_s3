"""
Central configuration — Bybit 0DTE BTC Synthetic Straddle.

Multi-session schedule (10 trades / week):
  • afternoon  13:30–15:30 UTC   Mon–Fri    0.25 BTC spot leg → 2 × 0.25 BTC puts
  • morning    01:00–02:00 UTC   Tue–Sat    0.25 BTC spot leg → 2 × 0.25 BTC puts

Both legs of a "trading day" share the same 08:00 UTC option expiry. e.g. the
Monday afternoon entry and the Tuesday morning entry both target the Tuesday
0800 UTC expiry and roll up into the Tuesday trading-day report.

Position structure (one straddle unit, regardless of session):
  • Long  qty_per_leg BTC spot @ 10× margin leverage
  • Long  NUM_PUTS × qty_per_leg BTC ITM puts @ same strike

Sizing: NUM_STRADDLES_OVERRIDE=1 → one straddle per session (fixed). Set to
0 to enable compound sizing via ALLOC_PCT × current_equity / straddle_cost.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import FrozenSet

from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────── Bybit API ───────────────────────────

BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET: str = os.getenv("BYBIT_API_SECRET", "")
HAS_BYBIT_CREDS: bool = bool(BYBIT_API_KEY and BYBIT_API_SECRET)

TESTNET: bool = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
DEMO: bool = os.getenv("BYBIT_DEMO", "false").lower() == "true"
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

# ─────────────────────────── Instrument ──────────────────────────

SPOT_SYMBOL = "BTCUSDT"
SPOT_CATEGORY = "spot"
SPOT_LEVERAGE: int = 10                    # margin leverage for spot
BASE_COIN = "BTC"
SETTLE_COIN = "USDT"
ACCOUNT_TYPE = "UNIFIED"

# ─────────────────────────── Position ────────────────────────────

NUM_PUTS: int = 2                          # put contracts per straddle (always 2)

# Legacy single-session knob — used as a fallback for trade-log rows that
# pre-date the multi-session refactor and as the default for any caller
# that doesn't pass an explicit qty_per_leg. New entries source the
# qty_per_leg value from the firing Session below.
QTY_PER_LEG: float = float(os.getenv("QTY_PER_LEG", "0.25"))

# ─────────────────────────── Sizing ──────────────────────────────

INITIAL_CAPITAL_USD: float = float(os.getenv("INITIAL_CAPITAL_USD", "8000"))
ALLOC_PCT: float = float(os.getenv("ALLOC_PCT", "0.80"))   # used only when override = 0

# Force exact straddle count regardless of capital (1 = always 1 straddle
# per session, no compounding). Set to 0 to fall back to ALLOC_PCT-driven
# sizing.
NUM_STRADDLES_OVERRIDE: int = int(os.getenv("NUM_STRADDLES_OVERRIDE", "1"))

# Wipe state/{equity,positions}.json + state/trade_log.csv at startup. Useful
# for cutting over from demo state to a clean live deployment. Set to true
# ONCE; the algo automatically flips it back to false after running.
RESET_STATE_ON_BOOT: bool = os.getenv("RESET_STATE_ON_BOOT", "false").lower() == "true"

# ──────────────────── Multi-session schedule ─────────────────────

EXPIRY_CUTOFF_UTC: time = time(8, 0)
"""Bybit/Deribit option expiry cutoff. A session firing BEFORE this time on
day D uses the D 08:00 UTC expiry; a session firing AFTER uses the
(D+1) 08:00 UTC expiry. Both sessions of a "trading day" share the same
expiry (afternoon Mon + morning Tue → Tuesday's 08:00 UTC expiry)."""


@dataclass(frozen=True)
class Session:
    """One firing of the algo per trading day.

    weekdays uses Python convention: Monday=0 ... Sunday=6 (UTC).
    """
    name: str
    entry_utc: time
    close_utc: time
    qty_per_leg: float
    weekdays: FrozenSet[int]

    @property
    def trading_day_offset_days(self) -> int:
        """0 if entry is BEFORE 08:00 UTC cutoff (same calendar day expiry),
        +1 if entry is AFTER (next calendar day expiry)."""
        return 0 if self.entry_utc < EXPIRY_CUTOFF_UTC else 1

    @property
    def trading_day_close_position(self) -> int:
        """Used for chronological sort: 0 = afternoon (1st), 1 = morning (2nd).

        Afternoon (13:30 UTC, offset=+1) chronologically PRECEDES the
        morning of the next calendar day (01:00 UTC, offset=0) within
        the SAME trading day. So afternoon sorts before morning.
        """
        # Afternoon offset=+1 → 0; morning offset=0 → 1 (i.e. afternoon first).
        return 0 if self.trading_day_offset_days == 1 else 1

    @property
    def time_label(self) -> str:
        """Human-friendly window label e.g. '13:30-15:30 UTC'."""
        return (
            f"{self.entry_utc.hour:02d}:{self.entry_utc.minute:02d}-"
            f"{self.close_utc.hour:02d}:{self.close_utc.minute:02d} UTC"
        )


# Notional per leg: 0.25 BTC spot → 2 × 0.25 = 0.5 BTC put notional per straddle.
AFTERNOON_QTY_PER_LEG: float = float(os.getenv("AFTERNOON_QTY_PER_LEG", "0.25"))
MORNING_QTY_PER_LEG: float = float(os.getenv("MORNING_QTY_PER_LEG", "0.25"))

SESSIONS: list[Session] = [
    Session(
        name="afternoon",
        entry_utc=time(13, 30),
        close_utc=time(15, 30),
        qty_per_leg=AFTERNOON_QTY_PER_LEG,
        weekdays=frozenset({0, 1, 2, 3, 4}),   # Mon–Fri
    ),
    Session(
        name="morning",
        entry_utc=time(1, 0),
        close_utc=time(2, 0),
        qty_per_leg=MORNING_QTY_PER_LEG,
        weekdays=frozenset({1, 2, 3, 4, 5}),   # Tue–Sat
    ),
]


def trading_day_for(entry_dt: datetime) -> date:
    """Return the trading-day date (= option expiry date) for a session
    that fires at entry_dt UTC.

    A trading day is defined by the 08:00 UTC option expiry it pertains to.
    Sessions firing AFTER the 08:00 UTC cutoff target the NEXT calendar
    day's expiry; sessions firing BEFORE target the SAME day's expiry.
    """
    if entry_dt.timetz().replace(tzinfo=None) >= EXPIRY_CUTOFF_UTC:
        return (entry_dt + timedelta(days=1)).date()
    return entry_dt.date()


def _last_close_session_name() -> str:
    """The session whose close should trigger the daily report — i.e.
    the LAST session of a trading day (by trading_day_close_position)."""
    if not SESSIONS:
        return ""
    return max(SESSIONS, key=lambda s: s.trading_day_close_position).name


LAST_CLOSE_SESSION_NAME: str = _last_close_session_name()

# Legacy single-session times — retained only for ad-hoc/legacy callers.
# The active scheduler reads SESSIONS above. Reports are now CHAINED off
# the LAST_CLOSE_SESSION_NAME close handler (see main._on_close), not
# fired by these constants.
SESSION_ENTRY_UTC: time = time(13, 30)
SESSION_CLOSE_UTC: time = time(15, 30)
REPORT_UTC: time = time(2, 0)               # informational only
WEEKLY_REPORT_UTC: time = time(2, 30)       # informational only
ALLOWED_WEEKDAYS: set[int] = {0, 1, 2, 3, 4}

# ENTRY_NOW values:
#   • "false" / unset  → no immediate entry, scheduler handles it
#   • "true"           → fire the FIRST session in SESSIONS (afternoon)
#   • "afternoon"      → fire afternoon session immediately
#   • "morning"        → fire morning session immediately
ENTRY_NOW_RAW: str = os.getenv("ENTRY_NOW", "false").strip().lower()

# ──────────────────── Option Filters ─────────────────────────────

MAX_BID_ASK_SPREAD_PCT: float = float(os.getenv("MAX_BID_ASK_SPREAD_PCT", "0.10"))
MIN_OPEN_INTEREST: float = 0.0

# ────────────────────────── Exit ─────────────────────────────────
# No take-profit — all positions hold until the session close.

# ──────────────────── Execution Settings ─────────────────────────

SPOT_CHASE_INTERVAL_SEC: float = 3.0
SPOT_CHASE_MAX_ATTEMPTS: int = 15
SPOT_TICK_SIZE: float = 0.10               # BTCUSDT spot tick

OPTION_CHASE_INTERVAL_SEC: float = 3.0
OPTION_CHASE_MAX_ATTEMPTS: int = 50        # legacy
OPTION_CHASE_DEADLINE_SEC: float = float(
    os.getenv("OPTION_CHASE_DEADLINE_SEC", "3600.0")  # 60 min, mirrors OKX
)
OPTION_CHASE_GAP_NARROW_PCT: float = float(
    os.getenv("OPTION_CHASE_GAP_NARROW_PCT", "0.5")
)
OPTION_CHASE_MAX_SLIPPAGE_FACTOR: float = float(
    os.getenv("OPTION_CHASE_MAX_SLIPPAGE_FACTOR", "1.15")
)
OPTION_MAX_ENTRY_SPREAD_PCT: float = float(
    os.getenv("OPTION_MAX_ENTRY_SPREAD_PCT", "0.30")
)
OPTION_TICK_SIZE: float = 5.0

# ──────────────────── Risk Management ────────────────────────────

MAX_DAILY_LOSS_PCT: float | None = None
CIRCUIT_BREAKER_API_ERRORS: int = 5
CIRCUIT_BREAKER_COOLDOWN_SEC: float = 300.0

COLLATERAL_BUFFER_FACTOR: float = float(
    os.getenv("COLLATERAL_BUFFER_FACTOR", "1.2")
)
CONSECUTIVE_FAILURE_LIMIT: int = int(
    os.getenv("CONSECUTIVE_FAILURE_LIMIT", "3")
)

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
PID_FILE: str = "state/algo.pid"           # singleton lock
