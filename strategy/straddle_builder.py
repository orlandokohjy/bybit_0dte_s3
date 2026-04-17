"""
Atomic straddle construction and teardown.

One straddle = 0.5 BTC spot margin (long) + 2 × 0.5 BTC ITM put (long).

Entry : spot first (GTC limit at bid — maker) → puts (GTC limit at bid — maker × NUM_PUTS)
Exit  : spot first (GTC limit at ask — maker) → puts (GTC limit at ask — maker × NUM_PUTS)
"""
from __future__ import annotations

import asyncio
import csv
import os
import uuid
from typing import Optional

import structlog

import config
from core.exchange import BybitExchange
from core.portfolio import Portfolio, Straddle, StraddleLeg, TRADE_LOG_FIELDS
from data.market_data import MarketData
from data.option_chain import OptionInfo
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)


async def build_straddle(
    exchange: BybitExchange,
    market: MarketData,
    portfolio: Portfolio,
    put: OptionInfo,
    num_straddles: int,
) -> Optional[Straddle]:
    """
    Execute the atomic entry for N identical straddle units.

    1. Buy spot (margin):  QTY_PER_LEG × num_straddles BTC
    2. Buy puts:           NUM_PUTS legs, each QTY_PER_LEG × num_straddles BTC
    3. Subscribe to option ticker
    4. Register straddle in portfolio
    """
    straddle_id = f"S3-{uuid.uuid4().hex[:8]}"
    spot_price = await market.get_spot_price()
    total_spot_qty = config.QTY_PER_LEG * num_straddles

    log.info("building_straddle", id=straddle_id, spot=spot_price,
             put=put.symbol, strike=put.strike, num=num_straddles)

    # ── Step 1: Buy spot (GTC limit at bid — maker) ──
    try:
        spot_result = await exchange.buy_spot(total_spot_qty)
    except Exception as exc:
        log.error("spot_buy_failed", id=straddle_id, error=str(exc))
        return None

    if not spot_result or not spot_result.get("orderId"):
        log.error("spot_buy_chase_exhausted", id=straddle_id)
        return None

    spot_order_id = spot_result["orderId"]
    spot_fill = float(spot_result.get("avgPrice", 0)) or spot_price
    log.info("spot_filled", id=straddle_id, price=spot_fill, order_id=spot_order_id)

    spot_leg = StraddleLeg(
        instrument=config.SPOT_SYMBOL, side="Buy",
        qty=total_spot_qty, entry_price=spot_fill,
        order_id=spot_order_id, avg_fill_price=spot_fill,
    )

    # ── Step 2: Subscribe option WS + Buy NUM_PUTS put legs (nearest ITM) ──
    market.subscribe_option(put.symbol)
    await asyncio.sleep(1)

    put_bid = put.bid
    if put_bid <= 0:
        put_bid, _ = await market.get_option_bid_ask(put.symbol)
    if put_bid <= 0:
        log.error("put_no_bid", id=straddle_id, symbol=put.symbol)
        await _emergency_unwind_all(
            exchange, market, portfolio, put.symbol,
            spot_fill, total_spot_qty, [], straddle_id)
        return None

    put_legs: list[StraddleLeg] = []
    total_put_qty = config.QTY_PER_LEG * num_straddles

    for i in range(config.NUM_PUTS):
        result = await exchange.chase_buy_put(put.symbol, total_put_qty, put_bid)
        if result is None:
            log.error("put_chase_deadline_expired", id=straddle_id,
                      leg=i + 1, symbol=put.symbol)
            await _emergency_unwind_all(
                exchange, market, portfolio, put.symbol,
                spot_fill, total_spot_qty, put_legs, straddle_id)
            return None

        fill_price = float(result.get("avgPrice", put_bid))
        put_legs.append(StraddleLeg(
            instrument=put.symbol, side="Buy",
            qty=total_put_qty, entry_price=fill_price,
            order_id=result.get("orderId", ""), avg_fill_price=fill_price,
        ))
        log.info("put_filled", id=straddle_id, leg=i + 1, price=fill_price)

    # ── Step 3: Register ──
    avg_put_price = sum(p.avg_fill_price for p in put_legs) / len(put_legs)
    total_put_cost = config.NUM_PUTS * config.QTY_PER_LEG * avg_put_price
    straddle_cost = (config.QTY_PER_LEG * spot_fill / config.SPOT_LEVERAGE) + total_put_cost

    straddle = Straddle(
        id=straddle_id,
        spot_leg=spot_leg,
        put_legs=put_legs,
        put_strike=put.strike,
        spot_qty=config.QTY_PER_LEG,
        put_qty_each=config.QTY_PER_LEG,
        entry_time=now_utc().isoformat(),
        entry_spot=spot_fill,
        entry_put_price=avg_put_price,
        total_put_cost=total_put_cost,
        straddle_cost=straddle_cost,
        num_straddles=num_straddles,
    )

    portfolio.set_straddle(straddle)
    log.info("straddle_built", id=straddle_id, num=num_straddles,
             cost=f"${straddle_cost * num_straddles:,.2f}",
             spot=spot_fill, put_premium=avg_put_price, strike=put.strike)
    return straddle


