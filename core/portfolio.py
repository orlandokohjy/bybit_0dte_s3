"""
Equity tracking, position state, and trade logging.

Compound sizing: equity grows/shrinks with each trade's realised P&L.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

import structlog

import config
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)

TRADE_LOG_FIELDS = [
    "date", "entry_time", "exit_time", "exit_reason",
    "num_straddles", "spot_entry", "spot_exit",
    "put_strike", "put_premium_entry", "put_premium_exit",
    "spot_margin_used", "put_premium_cost", "total_capital_used",
    "straddle_cost", "capital_before",
    "spot_pnl", "put_pnl", "gross_pnl", "fees", "net_pnl",
    "capital_after",
]


@dataclass
class StraddleLeg:
    instrument: str
    side: str
    qty: float
    entry_price: float
    order_id: str = ""
    avg_fill_price: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Straddle:
    id: str
    spot_leg: StraddleLeg
    put_legs: list[StraddleLeg]        # NUM_PUTS put legs
    put_strike: float
    spot_qty: float                     # QTY_PER_LEG
    put_qty_each: float                 # QTY_PER_LEG per put leg
    entry_time: str
    entry_spot: float
    entry_put_price: float              # per-BTC put premium at entry
    total_put_cost: float               # NUM_PUTS × QTY_PER_LEG × put_premium
    straddle_cost: float                # margin + total_put_cost
    num_straddles: int                  # how many of this unit were opened

    status: str = "open"
    exit_time: Optional[str] = None
    exit_spot: Optional[float] = None
    exit_put_price: Optional[float] = None
    pnl: Optional[float] = None

    def spot_pnl(self, spot_now: float) -> float:
        return self.spot_qty * (spot_now - self.entry_spot) * self.num_straddles

    def put_pnl(self, put_mark_now: float) -> float:
        return (
            config.NUM_PUTS * self.put_qty_each
            * (put_mark_now - self.entry_put_price)
            * self.num_straddles
        )

    def combined_pnl(self, spot_now: float, put_mark_now: float) -> float:
        return self.spot_pnl(spot_now) + self.put_pnl(put_mark_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "spot_leg": self.spot_leg.to_dict(),
            "put_legs": [p.to_dict() for p in self.put_legs],
            "put_strike": self.put_strike,
            "spot_qty": self.spot_qty,
            "put_qty_each": self.put_qty_each,
            "entry_time": self.entry_time,
            "entry_spot": self.entry_spot,
            "entry_put_price": self.entry_put_price,
            "total_put_cost": self.total_put_cost,
            "straddle_cost": self.straddle_cost,
            "num_straddles": self.num_straddles,
            "status": self.status,
            "exit_time": self.exit_time,
            "exit_spot": self.exit_spot,
            "exit_put_price": self.exit_put_price,
            "pnl": self.pnl,
        }


class Portfolio:
    """Tracks equity and the current open straddle (at most one per day)."""

    def __init__(self) -> None:
        self._equity: float = config.INITIAL_CAPITAL_USD
        self._straddle: Optional[Straddle] = None
        self._daily_pnl: float = 0.0
        self._load_equity()

    @property
    def equity(self) -> float:
        return self._equity

    def sync_equity(self, live_equity: float) -> None:
        """Sync internal equity with the live Bybit wallet balance."""
        if live_equity <= 0:
            log.warning("sync_equity_skipped", live_equity=live_equity)
            return
        old = self._equity
        self._equity = live_equity
        self._save_equity()
        log.info("equity_synced", old=f"${old:,.2f}", live=f"${live_equity:,.2f}",
                 delta=f"${live_equity - old:,.2f}")

    def adjust_equity(self, delta: float) -> None:
        """Apply an equity adjustment (e.g. emergency unwind P&L) and persist."""
        self._equity += delta
        self._daily_pnl += delta
        self._save_equity()

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def has_open(self) -> bool:
        return self._straddle is not None and self._straddle.status == "open"

    @property
    def open_straddle(self) -> Optional[Straddle]:
        return self._straddle if self.has_open else None

    def set_straddle(self, s: Straddle) -> None:
        self._straddle = s
        self._save_positions()

    def close_straddle(
        self, exit_spot: float, exit_put_price: float, exit_reason: str,
    ) -> float:
        s = self._straddle
        if s is None or s.status != "open":
            return 0.0

        pnl = s.combined_pnl(exit_spot, exit_put_price)
        s.status = "closed"
        s.exit_time = now_utc().isoformat()
        s.exit_spot = exit_spot
        s.exit_put_price = exit_put_price
        s.pnl = pnl

        self._equity += pnl
        self._daily_pnl += pnl
        self._save_equity()
        self._save_positions()
        self._log_trade(s, exit_reason)

        log.info("straddle_closed", pnl=f"${pnl:,.2f}", equity=f"${self._equity:,.2f}")
        return pnl

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self._straddle = None
        self._save_positions()

    # ──────────────── Persistence ─────────────────────────────────

    def _save_equity(self) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(config.EQUITY_FILE, "w") as f:
            json.dump({"equity": self._equity}, f)

    def _load_equity(self) -> None:
        if os.path.exists(config.EQUITY_FILE):
            try:
                with open(config.EQUITY_FILE) as f:
                    self._equity = json.load(f).get("equity", config.INITIAL_CAPITAL_USD)
                log.info("equity_loaded", equity=self._equity)
            except Exception:
                log.warning("equity_load_failed", exc_info=True)

    def _save_positions(self) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        data = self._straddle.to_dict() if self._straddle else None
        with open(config.POSITIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _log_trade(self, s: Straddle, exit_reason: str) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        spot_margin = s.spot_qty * s.entry_spot / config.SPOT_LEVERAGE * s.num_straddles
        put_cost = s.total_put_cost * s.num_straddles
        total_capital_used = spot_margin + put_cost

        row = {
            "date": s.entry_time[:10],
            "entry_time": s.entry_time,
            "exit_time": s.exit_time,
            "exit_reason": exit_reason,
            "num_straddles": s.num_straddles,
            "spot_entry": s.entry_spot,
            "spot_exit": s.exit_spot,
            "put_strike": s.put_strike,
            "put_premium_entry": s.entry_put_price,
            "put_premium_exit": s.exit_put_price,
            "spot_margin_used": round(spot_margin, 2),
            "put_premium_cost": round(put_cost, 2),
            "total_capital_used": round(total_capital_used, 2),
            "straddle_cost": s.straddle_cost,
            "capital_before": self._equity - (s.pnl or 0),
            "spot_pnl": s.spot_pnl(s.exit_spot or s.entry_spot),
            "put_pnl": s.put_pnl(s.exit_put_price or s.entry_put_price),
            "gross_pnl": s.pnl,
            "fees": 0.0,
            "net_pnl": s.pnl,
            "capital_after": self._equity,
        }

        needs_header = not os.path.exists(config.TRADE_LOG_FILE)
        if not needs_header:
            with open(config.TRADE_LOG_FILE, "r") as f:
                existing_header = f.readline().strip().split(",")
            if existing_header != TRADE_LOG_FIELDS:
                needs_header = True
                log.warning("trade_log_schema_mismatch", rewriting_header=True)
                self._rewrite_csv_header(existing_header)

        with open(config.TRADE_LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)

    @staticmethod
    def _rewrite_csv_header(old_fields: list[str]) -> None:
        """Rewrite the CSV with the canonical header, re-mapping old rows by position."""
        import tempfile
        import shutil

        path = config.TRADE_LOG_FILE
        tmp = path + ".tmp"
        with open(path, "r", newline="") as fin, open(tmp, "w", newline="") as fout:
            reader = csv.reader(fin)
            writer = csv.DictWriter(fout, fieldnames=TRADE_LOG_FIELDS)
            writer.writeheader()
            next(reader)  # skip old header
            for values in reader:
                if len(values) == len(old_fields):
                    row = dict(zip(old_fields, values))
                elif len(values) == len(TRADE_LOG_FIELDS):
                    row = dict(zip(TRADE_LOG_FIELDS, values))
                else:
                    continue
                padded = {f: row.get(f, "") for f in TRADE_LOG_FIELDS}
                writer.writerow(padded)
        shutil.move(tmp, path)
