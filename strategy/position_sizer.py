"""
Compound position sizing with pre-flight capital verification.

straddle_cost = (qty_per_leg × spot / LEVERAGE) + (NUM_PUTS × qty_per_leg × put_premium)
num_straddles = floor(ALLOC_PCT × equity / straddle_cost)

`qty_per_leg` is supplied per-call by the firing Session (see config.SESSIONS).
A NUM_STRADDLES_OVERRIDE clamp is applied later in main._run_entry, so this
sizer is only authoritative when override = 0.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import structlog

import config

log = structlog.get_logger(__name__)

SLIPPAGE_BUFFER: float = 0.05  # 5 % buffer for option fill slippage


@dataclass
class SizingResult:
    num_straddles: int
    spot_margin_per: float
    put_cost_per: float
    straddle_cost: float
    total_spot_margin: float
    total_put_cost: float
    total_capital_required: float
    equity: float
    available_capital: float


def compute_straddle_cost(
    spot: float, put_premium: float, qty_per_leg: float | None = None,
) -> float:
    qty = qty_per_leg if qty_per_leg is not None else config.QTY_PER_LEG
    spot_margin = qty * spot / config.SPOT_LEVERAGE
    put_cost = config.NUM_PUTS * qty * put_premium
    return spot_margin + put_cost


def size_position(
    equity: float, spot: float, put_premium: float,
    qty_per_leg: float | None = None,
) -> SizingResult:
    """
    Compute full sizing with capital breakdown.

    Args:
        equity:        USD equity available to the algo
        spot:          BTC spot in USD
        put_premium:   Per-BTC put ask price (USD)
        qty_per_leg:   BTC notional for the spot leg of one straddle.
                       Defaults to config.QTY_PER_LEG when None.
    """
    qty = qty_per_leg if qty_per_leg is not None else config.QTY_PER_LEG

    spot_margin_per = qty * spot / config.SPOT_LEVERAGE
    put_cost_per = config.NUM_PUTS * qty * put_premium
    straddle_cost = spot_margin_per + put_cost_per

    if straddle_cost <= 0:
        return SizingResult(
            num_straddles=0, spot_margin_per=0, put_cost_per=0,
            straddle_cost=0, total_spot_margin=0, total_put_cost=0,
            total_capital_required=0, equity=equity,
            available_capital=config.ALLOC_PCT * equity,
        )

    available = config.ALLOC_PCT * equity
    buffered_cost = spot_margin_per + put_cost_per * (1 + SLIPPAGE_BUFFER)
    n = math.floor(available / buffered_cost)
    n = max(0, n)

    total_spot = spot_margin_per * n
    total_put = put_cost_per * n
    total_required = total_spot + total_put * (1 + SLIPPAGE_BUFFER)

    result = SizingResult(
        num_straddles=n,
        spot_margin_per=spot_margin_per,
        put_cost_per=put_cost_per,
        straddle_cost=straddle_cost,
        total_spot_margin=total_spot,
        total_put_cost=total_put,
        total_capital_required=total_required,
        equity=equity,
        available_capital=available,
    )

    log.info(
        "position_sized",
        equity=f"${equity:,.0f}",
        available=f"${available:,.0f}",
        num_straddles=n,
        qty_per_leg=qty,
        spot_margin_per=f"${spot_margin_per:,.2f}",
        put_cost_per=f"${put_cost_per:,.2f}",
        straddle_cost=f"${straddle_cost:,.2f}",
        total_required=f"${total_required:,.2f}",
        buffer=f"{SLIPPAGE_BUFFER:.0%}",
    )
    return result
