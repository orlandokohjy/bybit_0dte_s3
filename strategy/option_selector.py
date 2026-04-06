"""Select the best ITM put for the straddle."""
from __future__ import annotations

from typing import Optional

import structlog

import config
from data.option_chain import OptionChain, OptionInfo

log = structlog.get_logger(__name__)


def select_put(chain: OptionChain, spot: float) -> Optional[OptionInfo]:
    """
    Pick the nearest ITM put (lowest strike > spot) that passes spread/liquidity filters.
    """
    put = chain.get_nearest_itm_put(spot)
    if put is None:
        log.warning("no_itm_puts", spot=spot)
        return None

    if put.spread_pct > config.MAX_BID_ASK_SPREAD_PCT:
        log.warning("put_spread_too_wide", strike=put.strike,
                    spread_pct=f"{put.spread_pct:.1%}")
        return None

    if put.ask <= 0:
        log.warning("put_no_ask", strike=put.strike)
        return None

    log.info("put_selected", strike=put.strike, bid=put.bid,
             ask=put.ask, mid=put.mid, iv=put.iv)
    return put
