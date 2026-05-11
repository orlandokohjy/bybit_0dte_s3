"""
Bybit 0DTE BTC Synthetic Straddle — multi-session.

Two sessions per trading day (10 trades / week):
  • afternoon  13:30–15:30 UTC   Mon–Fri    qty_per_leg = AFTERNOON_QTY_PER_LEG
  • morning    01:00–02:00 UTC   Tue–Sat    qty_per_leg = MORNING_QTY_PER_LEG

Both legs share the same 08:00 UTC option expiry (afternoon Mon + morning Tue
both target Tue 08:00 UTC) and roll up into the same "trading day" report.

Reports flow:
  • Daily report → CHAINED off the morning close (Tue–Sat, ~02:00 UTC)
  • Weekly report → CHAINED off the SATURDAY morning close (~02:00 UTC)
  • No standalone DAILY SUMMARY message (removed).
  • Daily report omits Risk Metrics & Edge sections (trimmed).
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
from typing import Optional

import structlog

import config
from core import notifier
from core.exchange import BybitExchange
from core.portfolio import Portfolio
from core.scheduler import Scheduler
from data.market_data import MarketData
from data.option_chain import OptionChain
from risk.risk_manager import RiskManager
from strategy.exit_manager import ExitManager
from strategy.option_selector import select_put
from strategy.position_sizer import size_position
from strategy.straddle_builder import build_straddle, unwind_straddle
from utils.logging_config import setup_logging
from utils.time_utils import format_utc_sgt, now_utc
from utils import volume_tracker

log = structlog.get_logger(__name__)


# ────────────────────────── helpers ──────────────────────────────

def _disable_env_flag(env_path: str, flag: str) -> None:
    """Flip <flag>=true → <flag>=false in the .env file. No-op if missing."""
    try:
        if not os.path.exists(env_path):
            return
        with open(env_path, "r") as f:
            content = f.read()
        new_content = re.sub(
            rf"^(\s*{flag}\s*=\s*)(true|TRUE|True|1)\b.*$",
            r"\1false",
            content,
            flags=re.MULTILINE,
        )
        if new_content != content:
            with open(env_path, "w") as f:
                f.write(new_content)
            log.info("env_flag_auto_disabled", flag=flag, env_path=env_path)
    except Exception:
        log.warning("env_flag_disable_failed", flag=flag, exc_info=True)


def _wipe_state_files() -> None:
    """Wipe equity.json + positions.json + trade_log.csv + monthly_volumes.csv."""
    for path in (
        config.EQUITY_FILE, config.POSITIONS_FILE,
        config.TRADE_LOG_FILE, config.VOLUME_FILE,
    ):
        try:
            if os.path.exists(path):
                os.remove(path)
                log.info("state_file_wiped", path=path)
        except Exception:
            log.warning("state_file_wipe_failed", path=path, exc_info=True)


def _acquire_singleton_lock() -> bool:
    """Write current PID to config.PID_FILE; return False if another live
    instance is already holding the lock.

    On stale-lock (PID file exists but the process is dead) the file is
    overwritten and the lock is granted.
    """
    os.makedirs(config.STATE_DIR, exist_ok=True)
    pid = os.getpid()
    if os.path.exists(config.PID_FILE):
        try:
            with open(config.PID_FILE) as f:
                old_pid = int(f.read().strip() or "0")
            if old_pid > 0 and _pid_alive(old_pid):
                log.error("singleton_lock_held", existing_pid=old_pid)
                return False
            log.warning("singleton_stale_lock_overwritten",
                        stale_pid=old_pid, new_pid=pid)
        except Exception:
            log.warning("singleton_lock_read_failed", exc_info=True)
    with open(config.PID_FILE, "w") as f:
        f.write(str(pid))
    log.info("singleton_lock_acquired", lock_path=config.PID_FILE, pid=pid)
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _release_singleton_lock() -> None:
    try:
        if os.path.exists(config.PID_FILE):
            os.remove(config.PID_FILE)
    except Exception:
        pass


def _resolve_immediate_entry_session() -> Optional[config.Session]:
    """ENTRY_NOW values:
        false / unset    → None
        true             → first session in config.SESSIONS
        afternoon|morning → that named session
    """
    raw = config.ENTRY_NOW_RAW
    if not raw or raw == "false":
        return None
    if raw == "true":
        return config.SESSIONS[0] if config.SESSIONS else None
    for s in config.SESSIONS:
        if s.name == raw:
            return s
    log.warning("entry_now_unknown_value", value=raw,
                known=[s.name for s in config.SESSIONS])
    return None


# ──────────────────────────── Algo ────────────────────────────────

class Algo:
    def __init__(self) -> None:
        self.exchange = BybitExchange()
        self.chain = OptionChain(self.exchange)
        self.market = MarketData(self.exchange, self.chain)
        self.portfolio = Portfolio()
        self.risk = RiskManager(self.portfolio)
        self.exit_mgr = ExitManager(self.exchange, self.market, self.portfolio)
        self.scheduler = Scheduler()
        self._shutdown = asyncio.Event()
        self._entry_locked: bool = False
        self._lock_reason: str = ""
        self._consecutive_failures: int = 0

    async def start(self) -> None:
        setup_logging()

        # ── Singleton lock ──
        if not _acquire_singleton_lock():
            log.error("aborting_duplicate_instance")
            sys.exit(2)

        # ── One-shot state wipe (RESET_STATE_ON_BOOT=true) ──
        if config.RESET_STATE_ON_BOOT:
            log.warning("resetting_state_on_boot")
            _wipe_state_files()
            _disable_env_flag(".env", "RESET_STATE_ON_BOOT")
            # Reload portfolio with clean slate.
            self.portfolio = Portfolio()
            self.risk = RiskManager(self.portfolio)
            self.exit_mgr = ExitManager(self.exchange, self.market, self.portfolio)

        log.info(
            "algo_starting",
            mode="LIVE" if not config.DEMO else "DEMO",
            dry_run=config.DRY_RUN,
            has_creds=config.HAS_BYBIT_CREDS,
            reset_state=config.RESET_STATE_ON_BOOT,
        )

        if not config.HAS_BYBIT_CREDS:
            log.error("missing_api_credentials")
            await notifier.send(
                "<b>S3 ALGO STARTUP FAILED</b>\n"
                "Missing BYBIT_API_KEY / BYBIT_API_SECRET in .env"
            )
            sys.exit(1)

        if not config.DRY_RUN:
            await self.exchange.set_portfolio_margin()
            await self.exchange.set_spot_hedging()
            await self.exchange.set_spot_margin_leverage()
            await self._startup_cancel_stale_orders()
            await self._startup_reconcile_positions()

        margin_mode = await self.exchange.get_margin_mode()
        await self.market.start()
        spot = await self.market.get_spot_price()

        if not config.DRY_RUN:
            live_equity = await self.exchange.get_total_equity_usd()
            if live_equity > 0:
                self.portfolio.sync_equity(live_equity)

        log.info("algo_initialized",
                 spot=f"${spot:,.2f}",
                 equity=f"${self.portfolio.equity:,.2f}",
                 margin_mode=margin_mode,
                 entry_locked=self._entry_locked)

        # ── Build startup Telegram with full session breakdown ──
        sessions_block = "\n".join(
            f"  • <b>{s.name}</b>  {s.time_label}  qty/leg: {s.qty_per_leg:.4f} BTC"
            for s in config.SESSIONS
        )
        lock_line = (f"\n<b>ENTRY LOCKED</b>: {self._lock_reason}"
                     if self._entry_locked else "")
        await notifier.send(
            f"<b>S3 ALGO STARTED</b>\n"
            f"Mode: {'DEMO' if config.DEMO else 'LIVE'}"
            f"{' (DRY RUN)' if config.DRY_RUN else ''}\n"
            f"Margin: {margin_mode}\n"
            f"Spot: ${spot:,.2f}\n"
            f"Equity: ${self.portfolio.equity:,.2f}\n"
            f"Time: {format_utc_sgt(now_utc())}\n"
            f"\n<b>Sessions</b>\n{sessions_block}\n"
            f"\nReports chained after the morning close ("
            f"{[s.time_label for s in config.SESSIONS if s.name == config.LAST_CLOSE_SESSION_NAME][0] if config.LAST_CLOSE_SESSION_NAME else 'n/a'})."
            f"{lock_line}\n"
        )

        self.exchange.start_private_ws()

        self.scheduler.register_session(
            on_entry=self._on_entry,
            on_close=self._on_close,
        )
        self.scheduler.start()

        fire_times = self.scheduler.get_next_fire_times()
        for job_id, ft in fire_times.items():
            if ft:
                log.info("next_fire", job=job_id, time=format_utc_sgt(ft))

        # ── ENTRY_NOW (afternoon | morning | true | false) ──
        immediate = _resolve_immediate_entry_session()
        if immediate is not None:
            log.info("immediate_entry_triggered", session=immediate.name)
            _disable_env_flag(".env", "ENTRY_NOW")
            await self._on_entry(immediate)

        log.info("algo_running")
        await self._shutdown.wait()

    # ──────────────────── Startup Safeguards ──────────────────────

    async def _startup_cancel_stale_orders(self) -> None:
        try:
            cancelled = await self.exchange.cancel_all_open_orders()
            if cancelled > 0:
                await notifier.send(
                    f"<b>STARTUP CLEANUP</b>\n"
                    f"Cancelled {cancelled} stale open order(s) from previous run."
                )
        except Exception:
            log.error("startup_cancel_failed", exc_info=True)
            await notifier.notify_error(
                "Startup cleanup",
                "Failed to cancel stale orders — check logs manually")

    async def _startup_reconcile_positions(self) -> None:
        try:
            exchange_positions = await self.exchange.list_open_positions()
        except Exception:
            log.error("reconcile_fetch_failed", exc_info=True)
            self._entry_locked = True
            self._lock_reason = "Could not fetch positions from Bybit"
            await notifier.notify_error(
                "Startup reconciliation",
                "Failed to fetch exchange positions — entries blocked")
            return

        exchange_has_positions = len(exchange_positions) > 0
        local_has_straddle = self.portfolio.has_open

        log.info("startup_reconcile",
                 exchange_positions=len(exchange_positions),
                 exchange_detail=[f"{p['category']}/{p['symbol']} "
                                  f"{p['amount']:+.4f}"
                                  for p in exchange_positions],
                 local_has_straddle=local_has_straddle)

        if exchange_has_positions and not local_has_straddle:
            details = "\n".join(
                f"  • {p['category']}/{p['symbol']}  "
                f"amt={p['amount']:+.4f}  "
                f"avg=${p['average_price']:,.2f}  "
                f"mark=${p['mark_price']:,.2f}  "
                f"uPnL=${p['unrealized_pnl']:+,.2f}"
                for p in exchange_positions
            )
            self._entry_locked = True
            self._lock_reason = (
                f"Exchange has {len(exchange_positions)} open position(s) "
                f"but algo state is empty — possible orphan"
            )
            await notifier.send(
                f"<b>RECONCILIATION MISMATCH</b>\n"
                f"Exchange has open positions but algo state is empty.\n\n"
                f"<b>Exchange positions:</b>\n{details}\n\n"
                f"<b>ACTION</b>: Entry locked until manually resolved.\n"
                f"Either close the positions or update positions.json.\n"
            )
            return

        if local_has_straddle and not exchange_has_positions:
            self._entry_locked = True
            self._lock_reason = (
                "Algo state has open straddle but exchange shows flat — "
                "stale positions.json"
            )
            await notifier.send(
                f"<b>RECONCILIATION MISMATCH</b>\n"
                f"Algo state claims open straddle but exchange shows flat.\n\n"
                f"<b>ACTION</b>: Entry locked. Clear state/positions.json to reset."
            )
            return

        log.info("startup_reconcile_ok",
                 flat=(not exchange_has_positions and not local_has_straddle),
                 matched_open=(exchange_has_positions and local_has_straddle))

    # ──────────────────── Entry ───────────────────────────────────

    async def _on_entry(self, session: config.Session) -> None:
        try:
            await self._run_entry(session)
        except Exception:
            log.error("entry_error", session=session.name, exc_info=True)
            await notifier.notify_error(
                f"Entry [{session.name}]",
                "Unhandled exception — check logs")

    async def _run_entry(self, session: config.Session) -> None:
        log.info("session_entry_start",
                 session=session.name, qty_per_leg=session.qty_per_leg)
        label = session.time_label

        if self._entry_locked:
            log.warning("entry_blocked_lock", reason=self._lock_reason)
            await notifier.notify_skip(
                f"Entry locked: {self._lock_reason}", session_label=label)
            return

        api_check = self.risk.check_api_health(self.exchange.error_count)
        if not api_check.allowed:
            log.warning("entry_blocked_api", reason=api_check.reason)
            await notifier.notify_skip(api_check.reason, session_label=label)
            return

        loss_check = self.risk.check_daily_loss()
        if not loss_check.allowed:
            log.warning("entry_blocked_loss", reason=loss_check.reason)
            await notifier.notify_skip(loss_check.reason, session_label=label)
            return

        if self.portfolio.has_open:
            log.warning("already_has_open_straddle", session=session.name)
            return

        # Refresh chain
        n_puts = await self.chain.refresh()
        if n_puts == 0:
            log.error("no_0dte_puts", session=session.name)
            await notifier.notify_skip("No 0DTE puts found", session_label=label)
            return

        spot = await self.market.get_spot_price()
        put = select_put(self.chain, spot)
        if put is None:
            await notifier.notify_skip(
                f"No ITM put near spot ${spot:,.0f}", session_label=label)
            return

        # ── Sync equity from live wallet before sizing ──
        if not config.DRY_RUN:
            live_equity = await self.exchange.get_total_equity_usd()
            if live_equity > 0:
                self.portfolio.sync_equity(live_equity)

        equity = self.portfolio.equity
        sizing = size_position(
            equity, spot, put.ask, qty_per_leg=session.qty_per_leg,
        )

        # ── Hard override: force exact straddle count ──
        if config.NUM_STRADDLES_OVERRIDE > 0:
            sizing.num_straddles = config.NUM_STRADDLES_OVERRIDE
            sizing.total_spot_margin = (
                sizing.spot_margin_per * sizing.num_straddles
            )
            sizing.total_put_cost = (
                sizing.put_cost_per * sizing.num_straddles
            )
            sizing.total_capital_required = (
                sizing.total_spot_margin
                + sizing.total_put_cost * (1 + 0.05)
            )
            log.info("straddles_override",
                     forced=config.NUM_STRADDLES_OVERRIDE,
                     session=session.name)

        if sizing.num_straddles == 0:
            msg = (
                f"Insufficient capital for even 1 straddle.\n"
                f"Equity: ${equity:,.2f}\n"
                f"Available ({config.ALLOC_PCT:.0%}): ${sizing.available_capital:,.2f}\n"
                f"Straddle cost: ${sizing.straddle_cost:,.2f}"
            )
            log.warning("zero_straddles", msg=msg)
            await notifier.notify_skip(msg, session_label=label)
            return

        entry_check = self.risk.check_entry(
            sizing.num_straddles, sizing.straddle_cost,
        )
        if not entry_check.allowed:
            log.warning("entry_blocked", reason=entry_check.reason)
            await notifier.notify_skip(entry_check.reason, session_label=label)
            return

        # ── Pre-entry collateral check ──
        if not config.DRY_RUN:
            available = await self.exchange.get_available_balance_usd(
                config.SETTLE_COIN)
            required = sizing.total_capital_required \
                * config.COLLATERAL_BUFFER_FACTOR
            if available < required:
                msg = (
                    f"Insufficient collateral.\n"
                    f"Available: ${available:,.2f}\n"
                    f"Required (× {config.COLLATERAL_BUFFER_FACTOR:.2f} "
                    f"buffer): ${required:,.2f}"
                )
                log.warning("collateral_check_failed", msg=msg)
                await notifier.notify_skip(msg, session_label=label)
                return
            log.info("collateral_check_ok",
                     available=f"${available:,.2f}",
                     required=f"${required:,.2f}")

        log.info(
            "preflight_check_passed",
            session=session.name,
            qty_per_leg=session.qty_per_leg,
            num_straddles=sizing.num_straddles,
            spot_margin_per=f"${sizing.spot_margin_per:,.2f}",
            put_cost_per=f"${sizing.put_cost_per:,.2f}",
            total_spot_margin=f"${sizing.total_spot_margin:,.2f}",
            total_put_cost=f"${sizing.total_put_cost:,.2f}",
            total_required=f"${sizing.total_capital_required:,.2f}",
            available=f"${sizing.available_capital:,.2f}",
            headroom=f"${sizing.available_capital - sizing.total_capital_required:,.2f}",
        )

        await notifier.send(
            f"<b>PRE-FLIGHT CHECK</b> [{label}]\n"
            f"Straddles: {sizing.num_straddles}\n"
            f"BTC per leg: {session.qty_per_leg:.4f}\n"
            f"Spot: ${spot:,.0f} | Strike: ${put.strike:,.0f}\n"
            f"\n<b>Per straddle:</b>\n"
            f"  Spot margin: ${sizing.spot_margin_per:,.2f}\n"
            f"  Put cost ({config.NUM_PUTS}×{session.qty_per_leg:.4f} BTC): ${sizing.put_cost_per:,.2f}\n"
            f"  Total: ${sizing.straddle_cost:,.2f}\n"
            f"\n<b>All {sizing.num_straddles} straddles:</b>\n"
            f"  Spot margin: ${sizing.total_spot_margin:,.2f}\n"
            f"  Put cost: ${sizing.total_put_cost:,.2f}\n"
            f"  Total (w/ 5% buffer): ${sizing.total_capital_required:,.2f}\n"
            f"  Available: ${sizing.available_capital:,.2f}\n"
            f"  Headroom: ${sizing.available_capital - sizing.total_capital_required:,.2f}\n"
        )

        straddle = await build_straddle(
            self.exchange, self.market, self.portfolio, put,
            sizing.num_straddles,
            qty_per_leg=session.qty_per_leg,
            session_name=session.name,
        )
        if straddle:
            self._consecutive_failures = 0
            volume_tracker.record_trade(
                sizing.num_straddles,
                qty_per_leg=session.qty_per_leg,
                session_name=session.name,
            )
            await notifier.notify_entry(
                num_straddles=sizing.num_straddles,
                equity=equity,
                straddle_cost=sizing.straddle_cost,
                spot_fill=straddle.entry_spot,
                strike=put.strike,
                put_premium=straddle.entry_put_price,
                spot_margin_used=(
                    straddle.entry_spot * session.qty_per_leg
                    / config.SPOT_LEVERAGE
                ),
                put_cost_total=straddle.total_put_cost,
                qty_per_leg=session.qty_per_leg,
                session_label=label,
            )
            log.info("session_entry_done",
                     session=session.name,
                     num_straddles=sizing.num_straddles)
        else:
            log.error("straddle_build_failed", session=session.name)
            self._register_session_failure(
                f"build_straddle [{session.name}] returned None")

    # ──────────────────── Failure tracking / circuit breaker ─────

    def _register_session_failure(self, reason: str) -> None:
        self._consecutive_failures += 1
        log.warning("session_failure_recorded",
                    count=self._consecutive_failures,
                    limit=config.CONSECUTIVE_FAILURE_LIMIT, reason=reason)
        if self._consecutive_failures >= config.CONSECUTIVE_FAILURE_LIMIT:
            self._entry_locked = True
            self._lock_reason = (
                f"{self._consecutive_failures} consecutive session failures "
                f"— restart algo to reset"
            )
            asyncio.create_task(notifier.send(
                f"<b>CIRCUIT BREAKER TRIPPED</b>\n"
                f"{self._consecutive_failures} consecutive session failures.\n"
                f"Entry LOCKED until restart."
            ))

    # ──────────────────── End-of-session reconciliation ──────────

    async def _post_close_reconcile(self) -> None:
        try:
            positions = await self.exchange.list_open_positions()
        except Exception:
            log.warning("post_close_reconcile_fetch_failed", exc_info=True)
            return

        if not positions:
            log.info("post_close_flat_ok")
            return

        details = "\n".join(
            f"  • {p['category']}/{p['symbol']}  amt={p['amount']:+.4f}  "
            f"mark=${p['mark_price']:,.2f}  uPnL=${p['unrealized_pnl']:+,.2f}"
            for p in positions
        )
        log.warning("post_close_orphan_detected", positions=len(positions))
        await notifier.send(
            f"<b>POST-CLOSE ORPHAN DETECTED</b>\n"
            f"Unwind ran but exchange still has {len(positions)} "
            f"position(s):\n\n"
            f"{details}\n\n"
            f"<b>ACTION</b>: investigate & close manually. Next entry "
            f"will be blocked at startup reconciliation."
        )

    # ──────────────────── Close ───────────────────────────────────

    async def _on_close(self, session: config.Session) -> None:
        label = session.time_label
        try:
            equity_before = self.portfolio.equity
            pnl = await self.exit_mgr.hard_close(
                session_name=session.name, session_label=label,
            )

            if not config.DRY_RUN:
                live_equity = await self.exchange.get_total_equity_usd()
                if live_equity > 0:
                    self.portfolio.sync_equity(live_equity)
                await self._post_close_reconcile()

            actual_pnl = self.portfolio.equity - equity_before
            self.portfolio.reset_daily()
            log.info("session_close_done",
                     session=session.name,
                     pnl=f"${pnl:,.2f}",
                     actual_pnl=f"${actual_pnl:,.2f}",
                     equity=f"${self.portfolio.equity:,.2f}")

            # ── Chained reports off the LAST_CLOSE session ──
            if session.name == config.LAST_CLOSE_SESSION_NAME:
                await notifier.send_daily_report(self.portfolio.equity)
                if self._is_saturday_morning_close():
                    await notifier.send_weekly_report(self.portfolio.equity)
        except Exception:
            log.error("close_error", session=session.name, exc_info=True)
            await notifier.notify_error(
                f"Close [{session.name}]",
                "Unhandled exception — check logs")

    def _is_saturday_morning_close(self) -> bool:
        """True when the current UTC weekday is Saturday (5) — used to
        chain the weekly report off the Saturday morning close.

        The morning session runs Tue–Sat. The Saturday firing therefore
        closes the trading week (Mon→Sat trading-day window covering
        Mon afternoon through Fri afternoon + Sat morning), which is
        the most natural moment to report the week's P&L.
        """
        return now_utc().weekday() == 5

    # ──────────────────── Shutdown ────────────────────────────────

    async def shutdown(self) -> None:
        log.info("shutdown_initiated")
        await notifier.send("<b>S3 ALGO SHUTTING DOWN</b>")

        self.scheduler.stop()

        if self.portfolio.has_open:
            log.warning("closing_remaining_position")
            await unwind_straddle(
                self.exchange, self.market, self.portfolio, reason="shutdown",
            )

        self.market.stop()
        _release_singleton_lock()
        log.info("algo_stopped")
        self._shutdown.set()


async def main() -> None:
    algo = Algo()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(algo.shutdown()))

    try:
        await algo.start()
    except KeyboardInterrupt:
        await algo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
