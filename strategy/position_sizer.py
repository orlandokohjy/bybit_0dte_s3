"""
Compound position sizing with pre-flight capital verification.

straddle_cost = (QTY_PER_LEG × spot / LEVERAGE) + (NUM_PUTS × QTY_PER_LEG × put_premium)
num_straddles = floor(ALLOC_PCT × equity / straddle_cost)

No cap — trade as many as equity allows.
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
    spot_margin_per: float       # margin required for spot leg (per straddle)
    put_cost_per: float          # option premium cost (per straddle)
    straddle_cost: float         # total per straddle
    total_spot_margin: float     # all straddles combined
    total_put_cost: float        # all straddles combined
    total_capital_required: float  # including slippage buffer
    equity: float
    available_capital: float     # ALLOC_PCT × equity


def compute_straddle_cost(spot: float, put_premium: float) -> float:
    spot_margin = config.QTY_PER_LEG * spot / config.SPOT_LEVERAGE
    put_cost = config.NUM_PUTS * config.QTY_PER_LEG * put_premium
    return spot_margin + put_cost


def size_position(equity: float, spot: float, put_premium: float) -> SizingResult:
    """
    Compute full sizing with capital breakdown.

    Returns a SizingResult with per-straddle and total capital requirements,
    including a slippage buffer on the option leg.
    """
    spot_margin_per = config.QTY_PER_LEG * spot / config.SPOT_LEVERAGE
    put_cost_per = config.NUM_PUTS * config.QTY_PER_LEG * put_premium
    straddle_cost = spot_margin_per + put_cost_per

    if straddle_cost <= 0:
        return SizingResult(
            num_straddles=0, spot_margin_per=0, put_cost_per=0,
            straddle_cost=0, total_spot_margin=0, total_put_cost=0,
            total_capital_required=0, equity=equity,
            available_capital=config.ALLOC_PCT * equity,
        )

    available = config.ALLOC_PCT * equity
    # Size using straddle_cost with slippage buffer on the put leg
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
        spot_margin_per=f"${spot_margin_per:,.2f}",
        put_cost_per=f"${put_cost_per:,.2f}",
        straddle_cost=f"${straddle_cost:,.2f}",
        total_required=f"${total_required:,.2f}",
        buffer=f"{SLIPPAGE_BUFFER:.0%}",
    )
    return result
