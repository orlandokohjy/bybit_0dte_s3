"""Select the best ITM put for the straddle."""
from __future__ import annotations

from typing import Optional

import structlog

import config
from data.option_chain import OptionChain, OptionInfo

log = structlog.get_logger(__name__)

MAX_ITM_SCAN = 6


def select_put(chain: OptionChain, spot: float) -> Optional[OptionInfo]:
    """
    Pick the best ITM put (strike > spot) that passes spread/liquidity filters.

    Starts at the nearest ITM strike and walks deeper until a put with
    an acceptable spread is found, up to MAX_ITM_SCAN strikes.
    """
    candidates = sorted(
        [p for p in chain.all_puts if p.strike > spot],
        key=lambda p: p.strike,
    )
    if not candidates:
        log.warning("no_itm_puts", spot=spot)
        return None

    for put in candidates[:MAX_ITM_SCAN]:
        if put.ask <= 0:
            log.debug("put_no_ask", strike=put.strike)
            continue
        if put.spread_pct > config.MAX_BID_ASK_SPREAD_PCT:
            log.debug("put_spread_wide", strike=put.strike,
                      spread_pct=f"{put.spread_pct:.1%}")
            continue

        log.info("put_selected", strike=put.strike, bid=put.bid,
                 ask=put.ask, mid=put.mid, spread_pct=f"{put.spread_pct:.1%}", iv=put.iv)
        return put

    log.warning("no_put_passed_filters", scanned=min(len(candidates), MAX_ITM_SCAN),
                spot=spot, max_spread=f"{config.MAX_BID_ASK_SPREAD_PCT:.0%}")
    return None
