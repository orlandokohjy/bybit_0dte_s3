"""
Exit management: hard close only (no take-profit).

All positions hold until the session close. With multi-session support,
the caller passes the firing Session so the close-notification can be
labelled with its time-window.
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

    async def hard_close(
        self, session_name: str = "", session_label: str = "",
    ) -> float:
        """Hard close at session end.

        Args:
            session_name:  Internal session name (used for logging).
            session_label: Human-friendly label (e.g. '13:30-15:30 UTC') used
                           in the Telegram CLOSE message header.
        """
        if not self._portfolio.has_open:
            log.info("nothing_to_close", session=session_name)
            return 0.0
        pnl = await unwind_straddle(
            self._exchange, self._market, self._portfolio,
            reason="hard_close",
        )
        await notifier.notify_close(
            pnl, "session_close", session_label=session_label,
        )
        return pnl
