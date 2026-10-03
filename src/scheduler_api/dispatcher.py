"""Delivers reminders to a Discord webhook and to a bot callback, with retries."""

import asyncio
import enum
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from . import database
from .config import Settings

__all__ = ["close_client", "fire", "get_client", "sign_callback"]

logger = logging.getLogger(__name__)

# The delays between attempts, in seconds, are 30 seconds, 2 minutes, and 10 minutes.
_RETRY_DELAYS = [30, 120, 600]
# A 429 answer waits and resends without using up a retry, but only this many times per send.
_MAX_RATE_LIMIT_WAITS = 3
_MAX_RATE_LIMIT_DELAY = 60.0
_DEFAULT_RATE_LIMIT_DELAY = 1.0
_LOGGED_BODY_CHARS = 300
_TIMEOUT = httpx.Timeout(10.0)

_client: httpx.AsyncClient | None = None


class _Outcome(enum.Enum):
    """How one send to one destination ended."""

    DELIVERED = "delivered"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    STOPPED = "stopped"


@dataclass
class _Destination:
    """One configured destination and whether it has already been delivered."""

    kind: str
    url: str
    delivered: bool = False


def get_client() -> httpx.AsyncClient:
    """Returns the shared HTTP client, creating it on first use.

    Returns:
        The client that every delivery uses for the life of the service.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


async def close_client() -> None:
    """Closes the shared HTTP client, if one was created."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def sign_callback(secret: str, timestamp: str, body: bytes) -> str:
    """Computes the signature that a bot callback carries in X-Signature-SHA256.

    Args:
        secret: The shared API_SECRET.
        timestamp: The Unix time in seconds, as sent in X-Signature-Timestamp.
        body: The exact request body bytes.

    Returns:
        The hex HMAC-SHA256 of the timestamp, a dot, and the body.
    """
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


async def fire(reminder: dict[str, Any], settings: Settings) -> None:
    """Delivers a reminder to every configured destination and records the result.

    The rule is simple.
    A reminder is fired when every configured destination has answered with a 2xx status.
    A destination that succeeded is not sent again on a later retry.
    A 429 answer waits for the time Discord asks for and resends without using up a retry.
    Any other 4xx answer is permanent, so the reminder is marked failed at once.
    A 5xx answer or a network error is transient and retries the undelivered destinations.
    The retries follow the delays in _RETRY_DELAYS, up to settings.dispatcher_max_retries.
    The dispatcher changes only a reminder that is still scheduled.
    It re-reads the status before every send, so a reminder cancelled during a wait is not sent.
    Delivery is at least once, because the delivered destinations are tracked only in memory.
    A restart during retries can therefore resend a destination that already succeeded.

    Args:
        reminder: The stored reminder row.
        settings: The service settings, which hold the retry limit and the signing secret.
    """
    reminder_id = reminder["reminder_id"]
    destinations = [
        _Destination(kind, url)
        for kind, url in (
            ("webhook", reminder.get("webhook_url")),
            ("bot callback", reminder.get("bot_callback_url")),
        )
        if url
    ]
    max_retries = settings.dispatcher_max_retries
    retry_count = 0
    while True:
        permanent = False
        for destination in destinations:
            if destination.delivered:
                continue
            outcome = await _send(destination, reminder, settings)
            if outcome is _Outcome.STOPPED:
                return
            if outcome is _Outcome.DELIVERED:
                destination.delivered = True
            elif outcome is _Outcome.PERMANENT:
                permanent = True

        if all(destination.delivered for destination in destinations):
            await _finish(reminder_id, "fired")
            return
        if permanent:
            logger.error(
                "Reminder %s failed permanently and will not be retried.", reminder_id
            )
            await _finish(reminder_id, "failed")
            return
        if retry_count >= max_retries:
            logger.error(
                "Reminder %s used all %d retries and is marked failed.",
                reminder_id,
                max_retries,
            )
            await _finish(reminder_id, "failed")
            return

        retry_count += 1
        if not await database.update_status(
            reminder_id, "scheduled", retry_count=retry_count, only_if="scheduled"
        ):
            await _log_stop(reminder_id)
            return
        delay = _RETRY_DELAYS[min(retry_count - 1, len(_RETRY_DELAYS) - 1)]
        logger.warning(
            "Reminder %s delivery failed. Retry %d of %d starts in %d seconds.",
            reminder_id,
            retry_count,
            max_retries,
            delay,
        )
        await asyncio.sleep(delay)


async def _is_scheduled(reminder_id: str) -> bool:
    """Reports whether the stored reminder still has the status scheduled."""
    row = await database.get_reminder(reminder_id)
    return row is not None and row["status"] == "scheduled"


