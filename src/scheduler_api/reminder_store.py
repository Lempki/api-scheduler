import uuid
from datetime import UTC, datetime
from typing import Any

from . import database, scheduler
from .models import CreateReminderRequest, ReminderListResponse, ReminderResponse


def _to_response(row: dict[str, Any]) -> ReminderResponse:
    return ReminderResponse(
        reminder_id=row["reminder_id"],
        fire_at=row["fire_at"],
        channel_id=row["channel_id"],
        guild_id=row["guild_id"],
        payload=row["payload"],
        webhook_url=row.get("webhook_url"),
        bot_callback_url=row.get("bot_callback_url"),
        status=row["status"],
        retry_count=row["retry_count"],
        created_at=row["created_at"],
    )


async def create(req: CreateReminderRequest) -> ReminderResponse:
    reminder_id = str(uuid.uuid4())
    row = {
        "reminder_id": reminder_id,
        "fire_at": req.fire_at.isoformat(),
        "channel_id": req.channel_id,
        "guild_id": req.guild_id,
        "payload": req.payload,
        "webhook_url": req.webhook_url,
        "bot_callback_url": req.bot_callback_url,
        "status": "scheduled",
        "retry_count": 0,
        "created_at": datetime.now(UTC).isoformat(),
    }
    await database.insert_reminder(row)
    scheduler.schedule_reminder(row)
    return _to_response(row)


async def get(reminder_id: str) -> ReminderResponse | None:
    row = await database.get_reminder(reminder_id)
    return _to_response(row) if row else None


async def cancel(reminder_id: str) -> bool:
    row = await database.get_reminder(reminder_id)
    if row is None or row["status"] != "scheduled":
        return False
    scheduler.cancel_reminder(reminder_id)
    await database.update_status(reminder_id, "cancelled")
    return True


async def list_reminders(
    guild_id: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> ReminderListResponse:
    rows, total = await database.list_reminders(guild_id, status, limit, offset)
    return ReminderListResponse(
        reminders=[_to_response(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
