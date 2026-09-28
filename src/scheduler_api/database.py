"""The SQLite schema and async query helpers."""

import json
from typing import Any

import aiosqlite

_db_path: str = "/data/scheduler.db"
_conn: aiosqlite.Connection | None = None


async def init(db_path: str) -> None:
    global _db_path, _conn
    _db_path = db_path
    _conn = await aiosqlite.connect(db_path)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            reminder_id   TEXT PRIMARY KEY,
            fire_at       TEXT NOT NULL,
            channel_id    TEXT NOT NULL,
            guild_id      TEXT NOT NULL,
            payload       TEXT NOT NULL DEFAULT '{}',
            webhook_url   TEXT,
            callback_url  TEXT,
            status        TEXT NOT NULL DEFAULT 'scheduled',
            retry_count   INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        )
        """
    )
    await _conn.commit()


async def close() -> None:
    global _conn
    if _conn:
        await _conn.close()
        _conn = None


def _get_conn() -> aiosqlite.Connection:
    """Returns the open connection.

    Raises:
        RuntimeError: When init() has not been called.
    """
    if _conn is None:
        raise RuntimeError("The database is not initialized. Call init() first.")
    return _conn


async def insert_reminder(reminder: dict[str, Any]) -> None:
    db = _get_conn()
    await db.execute(
        """
        INSERT INTO reminders
            (reminder_id, fire_at, channel_id, guild_id, payload,
             webhook_url, callback_url, status, retry_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reminder["reminder_id"],
            reminder["fire_at"],
            reminder["channel_id"],
            reminder["guild_id"],
            json.dumps(reminder["payload"]),
            reminder.get("webhook_url"),
            reminder.get("bot_callback_url"),
            reminder["status"],
            reminder["retry_count"],
            reminder["created_at"],
        ),
    )
    await db.commit()


async def get_reminder(reminder_id: str) -> dict[str, Any] | None:
    db = _get_conn()
    async with db.execute(
        "SELECT * FROM reminders WHERE reminder_id = ?", (reminder_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def update_status(
    reminder_id: str,
    status: str,
    retry_count: int | None = None,
    only_if: str | None = None,
) -> bool:
    """Sets a reminder's status and, optionally, its retry count.

    Args:
        reminder_id: The reminder to update.
        status: The new status.
        retry_count: The new retry count, or None to leave it unchanged.
        only_if: When set, the row changes only while its status still equals this value.

    Returns:
        True when a row changed, False when none matched.
    """
    db = _get_conn()
    assignments = "status = ?"
    params: list[Any] = [status]
    if retry_count is not None:
        assignments += ", retry_count = ?"
        params.append(retry_count)
    condition = "reminder_id = ?"
    params.append(reminder_id)
    if only_if is not None:
        condition += " AND status = ?"
        params.append(only_if)
    cursor = await db.execute(
        f"UPDATE reminders SET {assignments} WHERE {condition}", params
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_reminders(
    guild_id: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    db = _get_conn()
    conditions = []
    params: list[Any] = []
    if guild_id:
        conditions.append("guild_id = ?")
        params.append(guild_id)
    if status:
        conditions.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with db.execute(f"SELECT COUNT(*) FROM reminders {where}", params) as cur:
        # COUNT(*) always returns exactly one row.
        count_row = await cur.fetchone()
    total: int = count_row[0] if count_row is not None else 0

    async with db.execute(
        f"SELECT * FROM reminders {where} ORDER BY fire_at ASC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ) as cur:
        rows = await cur.fetchall()

    return [_row_to_dict(r) for r in rows], total


async def get_scheduled_reminders() -> list[dict[str, Any]]:
    db = _get_conn()
    async with db.execute("SELECT * FROM reminders WHERE status = 'scheduled'") as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    d["bot_callback_url"] = d.pop("callback_url", None)
    return d