async def unwind_straddle(
    exchange: BybitExchange,
    market: MarketData,
    portfolio: Portfolio,
    reason: str = "hard_close",
) -> float:
    """
    Close the open straddle: sell spot first, then sell all put legs.
    Returns the P&L.
    """
    straddle = portfolio.open_straddle
    if straddle is None:
        return 0.0

    log.info("unwinding", id=straddle.id, reason=reason)

    # ── Sell spot (GTC limit at ask — maker) ──
    try:
        sell_result = await exchange.sell_spot(straddle.spot_leg.qty)
        if not sell_result or not sell_result.get("orderId"):
            log.error("spot_sell_chase_exhausted", id=straddle.id)
    except Exception as exc:
        log.error("spot_sell_failed", id=straddle.id, error=str(exc))

    exit_spot = await market.get_spot_price()

    # ── Sell all put legs (GTC limit at ask — maker) ──
    exit_put_price = 0.0
    put_prices: list[float] = []
    for pl in straddle.put_legs:
        _, ask = await market.get_option_bid_ask(pl.instrument)
        if ask > 0:
            result = await exchange.chase_sell_put(pl.instrument, pl.qty, ask)
            if result:
                price = float(result.get("avgPrice", ask))
                put_prices.append(price)
                log.info("put_sold", instrument=pl.instrument, price=price)
            else:
                log.warning("put_sell_failed", instrument=pl.instrument)
                put_prices.append(0.0)
        else:
            log.warning("put_no_ask", instrument=pl.instrument)
            put_prices.append(0.0)

    exit_put_price = sum(put_prices) / len(put_prices) if put_prices else 0.0

    pnl = portfolio.close_straddle(exit_spot, exit_put_price, reason)
    log.info("straddle_unwound", id=straddle.id, reason=reason,
             pnl=f"${pnl:,.2f}", exit_spot=exit_spot, exit_put=exit_put_price)
    return pnl


