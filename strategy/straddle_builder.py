"""
Atomic straddle construction and teardown.

One straddle = 0.5 BTC spot margin (long) + 2 × 0.5 BTC ITM put (long).

Entry : spot first (Post-Only limit for maker rebate) → puts (aggressive limit chase × NUM_PUTS)
Exit  : spot first (Post-Only limit for maker rebate) → puts (aggressive limit chase × NUM_PUTS)
"""
from __future__ import annotations

import uuid
from typing import Optional

import structlog

import config
from core.exchange import BybitExchange
from core.portfolio import Portfolio, Straddle, StraddleLeg
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

    # ── Step 1: Buy spot (Post-Only limit for maker rebate) ──
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

    # ── Step 2: Buy NUM_PUTS put legs (nearest ITM) ──
    put_ask = put.ask
    if put_ask <= 0:
        _, put_ask = await market.get_option_bid_ask(put.symbol)
    if put_ask <= 0:
        log.error("put_no_ask", id=straddle_id, symbol=put.symbol)
        await _emergency_sell_spot(exchange, total_spot_qty)
        return None

    put_legs: list[StraddleLeg] = []
    total_put_qty = config.QTY_PER_LEG * num_straddles

    for i in range(config.NUM_PUTS):
        result = await exchange.chase_buy_put(put.symbol, total_put_qty, put_ask)
        if result is None:
            log.error("put_buy_failed", id=straddle_id, leg=i + 1, symbol=put.symbol)
            # Unwind spot + any puts already bought
            await _emergency_sell_spot(exchange, total_spot_qty)
            for pl in put_legs:
                bid, _ = await market.get_option_bid_ask(put.symbol)
                if bid > 0:
                    await exchange.chase_sell_put(put.symbol, pl.qty, bid)
            return None

        fill_price = float(result.get("avgPrice", put_ask))
        put_legs.append(StraddleLeg(
            instrument=put.symbol, side="Buy",
            qty=total_put_qty, entry_price=fill_price,
            order_id=result.get("orderId", ""), avg_fill_price=fill_price,
        ))
        log.info("put_filled", id=straddle_id, leg=i + 1, price=fill_price)

    # ── Step 3: Subscribe to option ticker ──
    market.subscribe_option(put.symbol)

    # ── Step 4: Register ──
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

    # ── Sell spot (Post-Only limit for maker rebate) ──
    try:
        sell_result = await exchange.sell_spot(straddle.spot_leg.qty)
        if not sell_result or not sell_result.get("orderId"):
            log.error("spot_sell_chase_exhausted", id=straddle.id)
    except Exception as exc:
        log.error("spot_sell_failed", id=straddle.id, error=str(exc))

    exit_spot = await market.get_spot_price()

    # ── Sell all put legs ──
    exit_put_price = 0.0
    put_prices: list[float] = []
    for pl in straddle.put_legs:
        bid, _ = await market.get_option_bid_ask(pl.instrument)
        if bid > 0:
            result = await exchange.chase_sell_put(pl.instrument, pl.qty, bid)
            if result:
                price = float(result.get("avgPrice", bid))
                put_prices.append(price)
                log.info("put_sold", instrument=pl.instrument, price=price)
            else:
                log.warning("put_sell_failed", instrument=pl.instrument)
                put_prices.append(0.0)
        else:
            log.warning("put_no_bid", instrument=pl.instrument)
            put_prices.append(0.0)

    exit_put_price = sum(put_prices) / len(put_prices) if put_prices else 0.0

    pnl = portfolio.close_straddle(exit_spot, exit_put_price, reason)
    log.info("straddle_unwound", id=straddle.id, reason=reason,
             pnl=f"${pnl:,.2f}", exit_spot=exit_spot, exit_put=exit_put_price)
    return pnl


async def _emergency_sell_spot(exchange: BybitExchange, qty: float) -> None:
    try:
        await exchange.sell_spot(qty)
        log.info("emergency_spot_sold", qty=qty)
    except Exception:
        log.error("emergency_spot_sell_failed", qty=qty, exc_info=True)
