"""The SQLite schema and async query helpers."""

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

_db_path: str = "/data/scheduler.db"
_conn: aiosqlite.Connection | None = None


async def init(db_path: str) -> None:
    """Opens the database connection and creates the reminders table when it is missing.

    Args:
        db_path: The path to the SQLite database file, or ":memory:" for an in-memory database.
    """
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
    await _normalize_fire_times(_conn)
    await _conn.commit()


async def _normalize_fire_times(db: aiosqlite.Connection) -> int:
    """Rewrites every stored fire_at in UTC, so ORDER BY fire_at sorts by time.

    Reminders created before fire_at was stored in UTC kept the caller's offset.
    Sorted as text, such times come out in the wrong order.
    The rewrite is idempotent, so it runs at every start.

    Args:
        db: The open connection.

    Returns:
        How many rows were rewritten.
    """
    async with db.execute("SELECT reminder_id, fire_at FROM reminders") as cur:
        rows = await cur.fetchall()
    updates = []
    for reminder_id, fire_at in rows:
        normalized = datetime.fromisoformat(fire_at).astimezone(UTC).isoformat()
        if normalized != fire_at:
            updates.append((normalized, reminder_id))
    await db.executemany(
        "UPDATE reminders SET fire_at = ? WHERE reminder_id = ?", updates
    )
    return len(updates)


async def close() -> None:
    """Closes the database connection, if one is open."""
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
    """Stores a new reminder row.

    Args:
        reminder: The reminder fields. The payload is stored as JSON text.
    """
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
    """Returns one reminder as a dict, or None when no reminder has this ID.

    Args:
        reminder_id: The reminder to look up.
    """
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
    """Returns one page of reminders, ordered by fire time, and the total count of matches.

    Args:
        guild_id: Only reminders for this guild, or None for every guild.
        status: Only reminders with this status, or None for every status.
        limit: The largest number of reminders to return.
        offset: The number of matching reminders to skip.

    Returns:
        The reminders on the page and the number of reminders that match the filters.
    """
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
    """Returns every reminder whose status is still scheduled."""
    db = _get_conn()
    async with db.execute("SELECT * FROM reminders WHERE status = 'scheduled'") as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    """Converts a row to a dict and decodes its payload.

    The callback_url column is returned as bot_callback_url, the name the API uses.
    """
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    d["bot_callback_url"] = d.pop("callback_url", None)
    return d
