"""Fires reminders by POSTing to webhook_url or bot_callback_url with retry."""

import asyncio
import logging
from typing import Any

import httpx

from . import database

logger = logging.getLogger(__name__)

# The delays between attempts, in seconds, are 30 seconds, 2 minutes, and 10 minutes.
_RETRY_DELAYS = [30, 120, 600]


async def fire(reminder: dict[str, Any], max_retries: int) -> None:
    reminder_id = reminder["reminder_id"]
    destinations = [
        d for d in [reminder.get("webhook_url"), reminder.get("bot_callback_url")] if d
    ]

    payload = {
        "reminder_id": reminder_id,
        "fired_at": reminder["fire_at"],
        "channel_id": reminder["channel_id"],
        "guild_id": reminder["guild_id"],
        "payload": reminder["payload"],
    }

    retry_count = 0
    while retry_count <= max_retries:
        success = await _try_dispatch(destinations, payload, reminder_id)
        if success:
            await database.update_status(reminder_id, "fired")
            return

        retry_count += 1
        await database.update_status(reminder_id, "scheduled", retry_count=retry_count)

        if retry_count <= max_retries:
            delay = _RETRY_DELAYS[min(retry_count - 1, len(_RETRY_DELAYS) - 1)]
            logger.warning(
                "reminder %s dispatch failed (attempt %d/%d); retrying in %ds",
                reminder_id,
                retry_count,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)

    logger.error("reminder %s exhausted all retries — marking failed", reminder_id)
    await database.update_status(reminder_id, "failed")


async def _try_dispatch(
    destinations: list[str], payload: dict[str, Any], reminder_id: str
) -> bool:
    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in destinations:
            try:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                logger.info("reminder %s delivered to %s", reminder_id, url)
                return True
            except Exception as exc:
                logger.warning(
                    "reminder %s delivery to %s failed: %s", reminder_id, url, exc
                )
    return False
