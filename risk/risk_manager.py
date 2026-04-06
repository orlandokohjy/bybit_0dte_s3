"""Pre-trade risk checks: daily loss limit, API circuit breaker."""
from __future__ import annotations

from dataclasses import dataclass

import structlog

import config
from core.portfolio import Portfolio

log = structlog.get_logger(__name__)


@dataclass
class RiskVerdict:
    allowed: bool
    reason: str = ""


class RiskManager:
    def __init__(self, portfolio: Portfolio) -> None:
        self._portfolio = portfolio

    def check_daily_loss(self) -> RiskVerdict:
        """Block trading if daily loss exceeds MAX_DAILY_LOSS_PCT of equity."""
        equity = self._portfolio.equity
        if equity <= 0:
            return RiskVerdict(False, "Zero equity")
        loss_pct = abs(min(0, self._portfolio.daily_pnl)) / equity
        if loss_pct >= config.MAX_DAILY_LOSS_PCT:
            return RiskVerdict(
                False,
                f"Daily loss {loss_pct:.1%} >= {config.MAX_DAILY_LOSS_PCT:.0%} limit",
            )
        return RiskVerdict(True)

    def check_api_health(self, error_count: int) -> RiskVerdict:
        if error_count >= config.CIRCUIT_BREAKER_API_ERRORS:
            return RiskVerdict(
                False,
                f"Circuit breaker: {error_count} consecutive API errors",
            )
        return RiskVerdict(True)

    def check_entry(self, num_straddles: int, straddle_cost: float) -> RiskVerdict:
        if num_straddles <= 0:
            return RiskVerdict(False, "Zero straddles — insufficient capital")
        total = num_straddles * straddle_cost
        if total > self._portfolio.equity:
            return RiskVerdict(False, f"Total cost ${total:,.0f} > equity ${self._portfolio.equity:,.0f}")
        return RiskVerdict(True)
