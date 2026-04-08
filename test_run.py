"""
Manual test run — triggers a full entry → hold → close cycle immediately.

Usage:
    1. Set up .env:
         BYBIT_API_KEY=<your demo key>
         BYBIT_API_SECRET=<your demo secret>
         BYBIT_DEMO=true
         DRY_RUN=false
         LOG_LEVEL=DEBUG

    2. Run during market hours (when 0DTE options exist):
         python test_run.py

    3. The script will:
         - Connect to Bybit Demo
         - Show your account balance
         - Refresh the 0DTE option chain
         - Run the pre-flight sizing
         - Execute the full entry (spot + puts)
         - Wait HOLD_SECONDS (default 60s)
         - Execute the full close (sell spot + sell puts)
         - Generate and print the daily report

    Set HOLD_SECONDS=0 to close immediately after entry (tests execution only).
"""
from __future__ import annotations

import asyncio
import os
import sys

import structlog

import config
from core.exchange import BybitExchange
from core.portfolio import Portfolio
from core import notifier
from data.market_data import MarketData
from data.option_chain import OptionChain
from risk.risk_manager import RiskManager
from strategy.option_selector import select_put
from strategy.position_sizer import size_position
from strategy.straddle_builder import build_straddle, unwind_straddle
from utils.logging_config import setup_logging
from utils.time_utils import now_utc
from reporting.daily_report import compute_report, format_telegram_report

HOLD_SECONDS = int(os.getenv("HOLD_SECONDS", "60"))

log = structlog.get_logger(__name__)


