"""APScheduler v3 wrapper. Restores all scheduled reminders from SQLite on startup."""

import logging
from datetime import UTC

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from . import database, dispatcher

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_max_retries: int = 3


def get_scheduler() -> AsyncIOScheduler:
    assert _scheduler is not None, "scheduler not started"
    return _scheduler


async def start(max_retries: int) -> None:
    global _scheduler, _max_retries
    _max_retries = max_retries
    _scheduler = AsyncIOScheduler(timezone=UTC)
    _scheduler.start()

    pending = await database.get_scheduled_reminders()
    for reminder in pending:
        _schedule_job(reminder)

    logger.info("scheduler started; restored %d pending reminders", len(pending))


def stop() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


def _schedule_job(reminder: dict) -> None:
    from dateutil.parser import parse as parse_dt

    fire_at = reminder["fire_at"]
    if isinstance(fire_at, str):
        fire_at = parse_dt(fire_at)

    fire_at = fire_at.astimezone(UTC)

    _scheduler.add_job(  # type: ignore[union-attr]
        _fire_wrapper,
        trigger=DateTrigger(run_date=fire_at),
        args=[reminder, _max_retries],
        id=reminder["reminder_id"],
        replace_existing=True,
        misfire_grace_time=300,
    )


def schedule_reminder(reminder: dict) -> None:
    _schedule_job(reminder)


def cancel_reminder(reminder_id: str) -> bool:
    try:
        _scheduler.remove_job(reminder_id)  # type: ignore[union-attr]
        return True
    except Exception:
        return False


def pending_count() -> int:
    if _scheduler is None:
        return 0
    return len(_scheduler.get_jobs())


async def _fire_wrapper(reminder: dict, max_retries: int) -> None:
    await dispatcher.fire(reminder, max_retries)
