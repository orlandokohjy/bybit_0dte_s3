"""
Atomic straddle construction and teardown.

One straddle = 0.5 BTC spot margin (long) + 2 × 0.5 BTC ITM put (long).

Entry (NEW — hard leg first):
  1. Pre-entry spread gate — skip session if put spread > OPTION_MAX_ENTRY_SPREAD_PCT
  2. Buy puts FIRST (illiquid leg) — maker chase with 50% gap-narrowing + fair-value cap
  3. If puts fail → skip session (no spot bought, no exposure)
  4. Buy spot — maker (fast fill on BTCUSDT)
  5. If spot fails after puts filled → emergency-sell puts only (narrow unwind)

Exit (unchanged): spot first (GTC limit at ask — maker) → puts (GTC limit at ask — maker × NUM_PUTS)
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

    NEW ORDER (hard leg first):
      1. Pre-entry spread gate
      2. Buy puts (NUM_PUTS legs, each QTY_PER_LEG × num_straddles BTC)
      3. If puts fail → skip session, no spot exposure
      4. Buy spot (margin): QTY_PER_LEG × num_straddles BTC
      5. If spot fails → roll back puts only
      6. Register straddle in portfolio
    """
    from core import notifier

    straddle_id = f"S3-{uuid.uuid4().hex[:8]}"
    spot_price = await market.get_spot_price()
    total_spot_qty = config.QTY_PER_LEG * num_straddles
    total_put_qty = config.QTY_PER_LEG * num_straddles

    log.info("building_straddle", id=straddle_id, spot=spot_price,
             put=put.symbol, strike=put.strike, num=num_straddles)

    # ── Step 1: Subscribe option WS + pre-entry spread gate ──
    market.subscribe_option(put.symbol)
    await asyncio.sleep(1)

    put_bid, put_ask = await market.get_option_bid_ask(put.symbol)
    if put_bid <= 0 or put_ask <= 0:
        log.error("put_no_quote", id=straddle_id, symbol=put.symbol,
                  bid=put_bid, ask=put_ask)
        await notifier.send(
            f"<b>ENTRY SKIPPED</b> [{straddle_id}]\n"
            f"No bid/ask on {put.symbol} — market closed or illiquid"
        )
        return None

    put_mid = (put_bid + put_ask) / 2
    put_spread_pct = (put_ask - put_bid) / put_mid if put_mid > 0 else 1.0
    if put_spread_pct > config.OPTION_MAX_ENTRY_SPREAD_PCT:
        log.warning("put_spread_too_wide", id=straddle_id, symbol=put.symbol,
                    bid=put_bid, ask=put_ask,
                    spread_pct=f"{put_spread_pct:.1%}",
                    cap_pct=f"{config.OPTION_MAX_ENTRY_SPREAD_PCT:.1%}")
        await notifier.send(
            f"<b>ENTRY SKIPPED — wide spread</b> [{straddle_id}]\n"
            f"{put.symbol}\n"
            f"  bid: ${put_bid:,.2f}  ask: ${put_ask:,.2f}\n"
            f"  spread: {put_spread_pct:.1%} (cap: "
            f"{config.OPTION_MAX_ENTRY_SPREAD_PCT:.1%})\n"
            f"No spot bought, session closed."
        )
        return None

    log.info("put_spread_ok", id=straddle_id, symbol=put.symbol,
             bid=put_bid, ask=put_ask, spread_pct=f"{put_spread_pct:.1%}")

    # ── Step 2: Buy puts FIRST (hard leg) ──
    put_legs: list[StraddleLeg] = []
    for i in range(config.NUM_PUTS):
        # Refresh bid quote for each subsequent leg
        if i > 0:
            put_bid, _ = await market.get_option_bid_ask(put.symbol)
            if put_bid <= 0:
                put_bid = put_legs[-1].avg_fill_price  # fallback to last fill

        result = await exchange.chase_buy_put(put.symbol, total_put_qty, put_bid)
        if result is None or result.get("orderStatus") != "Filled":
            log.error("put_chase_failed_skipping_session",
                      id=straddle_id, leg=i + 1, symbol=put.symbol,
                      result=result)

            # Rollback: sell any partial puts that did fill across all legs
            await _rollback_puts_only(
                exchange, market, put.symbol, put_legs, straddle_id)

            await notifier.send(
                f"<b>ENTRY SKIPPED</b> [{straddle_id}]\n"
                f"Put chase deadline expired "
                f"({config.OPTION_CHASE_DEADLINE_SEC:.0f}s)\n"
                f"Leg {i + 1}/{config.NUM_PUTS} did not fill — "
                f"spot NOT purchased.\n"
                f"No directional exposure. Session closed for the day."
            )
            return None

        fill_price = float(result.get("avgPrice", put_bid))
        put_legs.append(StraddleLeg(
            instrument=put.symbol, side="Buy",
            qty=total_put_qty, entry_price=fill_price,
            order_id=result.get("orderId", ""), avg_fill_price=fill_price,
        ))
        log.info("put_filled", id=straddle_id, leg=i + 1, price=fill_price)

    # ── Step 4: Buy spot (easy leg, fills within seconds) ──
    try:
        spot_result = await exchange.buy_spot(total_spot_qty)
    except Exception as exc:
        log.error("spot_buy_failed_unwinding_puts",
                  id=straddle_id, error=str(exc), exc_info=True)
        await _rollback_puts_only(
            exchange, market, put.symbol, put_legs, straddle_id)
        await notifier.send(
            f"<b>ENTRY FAILED</b> [{straddle_id}]\n"
            f"Puts filled but spot buy errored — puts unwound\n"
            f"Error: {exc}"
        )
        return None

    if not spot_result or not spot_result.get("orderId"):
        log.error("spot_buy_chase_exhausted_unwinding_puts", id=straddle_id)
        await _rollback_puts_only(
            exchange, market, put.symbol, put_legs, straddle_id)
        await notifier.send(
            f"<b>ENTRY FAILED</b> [{straddle_id}]\n"
            f"Puts filled but spot chase exhausted — puts unwound"
        )
        return None

    spot_order_id = spot_result["orderId"]
    spot_fill = float(spot_result.get("avgPrice", 0)) or spot_price
    log.info("spot_filled", id=straddle_id, price=spot_fill, order_id=spot_order_id)

    spot_leg = StraddleLeg(
        instrument=config.SPOT_SYMBOL, side="Buy",
        qty=total_spot_qty, entry_price=spot_fill,
        order_id=spot_order_id, avg_fill_price=spot_fill,
    )

    # ── Step 5: Register straddle ──
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

    # ── Sell all put legs (maker chase) ──
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

                remaining_pos = await exchange.get_option_position(pl.instrument)
                if remaining_pos > 0:
                    log.warning("put_position_remaining_after_sell",
                                instrument=pl.instrument, remaining=remaining_pos)
                    _, ask2 = await market.get_option_bid_ask(pl.instrument)
                    if ask2 > 0:
                        retry = await exchange.chase_sell_put(
                            pl.instrument, remaining_pos, ask2)
                        if retry:
                            log.info("put_remaining_sold",
                                     instrument=pl.instrument, qty=remaining_pos)
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


