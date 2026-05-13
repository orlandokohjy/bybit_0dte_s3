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
from datetime import datetime, timezone
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


def _utc_iso(t_unix: float) -> str:
    """Convert a unix timestamp to a UTC ISO8601 string."""
    return datetime.fromtimestamp(t_unix, tz=timezone.utc).isoformat()


def _build_fill_metrics(
    *,
    side: str,
    instrument: str,
    qty_btc: float,
    fill_price: float,
    t_started: float,
    t_filled: float,
    attempts: int,
    ref_bid: float,
    ref_ask: float,
    ref_mark: float,
) -> dict:
    """
    Build the fill-quality metrics dict that flows from chase_buy/sell
    (and buy_spot/sell_spot) back to the straddle builder/exit manager
    and ultimately into the daily report.

    Slippage is positive when we paid more than mark (buys) or
    received less than mark (sells) — i.e. execution worse than fair
    value. Negative = better than fair value.

    For spot legs the "mark" is taken as the mid (bid + ask) / 2 since
    Bybit spot does not publish a mark price.
    """
    duration = max(0.0, t_filled - t_started)
    ref_mid = (ref_bid + ref_ask) / 2 if ref_bid > 0 and ref_ask > 0 else 0.0

    if side.lower() in ("buy", "b"):
        slip_mark = ((fill_price - ref_mark) / ref_mark
                     if ref_mark > 0 else 0.0)
        slip_mid = ((fill_price - ref_mid) / ref_mid
                    if ref_mid > 0 else 0.0)
        # Maker buy → taker alternative is paying the ask.
        taker_price = ref_ask
        saved_per_unit = (ref_ask - fill_price) if ref_ask > 0 else 0.0
        saved_pct = (saved_per_unit / ref_ask) if ref_ask > 0 else 0.0
    else:  # sell
        slip_mark = ((ref_mark - fill_price) / ref_mark
                     if ref_mark > 0 else 0.0)
        slip_mid = ((ref_mid - fill_price) / ref_mid
                    if ref_mid > 0 else 0.0)
        # Maker sell → taker alternative is hitting the bid.
        taker_price = ref_bid
        saved_per_unit = (fill_price - ref_bid) if ref_bid > 0 else 0.0
        saved_pct = (saved_per_unit / ref_bid) if ref_bid > 0 else 0.0

    return {
        "instrument": instrument,
        "side": side,
        "qty_btc": qty_btc,
        "t_started_iso": _utc_iso(t_started),
        "t_filled_iso": _utc_iso(t_filled),
        "duration_sec": round(duration, 2),
        "attempts": attempts,
        "ref_bid": round(ref_bid, 4),
        "ref_ask": round(ref_ask, 4),
        "ref_mid": round(ref_mid, 4),
        "ref_mark": round(ref_mark, 4),
        "fill_price": round(fill_price, 4),
        "slippage_vs_mark_pct": round(slip_mark * 100, 4),
        "slippage_vs_mid_pct": round(slip_mid * 100, 4),
        "taker_price_at_start": round(taker_price, 4),
        "saved_vs_taker_per_unit_usd": round(saved_per_unit, 4),
        "saved_vs_taker_pct": round(saved_pct * 100, 4),
        "saved_vs_taker_total_usd": round(saved_per_unit * qty_btc, 2),
    }


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

    async def get_option_position(self, symbol: str) -> float:
        """Query actual held qty for an option symbol. Returns 0 if none."""
        try:
            data = await self._call(
                self._http.get_positions, category="option", symbol=symbol)
            for pos in data.get("result", {}).get("list", []):
                size = float(pos.get("size", 0))
                if size > 0:
                    return size
        except Exception:
            log.warning("get_option_position_failed", symbol=symbol, exc_info=True)
        return 0.0

    async def get_spot_balance(self, coin: str = "BTC") -> float:
        """Return current spot wallet balance for a coin. Returns 0 on failure."""
        try:
            data = await self._call(
                self._http.get_wallet_balance,
                accountType=config.ACCOUNT_TYPE,
            )
            for acct in data["result"]["list"]:
                for c in acct.get("coin", []):
                    if c.get("coin") == coin:
                        return float(c.get("walletBalance", 0) or 0)
        except Exception:
            log.warning("get_spot_balance_failed", coin=coin, exc_info=True)
        return 0.0

    async def get_available_balance_usd(self, coin: str = "USDT") -> float:
        """Return the available (free) balance in `coin` for the unified account.

        Used for pre-entry collateral checks to ensure we have headroom for
        the option premiums before placing any orders.
        """
        try:
            data = await self._call(
                self._http.get_wallet_balance,
                accountType=config.ACCOUNT_TYPE,
            )
            for acct in data["result"]["list"]:
                for c in acct.get("coin", []):
                    if c.get("coin") == coin:
                        avail = c.get("availableToWithdraw") or c.get("free") or 0
                        return float(avail or 0)
        except Exception:
            log.warning("get_available_balance_failed", coin=coin, exc_info=True)
        return 0.0

    # ──────────────────── Open Orders & Positions (reconcile) ─────

    async def list_open_orders(self) -> list[dict]:
        """List all open option + spot orders on the unified account.

        Returns a list of dicts with keys: category, symbol, orderId, side,
        qty, price, orderStatus.  Used by the startup safeguards to clear
        stale orders left from a previous run.
        """
        out: list[dict] = []
        for category in ("option", "spot"):
            try:
                kwargs = {"category": category}
                if category == "option":
                    kwargs["baseCoin"] = config.BASE_COIN
                data = await self._call(self._http.get_open_orders, **kwargs)
                for o in data.get("result", {}).get("list", []) or []:
                    out.append({
                        "category": category,
                        "symbol": o.get("symbol", ""),
                        "orderId": o.get("orderId", ""),
                        "side": o.get("side", ""),
                        "qty": o.get("qty", ""),
                        "price": o.get("price", ""),
                        "orderStatus": o.get("orderStatus", ""),
                    })
            except Exception:
                log.warning("list_open_orders_failed", category=category,
                            exc_info=True)
        return out

    async def cancel_all_open_orders(self) -> int:
        """Cancel every resting order across option + spot.

        Returns the count cancelled. Stale orders eat margin and can cause
        'Insufficient funds' errors on new entries (see 2026-04-20 incident
        on Derive — same risk applies on Bybit).
        """
        orders = await self.list_open_orders()
        if not orders:
            log.info("cancel_all_no_open_orders")
            return 0

        log.warning("cancel_all_found_stale_orders", count=len(orders),
                    orders=[f"{o['category']}/{o['symbol']} "
                            f"{o['side']} {o['qty']}@{o['price']}"
                            for o in orders])

        cancelled = 0
        for o in orders:
            cat = o.get("category", "")
            sym = o.get("symbol", "")
            oid = o.get("orderId", "")
            if not cat or not sym or not oid:
                continue
            try:
                await self.cancel_order(cat, sym, oid)
                cancelled += 1
            except Exception:
                log.warning("cancel_one_failed", category=cat, symbol=sym,
                            order_id=oid, exc_info=True)
        log.info("cancel_all_done", cancelled=cancelled,
                 attempted=len(orders))
        return cancelled

    async def list_open_positions(self) -> list[dict]:
        """List non-zero option positions + significant BTC spot holdings.

        Returns dicts with keys:
          category ('option' or 'spot'), symbol, amount (signed),
          average_price, mark_price, unrealized_pnl
        """
        out: list[dict] = []

        # Options
        try:
            data = await self._call(
                self._http.get_positions,
                category="option",
                baseCoin=config.BASE_COIN,
            )
            for p in data.get("result", {}).get("list", []) or []:
                size = float(p.get("size", 0) or 0)
                if size == 0:
                    continue
                side = p.get("side", "")
                signed = size if side == "Buy" else -size
                out.append({
                    "category": "option",
                    "symbol": p.get("symbol", ""),
                    "amount": signed,
                    "average_price": float(p.get("avgPrice", 0) or 0),
                    "mark_price": float(p.get("markPrice", 0) or 0),
                    "unrealized_pnl": float(p.get("unrealisedPnl", 0) or 0),
                })
        except Exception:
            log.warning("list_option_positions_failed", exc_info=True)

        # Spot (only flag if the BTC balance looks like a held position)
        try:
            btc_bal = await self.get_spot_balance(config.BASE_COIN)
            spot_threshold = config.QTY_PER_LEG / 2  # half a leg = orphan
            if btc_bal >= spot_threshold:
                spot_price = await self.get_spot_price()
                out.append({
                    "category": "spot",
                    "symbol": config.SPOT_SYMBOL,
                    "amount": btc_bal,
                    "average_price": 0.0,
                    "mark_price": spot_price,
                    "unrealized_pnl": 0.0,
                })
        except Exception:
            log.warning("list_spot_position_failed", exc_info=True)

        return out

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

        Returns dict with `metrics` populated on fill (see _build_fill_metrics).
        """
        log.info("buy_spot_maker", qty=qty)
        if config.DRY_RUN:
            price = await self.get_spot_price()
            return self._fake_order("Buy", config.SPOT_SYMBOL, qty, price)

        t_started = _time.time()
        ref_bid = ref_ask = ref_mark = 0.0
        captured_ref = False

        for attempt in range(config.SPOT_CHASE_MAX_ATTEMPTS):
            cached = self.get_cached_spot()
            if cached and cached.bid > 0:
                price = self._round_spot_price(cached.bid, "down")
            else:
                price = self._round_spot_price(await self.get_spot_price(), "down")

            # Capture decision-time market state for slippage metrics.
            if not captured_ref and cached and cached.bid > 0 and cached.ask > 0:
                ref_bid = cached.bid
                ref_ask = cached.ask
                ref_mark = (cached.bid + cached.ask) / 2  # spot has no mark
                captured_ref = True

            result = await self._place_spot_limit("Buy", qty, price)
            order_id = result.get("orderId", "")
            if not order_id:
                break

            fill = await self._wait_spot_fill(order_id, price, timeout=config.SPOT_CHASE_INTERVAL_SEC)

            if fill and fill.get("orderStatus") == "Filled":
                fill_price = float(fill.get("avgPrice", price))
                t_filled = _time.time()
                metrics = _build_fill_metrics(
                    side="buy", instrument=config.SPOT_SYMBOL,
                    qty_btc=qty, fill_price=fill_price,
                    t_started=t_started, t_filled=t_filled,
                    attempts=attempt + 1,
                    ref_bid=ref_bid or price, ref_ask=ref_ask or price,
                    ref_mark=ref_mark or price,
                )
                log.info("spot_buy_filled", price=fill_price,
                         attempt=attempt + 1,
                         duration_sec=metrics["duration_sec"],
                         saved_vs_taker_total_usd=metrics["saved_vs_taker_total_usd"])
                return {"orderId": order_id, "orderStatus": "Filled",
                        "avgPrice": str(fill_price), "metrics": metrics}

            await self.cancel_order(config.SPOT_CATEGORY, config.SPOT_SYMBOL, order_id)

            final = await self._get_order_final_state(
                config.SPOT_CATEGORY, config.SPOT_SYMBOL, order_id,
            )
            final_status = final.get("orderStatus", "")
            cum_qty = float(final.get("cumExecQty", 0))
            if cum_qty > 0:
                fill_price = float(final.get("avgPrice", price))
                t_filled = _time.time()
                metrics = _build_fill_metrics(
                    side="buy", instrument=config.SPOT_SYMBOL,
                    qty_btc=cum_qty, fill_price=fill_price,
                    t_started=t_started, t_filled=t_filled,
                    attempts=attempt + 1,
                    ref_bid=ref_bid or price, ref_ask=ref_ask or price,
                    ref_mark=ref_mark or price,
                )
                log.info("spot_buy_filled_post_cancel", price=fill_price,
                         qty_filled=cum_qty, status=final_status,
                         attempt=attempt + 1,
                         duration_sec=metrics["duration_sec"])
                return {"orderId": order_id, "orderStatus": "Filled",
                        "avgPrice": str(fill_price),
                        "cumExecQty": str(cum_qty), "metrics": metrics}

            log.debug("spot_buy_chase", attempt=attempt + 1, price=price)

        log.warning("spot_buy_chase_exhausted", qty=qty)
        return {}

    async def sell_spot(self, qty: float) -> dict:
        """
        Limit sell BTC spot — post at ask for maker rebate.

        Uses GTC limit at ask price. If not filled within the chase interval,
        cancels and re-posts at the updated ask.  Post-cancel verification
        prevents duplicate sells when a fill races with the timeout.

        Returns dict with `metrics` populated on fill (see _build_fill_metrics).
        """
        log.info("sell_spot_maker", qty=qty)
        if config.DRY_RUN:
            price = await self.get_spot_price()
            return self._fake_order("Sell", config.SPOT_SYMBOL, qty, price)

        t_started = _time.time()
        ref_bid = ref_ask = ref_mark = 0.0
        captured_ref = False

        for attempt in range(config.SPOT_CHASE_MAX_ATTEMPTS):
            cached = self.get_cached_spot()
            if cached and cached.ask > 0:
                price = self._round_spot_price(cached.ask, "up")
            else:
                price = self._round_spot_price(await self.get_spot_price(), "up")

            if not captured_ref and cached and cached.bid > 0 and cached.ask > 0:
                ref_bid = cached.bid
                ref_ask = cached.ask
                ref_mark = (cached.bid + cached.ask) / 2
                captured_ref = True

            result = await self._place_spot_limit("Sell", qty, price)
            order_id = result.get("orderId", "")
            if not order_id:
                break

            fill = await self._wait_spot_fill(order_id, price, timeout=config.SPOT_CHASE_INTERVAL_SEC)

            if fill and fill.get("orderStatus") == "Filled":
                fill_price = float(fill.get("avgPrice", price))
                t_filled = _time.time()
                metrics = _build_fill_metrics(
                    side="sell", instrument=config.SPOT_SYMBOL,
                    qty_btc=qty, fill_price=fill_price,
                    t_started=t_started, t_filled=t_filled,
                    attempts=attempt + 1,
                    ref_bid=ref_bid or price, ref_ask=ref_ask or price,
                    ref_mark=ref_mark or price,
                )
                log.info("spot_sell_filled", price=fill_price,
                         attempt=attempt + 1,
                         duration_sec=metrics["duration_sec"],
                         saved_vs_taker_total_usd=metrics["saved_vs_taker_total_usd"])
                return {"orderId": order_id, "orderStatus": "Filled",
                        "avgPrice": str(fill_price), "metrics": metrics}

            await self.cancel_order(config.SPOT_CATEGORY, config.SPOT_SYMBOL, order_id)

            final = await self._get_order_final_state(
                config.SPOT_CATEGORY, config.SPOT_SYMBOL, order_id,
            )
            final_status = final.get("orderStatus", "")
            cum_qty = float(final.get("cumExecQty", 0))
            if cum_qty > 0:
                fill_price = float(final.get("avgPrice", price))
                t_filled = _time.time()
                metrics = _build_fill_metrics(
                    side="sell", instrument=config.SPOT_SYMBOL,
                    qty_btc=cum_qty, fill_price=fill_price,
                    t_started=t_started, t_filled=t_filled,
                    attempts=attempt + 1,
                    ref_bid=ref_bid or price, ref_ask=ref_ask or price,
                    ref_mark=ref_mark or price,
                )
                log.info("spot_sell_filled_post_cancel", price=fill_price,
                         qty_filled=cum_qty, status=final_status,
                         attempt=attempt + 1,
                         duration_sec=metrics["duration_sec"])
                return {"orderId": order_id, "orderStatus": "Filled",
                        "avgPrice": str(fill_price),
                        "cumExecQty": str(cum_qty), "metrics": metrics}

            log.debug("spot_sell_chase", attempt=attempt + 1, price=price)

        log.warning("spot_sell_chase_exhausted", qty=qty)
        return {}

    # ──────────────────── Option Orders (PostOnly for guaranteed maker) ──

    async def _place_option_limit(
        self, side: str, symbol: str, qty: float, price: float,
        reduce: bool = False, post_only: bool = True,
    ) -> dict:
        """Place a PostOnly Limit order on options.

        timeInForce='PostOnly' guarantees the order is rejected if it would
        cross the spread — no taker fills possible. If rejected, returns a
        dict with rejected_post_only=True so the caller can reprice.
        """
        tif = "PostOnly" if post_only else "GTC"
        params = dict(
            category="option",
            symbol=symbol,
            side=side,
            orderType="Limit",
            qty=str(qty),
            price=str(price),
            timeInForce=tif,
            orderLinkId=f"{'bp' if side == 'Buy' else 'sp'}-{uuid.uuid4().hex[:16]}",
        )
        if reduce:
            params["reduceOnly"] = True
        try:
            data = await self._call(self._http.place_order, **params)
            return data["result"]
        except Exception as exc:
            err_str = str(exc)
            # Bybit V5 post-only rejection codes:
            #   110017 — order would immediately match
            #   110079 — order would match with own order
            if "ErrCode: 110017" in err_str or "ErrCode: 110079" in err_str:
                log.debug("post_only_rejected", symbol=symbol, price=price)
                return {"rejected_post_only": True}
            raise

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

    async def _read_option_order(self, symbol: str, order_id: str) -> dict:
        """One-shot read of an option order's status.

        Checks open orders first (still resting), falls back to order
        history (terminal). Used by chase_buy_put / chase_sell_put for
        keep-alive iteration without cancel-replace.
        """
        if not order_id:
            return {}
        try:
            data = await self._call(
                self._http.get_open_orders,
                category="option", symbol=symbol, orderId=order_id,
            )
            open_list = data["result"]["list"]
            if open_list:
                return open_list[0]
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
        Maker-only persistent buy with 50% bid-ask gap narrowing, fair-value
        cap, and queue-priority preservation (added 2026-05-13).

        Strategy:
          - Start at the current bid (or initial_bid if WS not ready).
          - On each no-fill cycle, narrow the remaining gap toward
            (ask − 1 tick) by OPTION_CHASE_GAP_NARROW_PCT (default 50%).
          - Never post above mark × OPTION_CHASE_MAX_SLIPPAGE_FACTOR
            (default 1.15).
          - Bail when OPTION_ENTRY_CHASE_DEADLINE_SEC expires — no taker fallback.

        Queue priority:
          - If the recomputed price equals the resting order's price, the
            chase keeps the order alive (no cancel-replace) so we don't
            forfeit FIFO position at that price level.
          - Reprice (price changed) cancels the resting order, credits any
            partial fills, and posts a fresh order at the new price.

        Partial fills accumulate. On full fill returns the filled order
        dict; on partial fill at deadline returns a synthetic dict with
        orderStatus='PartiallyFilled' so callers can decide what to do.
        Returns None only when nothing filled before the deadline.

        Args:
            initial_bid: REST-snapshot bid, used as the starting price when
                         the option WebSocket has not delivered data yet.
        """
        if config.DRY_RUN:
            return self._fake_order("Buy", symbol, qty, _round_price_up(initial_bid))

        deadline = _time.time() + config.OPTION_ENTRY_CHASE_DEADLINE_SEC
        tick = config.OPTION_TICK_SIZE
        remaining_qty = qty
        weighted_cost = 0.0
        total_filled = 0.0
        current_price = _round_price_up(initial_bid)
        attempt = 0
        t_started = _time.time()
        ref_bid = ref_ask = ref_mark = 0.0
        captured_ref = False

        # Resting-order state across iterations (queue-priority preserve)
        rested_ord_id: str = ""
        rested_price: float = 0.0
        rested_credited_qty: float = 0.0
        last_ord_id: str = ""

        log.info(
            "chase_buy_put_start",
            symbol=symbol,
            qty=qty,
            initial_bid=initial_bid,
            deadline_sec=config.OPTION_ENTRY_CHASE_DEADLINE_SEC,
            gap_narrow_pct=config.OPTION_CHASE_GAP_NARROW_PCT,
            max_slippage_factor=config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR,
        )

        while _time.time() < deadline and remaining_qty > 0:
            attempt += 1
            cached = self.get_cached_option(symbol)

            # Capture decision-time market state for slippage metrics.
            if not captured_ref and cached and cached.ask > 0:
                ref_bid = cached.bid
                ref_ask = cached.ask
                ref_mark = (cached.mark if cached.mark > 0
                            else (cached.bid + cached.ask) / 2)
                captured_ref = True

            # ── 0. Credit any fills on the existing resting order ──
            if rested_ord_id:
                status = await self._read_option_order(symbol, rested_ord_id)
                state = status.get("orderStatus", "")
                cum_qty = float(status.get("cumExecQty", 0) or 0)
                avg_px = float(status.get("avgPrice", rested_price) or rested_price)
                delta = max(0.0, cum_qty - rested_credited_qty)
                if delta > 0:
                    weighted_cost += avg_px * delta
                    total_filled += delta
                    remaining_qty = round(remaining_qty - delta, 5)
                    rested_credited_qty = cum_qty
                    log.info(
                        "chase_buy_resting_fill_credit",
                        symbol=symbol, attempt=attempt,
                        ord_id=rested_ord_id, delta=delta,
                        total_filled=total_filled, remaining=remaining_qty,
                        state=state,
                    )
                if state == "Filled" or remaining_qty <= 0:
                    avg_price = (weighted_cost / total_filled) if total_filled > 0 else avg_px
                    t_filled = _time.time()
                    metrics = _build_fill_metrics(
                        side="buy", instrument=symbol,
                        qty_btc=total_filled, fill_price=avg_price,
                        t_started=t_started, t_filled=t_filled,
                        attempts=attempt,
                        ref_bid=ref_bid, ref_ask=ref_ask, ref_mark=ref_mark,
                    )
                    log.info(
                        "chase_buy_filled",
                        symbol=symbol, price=avg_price,
                        total_filled=total_filled, attempt=attempt,
                        duration_sec=metrics["duration_sec"],
                        slippage_vs_mark_pct=metrics["slippage_vs_mark_pct"],
                        saved_vs_taker_total_usd=metrics["saved_vs_taker_total_usd"],
                    )
                    return {
                        "orderId": rested_ord_id,
                        "orderStatus": "Filled",
                        "avgPrice": str(avg_price),
                        "metrics": metrics,
                    }
                if state in ("Cancelled", "Rejected", "Deactivated", ""):
                    rested_ord_id = ""
                    rested_price = 0.0
                    rested_credited_qty = 0.0

            # ── 1. Compute new price ──
            if cached and cached.bid > 0 and cached.ask > 0:
                target_ceiling = cached.ask - tick
                if current_price < target_ceiling:
                    gap = target_ceiling - current_price
                    current_price = _round_price_up(
                        current_price + gap * config.OPTION_CHASE_GAP_NARROW_PCT
                    )
                mark = cached.mark if cached.mark > 0 else (cached.bid + cached.ask) / 2
                cap = _round_price_up(mark * config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR)
                if current_price > cap:
                    log.debug(
                        "chase_buy_capped_at_fair_value",
                        symbol=symbol, attempted_price=current_price,
                        cap=cap, mark=mark,
                    )
                    current_price = cap
                current_price = min(current_price, target_ceiling)
            else:
                current_price = _round_price_up(current_price + tick)

            # ── 2. Keep-alive: same price as resting order? ──
            same_price = (rested_ord_id and
                          abs(rested_price - current_price) < tick * 0.5)

            if same_price:
                log.info(
                    "chase_buy_keep_alive",
                    symbol=symbol, attempt=attempt,
                    price=current_price, ord_id=rested_ord_id,
                    remaining=remaining_qty,
                )
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            # ── 3. Reprice: cancel resting order, credit any final fills ──
            if rested_ord_id:
                log.info(
                    "chase_buy_reprice",
                    symbol=symbol, attempt=attempt,
                    from_price=rested_price, to_price=current_price,
                    ord_id=rested_ord_id,
                )
                await self.cancel_order("option", symbol, rested_ord_id)
                final = await self._get_order_final_state("option", symbol, rested_ord_id)
                final_cum = float(final.get("cumExecQty", 0) or 0)
                final_avg = float(final.get("avgPrice", rested_price) or rested_price)
                delta = max(0.0, final_cum - rested_credited_qty)
                if delta > 0:
                    weighted_cost += final_avg * delta
                    total_filled += delta
                    remaining_qty = round(remaining_qty - delta, 5)
                    log.info(
                        "chase_buy_reprice_partial_credit",
                        symbol=symbol, attempt=attempt,
                        delta=delta, total_filled=total_filled,
                        remaining=remaining_qty,
                    )
                rested_ord_id = ""
                rested_price = 0.0
                rested_credited_qty = 0.0
                if remaining_qty <= 0:
                    break

            # ── 4. Place new order at current_price ──
            result = await self._place_option_limit(
                "Buy", symbol, remaining_qty, current_price
            )
            if result.get("rejected_post_only"):
                log.debug("chase_buy_post_only_reject", symbol=symbol,
                          price=current_price, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue
            order_id = result.get("orderId", "")
            if not order_id:
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            rested_ord_id = order_id
            rested_price = current_price
            rested_credited_qty = 0.0
            last_ord_id = order_id
            await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)

        # ── Loop exit: cancel any remaining resting order, credit fills ──
        if rested_ord_id:
            await self.cancel_order("option", symbol, rested_ord_id)
            final = await self._get_order_final_state("option", symbol, rested_ord_id)
            final_cum = float(final.get("cumExecQty", 0) or 0)
            final_avg = float(final.get("avgPrice", rested_price) or rested_price)
            delta = max(0.0, final_cum - rested_credited_qty)
            if delta > 0:
                weighted_cost += final_avg * delta
                total_filled += delta
                remaining_qty = round(remaining_qty - delta, 5)
                log.info(
                    "chase_buy_exit_partial_credit",
                    symbol=symbol, attempt=attempt,
                    delta=delta, total_filled=total_filled,
                )
            rested_ord_id = ""
            rested_price = 0.0
            rested_credited_qty = 0.0

        # ── Deadline expired ──
        if total_filled > 0:
            avg_price = weighted_cost / total_filled
            t_filled = _time.time()
            metrics = _build_fill_metrics(
                side="buy", instrument=symbol,
                qty_btc=total_filled, fill_price=avg_price,
                t_started=t_started, t_filled=t_filled,
                attempts=attempt,
                ref_bid=ref_bid, ref_ask=ref_ask, ref_mark=ref_mark,
            )
            log.warning(
                "chase_buy_partial_at_deadline",
                symbol=symbol,
                total_filled=total_filled,
                remaining=remaining_qty,
                attempts=attempt,
            )
            return {
                "orderId": last_ord_id or "partial",
                "orderStatus": "PartiallyFilled",
                "avgPrice": str(avg_price),
                "cumExecQty": str(total_filled),
                "metrics": metrics,
            }

        log.warning(
            "chase_buy_deadline_expired",
            symbol=symbol,
            attempts=attempt,
            deadline_sec=config.OPTION_ENTRY_CHASE_DEADLINE_SEC,
        )
        return None

    async def chase_sell_put(
        self, symbol: str, qty: float, initial_ask: float,
    ) -> dict | None:
        """
        Maker-only persistent sell with 50% bid-ask gap narrowing, fair-value
        floor, and queue-priority preservation (added 2026-05-13).

        Mirrors chase_buy_put — see that docstring for the keep-alive
        semantics. Returns same shape on full fill, partial-fill-at-deadline,
        and total-zero-fill cases.

        Args:
            initial_ask: REST-snapshot ask, used as the starting price when
                         the option WebSocket has not delivered data yet.
        """
        if config.DRY_RUN:
            return self._fake_order("Sell", symbol, qty, _round_price_down(initial_ask))

        deadline = _time.time() + config.OPTION_EXIT_CHASE_DEADLINE_SEC
        tick = config.OPTION_TICK_SIZE
        remaining_qty = qty
        weighted_revenue = 0.0
        total_filled = 0.0
        current_price = _round_price_down(initial_ask)
        attempt = 0
        t_started = _time.time()
        ref_bid = ref_ask = ref_mark = 0.0
        captured_ref = False

        # Resting-order state across iterations (queue-priority preserve)
        rested_ord_id: str = ""
        rested_price: float = 0.0
        rested_credited_qty: float = 0.0
        last_ord_id: str = ""

        log.info(
            "chase_sell_put_start",
            symbol=symbol,
            qty=qty,
            initial_ask=initial_ask,
            deadline_sec=config.OPTION_EXIT_CHASE_DEADLINE_SEC,
            gap_narrow_pct=config.OPTION_CHASE_GAP_NARROW_PCT,
            max_slippage_factor=config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR,
        )

        while _time.time() < deadline and remaining_qty > 0:
            attempt += 1
            cached = self.get_cached_option(symbol)

            # Capture decision-time market state for slippage metrics.
            if not captured_ref and cached and (cached.bid > 0 or cached.mark > 0):
                ref_bid = cached.bid
                ref_ask = cached.ask
                ref_mark = (cached.mark if cached.mark > 0
                            else (cached.bid + cached.ask) / 2)
                captured_ref = True

            # ── 0. Credit any fills on the existing resting order ──
            if rested_ord_id:
                status = await self._read_option_order(symbol, rested_ord_id)
                state = status.get("orderStatus", "")
                cum_qty = float(status.get("cumExecQty", 0) or 0)
                avg_px = float(status.get("avgPrice", rested_price) or rested_price)
                delta = max(0.0, cum_qty - rested_credited_qty)
                if delta > 0:
                    weighted_revenue += avg_px * delta
                    total_filled += delta
                    remaining_qty = round(remaining_qty - delta, 5)
                    rested_credited_qty = cum_qty
                    log.info(
                        "chase_sell_resting_fill_credit",
                        symbol=symbol, attempt=attempt,
                        ord_id=rested_ord_id, delta=delta,
                        total_filled=total_filled, remaining=remaining_qty,
                        state=state,
                    )
                if state == "Filled" or remaining_qty <= 0:
                    avg_price = (weighted_revenue / total_filled) if total_filled > 0 else avg_px
                    t_filled = _time.time()
                    metrics = _build_fill_metrics(
                        side="sell", instrument=symbol,
                        qty_btc=total_filled, fill_price=avg_price,
                        t_started=t_started, t_filled=t_filled,
                        attempts=attempt,
                        ref_bid=ref_bid, ref_ask=ref_ask, ref_mark=ref_mark,
                    )
                    log.info(
                        "chase_sell_filled",
                        symbol=symbol, price=avg_price,
                        total_filled=total_filled, attempt=attempt,
                        duration_sec=metrics["duration_sec"],
                        slippage_vs_mark_pct=metrics["slippage_vs_mark_pct"],
                        saved_vs_taker_total_usd=metrics["saved_vs_taker_total_usd"],
                    )
                    return {
                        "orderId": rested_ord_id,
                        "orderStatus": "Filled",
                        "avgPrice": str(avg_price),
                        "metrics": metrics,
                    }
                if state in ("Cancelled", "Rejected", "Deactivated", ""):
                    rested_ord_id = ""
                    rested_price = 0.0
                    rested_credited_qty = 0.0

            # ── 1. Compute new price ──
            if cached and cached.bid > 0 and cached.ask > 0:
                target_floor = cached.bid + tick
                if current_price > target_floor:
                    gap = current_price - target_floor
                    current_price = _round_price_down(
                        current_price - gap * config.OPTION_CHASE_GAP_NARROW_PCT
                    )
                mark = cached.mark if cached.mark > 0 else (cached.bid + cached.ask) / 2
                floor_price = _round_price_down(
                    mark / config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
                )
                if current_price < floor_price:
                    log.debug(
                        "chase_sell_floored_at_fair_value",
                        symbol=symbol, attempted_price=current_price,
                        floor=floor_price, mark=mark,
                    )
                    current_price = floor_price
                current_price = max(current_price, target_floor)
            else:
                current_price = _round_price_down(max(tick, current_price - tick))

            # ── 2. Keep-alive: same price as resting order? ──
            same_price = (rested_ord_id and
                          abs(rested_price - current_price) < tick * 0.5)

            if same_price:
                log.info(
                    "chase_sell_keep_alive",
                    symbol=symbol, attempt=attempt,
                    price=current_price, ord_id=rested_ord_id,
                    remaining=remaining_qty,
                )
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            # ── 3. Reprice: cancel resting order, credit any final fills ──
            if rested_ord_id:
                log.info(
                    "chase_sell_reprice",
                    symbol=symbol, attempt=attempt,
                    from_price=rested_price, to_price=current_price,
                    ord_id=rested_ord_id,
                )
                await self.cancel_order("option", symbol, rested_ord_id)
                final = await self._get_order_final_state("option", symbol, rested_ord_id)
                final_cum = float(final.get("cumExecQty", 0) or 0)
                final_avg = float(final.get("avgPrice", rested_price) or rested_price)
                delta = max(0.0, final_cum - rested_credited_qty)
                if delta > 0:
                    weighted_revenue += final_avg * delta
                    total_filled += delta
                    remaining_qty = round(remaining_qty - delta, 5)
                    log.info(
                        "chase_sell_reprice_partial_credit",
                        symbol=symbol, attempt=attempt,
                        delta=delta, total_filled=total_filled,
                        remaining=remaining_qty,
                    )
                rested_ord_id = ""
                rested_price = 0.0
                rested_credited_qty = 0.0
                if remaining_qty <= 0:
                    break

            # ── 4. Place new order at current_price ──
            result = await self._place_option_limit(
                "Sell", symbol, remaining_qty, current_price, reduce=True
            )
            if result.get("rejected_post_only"):
                log.debug("chase_sell_post_only_reject", symbol=symbol,
                          price=current_price, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue
            order_id = result.get("orderId", "")
            if not order_id:
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            rested_ord_id = order_id
            rested_price = current_price
            rested_credited_qty = 0.0
            last_ord_id = order_id
            await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)

        # ── Loop exit: cancel any remaining resting order, credit fills ──
        if rested_ord_id:
            await self.cancel_order("option", symbol, rested_ord_id)
            final = await self._get_order_final_state("option", symbol, rested_ord_id)
            final_cum = float(final.get("cumExecQty", 0) or 0)
            final_avg = float(final.get("avgPrice", rested_price) or rested_price)
            delta = max(0.0, final_cum - rested_credited_qty)
            if delta > 0:
                weighted_revenue += final_avg * delta
                total_filled += delta
                remaining_qty = round(remaining_qty - delta, 5)
                log.info(
                    "chase_sell_exit_partial_credit",
                    symbol=symbol, attempt=attempt,
                    delta=delta, total_filled=total_filled,
                )
            rested_ord_id = ""
            rested_price = 0.0
            rested_credited_qty = 0.0

        # ── Deadline expired ──
        if total_filled > 0:
            avg_price = weighted_revenue / total_filled
            t_filled = _time.time()
            metrics = _build_fill_metrics(
                side="sell", instrument=symbol,
                qty_btc=total_filled, fill_price=avg_price,
                t_started=t_started, t_filled=t_filled,
                attempts=attempt,
                ref_bid=ref_bid, ref_ask=ref_ask, ref_mark=ref_mark,
            )
            log.warning(
                "chase_sell_partial_at_deadline",
                symbol=symbol,
                total_filled=total_filled,
                remaining=remaining_qty,
                attempts=attempt,
            )
            return {
                "orderId": last_ord_id or "partial",
                "orderStatus": "PartiallyFilled",
                "avgPrice": str(avg_price),
                "cumExecQty": str(total_filled),
                "metrics": metrics,
            }

        log.warning(
            "chase_sell_deadline_expired",
            symbol=symbol,
            attempts=attempt,
            deadline_sec=config.OPTION_EXIT_CHASE_DEADLINE_SEC,
        )
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
