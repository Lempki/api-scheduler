"""APScheduler 3 wrapper. Restores all scheduled reminders from SQLite on startup."""

import logging
from datetime import UTC, datetime
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from . import database, dispatcher
from .config import Settings

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_settings: Settings | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Returns the running scheduler.

    Raises:
        RuntimeError: When start() has not been called.
    """
    if _scheduler is None:
        raise RuntimeError("The scheduler is not started. Call start() first.")
    return _scheduler


def is_running() -> bool:
    """Reports whether the scheduler has been started and is still running."""
    return _scheduler is not None and bool(_scheduler.running)


async def start(settings: Settings) -> None:
    """Starts the scheduler and restores every reminder that is still scheduled.

    An overdue reminder follows settings.missed_reminder_policy.
    With "fire" it is delivered at once, and with "fail" it is marked failed.

    Args:
        settings: The service settings.
    """
    global _scheduler, _settings
    _settings = settings
    _scheduler = AsyncIOScheduler(timezone=UTC)
    _scheduler.start()

    now = datetime.now(UTC)
    pending = await database.get_scheduled_reminders()
    overdue = 0
    for reminder in pending:
        if _fire_time(reminder) <= now:
            overdue += 1
            if settings.missed_reminder_policy == "fail":
                logger.warning(
                    "Reminder %s came due at %s during downtime and is marked failed.",
                    reminder["reminder_id"],
                    reminder["fire_at"],
                )
                await database.update_status(reminder["reminder_id"], "failed")
                continue
            logger.info(
                "Reminder %s came due at %s during downtime and fires now.",
                reminder["reminder_id"],
                reminder["fire_at"],
            )
        _schedule_job(reminder)

    logger.info(
        "The scheduler started with %d scheduled reminders, of which %d were overdue.",
        len(pending),
        overdue,
    )


def stop() -> None:
    """Shuts the scheduler down without waiting for running deliveries."""
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)


def _fire_time(reminder: dict[str, Any]) -> datetime:
    """Returns a reminder's fire time as an aware UTC datetime."""
    fire_at = reminder["fire_at"]
    if isinstance(fire_at, str):
        fire_at = datetime.fromisoformat(fire_at)
    if not isinstance(fire_at, datetime):
        raise TypeError(
            f"fire_at must be a datetime or an ISO string, not {fire_at!r}."
        )
    return fire_at.astimezone(UTC)


def _schedule_job(reminder: dict[str, Any]) -> None:
    """Adds a one-shot job that delivers the reminder at its fire time.

    A fire time in the past runs the job at once.
    misfire_grace_time is None, so a busy event loop never skips a due job.
    """
    get_scheduler().add_job(
        _fire_wrapper,
        trigger=DateTrigger(run_date=_fire_time(reminder)),
        args=[reminder],
        id=reminder["reminder_id"],
        replace_existing=True,
        misfire_grace_time=None,
    )


def schedule_reminder(reminder: dict[str, Any]) -> None:
    """Schedules a newly created reminder."""
    _schedule_job(reminder)


def cancel_reminder(reminder_id: str) -> bool:
    """Removes a reminder's job.

    Returns:
        True when a job was removed, False when none existed.
    """
    try:
        get_scheduler().remove_job(reminder_id)
    except JobLookupError:
        return False
    return True


def pending_count() -> int:
    """Returns the number of jobs waiting to run, or 0 before the scheduler starts."""
    if _scheduler is None:
        return 0
    return len(_scheduler.get_jobs())


async def _fire_wrapper(reminder: dict[str, Any]) -> None:
    """Runs the dispatcher for one reminder with the settings from start()."""
    if _settings is None:
        raise RuntimeError("The scheduler is not started. Call start() first.")
    await dispatcher.fire(reminder, _settings)
