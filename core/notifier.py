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

async def _send_to(bot_token: str, chat_id: str, text: str) -> None:
    """Send a message using a specific bot token and chat ID."""
    if not bot_token or not chat_id:
        log.debug("telegram_disabled", chat_id=chat_id, msg=text[:80])
        return
    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            })
    except Exception:
        log.warning("telegram_send_failed", chat_id=chat_id, exc_info=True)


async def send(text: str) -> None:
    """Send to the ops/testing chat (personal bot)."""
    await _send_to(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, text)


async def send_report(text: str) -> None:
    """Send to the report group chat (group bot). Falls back to ops if not configured."""
    bot = config.TELEGRAM_REPORT_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
    chat = config.TELEGRAM_REPORT_CHAT_ID or config.TELEGRAM_CHAT_ID
    await _send_to(bot, chat, text)


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
    pnl_sign = "+" if pnl >= 0 else ""
    await send(
        f"<b>SESSION CLOSE</b>\n"
        f"P&L: {pnl_sign}${pnl:,.2f}\n"
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


async def send_weekly_report(equity: float) -> None:
    """Generate and send the weekly report to the report group chat."""
    from reporting.daily_report import compute_weekly_report, format_weekly_report
    try:
        metrics = compute_weekly_report(equity)
        if metrics is None:
            log.info("weekly_report_skipped", reason="no trades this week")
            return
        await send_report(format_weekly_report(metrics))
        log.info("weekly_report_sent", trades=metrics.total_trades, pnl=f"${metrics.trade_pnl:,.2f}")
    except Exception:
        log.warning("weekly_report_failed", exc_info=True)
