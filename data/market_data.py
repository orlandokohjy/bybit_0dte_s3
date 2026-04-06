"""Real-time market data feeds via WebSocket with REST fallback."""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from core.exchange import BybitExchange, TickerSnapshot
from data.option_chain import OptionChain

log = structlog.get_logger(__name__)


class MarketData:
    def __init__(self, exchange: BybitExchange, chain: OptionChain) -> None:
        self._exchange = exchange
        self._chain = chain
        self._subscribed_options: set[str] = set()

    async def start(self) -> None:
        self._exchange.start_spot_ws()
        await asyncio.sleep(2)
        log.info("market_data_started")

    def stop(self) -> None:
        self._exchange.close()

    async def get_spot_price(self) -> float:
        cached = self._exchange.get_cached_spot()
        if cached and cached.last > 0:
            return cached.last
        return await self._exchange.get_spot_price()

    async def get_spot_bid_ask(self) -> tuple[float, float]:
        cached = self._exchange.get_cached_spot()
        if cached and cached.bid > 0:
            return cached.bid, cached.ask
        price = await self._exchange.get_spot_price()
        return price, price

    def get_option_snapshot(self, symbol: str) -> Optional[TickerSnapshot]:
        return self._exchange.get_cached_option(symbol)

    async def get_option_mark(self, symbol: str) -> float:
        snap = self._exchange.get_cached_option(symbol)
        if snap and snap.mark > 0:
            return snap.mark
        if snap and snap.last > 0:
            return snap.last
        tickers = await self._exchange.get_option_tickers_rest(
            exp_date=self._chain.expiry_date,
        )
        for t in tickers:
            if t.get("symbol") == symbol:
                return float(t.get("markPrice", 0))
        return 0.0

    async def get_option_bid_ask(self, symbol: str) -> tuple[float, float]:
        snap = self._exchange.get_cached_option(symbol)
        if snap and snap.bid > 0:
            return snap.bid, snap.ask
        tickers = await self._exchange.get_option_tickers_rest(
            exp_date=self._chain.expiry_date,
        )
        for t in tickers:
            if t.get("symbol") == symbol:
                return float(t.get("bid1Price", 0)), float(t.get("ask1Price", 0))
        return 0.0, 0.0

    def subscribe_option(self, symbol: str) -> None:
        if symbol not in self._subscribed_options:
            self._exchange.subscribe_option_ticker(symbol)
            self._subscribed_options.add(symbol)
