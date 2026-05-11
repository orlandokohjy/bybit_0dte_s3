"""Telegram notification helper.

Two channels:
  - Ops chat (TELEGRAM_CHAT_ID): startup, pre-flight, entry, close, errors
  - Report chat (TELEGRAM_REPORT_CHAT_ID): daily / weekly reports

Multi-session: entry/close messages accept an optional session_label
(e.g. '13:30-15:30 UTC') which is prefixed in the header so you can
distinguish afternoon vs morning at a glance.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

import config

log = structlog.get_logger(__name__)


async def _send_to(bot_token: str, chat_id: str, text: str) -> None:
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


def _label_suffix(session_label: str) -> str:
    return f" [{session_label}]" if session_label else ""


async def notify_entry(
    num_straddles: int, equity: float, straddle_cost: float,
    spot_fill: float, strike: float, put_premium: float,
    spot_margin_used: float, put_cost_total: float,
    qty_per_leg: float | None = None,
    session_label: str = "",
) -> None:
    qty_line = (
        f"BTC per leg: {qty_per_leg:.4f}\n" if qty_per_leg is not None else ""
    )
    await send(
        f"<b>SESSION ENTRY</b>{_label_suffix(session_label)}\n"
        f"Straddles: {num_straddles}\n"
        f"{qty_line}"
        f"Equity: ${equity:,.2f}\n"
        f"\n<b>Fills</b>\n"
        f"Spot: ${spot_fill:,.2f}\n"
        f"Put strike: ${strike:,.0f}\n"
        f"Put premium (avg): ${put_premium:,.2f}\n"
        f"\n<b>Capital used</b>\n"
        f"Spot margin: ${spot_margin_used:,.2f}\n"
        f"Put cost: ${put_cost_total:,.2f}\n"
        f"Total: ${spot_margin_used + put_cost_total:,.2f}\n"
    )


async def notify_close(
    pnl: float, exit_reason: str, session_label: str = "",
) -> None:
    pnl_sign = "+" if pnl >= 0 else ""
    await send(
        f"<b>SESSION CLOSE</b>{_label_suffix(session_label)}\n"
        f"P&L: {pnl_sign}${pnl:,.2f}\n"
    )


async def notify_skip(reason: str, session_label: str = "") -> None:
    await send(f"<b>SKIPPED</b>{_label_suffix(session_label)}\n{reason}")


async def notify_error(context: str, message: str) -> None:
    await send(f"<b>ERROR</b> [{context}]\n{message}")


async def send_daily_report(equity: float) -> None:
    """Generate and send the FULL daily report (no risk metrics / no edge)
    to the report group chat.

    Chained off the morning session's close handler in main._on_close.
    """
    from reporting.daily_report import compute_report, format_telegram_report
    try:
        metrics = compute_report(equity)
        if metrics is None:
            log.info("daily_report_skipped", reason="no trades for trading day")
            return
        await send_report(format_telegram_report(metrics))
        log.info("daily_report_sent", trades=metrics.total_trades,
                 sharpe=f"{metrics.sharpe_ratio:.2f}")
    except Exception:
        log.warning("daily_report_failed", exc_info=True)


async def send_weekly_report(equity: float) -> None:
    """Generate and send the weekly report. Chained off Saturday morning close."""
    from reporting.daily_report import compute_weekly_report, format_weekly_report
    try:
        metrics = compute_weekly_report(equity)
        if metrics is None:
            log.info("weekly_report_skipped", reason="no trades this week")
            return
        await send_report(format_weekly_report(metrics))
        log.info("weekly_report_sent", trades=metrics.total_trades,
                 pnl=f"${metrics.trade_pnl:,.2f}")
    except Exception:
        log.warning("weekly_report_failed", exc_info=True)
