"""
Bybit V5 API wrapper — REST + WebSocket.

Handles spot margin orders (with leverage), option orders (GTC limit for maker rebate),
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
    NON_RETRYABLE_CODES = {"170131", "170210", "110001"}

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
                err_str = str(exc)
                if any(f"ErrCode: {code}" in err_str for code in self.NON_RETRYABLE_CODES):
                    log.error("api_non_retryable", error=err_str)
                    raise
                log.warning("api_retry", attempt=attempt + 1, error=str(exc))
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_DELAY * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    # ──────────────────── Account Margin Mode ───────────────────────

    async def set_portfolio_margin(self) -> None:
        """
        Switch UTA to Portfolio Margin mode via /v5/account/set-margin-mode.

        Portfolio Margin uses stress testing to evaluate the overall portfolio
        risk, giving lower maintenance margin for hedged positions (our long
        spot + long puts).  Requires net equity >= 1,000 USDC equivalent.
        """
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._http._submit_request(
                    method="POST",
                    path=f"{self._http.endpoint}/v5/account/set-margin-mode",
                    query={"setMarginMode": "PORTFOLIO_MARGIN"},
                    auth=True,
                ),
            )
            ret_code = result.get("retCode", -1)
            if ret_code == 0:
                log.info("margin_mode_set", mode="PORTFOLIO_MARGIN")
            else:
                reasons = result.get("result", {}).get("reasons", [])
                log.warning("margin_mode_set_skipped", retCode=ret_code,
                            retMsg=result.get("retMsg"), reasons=reasons,
                            note="May already be in PORTFOLIO_MARGIN mode")
        except Exception as exc:
            log.warning("margin_mode_set_failed", error=str(exc))

    async def set_spot_hedging(self) -> None:
        """
        Enable Spot Hedging in Portfolio Margin via /v5/account/set-hedging-mode.

        When ON, spot holdings are included in stress-testing scenarios and
        offset derivatives risk — reducing maintenance margin for our
        long-spot + long-put portfolio.
        """
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._http._submit_request(
                    method="POST",
                    path=f"{self._http.endpoint}/v5/account/set-hedging-mode",
                    query={"setHedgingMode": "ON"},
                    auth=True,
                ),
            )
            ret_code = result.get("retCode", -1)
            if ret_code == 0:
                log.info("spot_hedging_enabled")
            else:
                log.warning("spot_hedging_skipped", retCode=ret_code,
                            retMsg=result.get("retMsg"),
                            note="May already be enabled")
        except Exception as exc:
            log.warning("spot_hedging_failed", error=str(exc))

    async def get_margin_mode(self) -> str:
        """Return current account margin mode (REGULAR_MARGIN, PORTFOLIO_MARGIN, etc.)."""
        try:
            data = await self._call(
                self._http.get_account_info,
            )
            return data["result"].get("marginMode", "UNKNOWN")
        except Exception:
            return "UNKNOWN"

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

    # ──────────────────── Spot Margin Orders (GTC Limit for maker rebate) ──

    def _round_spot_price(self, price: float, direction: str = "down") -> float:
        """Round spot price to tick size. 'down' for buys, 'up' for sells."""
        tick = config.SPOT_TICK_SIZE
        if direction == "down":
            return round(math.floor(price / tick) * tick, 2)
        return round(math.ceil(price / tick) * tick, 2)

    async def _place_spot_limit(self, side: str, qty: float, price: float) -> dict:
        """Place a GTC Limit order on spot margin. Maker when priced at bid/ask."""
        params = dict(
            category=config.SPOT_CATEGORY,
            symbol=config.SPOT_SYMBOL,
            side=side,
            orderType="Limit",
            qty=str(qty),
            price=str(price),
            timeInForce="GTC",
            marketUnit="baseCoin",
        )
        params["isLeverage"] = 1
        data = await self._call(self._http.place_order, **params)
        return data["result"]

    async def _wait_spot_fill(self, order_id: str, price: float, timeout: float = 3.0) -> dict:
        """
        Wait for a spot order to fill.  Checks open orders then order history.
        Returns fill result or empty dict if timed out.
        """
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                data = await self._call(
                    self._http.get_open_orders,
                    category=config.SPOT_CATEGORY,
                    symbol=config.SPOT_SYMBOL,
                    orderId=order_id,
                )
                open_list = data["result"]["list"]
                if open_list:
                    status = open_list[0].get("orderStatus")
                    if status == "Filled":
                        return open_list[0]
                    if status in ("New", "PartiallyFilled"):
                        await asyncio.sleep(0.5)
                        continue
                    return {}
            except Exception:
                pass

            try:
                data = await self._call(
                    self._http.get_order_history,
                    category=config.SPOT_CATEGORY,
                    symbol=config.SPOT_SYMBOL,
                    orderId=order_id,
                )
                hist = data["result"]["list"]
                if hist:
                    return hist[0]
            except Exception:
                pass

            await asyncio.sleep(0.5)

        return {}

    async def _get_order_final_state(self, category: str, symbol: str, order_id: str) -> dict:
        """Query order history for the final state after a cancel attempt."""
        await asyncio.sleep(0.3)
        try:
            data = await self._call(
                self._http.get_order_history,
                category=category, symbol=symbol, orderId=order_id,
            )
            hist = data["result"]["list"]
            if hist:
                return hist[0]
        except Exception:
            pass
        return {}

    async def buy_spot(self, qty: float) -> dict:
        """
        Limit buy BTC spot with margin — post at bid for maker rebate.

        Uses GTC limit at bid price. If not filled within the chase interval,
        cancels and re-posts at the updated bid.  After every cancel, the
        order's final state is verified to prevent duplicate orders when a
        fill arrives between the timeout and the cancel.
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

            result = await self._place_spot_limit("Buy", qty, price)
            order_id = result.get("orderId", "")
            if not order_id:
                break

            fill = await self._wait_spot_fill(order_id, price, timeout=config.SPOT_CHASE_INTERVAL_SEC)

            if fill and fill.get("orderStatus") == "Filled":
                fill_price = float(fill.get("avgPrice", price))
                log.info("spot_buy_filled", price=fill_price, attempt=attempt + 1)
                return {"orderId": order_id, "orderStatus": "Filled", "avgPrice": str(fill_price)}

            await self.cancel_order(config.SPOT_CATEGORY, config.SPOT_SYMBOL, order_id)

            final = await self._get_order_final_state(
                config.SPOT_CATEGORY, config.SPOT_SYMBOL, order_id,
            )
            final_status = final.get("orderStatus", "")
            cum_qty = float(final.get("cumExecQty", 0))
            if cum_qty > 0:
                fill_price = float(final.get("avgPrice", price))
                log.info("spot_buy_filled_post_cancel", price=fill_price,
                         qty_filled=cum_qty, status=final_status, attempt=attempt + 1)
                return {"orderId": order_id, "orderStatus": "Filled",
                        "avgPrice": str(fill_price), "cumExecQty": str(cum_qty)}

            log.debug("spot_buy_chase", attempt=attempt + 1, price=price)

        log.warning("spot_buy_chase_exhausted", qty=qty)
        return {}

    async def sell_spot(self, qty: float) -> dict:
        """
        Limit sell BTC spot — post at ask for maker rebate.

        Uses GTC limit at ask price. If not filled within the chase interval,
        cancels and re-posts at the updated ask.  Post-cancel verification
        prevents duplicate sells when a fill races with the timeout.
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

            result = await self._place_spot_limit("Sell", qty, price)
            order_id = result.get("orderId", "")
            if not order_id:
                break

            fill = await self._wait_spot_fill(order_id, price, timeout=config.SPOT_CHASE_INTERVAL_SEC)

            if fill and fill.get("orderStatus") == "Filled":
                fill_price = float(fill.get("avgPrice", price))
                log.info("spot_sell_filled", price=fill_price, attempt=attempt + 1)
                return {"orderId": order_id, "orderStatus": "Filled", "avgPrice": str(fill_price)}

            await self.cancel_order(config.SPOT_CATEGORY, config.SPOT_SYMBOL, order_id)

            final = await self._get_order_final_state(
                config.SPOT_CATEGORY, config.SPOT_SYMBOL, order_id,
            )
            final_status = final.get("orderStatus", "")
            cum_qty = float(final.get("cumExecQty", 0))
            if cum_qty > 0:
                fill_price = float(final.get("avgPrice", price))
                log.info("spot_sell_filled_post_cancel", price=fill_price,
                         qty_filled=cum_qty, status=final_status, attempt=attempt + 1)
                return {"orderId": order_id, "orderStatus": "Filled",
                        "avgPrice": str(fill_price), "cumExecQty": str(cum_qty)}

            log.debug("spot_sell_chase", attempt=attempt + 1, price=price)

        log.warning("spot_sell_chase_exhausted", qty=qty)
        return {}

    # ──────────────────── Option Orders (GTC Limit for maker) ──────

    async def _place_option_limit(self, side: str, symbol: str, qty: float, price: float, reduce: bool = False) -> dict:
        """Place a GTC Limit order on options. Maker when priced at bid/ask."""
        params = dict(
            category="option",
            symbol=symbol,
            side=side,
            orderType="Limit",
            qty=str(qty),
            price=str(price),
            timeInForce="GTC",
            orderLinkId=f"{'bp' if side == 'Buy' else 'sp'}-{uuid.uuid4().hex[:16]}",
        )
        if reduce:
            params["reduceOnly"] = True
        data = await self._call(self._http.place_order, **params)
        return data["result"]

    async def _wait_option_fill(self, symbol: str, order_id: str, timeout: float) -> dict:
        """Wait for an option order to fill. Checks open orders then history."""
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                data = await self._call(
                    self._http.get_open_orders,
                    category="option", symbol=symbol, orderId=order_id,
                )
                open_list = data["result"]["list"]
                if open_list:
                    status = open_list[0].get("orderStatus")
                    if status == "Filled":
                        return open_list[0]
                    if status in ("New", "PartiallyFilled"):
                        await asyncio.sleep(0.5)
                        continue
                    return {}
            except Exception:
                pass

            try:
                data = await self._call(
                    self._http.get_order_history,
                    category="option", symbol=symbol, orderId=order_id,
                )
                hist = data["result"]["list"]
                if hist:
                    return hist[0]
            except Exception:
                pass

            await asyncio.sleep(0.5)
        return {}

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
        self, symbol: str, qty: float, initial_bid: float,
    ) -> dict | None:
        """
        Escalating maker buy: start at bid, walk up by 1 tick per attempt.

        Never crosses the spread (price capped at ask - 1 tick), so every
        order is a maker order.  On each attempt the price improves by one
        option tick ($5) to attract a fill.

        Args:
            initial_bid: REST-snapshot bid price, used as starting price
                         when the option WebSocket hasn't delivered data yet.
        """
        if config.DRY_RUN:
            return self._fake_order("Buy", symbol, qty, _round_price_up(initial_bid))

        log.info("chase_buy_put_maker", symbol=symbol, qty=qty)
        tick = config.OPTION_TICK_SIZE

        for attempt in range(config.OPTION_CHASE_MAX_ATTEMPTS):
            cached = self.get_cached_option(symbol)

            if cached and cached.bid > 0:
                base_price = cached.bid
                ceiling = cached.ask - tick if cached.ask > 0 else cached.bid + tick * 10
            else:
                base_price = initial_bid
                ceiling = initial_bid + tick * 10

            price = _round_price_up(base_price + tick * attempt)
            price = min(price, _round_price_up(ceiling))

            result = await self._place_option_limit("Buy", symbol, qty, price)
            order_id = result.get("orderId", "")
            if not order_id:
                break

            fill = await self._wait_option_fill(symbol, order_id, timeout=config.OPTION_CHASE_INTERVAL_SEC)

            if fill and fill.get("orderStatus") == "Filled":
                fill_price = float(fill.get("avgPrice", price))
                log.info("chase_filled", symbol=symbol, price=fill_price, attempt=attempt + 1)
                return {"orderId": order_id, "orderStatus": "Filled", "avgPrice": str(fill_price)}

            await self.cancel_order("option", symbol, order_id)

            final = await self._get_order_final_state("option", symbol, order_id)
            cum_qty = float(final.get("cumExecQty", 0))
            if cum_qty > 0:
                fill_price = float(final.get("avgPrice", price))
                log.info("chase_filled_post_cancel", symbol=symbol, price=fill_price,
                         qty_filled=cum_qty, attempt=attempt + 1)
                return {"orderId": order_id, "orderStatus": "Filled", "avgPrice": str(fill_price)}

            log.debug("chase_buy_reprice", symbol=symbol, attempt=attempt + 1, price=price)

        log.warning("chase_exhausted", symbol=symbol)
        return None

    async def chase_sell_put(
        self, symbol: str, qty: float, initial_ask: float,
    ) -> dict | None:
        """
        Escalating maker sell: start at ask, walk down by 1 tick per attempt.

        Never crosses the spread (price floored at bid + 1 tick), so every
        order is a maker order.  On each attempt the price improves by one
        option tick ($5) to attract a fill.

        Args:
            initial_ask: REST-snapshot ask price, used as starting price
                         when the option WebSocket hasn't delivered data yet.
        """
        if config.DRY_RUN:
            return self._fake_order("Sell", symbol, qty, _round_price_down(initial_ask))

        log.info("chase_sell_put_maker", symbol=symbol, qty=qty)
        tick = config.OPTION_TICK_SIZE

        for attempt in range(config.OPTION_CHASE_MAX_ATTEMPTS):
            cached = self.get_cached_option(symbol)

            if cached and cached.ask > 0:
                base_price = cached.ask
                floor = cached.bid + tick if cached.bid > 0 else max(tick, cached.ask - tick * 10)
            else:
                base_price = initial_ask
                floor = max(tick, initial_ask - tick * 10)

            price = _round_price_down(base_price - tick * attempt)
            price = max(price, _round_price_down(floor))

            result = await self._place_option_limit("Sell", symbol, qty, price, reduce=True)
            order_id = result.get("orderId", "")
            if not order_id:
                break

            fill = await self._wait_option_fill(symbol, order_id, timeout=config.OPTION_CHASE_INTERVAL_SEC)

            if fill and fill.get("orderStatus") == "Filled":
                fill_price = float(fill.get("avgPrice", price))
                log.info("chase_sell_filled", symbol=symbol, price=fill_price, attempt=attempt + 1)
                return {"orderId": order_id, "orderStatus": "Filled", "avgPrice": str(fill_price)}

            await self.cancel_order("option", symbol, order_id)

            final = await self._get_order_final_state("option", symbol, order_id)
            cum_qty = float(final.get("cumExecQty", 0))
            if cum_qty > 0:
                fill_price = float(final.get("avgPrice", price))
                log.info("chase_sell_filled_post_cancel", symbol=symbol, price=fill_price,
                         qty_filled=cum_qty, attempt=attempt + 1)
                return {"orderId": order_id, "orderStatus": "Filled", "avgPrice": str(fill_price)}

            log.debug("chase_sell_reprice", symbol=symbol, attempt=attempt + 1, price=price)

        log.warning("chase_sell_exhausted", symbol=symbol)
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
