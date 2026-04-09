"""Telegram notification helper.

Two channels:
  - Ops chat (TELEGRAM_CHAT_ID): startup, pre-flight, entry, close, errors
  - Report chat (TELEGRAM_REPORT_CHAT_ID): slim daily report only
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

import config

log = structlog.get_logger(__name__)

_BASE_URL: Optional[str] = None


def _url() -> str | None:
    global _BASE_URL
    if _BASE_URL is None and config.TELEGRAM_ENABLED:
        _BASE_URL = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
    return _BASE_URL


async def _send_to(chat_id: str, text: str) -> None:
    """Send a message to a specific chat ID."""
    if not config.TELEGRAM_ENABLED or not chat_id:
        log.debug("telegram_disabled", chat_id=chat_id, msg=text[:80])
        return
    try:
        import aiohttp
        url = f"{_url()}/sendMessage"
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            })
    except Exception:
        log.warning("telegram_send_failed", chat_id=chat_id, exc_info=True)


async def send(text: str) -> None:
    """Send to the ops/testing chat."""
    await _send_to(config.TELEGRAM_CHAT_ID, text)


async def send_report(text: str) -> None:
    """Send to the report group chat. Falls back to ops chat if not configured."""
    chat_id = config.TELEGRAM_REPORT_CHAT_ID or config.TELEGRAM_CHAT_ID
    await _send_to(chat_id, text)


async def notify_entry(
    num_straddles: int, equity: float, straddle_cost: float, spot: float, strike: float,
) -> None:
    await send(
        f"<b>SESSION ENTRY</b>\n"
        f"Straddles: {num_straddles}\n"
        f"Equity: ${equity:,.2f}\n"
        f"Straddle cost: ${straddle_cost:,.2f}\n"
        f"Spot: ${spot:,.2f}\n"
        f"Put strike: ${strike:,.0f}\n"
    )


async def notify_close(pnl: float, exit_reason: str) -> None:
    await send(
        f"<b>SESSION CLOSE</b> ({exit_reason})\n"
        f"P&L: ${pnl:,.2f}\n"
    )


async def notify_skip(reason: str) -> None:
    await send(f"<b>SKIPPED</b>\n{reason}")


async def notify_error(context: str, message: str) -> None:
    await send(f"<b>ERROR</b> [{context}]\n{message}")


async def notify_daily_summary(equity: float, daily_pnl: float, cum_return: float) -> None:
    await send(
        f"<b>DAILY SUMMARY</b>\n"
        f"Equity: ${equity:,.2f}\n"
        f"Today P&L: ${daily_pnl:,.2f}\n"
        f"Cumulative return: {cum_return:.1%}\n"
    )


async def send_daily_report(equity: float) -> None:
    """Generate and send the slim Trade Summary to the report group chat."""
    from reporting.daily_report import compute_report, format_telegram_summary
    try:
        metrics = compute_report(equity)
        if metrics is None:
            log.info("daily_report_skipped", reason="no trades in log")
            return
        await send_report(format_telegram_summary(metrics))
        log.info("daily_report_sent", trades=metrics.total_trades, sharpe=f"{metrics.sharpe_ratio:.2f}")
    except Exception:
        log.warning("daily_report_failed", exc_info=True)
