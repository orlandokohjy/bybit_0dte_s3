"""
Bybit V5 API wrapper — REST + WebSocket.

Handles spot margin orders (with leverage), option orders (aggressive limit chase),
and market data.
"""
from __future__ import annotations

import asyncio
import math
import threading
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import structlog
from pybit.unified_trading import HTTP, WebSocket

import config

log = structlog.get_logger(__name__)


def _round_price_up(price: float) -> float:
    """Round option price UP to nearest tick (for buys)."""
    return math.ceil(price / config.OPTION_TICK_SIZE) * config.OPTION_TICK_SIZE


def _round_price_down(price: float) -> float:
    """Round option price DOWN to nearest tick (for sells)."""
    return max(config.OPTION_TICK_SIZE,
               math.floor(price / config.OPTION_TICK_SIZE) * config.OPTION_TICK_SIZE)


@dataclass
class TickerSnapshot:
    symbol: str
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    mark: float = 0.0
    ts: float = field(default_factory=_time.time)


class BybitExchange:
    """Unified interface to Bybit REST + WebSocket APIs."""

    MAX_RETRIES = 3
    RETRY_DELAY = 1.0

    def __init__(self) -> None:
        self._http = HTTP(
            testnet=config.TESTNET,
            demo=config.DEMO,
            api_key=config.BYBIT_API_KEY,
            api_secret=config.BYBIT_API_SECRET,
        )

        self._spot_ticker: Optional[TickerSnapshot] = None
        self._option_tickers: dict[str, TickerSnapshot] = {}
        self._ws_spot: Optional[WebSocket] = None
        self._ws_option: Optional[WebSocket] = None
        self._ws_private: Optional[WebSocket] = None
        self._ws_lock = threading.Lock()
        self._error_count: int = 0

    @property
    def error_count(self) -> int:
        return self._error_count

    # ──────────────────── Generic REST Caller ─────────────────────

    async def _call(self, method: Callable, **kwargs: Any) -> dict:
        last_exc = None
        for attempt in range(self.MAX_RETRIES):
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: method(**kwargs)
                )
                if result.get("retCode", -1) != 0:
                    raise RuntimeError(
                        f"API error {result.get('retCode')}: {result.get('retMsg')}"
                    )
                self._error_count = 0
                return result
            except Exception as exc:
                self._error_count += 1
                last_exc = exc
                log.warning("api_retry", attempt=attempt + 1, error=str(exc))
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_DELAY * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    # ──────────────────── Spot Margin Leverage ──────────────────────

    async def set_spot_margin_leverage(self) -> None:
        """
        Set spot cross-margin leverage via /v5/spot-margin-trade/set-leverage.

        This is a DIFFERENT endpoint from the perp set_leverage.
        Range: 2–10x. Requires spot margin to be activated on the account.
        """
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._http._submit_request(
                    method="POST",
                    path=f"{self._http.endpoint}/v5/spot-margin-trade/set-leverage",
                    query={"leverage": str(config.SPOT_LEVERAGE)},
                    auth=True,
                ),
            )
            log.info("spot_margin_leverage_set", leverage=config.SPOT_LEVERAGE)
        except Exception as exc:
            log.warning("spot_margin_leverage_set_failed", error=str(exc),
                        note="Ensure spot margin is activated on your Bybit account")

    # ──────────────────── Market Data (REST) ─────────────────────

    async def get_spot_price(self) -> float:
        if self._spot_ticker:
            return self._spot_ticker.last
        data = await self._call(
            self._http.get_tickers,
            category=config.SPOT_CATEGORY,
            symbol=config.SPOT_SYMBOL,
        )
        return float(data["result"]["list"][0]["lastPrice"])

    async def get_option_tickers_rest(self, exp_date: str) -> list[dict]:
        data = await self._call(
            self._http.get_tickers,
            category="option",
            baseCoin=config.BASE_COIN,
            expDate=exp_date,
        )
        return data["result"]["list"]

    async def get_total_equity_usd(self) -> float:
        data = await self._call(
            self._http.get_wallet_balance,
            accountType=config.ACCOUNT_TYPE,
        )
        for acct in data["result"]["list"]:
            equity = acct.get("totalEquity")
            if equity:
                return float(equity)
        return 0.0

    # ──────────────────── Order Helpers ───────────────────────────

    def _fake_order(self, side: str, symbol: str, qty: float, price: float) -> dict:
        oid = f"dry-{uuid.uuid4().hex[:12]}"
        log.info("dry_run_order", side=side, symbol=symbol, qty=qty, price=price, oid=oid)
        return {"orderId": oid, "orderStatus": "Filled", "avgPrice": str(price)}

    # ──────────────────── Spot Margin Orders (Post-Only for maker rebate) ──

    def _round_spot_price(self, price: float, direction: str = "down") -> float:
        """Round spot price to tick size. 'down' for buys, 'up' for sells."""
        tick = config.SPOT_TICK_SIZE
        if direction == "down":
            return math.floor(price / tick) * tick
        return math.ceil(price / tick) * tick

    async def _place_spot_limit(self, side: str, qty: float, price: float) -> dict:
        """Place a single Post-Only limit order on spot (maker only)."""
        data = await self._call(
            self._http.place_order,
            category=config.SPOT_CATEGORY,
            symbol=config.SPOT_SYMBOL,
            side=side,
            orderType="Limit",
            qty=str(qty),
            price=str(price),
            timeInForce="PostOnly",
            marketUnit="baseCoin",
            isLeverage=1,
        )
        return data["result"]

    async def _get_spot_order_result(self, order_id: str) -> dict | None:
        """Check if a spot order has been filled."""
        try:
            data = await self._call(
                self._http.get_order_history,
                category=config.SPOT_CATEGORY,
                symbol=config.SPOT_SYMBOL,
                orderId=order_id,
            )
            orders = data["result"]["list"]
            return orders[0] if orders else None
        except Exception:
            return None

    async def buy_spot(self, qty: float) -> dict:
        """
        Post-Only limit buy BTC spot with margin — chase at bid for maker rebate.

        Posts at the current bid. If not filled, re-posts at updated bid.
        Guarantees maker status (PostOnly rejects if it would cross the spread).
        """
        log.info("buy_spot_maker", qty=qty)
        if config.DRY_RUN:
            price = await self.get_spot_price()
            return self._fake_order("Buy", config.SPOT_SYMBOL, qty, price)

        for attempt in range(config.SPOT_CHASE_MAX_ATTEMPTS):
            cached = self.get_cached_spot()
            if cached and cached.bid > 0:
                price = self._round_spot_price(cached.bid, "down")
            else:
                price = self._round_spot_price(await self.get_spot_price(), "down")

            try:
                result = await self._place_spot_limit("Buy", qty, price)
            except RuntimeError as exc:
                if "170213" in str(exc) or "PostOnly" in str(exc):
                    log.debug("spot_buy_postonly_rejected", price=price, attempt=attempt + 1)
                    await asyncio.sleep(config.SPOT_CHASE_INTERVAL_SEC)
                    continue
                raise

            order_id = result.get("orderId", "")
            if not order_id:
                break

            await asyncio.sleep(config.SPOT_CHASE_INTERVAL_SEC)

            order_result = await self._get_spot_order_result(order_id)
            if order_result and order_result.get("orderStatus") == "Filled":
                fill_price = float(order_result.get("avgPrice", price))
                log.info("spot_buy_filled", price=fill_price, attempt=attempt + 1)
                return {"orderId": order_id, "orderStatus": "Filled", "avgPrice": str(fill_price)}

            # Not filled — cancel and retry at new bid
            await self.cancel_order(config.SPOT_CATEGORY, config.SPOT_SYMBOL, order_id)
            log.debug("spot_buy_chase", attempt=attempt + 1, price=price)

        log.warning("spot_buy_chase_exhausted", qty=qty)
        return {}

    async def sell_spot(self, qty: float) -> dict:
        """
        Post-Only limit sell BTC spot — chase at ask for maker rebate.

        Posts at the current ask. If not filled, re-posts at updated ask.
        """
        log.info("sell_spot_maker", qty=qty)
        if config.DRY_RUN:
            price = await self.get_spot_price()
            return self._fake_order("Sell", config.SPOT_SYMBOL, qty, price)

        for attempt in range(config.SPOT_CHASE_MAX_ATTEMPTS):
            cached = self.get_cached_spot()
            if cached and cached.ask > 0:
                price = self._round_spot_price(cached.ask, "up")
            else:
                price = self._round_spot_price(await self.get_spot_price(), "up")

            try:
                result = await self._place_spot_limit("Sell", qty, price)
            except RuntimeError as exc:
                if "170213" in str(exc) or "PostOnly" in str(exc):
                    log.debug("spot_sell_postonly_rejected", price=price, attempt=attempt + 1)
                    await asyncio.sleep(config.SPOT_CHASE_INTERVAL_SEC)
                    continue
                raise

            order_id = result.get("orderId", "")
            if not order_id:
                break

            await asyncio.sleep(config.SPOT_CHASE_INTERVAL_SEC)

            order_result = await self._get_spot_order_result(order_id)
            if order_result and order_result.get("orderStatus") == "Filled":
                fill_price = float(order_result.get("avgPrice", price))
                log.info("spot_sell_filled", price=fill_price, attempt=attempt + 1)
                return {"orderId": order_id, "orderStatus": "Filled", "avgPrice": str(fill_price)}

            await self.cancel_order(config.SPOT_CATEGORY, config.SPOT_SYMBOL, order_id)
            log.debug("spot_sell_chase", attempt=attempt + 1, price=price)

        log.warning("spot_sell_chase_exhausted", qty=qty)
        return {}

    # ──────────────────── Option Orders ──────────────────────────

    async def buy_put(self, symbol: str, qty: float, price: float) -> dict:
        tick_price = _round_price_up(price)
        log.info("buy_put", symbol=symbol, qty=qty, price=tick_price)
        if config.DRY_RUN:
            return self._fake_order("Buy", symbol, qty, tick_price)
        data = await self._call(
            self._http.place_order,
            category="option",
            symbol=symbol,
            side="Buy",
            orderType="Limit",
            qty=str(qty),
            price=str(tick_price),
            timeInForce="IOC",
            orderLinkId=f"bp-{uuid.uuid4().hex[:16]}",
        )
        return data["result"]

    async def sell_put(self, symbol: str, qty: float, price: float) -> dict:
        tick_price = _round_price_down(price)
        log.info("sell_put", symbol=symbol, qty=qty, price=tick_price)
        if config.DRY_RUN:
            return self._fake_order("Sell", symbol, qty, tick_price)
        data = await self._call(
            self._http.place_order,
            category="option",
            symbol=symbol,
            side="Sell",
            orderType="Limit",
            qty=str(qty),
            price=str(tick_price),
            timeInForce="IOC",
            reduceOnly=True,
            orderLinkId=f"sp-{uuid.uuid4().hex[:16]}",
        )
        return data["result"]

    async def get_order_status(self, category: str, symbol: str, order_id: str) -> dict | None:
        try:
            data = await self._call(
                self._http.get_open_orders,
                category=category,
                symbol=symbol,
                orderId=order_id,
            )
            orders = data["result"]["list"]
            return orders[0] if orders else None
        except Exception:
            return None

    async def cancel_order(self, category: str, symbol: str, order_id: str) -> None:
        try:
            await self._call(
                self._http.cancel_order,
                category=category,
                symbol=symbol,
                orderId=order_id,
            )
        except Exception:
            log.debug("cancel_order_failed", symbol=symbol, order_id=order_id, exc_info=True)

    async def chase_buy_put(
        self, symbol: str, qty: float, initial_ask: float,
    ) -> dict | None:
        """Aggressive fill logic: walk the ask up until filled or exhausted."""
        if config.DRY_RUN:
            return self._fake_order("Buy", symbol, qty, _round_price_up(initial_ask))

        max_price = _round_price_up(initial_ask * (1 + config.OPTION_MAX_SLIPPAGE_PCT))
        price = _round_price_up(initial_ask * (1 + config.OPTION_LIMIT_AGGRESSION))

        for attempt in range(config.OPTION_CHASE_MAX_ATTEMPTS):
            price = min(price, max_price)
            result = await self.buy_put(symbol, qty, price)
            order_id = result.get("orderId", "")
            if not order_id:
                break

            await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
            order_status = await self.get_order_status("option", symbol, order_id)

            if order_status is None or order_status.get("orderStatus") == "Filled":
                log.info("chase_filled", symbol=symbol, price=price, attempt=attempt + 1)
                return result

            if order_status.get("orderStatus") in ("New", "PartiallyFilled"):
                await self.cancel_order("option", symbol, order_id)

            price = _round_price_up(price * (1 + config.OPTION_LIMIT_AGGRESSION))
            log.debug("chase_reprice", symbol=symbol, new_price=price, attempt=attempt + 1)
            await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)

        log.warning("chase_exhausted", symbol=symbol, final_price=price)
        return None

    async def chase_sell_put(
        self, symbol: str, qty: float, initial_bid: float,
    ) -> dict | None:
        """Aggressive fill logic for selling a put: walk the bid down."""
        if config.DRY_RUN:
            return self._fake_order("Sell", symbol, qty, _round_price_down(initial_bid))

        min_price = _round_price_down(initial_bid * (1 - config.OPTION_MAX_SLIPPAGE_PCT))
        price = _round_price_down(initial_bid * (1 - config.OPTION_LIMIT_AGGRESSION))

        for attempt in range(config.OPTION_CHASE_MAX_ATTEMPTS):
            price = max(price, min_price)
            result = await self.sell_put(symbol, qty, price)
            order_id = result.get("orderId", "")
            if not order_id:
                break

            await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
            order_status = await self.get_order_status("option", symbol, order_id)

            if order_status is None or order_status.get("orderStatus") == "Filled":
                log.info("chase_sell_filled", symbol=symbol, price=price, attempt=attempt + 1)
                return result

            if order_status.get("orderStatus") in ("New", "PartiallyFilled"):
                await self.cancel_order("option", symbol, order_id)

            price = _round_price_down(price * (1 - config.OPTION_LIMIT_AGGRESSION))
            await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)

        log.warning("chase_sell_exhausted", symbol=symbol, final_price=price)
        return None

    # ─────────────────── WebSocket Streams ───────────────────────

    def start_spot_ws(self) -> None:
        self._ws_spot = WebSocket(testnet=config.TESTNET, channel_type="spot")
        self._ws_spot.ticker_stream(
            symbol=config.SPOT_SYMBOL, callback=self._handle_spot_ticker,
        )
        log.info("ws_spot_started")

    def _handle_spot_ticker(self, msg: dict) -> None:
        try:
            d = msg.get("data", msg)
            self._spot_ticker = TickerSnapshot(
                symbol=config.SPOT_SYMBOL,
                bid=float(d.get("bid1Price", 0)),
                ask=float(d.get("ask1Price", 0)),
                last=float(d.get("lastPrice", 0)),
                mark=float(d.get("lastPrice", 0)),
            )
        except Exception:
            log.debug("spot_ticker_parse_error", exc_info=True)

    def subscribe_option_ticker(self, symbol: str) -> None:
        with self._ws_lock:
            if self._ws_option is None:
                self._ws_option = WebSocket(testnet=config.TESTNET, channel_type="option")
            self._ws_option.ticker_stream(symbol=symbol, callback=self._handle_option_ticker)
            log.debug("option_ticker_subscribed", symbol=symbol)

    def _handle_option_ticker(self, msg: dict) -> None:
        try:
            d = msg.get("data", msg)
            symbol = d.get("symbol", "")
            self._option_tickers[symbol] = TickerSnapshot(
                symbol=symbol,
                bid=float(d.get("bid1Price", 0)),
                ask=float(d.get("ask1Price", 0)),
                last=float(d.get("lastPrice", 0)),
                mark=float(d.get("markPrice", 0)),
            )
        except Exception:
            log.debug("option_ticker_parse_error", exc_info=True)

    def start_private_ws(self) -> None:
        try:
            self._ws_private = WebSocket(
                testnet=config.TESTNET,
                channel_type="private",
                api_key=config.BYBIT_API_KEY,
                api_secret=config.BYBIT_API_SECRET,
                demo=config.DEMO,
            )
            log.info("ws_private_started")
        except Exception:
            log.warning("ws_private_start_failed", exc_info=True)

    def get_cached_spot(self) -> TickerSnapshot | None:
        return self._spot_ticker

    def get_cached_option(self, symbol: str) -> TickerSnapshot | None:
        return self._option_tickers.get(symbol)

    # ────────────────────── Shutdown ─────────────────────────────

    def close(self) -> None:
        for ws in (self._ws_spot, self._ws_option, self._ws_private):
            if ws is not None:
                try:
                    ws.exit()
                except Exception:
                    pass
        log.info("exchange_closed")
