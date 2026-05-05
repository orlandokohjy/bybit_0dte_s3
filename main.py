"""
Bybit 0DTE BTC Synthetic Straddle — Session 3 Only.

Single daily session: 14:00–18:00 UTC, Mon–Fri.
Position: 0.5 BTC spot (margin) + 2 × 0.5 BTC ITM puts per straddle.
Compound sizing: 80 % of current equity, no cap on straddles.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import sys

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


def _disable_entry_now_in_env_file(env_path: str = ".env") -> None:
    """Rewrite ENTRY_NOW=true to ENTRY_NOW=false in the local .env file.

    Called immediately after consuming an immediate-entry trigger so that
    the next container restart does NOT fire entry again.  Must be done
    BEFORE the actual entry runs (so a crash mid-entry still leaves the
    env file disabled).

    Silent no-op if the file is missing, read-only, or doesn't contain
    ENTRY_NOW — the in-memory env var has already been read.
    """
    try:
        if not os.path.exists(env_path):
            log.debug("entry_now_disable_skipped", reason="no_env_file")
            return
        with open(env_path, "r") as f:
            content = f.read()
        new_content = re.sub(
            r"^(\s*ENTRY_NOW\s*=\s*)(true|TRUE|True|1)\b.*$",
            r"\1false",
            content,
            flags=re.MULTILINE,
        )
        if new_content != content:
            with open(env_path, "w") as f:
                f.write(new_content)
            log.info("entry_now_auto_disabled", env_path=env_path)
        else:
            log.debug("entry_now_disable_noop", reason="no_match")
    except Exception:
        log.warning("entry_now_disable_failed", exc_info=True)


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
        # Set by startup reconciliation when exchange disagrees with local
        # state, or by the consecutive-failure circuit breaker. Blocks new
        # entries until manually cleared (delete state/positions.json or
        # restart).
        self._entry_locked: bool = False
        self._lock_reason: str = ""
        self._consecutive_failures: int = 0

    async def start(self) -> None:
        setup_logging()
        log.info("algo_starting", demo=config.DEMO, dry_run=config.DRY_RUN)

        if not config.BYBIT_API_KEY or not config.BYBIT_API_SECRET:
            log.error("missing_api_credentials")
            sys.exit(1)

        if not config.DRY_RUN:
            # 1. Switch to Portfolio Margin (lower maintenance margin for our hedge)
            await self.exchange.set_portfolio_margin()
            # 2. Enable Spot Hedging (spot included in stress testing)
            await self.exchange.set_spot_hedging()
            # 3. Set spot margin leverage
            await self.exchange.set_spot_margin_leverage()

            # 4. Cancel any stale orders + reconcile positions
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

        lock_line = (f"\n<b>⚠️ ENTRY LOCKED</b>: {self._lock_reason}"
                     if self._entry_locked else "")
        await notifier.send(
            f"<b>S3 ALGO STARTED</b>\n"
            f"Mode: {'DEMO' if config.DEMO else 'LIVE'}"
            f"{' (DRY RUN)' if config.DRY_RUN else ''}\n"
            f"Margin: {margin_mode}\n"
            f"Spot: ${spot:,.2f}\n"
            f"Equity: ${self.portfolio.equity:,.2f}\n"
            f"Time: {format_utc_sgt(now_utc())}"
            f"{lock_line}\n"
        )

        self.exchange.start_private_ws()

        self.scheduler.register_session(
            on_entry=self._on_entry,
            on_close=self._on_close,
            on_report=self._on_report,
            on_weekly_report=self._on_weekly_report,
        )
        self.scheduler.start()

        fire_times = self.scheduler.get_next_fire_times()
        for job_id, ft in fire_times.items():
            if ft:
                log.info("next_fire", job=job_id, time=format_utc_sgt(ft))

        if os.getenv("ENTRY_NOW", "").lower() == "true":
            log.info("immediate_entry_triggered")
            _disable_entry_now_in_env_file()
            await self._on_entry()

        log.info("algo_running")
        await self._shutdown.wait()

    # ──────────────────── Startup Safeguards ──────────────────────

    async def _startup_cancel_stale_orders(self) -> None:
        """Cancel any resting orders left from a previous run.

        Stale orders eat margin and can cause 'Insufficient funds' rejections
        on new entries. Mirrors the same primitive on Derive/OKX.
        """
        try:
            cancelled = await self.exchange.cancel_all_open_orders()
            if cancelled > 0:
                await notifier.send(
                    f"<b>STARTUP CLEANUP</b>\n"
                    f"Cancelled {cancelled} stale open order(s) from "
                    f"previous run."
                )
        except Exception:
            log.error("startup_cancel_failed", exc_info=True)
            await notifier.notify_error(
                "Startup cleanup",
                "Failed to cancel stale orders — check logs manually")

    async def _startup_reconcile_positions(self) -> None:
        """Compare exchange positions against local positions.json.

        If they disagree, set entry lock to prevent blind re-entry on top of
        a mis-tracked position (which would compound errors).  This catches
        orphan options OR significant BTC spot balance that the algo isn't
        tracking — the historic 0.35 BTC orphan would have been caught here.
        """
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
                f"<b>⚠️ RECONCILIATION MISMATCH</b>\n"
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
                f"<b>⚠️ RECONCILIATION MISMATCH</b>\n"
                f"Algo state claims open straddle but exchange shows flat.\n\n"
                f"<b>ACTION</b>: Entry locked. Clear state/positions.json "
                f"to reset."
            )
            return

        log.info("startup_reconcile_ok",
                 flat=(not exchange_has_positions and not local_has_straddle),
                 matched_open=(exchange_has_positions and local_has_straddle))

    # ──────────────────── Entry ───────────────────────────────────

    async def _on_entry(self) -> None:
        try:
            await self._run_entry()
        except Exception:
            log.error("entry_error", exc_info=True)
            await notifier.notify_error("Entry", "Unhandled exception — check logs")

    async def _run_entry(self) -> None:
        log.info("session_entry_start")

        if self._entry_locked:
            log.warning("entry_blocked_lock", reason=self._lock_reason)
            await notifier.notify_skip(f"Entry locked: {self._lock_reason}")
            return

        # Pre-checks
        api_check = self.risk.check_api_health(self.exchange.error_count)
        if not api_check.allowed:
            log.warning("entry_blocked_api", reason=api_check.reason)
            await notifier.notify_skip(api_check.reason)
            return

        loss_check = self.risk.check_daily_loss()
        if not loss_check.allowed:
            log.warning("entry_blocked_loss", reason=loss_check.reason)
            await notifier.notify_skip(loss_check.reason)
            return

        if self.portfolio.has_open:
            log.warning("already_has_open_straddle")
            return

        # Refresh chain
        n_puts = await self.chain.refresh()
        if n_puts == 0:
            log.error("no_0dte_puts")
            await notifier.notify_skip("No 0DTE puts found")
            return

        # Select put
        spot = await self.market.get_spot_price()
        put = select_put(self.chain, spot)
        if put is None:
            await notifier.notify_skip(f"No ITM put near spot ${spot:,.0f}")
            return

        # ── Sync equity from live wallet before sizing ──
        if not config.DRY_RUN:
            live_equity = await self.exchange.get_total_equity_usd()
            if live_equity > 0:
                self.portfolio.sync_equity(live_equity)

        equity = self.portfolio.equity
        sizing = size_position(equity, spot, put.ask)

        if sizing.num_straddles == 0:
            msg = (
                f"Insufficient capital for even 1 straddle.\n"
                f"Equity: ${equity:,.2f}\n"
                f"Available (60%): ${sizing.available_capital:,.2f}\n"
                f"Straddle cost: ${sizing.straddle_cost:,.2f}"
            )
            log.warning("zero_straddles", msg=msg)
            await notifier.notify_skip(msg)
            return

        entry_check = self.risk.check_entry(sizing.num_straddles, sizing.straddle_cost)
        if not entry_check.allowed:
            log.warning("entry_blocked", reason=entry_check.reason)
            await notifier.notify_skip(entry_check.reason)
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
                await notifier.notify_skip(msg)
                return
            log.info("collateral_check_ok",
                     available=f"${available:,.2f}",
                     required=f"${required:,.2f}")

        # ── Log the pre-flight capital breakdown ──
        log.info(
            "preflight_check_passed",
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
            f"<b>PRE-FLIGHT CHECK</b>\n"
            f"Straddles: {sizing.num_straddles}\n"
            f"Spot: ${spot:,.0f} | Strike: ${put.strike:,.0f}\n"
            f"\n<b>Per straddle:</b>\n"
            f"  Spot margin: ${sizing.spot_margin_per:,.2f}\n"
            f"  Put cost ({config.NUM_PUTS}×{config.QTY_PER_LEG} BTC): ${sizing.put_cost_per:,.2f}\n"
            f"  Total: ${sizing.straddle_cost:,.2f}\n"
            f"\n<b>All {sizing.num_straddles} straddles:</b>\n"
            f"  Spot margin: ${sizing.total_spot_margin:,.2f}\n"
            f"  Put cost: ${sizing.total_put_cost:,.2f}\n"
            f"  Total (w/ 5% buffer): ${sizing.total_capital_required:,.2f}\n"
            f"  Available: ${sizing.available_capital:,.2f}\n"
            f"  Headroom: ${sizing.available_capital - sizing.total_capital_required:,.2f}\n"
        )

        # ── Execute: only proceed with complete straddles ──
        straddle = await build_straddle(
            self.exchange, self.market, self.portfolio, put, sizing.num_straddles,
        )
        if straddle:
            self._consecutive_failures = 0  # reset on success
            volume_tracker.record_trade(sizing.num_straddles)
            await notifier.notify_entry(
                num_straddles=sizing.num_straddles,
                equity=equity,
                straddle_cost=sizing.straddle_cost,
                spot_fill=straddle.entry_spot,
                strike=put.strike,
                put_premium=straddle.entry_put_price,
                spot_margin_used=straddle.entry_spot * config.QTY_PER_LEG / config.SPOT_LEVERAGE,
                put_cost_total=straddle.total_put_cost,
            )
            log.info("session_entry_done", num_straddles=sizing.num_straddles)
        else:
            log.error("straddle_build_failed")
            self._register_session_failure("build_straddle returned None")

    # ──────────────────── Failure tracking / circuit breaker ─────

    def _register_session_failure(self, reason: str) -> None:
        """Increment failure counter; lock entries if threshold exceeded."""
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
                f"<b>⚠️ CIRCUIT BREAKER TRIPPED</b>\n"
                f"{self._consecutive_failures} consecutive session failures.\n"
                f"Entry LOCKED until restart."
            ))

    # ──────────────────── End-of-session reconciliation ──────────

    async def _post_close_reconcile(self) -> None:
        """After unwind, verify exchange is actually flat. Alert on orphans."""
        try:
            positions = await self.exchange.list_open_positions()
        except Exception:
            log.warning("post_close_reconcile_fetch_failed", exc_info=True)
            return

        if not positions:
            log.info("post_close_flat_ok")
            return

        # We're not flat — orphan exists
        details = "\n".join(
            f"  • {p['category']}/{p['symbol']}  amt={p['amount']:+.4f}  "
            f"mark=${p['mark_price']:,.2f}  uPnL=${p['unrealized_pnl']:+,.2f}"
            for p in positions
        )
        log.warning("post_close_orphan_detected", positions=len(positions))
        await notifier.send(
            f"<b>⚠️ POST-CLOSE ORPHAN DETECTED</b>\n"
            f"Unwind ran but exchange still has {len(positions)} "
            f"position(s):\n\n"
            f"{details}\n\n"
            f"<b>ACTION</b>: investigate & close manually. Next entry "
            f"will be blocked at startup reconciliation."
        )

    # ──────────────────── Close ───────────────────────────────────

    async def _on_close(self) -> None:
        try:
            equity_before = self.portfolio.equity
            pnl = await self.exit_mgr.hard_close()

            if not config.DRY_RUN:
                live_equity = await self.exchange.get_total_equity_usd()
                if live_equity > 0:
                    self.portfolio.sync_equity(live_equity)
                # Verify flat after unwind
                await self._post_close_reconcile()

            actual_pnl = self.portfolio.equity - equity_before
            if actual_pnl != 0.0:
                cum_return = (self.portfolio.equity - config.INITIAL_CAPITAL_USD) / config.INITIAL_CAPITAL_USD
                await notifier.notify_daily_summary(
                    self.portfolio.equity, actual_pnl, cum_return,
                )
            self.portfolio.reset_daily()
            log.info("session_close_done", pnl=f"${pnl:,.2f}",
                     actual_pnl=f"${actual_pnl:,.2f}",
                     equity=f"${self.portfolio.equity:,.2f}")
        except Exception:
            log.error("close_error", exc_info=True)
            await notifier.notify_error("Close", "Unhandled exception — check logs")

    # ──────────────────── Daily Report (19:00 UTC) ────────────────

    async def _on_report(self) -> None:
        try:
            await notifier.send_daily_report(self.portfolio.equity)
        except Exception:
            log.error("report_error", exc_info=True)
            await notifier.notify_error("Report", "Daily report failed — check logs")

    # ──────────────────── Weekly Report (Fri 20:00 UTC) ─────────

    async def _on_weekly_report(self) -> None:
        try:
            await notifier.send_weekly_report(self.portfolio.equity)
        except Exception:
            log.error("weekly_report_error", exc_info=True)
            await notifier.notify_error("Weekly Report", "Weekly report failed — check logs")

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
