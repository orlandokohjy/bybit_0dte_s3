"""APScheduler wrapper — multi-session entry/close per config.SESSIONS.

Reports are NOT scheduled here; they are chained off the LAST_CLOSE
session's `on_close` handler in `main._on_close`. This guarantees the
daily report fires immediately after the morning session closes
(currently 02:00 UTC).
"""
from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import structlog

import config
from utils.time_utils import UTC

log = structlog.get_logger(__name__)

SessionHandler = Callable[[config.Session], Awaitable[None]]

_WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _weekday_str(weekdays: frozenset[int]) -> str:
    return ",".join(_WEEKDAY_NAMES[d] for d in sorted(weekdays))


class Scheduler:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=UTC)

    def register_session(
        self,
        on_entry: SessionHandler,
        on_close: SessionHandler,
    ) -> None:
        """Register entry+close cron jobs for each Session in config.SESSIONS.

        Each handler receives the firing Session as its only positional argument.
        """
        for session in config.SESSIONS:
            days = _weekday_str(session.weekdays)
            entry_t = session.entry_utc
            close_t = session.close_utc

            self._scheduler.add_job(
                on_entry,
                CronTrigger(
                    hour=entry_t.hour, minute=entry_t.minute,
                    day_of_week=days, timezone=UTC,
                ),
                id=f"session_entry_{session.name}",
                name=(
                    f"Session Entry [{session.name}] "
                    f"({entry_t.hour:02d}:{entry_t.minute:02d} UTC)"
                ),
                args=[session],
                replace_existing=True,
            )

            self._scheduler.add_job(
                on_close,
                CronTrigger(
                    hour=close_t.hour, minute=close_t.minute,
                    day_of_week=days, timezone=UTC,
                ),
                id=f"session_close_{session.name}",
                name=(
                    f"Session Close [{session.name}] "
                    f"({close_t.hour:02d}:{close_t.minute:02d} UTC)"
                ),
                args=[session],
                replace_existing=True,
            )

            log.info(
                "session_scheduled",
                name=session.name,
                entry=f"{entry_t.hour:02d}:{entry_t.minute:02d} UTC",
                close=f"{close_t.hour:02d}:{close_t.minute:02d} UTC",
                qty_per_leg=session.qty_per_leg,
                days=days,
                trading_day_offset_days=session.trading_day_offset_days,
            )

        log.info(
            "all_sessions_scheduled",
            sessions=[s.name for s in config.SESSIONS],
            last_close_session=config.LAST_CLOSE_SESSION_NAME,
            reports="chained off morning close (see main._on_close)",
        )

    def start(self) -> None:
        self._scheduler.start()
        log.info("scheduler_started", jobs=len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")

    def get_next_fire_times(self) -> dict[str, datetime | None]:
        return {
            job.id: job.next_run_time
            for job in self._scheduler.get_jobs()
        }