async def run_test() -> None:
    setup_logging()

    print("=" * 60)
    print("  BYBIT 0DTE S3 — MANUAL TEST RUN")
    print(f"  Demo: {config.DEMO} | DRY_RUN: {config.DRY_RUN}")
    print(f"  Hold time: {HOLD_SECONDS}s")
    print("=" * 60)
    print()

    if not config.BYBIT_API_KEY or not config.BYBIT_API_SECRET:
        print("ERROR: Set BYBIT_API_KEY and BYBIT_API_SECRET in .env")
        sys.exit(1)

    exchange = BybitExchange()
    chain = OptionChain(exchange)
    market = MarketData(exchange, chain)
    portfolio = Portfolio()
    risk = RiskManager(portfolio)

    # ── Step 1: Connect and show account info ──
    print("[1/7] Connecting to Bybit...")
    if not config.DRY_RUN:
        await exchange.set_portfolio_margin()
        await exchange.set_spot_hedging()
        await exchange.set_spot_margin_leverage()

    margin_mode = await exchange.get_margin_mode()
    await market.start()
    spot = await market.get_spot_price()
    wallet_eq = await exchange.get_total_equity_usd()

    print(f"  Margin mode:   {margin_mode}")
    print(f"  Spot price:    ${spot:,.2f}")
    print(f"  Wallet equity: ${wallet_eq:,.2f}")
    print(f"  Local equity:  ${portfolio.equity:,.2f}")
    print()

    # ── Step 2: Refresh option chain ──
    print("[2/7] Refreshing 0DTE option chain...")
    n_puts = await chain.refresh()
    if n_puts == 0:
        print("  ERROR: No 0DTE puts found. Are markets open?")
        print("  0DTE options only exist during trading hours and expire daily at 08:00 UTC.")
        market.stop()
        return
    print(f"  Found {n_puts} puts")
    print()

    # ── Step 3: Select ITM put ──
    print("[3/7] Selecting nearest ITM put...")
    put = select_put(chain, spot)
    if put is None:
        print("  ERROR: No suitable ITM put found")
        market.stop()
        return
    print(f"  Selected: {put.symbol}")
    print(f"  Strike:   ${put.strike:,.0f}  (spot ${spot:,.0f})")
    print(f"  Bid/Ask:  ${put.bid:,.2f} / ${put.ask:,.2f}")
    print()

    # ── Step 4: Pre-flight sizing ──
    print("[4/7] Pre-flight capital check...")
    sizing = size_position(portfolio.equity, spot, put.ask)
    print(f"  Available (60%):   ${sizing.available_capital:,.2f}")
    print(f"  Per straddle:")
    print(f"    Spot margin:     ${sizing.spot_margin_per:,.2f}")
    print(f"    Put cost:        ${sizing.put_cost_per:,.2f}")
    print(f"    Total:           ${sizing.straddle_cost:,.2f}")
    print(f"  Straddles:         {sizing.num_straddles}")
    print(f"  Total required:    ${sizing.total_capital_required:,.2f}")
    print(f"  Headroom:          ${sizing.available_capital - sizing.total_capital_required:,.2f}")
    print()

    if sizing.num_straddles == 0:
        print("  Cannot size even 1 straddle — insufficient capital.")
        market.stop()
        return

    # ── Step 5: Execute entry ──
    print(f"[5/7] Executing entry ({sizing.num_straddles} straddle(s))...")
    straddle = await build_straddle(
        exchange, market, portfolio, put, sizing.num_straddles,
    )
    if straddle is None:
        print("  ERROR: Straddle build failed — check logs")
        market.stop()
        return
    print(f"  Straddle ID:  {straddle.id}")
    print(f"  Spot fill:    ${straddle.entry_spot:,.2f}")
    print(f"  Put premium:  ${straddle.entry_put_price:,.2f}")
    print(f"  Total cost:   ${straddle.straddle_cost:,.2f}")
    print()

    # ── Step 6: Hold ──
    PRINT_INTERVAL = int(os.getenv("PRINT_INTERVAL", "300"))  # seconds between prints
    if HOLD_SECONDS > 0:
        print(f"[6/7] Holding position for {HOLD_SECONDS}s (print every {PRINT_INTERVAL}s)...")
        put_symbol = straddle.put_legs[0].instrument
        elapsed = 0
        while elapsed < HOLD_SECONDS:
            sleep_time = min(PRINT_INTERVAL, HOLD_SECONDS - elapsed)
            await asyncio.sleep(sleep_time)
            elapsed += sleep_time
            cur_spot = await market.get_spot_price()
            cached_put = exchange.get_cached_option(put_symbol)
            put_mark = cached_put.mark if cached_put and cached_put.mark > 0 else straddle.entry_put_price
            spot_unr = straddle.spot_pnl(cur_spot)
            put_unr = straddle.put_pnl(put_mark)
            total_unr = spot_unr + put_unr
            hrs = elapsed // 3600
            mins = (elapsed % 3600) // 60
            print(
                f"  {hrs}h{mins:02d}m | Spot: ${cur_spot:,.2f} | "
                f"Spot P&L: ${spot_unr:,.2f} | Put P&L: ${put_unr:,.2f} | "
                f"Total: ${total_unr:,.2f}"
            )
        print()
    else:
        print("[6/7] Hold skipped (HOLD_SECONDS=0)")
        print()

    # ── Step 7: Execute close ──
    print("[7/7] Closing position...")
    pnl = await unwind_straddle(exchange, market, portfolio, reason="test_close")
    print(f"  P&L:    ${pnl:,.2f}")
    print(f"  Equity: ${portfolio.equity:,.2f}")
    print()

    # ── Report ──
    print("=" * 60)
    print("  PERFORMANCE REPORT")
    print("=" * 60)
    metrics = compute_report(portfolio.equity)
    if metrics:
        report = format_telegram_report(metrics)
        clean = report.replace("<b>", "").replace("</b>", "")
        print(clean)
        await notifier.send_daily_report(portfolio.equity)
    else:
        print("  (No trades in log yet)")
    print()

    portfolio.reset_daily()
    market.stop()
    print("Test complete.")


if __name__ == "__main__":
    asyncio.run(run_test())
