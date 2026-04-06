"""UTC time helpers and 0DTE expiry-date logic."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def today_expiry_date_str() -> str:
    """
    Return the Bybit-formatted expiry string for today's 0DTE options.

    Deribit/Bybit options settle at 08:00 UTC.  Before 08:00 UTC the
    0DTE expiry is *today*; after 08:00 UTC the 0DTE is *tomorrow*.
    """
    now = now_utc()
    if now.hour < 8:
        exp = now.date()
    else:
        exp = (now + timedelta(days=1)).date()
    return exp.strftime("%d%b%y").upper()     # e.g. 18MAR26


def format_utc_sgt(dt: datetime) -> str:
    sgt = dt.astimezone(timezone(timedelta(hours=8)))
    return sgt.strftime("%Y-%m-%d %H:%M SGT")


def is_weekday() -> bool:
    return now_utc().weekday() < 5