async def _log_stop(reminder_id: str) -> None:
    """Logs that delivery stops because the reminder left the scheduled status."""
    row = await database.get_reminder(reminder_id)
    status = row["status"] if row is not None else "deleted"
    logger.info("Reminder %s is now %s, so its delivery stops.", reminder_id, status)


async def _finish(reminder_id: str, status: str) -> None:
    """Moves a reminder to its final status, unless it already left scheduled."""
    if not await database.update_status(reminder_id, status, only_if="scheduled"):
        await _log_stop(reminder_id)


async def _send(
    destination: _Destination, reminder: dict[str, Any], settings: Settings
) -> _Outcome:
    """Sends a reminder to one destination and waits out rate limits.

    Args:
        destination: The webhook or bot callback to send to.
        reminder: The stored reminder row.
        settings: The service settings, which hold the signing secret.

    Returns:
        Whether the send was delivered, failed transiently, or failed permanently.
        STOPPED means the reminder left the scheduled status, so nothing was sent.
    """
    reminder_id = reminder["reminder_id"]
    rate_limit_waits = 0
    while True:
        if not await _is_scheduled(reminder_id):
            await _log_stop(reminder_id)
            return _Outcome.STOPPED
        try:
            if destination.kind == "webhook":
                response = await _post_webhook(destination.url, reminder["payload"])
            else:
                secret = settings.api_secret.get_secret_value()
                response = await _post_callback(destination.url, reminder, secret)
        except httpx.HTTPError as exc:
            logger.warning(
                "Reminder %s %s delivery hit a network error of type %s.",
                reminder_id,
                destination.kind,
                type(exc).__name__,
            )
            return _Outcome.TRANSIENT

        status = response.status_code
        if 200 <= status < 300:
            logger.info(
                "Reminder %s was delivered to the %s.", reminder_id, destination.kind
            )
            return _Outcome.DELIVERED
        if status == 429 and rate_limit_waits < _MAX_RATE_LIMIT_WAITS:
            rate_limit_waits += 1
            delay = _retry_after(response)
            logger.warning(
                "Reminder %s %s delivery was rate limited. It resends in %.2f seconds.",
                reminder_id,
                destination.kind,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        body = response.text[:_LOGGED_BODY_CHARS]
        if 400 <= status < 500 and status != 429:
            logger.error(
                "Reminder %s %s delivery was refused with status %d. The response body was %r.",
                reminder_id,
                destination.kind,
                status,
                body,
            )
            return _Outcome.PERMANENT
        logger.warning(
            "Reminder %s %s delivery failed with status %d. The response body was %r.",
            reminder_id,
            destination.kind,
            status,
            body,
        )
        return _Outcome.TRANSIENT


async def _post_webhook(url: str, body: dict[str, Any]) -> httpx.Response:
    """Posts the stored Discord body to an Execute Webhook URL.

    The URL gets wait=true, so Discord answers only after the message exists.
    It also gets with_components=true when the body has components.
    A thread_id already in the URL is kept.
    """
    params = {"wait": "true"}
    if body.get("components"):
        params["with_components"] = "true"
    target = httpx.URL(url).copy_merge_params(params)
    return await get_client().post(target, json=body)


async def _post_callback(
    url: str, reminder: dict[str, Any], secret: str
) -> httpx.Response:
    """Posts the signed reminder envelope to a bot callback URL.

    The body is serialized once, and those exact bytes are signed and sent.
    """
    envelope = {
        "reminder_id": reminder["reminder_id"],
        "fired_at": reminder["fire_at"],
        "channel_id": reminder["channel_id"],
        "guild_id": reminder["guild_id"],
        "payload": reminder["payload"],
    }
    body = json.dumps(envelope, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Signature-Timestamp": timestamp,
        "X-Signature-SHA256": sign_callback(secret, timestamp, body),
    }
    return await get_client().post(url, content=body, headers=headers)


def _retry_after(response: httpx.Response) -> float:
    """Reads how long a 429 answer asks to wait, capped at _MAX_RATE_LIMIT_DELAY seconds.

    The retry_after field of the JSON body wins over the Retry-After header.
    """
    delay: float | None = None
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("retry_after"), int | float):
        delay = float(data["retry_after"])
    if delay is None:
        try:
            delay = float(response.headers["Retry-After"])
        except (KeyError, ValueError):
            delay = _DEFAULT_RATE_LIMIT_DELAY
    return min(max(delay, 0.0), _MAX_RATE_LIMIT_DELAY)