async def _rollback_puts_only(
    exchange: BybitExchange,
    market: MarketData,
    put_symbol: str,
    filled_put_legs: list[StraddleLeg],
    straddle_id: str,
) -> None:
    """
    Roll back any partially-filled put legs after a failed entry.

    Used when entry fails before spot is bought — there is no spot
    exposure to unwind, only puts that may have been filled in earlier
    iterations. Logs the rollback P&L to trade_log.csv and sends a
    Telegram alert.
    """
    from core import notifier

    actual_put_qty = await exchange.get_option_position(put_symbol)
    if actual_put_qty <= 0:
        log.info("rollback_no_puts_held",
                 id=straddle_id, known_legs=len(filled_put_legs))
        return

    log.warning("rollback_selling_partial_puts",
                id=straddle_id, qty=actual_put_qty,
                known_legs=len(filled_put_legs))

    _, ask = await market.get_option_bid_ask(put_symbol)
    put_exit_price = 0.0
    if ask > 0:
        result = await exchange.chase_sell_put(put_symbol, actual_put_qty, ask)
        if result:
            put_exit_price = float(result.get("avgPrice", ask))
            log.info("rollback_puts_sold", id=straddle_id,
                     qty=actual_put_qty, price=put_exit_price)
        else:
            log.error("rollback_put_sell_failed", id=straddle_id,
                      qty=actual_put_qty)
    else:
        log.warning("rollback_put_no_ask", id=straddle_id, symbol=put_symbol)

    # Compute rollback P&L vs known leg fill prices
    avg_put_entry = (
        sum(p.avg_fill_price for p in filled_put_legs) / len(filled_put_legs)
        if filled_put_legs else 0.0
    )
    rollback_pnl = (put_exit_price - avg_put_entry) * actual_put_qty if actual_put_qty > 0 else 0.0

    _log_rollback_trade(
        portfolio_equity_pnl=rollback_pnl,
        put_entry_price=avg_put_entry,
        put_exit_price=put_exit_price,
        put_qty=actual_put_qty,
    )

    pnl_sign = "+" if rollback_pnl >= 0 else ""
    await notifier.send(
        f"<b>ROLLBACK COMPLETE</b> [{straddle_id}]\n"
        f"Sold partially-filled puts ({actual_put_qty} @ "
        f"${put_exit_price:,.2f})\n"
        f"Avg entry: ${avg_put_entry:,.2f}\n"
        f"Rollback P&L: {pnl_sign}${rollback_pnl:,.2f}"
    )


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
    Comprehensive emergency unwind — kept for the rare edge case where
    spot fails AFTER puts have filled. With puts-first ordering this
    should almost never trigger (BTC spot is the most liquid market on
    Bybit), but the logic is preserved for safety.

    1. Query Bybit for actual held put position
    2. Sell all held puts
    3. Sell spot (if any was bought)
    4. Log emergency P&L to trade_log.csv
    5. Send Telegram alert
    """
    from core import notifier

    log.warning("emergency_unwind_start", id=straddle_id,
                spot_qty=spot_qty, known_put_legs=len(filled_put_legs))

    entry_time = now_utc().isoformat()
    total_put_pnl = 0.0
    total_put_entry_cost = 0.0

    # ── 1. Query actual put position ──
    actual_put_qty = await exchange.get_option_position(put_symbol)
    log.info("emergency_actual_put_position", symbol=put_symbol, qty=actual_put_qty)

    # ── 2. Sell held puts ──
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

    # ── 3. Sell spot (only if some was bought) ──
    spot_exit_price = 0.0
    spot_pnl = 0.0
    if spot_qty > 0:
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
        f"Spot failed after puts filled (rare path)\n"
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


def _log_rollback_trade(
    portfolio_equity_pnl: float,
    put_entry_price: float,
    put_exit_price: float,
    put_qty: float,
) -> None:
    """Write a row to trade_log.csv for a puts-only rollback."""
    entry_time = now_utc().isoformat()

    row = {
        "date": entry_time[:10],
        "entry_time": entry_time,
        "exit_time": entry_time,
        "exit_reason": "rollback_puts_only",
        "num_straddles": 0,
        "spot_entry": 0,
        "spot_exit": 0,
        "put_strike": 0,
        "put_premium_entry": round(put_entry_price, 2),
        "put_premium_exit": round(put_exit_price, 2),
        "spot_margin_used": 0,
        "put_premium_cost": round(put_entry_price * put_qty, 2),
        "total_capital_used": round(put_entry_price * put_qty, 2),
        "straddle_cost": 0,
        "capital_before": 0,
        "spot_pnl": 0,
        "put_pnl": round(portfolio_equity_pnl, 2),
        "gross_pnl": round(portfolio_equity_pnl, 2),
        "fees": 0.0,
        "net_pnl": round(portfolio_equity_pnl, 2),
        "capital_after": 0,
    }

    os.makedirs(config.STATE_DIR, exist_ok=True)
    needs_header = not os.path.exists(config.TRADE_LOG_FILE)
    with open(config.TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)

    log.info("rollback_trade_logged",
             pnl=f"${portfolio_equity_pnl:,.2f}", qty=put_qty)


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
