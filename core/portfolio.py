"""
Equity tracking, position state, and trade logging.

Compound sizing: equity grows/shrinks with each trade's realised P&L.

Multi-session: Straddle now carries `session_name`, `qty_per_leg`,
`trading_day` (YYYY-MM-DD of the 08:00 UTC option expiry) so daily
reports can group both sessions of a trading day together.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

import structlog

import config
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)

TRADE_LOG_FIELDS = [
    "date", "trading_day", "session", "entry_time", "exit_time", "exit_reason",
    "num_straddles", "qty_per_leg",
    "spot_entry", "spot_exit",
    "put_strike", "put_premium_entry", "put_premium_exit",
    "spot_margin_used", "put_premium_cost", "total_capital_used",
    "straddle_cost", "capital_before",
    "spot_pnl", "put_pnl", "gross_pnl", "fees", "net_pnl",
    "capital_after",
    # Execution-quality metrics — entry
    "spot_entry_duration_sec", "spot_entry_attempts",
    "spot_entry_ref_mark", "spot_entry_ref_ask",
    "spot_entry_slippage_vs_mark_pct",
    "spot_entry_saved_vs_taker_usd",
    "put_entry_duration_sec", "put_entry_attempts",
    "put_entry_ref_mark", "put_entry_ref_ask",
    "put_entry_slippage_vs_mark_pct",
    "put_entry_saved_vs_taker_usd",
    # Execution-quality metrics — exit
    "spot_exit_duration_sec", "spot_exit_attempts",
    "spot_exit_ref_mark", "spot_exit_ref_bid",
    "spot_exit_slippage_vs_mark_pct",
    "spot_exit_saved_vs_taker_usd",
    "put_exit_duration_sec", "put_exit_attempts",
    "put_exit_ref_mark", "put_exit_ref_bid",
    "put_exit_slippage_vs_mark_pct",
    "put_exit_saved_vs_taker_usd",
]


@dataclass
class StraddleLeg:
    instrument: str
    side: str
    qty: float
    entry_price: float
    order_id: str = ""
    avg_fill_price: float = 0.0
    entry_metrics: dict = field(default_factory=dict)
    exit_metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Straddle:
    id: str
    spot_leg: StraddleLeg
    put_legs: list[StraddleLeg]
    put_strike: float
    spot_qty: float                     # qty_per_leg (BTC) for spot
    put_qty_each: float                 # qty_per_leg (BTC) per put leg
    entry_time: str
    entry_spot: float
    entry_put_price: float
    total_put_cost: float
    straddle_cost: float
    num_straddles: int

    # Multi-session metadata
    session_name: str = ""
    trading_day: str = ""

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
            "session_name": self.session_name,
            "trading_day": self.trading_day,
            "status": self.status,
            "exit_time": self.exit_time,
            "exit_spot": self.exit_spot,
            "exit_put_price": self.exit_put_price,
            "pnl": self.pnl,
        }


class Portfolio:
    """Tracks equity and the current open straddle (at most one at a time)."""

    def __init__(self) -> None:
        self._equity: float = config.INITIAL_CAPITAL_USD
        self._straddle: Optional[Straddle] = None
        self._daily_pnl: float = 0.0
        self._load_equity()
        self._migrate_trade_log()

    @property
    def equity(self) -> float:
        return self._equity

    def sync_equity(self, live_equity: float) -> None:
        if live_equity <= 0:
            log.warning("sync_equity_skipped", live_equity=live_equity)
            return
        old = self._equity
        self._equity = live_equity
        self._save_equity()
        log.info("equity_synced", old=f"${old:,.2f}", live=f"${live_equity:,.2f}",
                 delta=f"${live_equity - old:,.2f}")

    def adjust_equity(self, delta: float) -> None:
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

        se = s.spot_leg.entry_metrics or {}
        sx = s.spot_leg.exit_metrics or {}
        pe_metrics = [pl.entry_metrics for pl in s.put_legs if pl.entry_metrics]
        px_metrics = [pl.exit_metrics for pl in s.put_legs if pl.exit_metrics]

        def _avg(items: list, key: str) -> float:
            vals = [m.get(key, 0) for m in items if m.get(key) not in (None, "")]
            return round(sum(vals) / len(vals), 4) if vals else 0.0

        def _sum(items: list, key: str) -> float:
            vals = [m.get(key, 0) for m in items if m.get(key) not in (None, "")]
            return round(sum(vals), 2) if vals else 0.0

        def _max_int(items: list, key: str) -> int:
            vals = [int(m.get(key, 0) or 0) for m in items]
            return max(vals) if vals else 0

        # Resolve trading_day if not set on the straddle.
        trading_day = s.trading_day
        if not trading_day:
            try:
                entry_dt = datetime.fromisoformat(s.entry_time)
                trading_day = config.trading_day_for(entry_dt).isoformat()
            except Exception:
                trading_day = s.entry_time[:10]

        row = {
            "date": s.entry_time[:10],
            "trading_day": trading_day,
            "session": s.session_name,
            "entry_time": s.entry_time,
            "exit_time": s.exit_time,
            "exit_reason": exit_reason,
            "num_straddles": s.num_straddles,
            "qty_per_leg": s.spot_qty,
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
            "spot_entry_duration_sec": se.get("duration_sec", ""),
            "spot_entry_attempts": se.get("attempts", ""),
            "spot_entry_ref_mark": se.get("ref_mark", ""),
            "spot_entry_ref_ask": se.get("ref_ask", ""),
            "spot_entry_slippage_vs_mark_pct": se.get("slippage_vs_mark_pct", ""),
            "spot_entry_saved_vs_taker_usd": se.get("saved_vs_taker_total_usd", ""),
            "put_entry_duration_sec": _avg(pe_metrics, "duration_sec"),
            "put_entry_attempts": _max_int(pe_metrics, "attempts"),
            "put_entry_ref_mark": _avg(pe_metrics, "ref_mark"),
            "put_entry_ref_ask": _avg(pe_metrics, "ref_ask"),
            "put_entry_slippage_vs_mark_pct": _avg(pe_metrics, "slippage_vs_mark_pct"),
            "put_entry_saved_vs_taker_usd": _sum(pe_metrics, "saved_vs_taker_total_usd"),
            "spot_exit_duration_sec": sx.get("duration_sec", ""),
            "spot_exit_attempts": sx.get("attempts", ""),
            "spot_exit_ref_mark": sx.get("ref_mark", ""),
            "spot_exit_ref_bid": sx.get("ref_bid", ""),
            "spot_exit_slippage_vs_mark_pct": sx.get("slippage_vs_mark_pct", ""),
            "spot_exit_saved_vs_taker_usd": sx.get("saved_vs_taker_total_usd", ""),
            "put_exit_duration_sec": _avg(px_metrics, "duration_sec"),
            "put_exit_attempts": _max_int(px_metrics, "attempts"),
            "put_exit_ref_mark": _avg(px_metrics, "ref_mark"),
            "put_exit_ref_bid": _avg(px_metrics, "ref_bid"),
            "put_exit_slippage_vs_mark_pct": _avg(px_metrics, "slippage_vs_mark_pct"),
            "put_exit_saved_vs_taker_usd": _sum(px_metrics, "saved_vs_taker_total_usd"),
        }

        needs_header = not os.path.exists(config.TRADE_LOG_FILE)
        if not needs_header:
            with open(config.TRADE_LOG_FILE, "r") as f:
                existing_header = f.readline().strip().split(",")
            if existing_header != TRADE_LOG_FIELDS:
                log.warning("trade_log_schema_mismatch", rewriting_header=True)
                self._rewrite_csv_header(existing_header)
                needs_header = False

        with open(config.TRADE_LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)

    # ──────────────── CSV migration helpers ──────────────────────

    def _migrate_trade_log(self) -> None:
        """One-shot in-place migration to the current TRADE_LOG_FIELDS schema.

        Adds any missing columns (notably trading_day, session, qty_per_leg)
        with sensible defaults so old rows remain readable by the new
        DictReader-based loaders.
        """
        path = config.TRADE_LOG_FILE
        if not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                first = f.readline().strip()
            if not first:
                return
            existing_header = first.split(",")
            if existing_header == TRADE_LOG_FIELDS:
                return
            self._rewrite_csv_header(existing_header)
            added = [c for c in TRADE_LOG_FIELDS if c not in existing_header]
            try:
                with open(path) as f:
                    rows = sum(1 for _ in f) - 1
            except Exception:
                rows = -1
            log.info("trade_log_migrated", path=path, added_columns=added, rows=rows)
        except Exception:
            log.warning("trade_log_migration_failed", exc_info=True)

    @staticmethod
    def _rewrite_csv_header(old_fields: list[str]) -> None:
        """Rewrite the CSV with the canonical header, re-mapping old rows by name.

        Backfills `trading_day` from `date` for rows that pre-date the
        multi-session schema.
        """
        import shutil

        path = config.TRADE_LOG_FILE
        tmp = path + ".tmp"
        with open(path, "r", newline="") as fin, open(tmp, "w", newline="") as fout:
            reader = csv.reader(fin)
            writer = csv.DictWriter(fout, fieldnames=TRADE_LOG_FIELDS)
            writer.writeheader()
            next(reader)  # skip old header
            for values in reader:
                if not values:
                    continue
                if len(values) == len(old_fields):
                    row = dict(zip(old_fields, values))
                elif len(values) == len(TRADE_LOG_FIELDS):
                    row = dict(zip(TRADE_LOG_FIELDS, values))
                else:
                    # Best-effort: zip what we have, leave the rest empty.
                    row = dict(zip(old_fields, values))
                padded = {f: row.get(f, "") for f in TRADE_LOG_FIELDS}
                # Backfill trading_day: prefer parsing entry_time so we can
                # apply the 08:00 UTC cutoff; fall back to date.
                if not padded.get("trading_day"):
                    entry_time = row.get("entry_time", "")
                    derived = ""
                    if entry_time:
                        try:
                            entry_dt = datetime.fromisoformat(entry_time)
                            derived = config.trading_day_for(entry_dt).isoformat()
                        except Exception:
                            derived = ""
                    padded["trading_day"] = derived or row.get("date", "")
                if padded.get("qty_per_leg") in ("", None):
                    padded["qty_per_leg"] = config.QTY_PER_LEG
                writer.writerow(padded)
        shutil.move(tmp, path)