async def _emergency_unwind_all(
    exchange: BybitExchange,
    market: MarketData,
    portfolio: Portfolio,
    put_symbol: str,
    spot_entry_price: float,
    spot_qty: float,
    filled_put_legs: list[StraddleLeg],
    straddle_id: str,
) -> None:
    """
    Comprehensive emergency unwind after a put chase deadline expires.

    1. Query Bybit for actual held put position (catches orphaned partial fills)
    2. Sell all held puts
    3. Sell spot
    4. Log the emergency P&L to trade_log.csv
    5. Send Telegram alert
    """
    from core import notifier

    log.warning("emergency_unwind_start", id=straddle_id,
                spot_qty=spot_qty, known_put_legs=len(filled_put_legs))

    entry_time = now_utc().isoformat()
    total_put_pnl = 0.0
    total_put_entry_cost = 0.0

    # ── 1. Query actual put position from Bybit ──
    actual_put_qty = await exchange.get_option_position(put_symbol)
    log.info("emergency_actual_put_position", symbol=put_symbol, qty=actual_put_qty)

    # ── 2. Sell all held puts ──
    put_exit_price = 0.0
    if actual_put_qty > 0:
        _, ask = await market.get_option_bid_ask(put_symbol)
        if ask > 0:
            result = await exchange.chase_sell_put(put_symbol, actual_put_qty, ask)
            if result:
                put_exit_price = float(result.get("avgPrice", ask))
                log.info("emergency_puts_sold", symbol=put_symbol,
                         qty=actual_put_qty, price=put_exit_price)
            else:
                log.error("emergency_put_sell_chase_failed", symbol=put_symbol)
        else:
            log.warning("emergency_put_no_ask", symbol=put_symbol)

        avg_put_entry = (
            sum(p.avg_fill_price for p in filled_put_legs) / len(filled_put_legs)
            if filled_put_legs else 0.0)
        total_put_entry_cost = avg_put_entry * actual_put_qty
        total_put_pnl = (put_exit_price - avg_put_entry) * actual_put_qty

    # ── 3. Sell spot ──
    spot_exit_price = 0.0
    spot_pnl = 0.0
    try:
        sell_result = await exchange.sell_spot(spot_qty)
        if sell_result and sell_result.get("orderId"):
            spot_exit_price = float(sell_result.get("avgPrice", 0))
            if spot_exit_price <= 0:
                spot_exit_price = await market.get_spot_price()
            log.info("emergency_spot_sold", qty=spot_qty, price=spot_exit_price)
        else:
            spot_exit_price = await market.get_spot_price()
            log.error("emergency_spot_sell_chase_failed", qty=spot_qty)
    except Exception:
        spot_exit_price = await market.get_spot_price()
        log.error("emergency_spot_sell_failed", qty=spot_qty, exc_info=True)

    spot_pnl = (spot_exit_price - spot_entry_price) * spot_qty
    net_pnl = spot_pnl + total_put_pnl

    # ── 4. Log to trade_log.csv ──
    _log_emergency_trade(
        portfolio=portfolio,
        entry_time=entry_time,
        spot_entry=spot_entry_price,
        spot_exit=spot_exit_price,
        spot_qty=spot_qty,
        put_entry_price=total_put_entry_cost / actual_put_qty if actual_put_qty > 0 else 0,
        put_exit_price=put_exit_price,
        spot_pnl=spot_pnl,
        put_pnl=total_put_pnl,
        net_pnl=net_pnl,
    )

    # ── 5. Telegram alert ──
    pnl_sign = "+" if net_pnl >= 0 else ""
    await notifier.send(
        f"<b>EMERGENCY UNWIND</b> [{straddle_id}]\n"
        f"Put chase deadline expired ({config.OPTION_CHASE_DEADLINE_SEC:.0f}s)\n"
        f"\n<b>Spot</b>\n"
        f"  Entry: ${spot_entry_price:,.2f} → Exit: ${spot_exit_price:,.2f}\n"
        f"  P&L: ${spot_pnl:,.2f}\n"
        f"\n<b>Puts ({put_symbol})</b>\n"
        f"  Qty held: {actual_put_qty}\n"
        f"  Exit price: ${put_exit_price:,.2f}\n"
        f"  P&L: ${total_put_pnl:,.2f}\n"
        f"\n<b>Net P&L: {pnl_sign}${net_pnl:,.2f}</b>"
    )

    log.warning("emergency_unwind_done", id=straddle_id,
                spot_pnl=f"${spot_pnl:,.2f}",
                put_pnl=f"${total_put_pnl:,.2f}",
                net_pnl=f"${net_pnl:,.2f}",
                actual_put_qty=actual_put_qty)


def _log_emergency_trade(
    portfolio: Portfolio,
    entry_time: str,
    spot_entry: float,
    spot_exit: float,
    spot_qty: float,
    put_entry_price: float,
    put_exit_price: float,
    spot_pnl: float,
    put_pnl: float,
    net_pnl: float,
) -> None:
    """Write a row to trade_log.csv for the emergency unwind."""
    equity_before = portfolio.equity
    portfolio.adjust_equity(net_pnl)
    equity_after = portfolio.equity

    row = {
        "date": entry_time[:10],
        "entry_time": entry_time,
        "exit_time": now_utc().isoformat(),
        "exit_reason": "emergency_unwind",
        "num_straddles": 0,
        "spot_entry": spot_entry,
        "spot_exit": spot_exit,
        "put_strike": 0,
        "put_premium_entry": put_entry_price,
        "put_premium_exit": put_exit_price,
        "spot_margin_used": round(spot_qty * spot_entry / config.SPOT_LEVERAGE, 2),
        "put_premium_cost": 0,
        "total_capital_used": 0,
        "straddle_cost": 0,
        "capital_before": equity_before,
        "spot_pnl": round(spot_pnl, 2),
        "put_pnl": round(put_pnl, 2),
        "gross_pnl": round(net_pnl, 2),
        "fees": 0.0,
        "net_pnl": round(net_pnl, 2),
        "capital_after": equity_after,
    }

    os.makedirs(config.STATE_DIR, exist_ok=True)
    needs_header = not os.path.exists(config.TRADE_LOG_FILE)
    with open(config.TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)

    log.info("emergency_trade_logged", net_pnl=f"${net_pnl:,.2f}",
             equity_after=f"${equity_after:,.2f}")
