"""
Exit management: hard close only (no take-profit).

All positions hold until 18:00 UTC session close.
"""
from __future__ import annotations

import structlog

from core import notifier
from core.exchange import BybitExchange
from core.portfolio import Portfolio
from data.market_data import MarketData
from strategy.straddle_builder import unwind_straddle

log = structlog.get_logger(__name__)


class ExitManager:
    def __init__(
        self,
        exchange: BybitExchange,
        market: MarketData,
        portfolio: Portfolio,
    ) -> None:
        self._exchange = exchange
        self._market = market
        self._portfolio = portfolio

    async def hard_close(self) -> float:
        """Hard close at session end (18:00 UTC)."""
        if not self._portfolio.has_open:
            log.info("nothing_to_close")
            return 0.0
        pnl = await unwind_straddle(
            self._exchange, self._market, self._portfolio,
            reason="hard_close",
        )
        await notifier.notify_close(pnl, "session_close")
        return pnl
