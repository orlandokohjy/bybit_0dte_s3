"""0DTE option chain: fetch, filter, and cache ITM puts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import structlog

import config
from core.exchange import BybitExchange
from utils.time_utils import now_utc, today_expiry_date_str

log = structlog.get_logger(__name__)


@dataclass
class OptionInfo:
    symbol: str
    strike: float
    option_type: str
    expiry_str: str
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    mark: float = 0.0
    last: float = 0.0
    iv: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    open_interest: float = 0.0
    volume_24h: float = 0.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid if self.ask > 0 and self.bid > 0 else float("inf")

    @property
    def spread_pct(self) -> float:
        if self.mid > 0:
            return self.spread / self.mid
        return float("inf")


class OptionChain:
    def __init__(self, exchange: BybitExchange) -> None:
        self._exchange = exchange
        self._puts: dict[float, OptionInfo] = {}
        self._calls: dict[float, OptionInfo] = {}
        self._last_refresh: Optional[datetime] = None
        self._exp_date_str: str = ""

    async def refresh(self) -> int:
        self._exp_date_str = today_expiry_date_str()
        tickers = await self._exchange.get_option_tickers_rest(exp_date=self._exp_date_str)

        self._puts.clear()
        self._calls.clear()

        for t in tickers:
            symbol: str = t.get("symbol", "")
            parts = symbol.split("-")
            if len(parts) < 4:
                continue

            # Only use USDT-settled options (5-part symbols ending in "USDT")
            # so that premium and P&L are in USDT, matching the perp leg.
            is_usdt_settled = len(parts) == 5 and parts[4] == "USDT"
            if not is_usdt_settled:
                continue

            strike = float(parts[2])
            opt_type = parts[3]

            info = OptionInfo(
                symbol=symbol,
                strike=strike,
                option_type=opt_type,
                expiry_str=self._exp_date_str,
                bid=float(t.get("bid1Price", 0)),
                ask=float(t.get("ask1Price", 0)),
                mark=float(t.get("markPrice", 0)),
                last=float(t.get("lastPrice", 0)),
                iv=float(t.get("markIv", 0)),
                delta=float(t.get("delta", 0)),
                gamma=float(t.get("gamma", 0)),
                theta=float(t.get("theta", 0)),
                vega=float(t.get("vega", 0)),
                open_interest=float(t.get("openInterest", 0)),
                volume_24h=float(t.get("volume24h", 0)),
            )
            info.mid = (info.bid + info.ask) / 2 if info.bid > 0 and info.ask > 0 else info.mark

            if opt_type == "P":
                self._puts[strike] = info
            else:
                self._calls[strike] = info

        self._last_refresh = now_utc()
        log.info("chain_refreshed", expiry=self._exp_date_str,
                 puts=len(self._puts), calls=len(self._calls))
        return len(self._puts)

    def get_nearest_itm_put(self, spot: float) -> Optional[OptionInfo]:
        """Closest ITM put: lowest strike that is still above spot."""
        itm = sorted(
            [p for p in self._puts.values() if p.strike > spot],
            key=lambda p: p.strike,
        )
        return itm[0] if itm else None

    def get_put(self, strike: float) -> Optional[OptionInfo]:
        return self._puts.get(strike)

    @property
    def all_puts(self) -> list[OptionInfo]:
        return sorted(self._puts.values(), key=lambda p: p.strike)

    @property
    def expiry_date(self) -> str:
        return self._exp_date_str

    @property
    def stale(self) -> bool:
        if self._last_refresh is None:
            return True
        return (now_utc() - self._last_refresh).total_seconds() > 60
