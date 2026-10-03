import asyncio
import hashlib
import hmac
import json
import os
import time
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

SECRET = "test-secret-0123456789"
os.environ["DISCORD_API_SECRET"] = SECRET
os.environ.setdefault("SCHEDULER_DB_PATH", ":memory:")

from scheduler_api import database, dispatcher, main, scheduler  # noqa: E402
from scheduler_api.config import Settings, get_settings  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/abc_DEF-123"
CALLBACK = "https://my-bot.example.com/callback"
DISCORD_BODY = {"content": "Hello.", "allowed_mentions": {"parse": []}}

Handler = Callable[[httpx.Request], httpx.Response]


def _reminder(**overrides: Any) -> dict[str, Any]:
    reminder: dict[str, Any] = {
        "reminder_id": "r1",
        "fire_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "channel_id": "123456789012345678",
        "guild_id": "987654321098765432",
        "payload": DISCORD_BODY,
        "webhook_url": WEBHOOK,
        "bot_callback_url": None,
        "status": "scheduled",
        "retry_count": 0,
        "created_at": datetime.now(UTC).isoformat(),
    }
    reminder.update(overrides)
    return reminder


class Recorder:
    """Answers requests with a scripted list of responses and records every request."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def _use_transport(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(dispatcher, "_client", client)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replaces asyncio.sleep with a recorder, so retry delays take no time."""
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


@pytest.fixture
async def db() -> AsyncIterator[None]:
    await database.init(":memory:")
    yield
    await dispatcher.close_client()
    await database.close()


async def _fire(reminder: dict[str, Any], max_retries: int = 3) -> dict[str, Any]:
    await database.insert_reminder(reminder)
    settings = Settings(discord_api_secret=SECRET, dispatcher_max_retries=max_retries)
    await dispatcher.fire(reminder, settings)
    row = await database.get_reminder(reminder["reminder_id"])
    assert row is not None
    return row


async def test_2xx_marks_the_reminder_fired(
    db: None, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(httpx.Response(200, json={"id": "1"}))
    _use_transport(monkeypatch, recorder)
    row = await _fire(_reminder())
    assert row["status"] == "fired"
    assert sleeps == []
    request = recorder.requests[0]
    assert request.url.params["wait"] == "true"
    assert "with_components" not in request.url.params
    assert json.loads(request.content) == DISCORD_BODY


async def test_webhook_keeps_thread_id_and_asks_for_components(
    db: None, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(httpx.Response(200))
    _use_transport(monkeypatch, recorder)
    body = {**DISCORD_BODY, "components": [{"type": 1, "components": []}]}
    url = f"{WEBHOOK}?thread_id=111111111111111111"
    await _fire(_reminder(webhook_url=url, payload=body))
    params = recorder.requests[0].url.params
    assert params["thread_id"] == "111111111111111111"
    assert params["wait"] == "true"
    assert params["with_components"] == "true"


async def test_400_marks_failed_without_retry(
    db: None, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(
        httpx.Response(400, json={"message": "Cannot send an empty message"})
    )
    _use_transport(monkeypatch, recorder)
    row = await _fire(_reminder())
    assert row["status"] == "failed"
    assert row["retry_count"] == 0
    assert len(recorder.requests) == 1
    assert sleeps == []


async def test_429_waits_retry_after_without_using_a_retry(
    db: None, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(
        httpx.Response(429, json={"retry_after": 1.5, "global": False}),
        httpx.Response(200),
    )
    _use_transport(monkeypatch, recorder)
    row = await _fire(_reminder(), max_retries=0)
    assert row["status"] == "fired"
    assert row["retry_count"] == 0
    assert sleeps == [1.5]
    assert len(recorder.requests) == 2


async def test_429_falls_back_to_the_header_and_caps_the_wait(
    db: None, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(
        httpx.Response(429, text="slow down", headers={"Retry-After": "120"}),
        httpx.Response(204),
    )
    _use_transport(monkeypatch, recorder)
    row = await _fire(_reminder())
    assert row["status"] == "fired"
    assert sleeps == [60.0]


async def test_500_uses_the_retry_schedule(
    db: None, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(httpx.Response(500), httpx.Response(502), httpx.Response(200))
    _use_transport(monkeypatch, recorder)
    row = await _fire(_reminder())
    assert row["status"] == "fired"
    assert row["retry_count"] == 2
    assert sleeps == [30, 120]


async def test_500_until_retries_run_out_marks_failed(
    db: None, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(httpx.Response(500))
    _use_transport(monkeypatch, recorder)
    row = await _fire(_reminder(), max_retries=2)
    assert row["status"] == "failed"
    assert len(recorder.requests) == 3
    assert sleeps == [30, 120]


async def test_network_error_is_retried(
    db: None, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("Connection refused.", request=request)
        return httpx.Response(200)

    _use_transport(monkeypatch, handler)
    row = await _fire(_reminder())
    assert row["status"] == "fired"
    assert sleeps == [30]


async def test_a_delivered_destination_is_not_sent_again(
    db: None, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    hits: dict[str, int] = {"discord.com": 0, "my-bot.example.com": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits[request.url.host] += 1
        if request.url.host == "my-bot.example.com" and hits["my-bot.example.com"] == 1:
            return httpx.Response(503)
        return httpx.Response(200)

    _use_transport(monkeypatch, handler)
    row = await _fire(_reminder(bot_callback_url=CALLBACK))
    assert row["status"] == "fired"
    assert hits == {"discord.com": 1, "my-bot.example.com": 2}


async def test_cancel_during_a_retry_wait_stops_delivery(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(httpx.Response(500), httpx.Response(200))
    _use_transport(monkeypatch, recorder)

    async def cancel_while_sleeping(delay: float) -> None:
        # This is what DELETE /reminders/{id} does to the stored row.
        await database.update_status("r1", "cancelled")

    monkeypatch.setattr(asyncio, "sleep", cancel_while_sleeping)
    row = await _fire(_reminder())
    assert row["status"] == "cancelled"
    assert row["retry_count"] == 1
    assert len(recorder.requests) == 1


async def test_cancel_during_a_rate_limit_wait_stops_delivery(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(
        httpx.Response(429, json={"retry_after": 0.5}), httpx.Response(200)
    )
    _use_transport(monkeypatch, recorder)

    async def cancel_while_sleeping(delay: float) -> None:
        await database.update_status("r1", "cancelled")

    monkeypatch.setattr(asyncio, "sleep", cancel_while_sleeping)
    row = await _fire(_reminder())
    assert row["status"] == "cancelled"
    assert len(recorder.requests) == 1


async def test_a_cancelled_reminder_is_not_sent_or_overwritten(
    db: None, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(httpx.Response(200))
    _use_transport(monkeypatch, recorder)
    reminder = _reminder()
    await database.insert_reminder(reminder)
    await database.update_status("r1", "cancelled")
    await dispatcher.fire(reminder, Settings(discord_api_secret=SECRET))
    row = await database.get_reminder("r1")
    assert row is not None
    assert row["status"] == "cancelled"
    assert recorder.requests == []


async def test_conditional_update_reports_whether_a_row_changed(db: None) -> None:
    await database.insert_reminder(_reminder())
    assert await database.update_status("r1", "fired", only_if="scheduled")
    assert not await database.update_status("r1", "failed", only_if="scheduled")
    row = await database.get_reminder("r1")
    assert row is not None
    assert row["status"] == "fired"


def verify_callback(
    secret: str, timestamp: str, signature: str, body: bytes, max_age: int = 300
) -> bool:
    """The receiver-side check that the README documents."""
    if abs(time.time() - int(timestamp)) > max_age:
        return False
    message = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def test_callback_is_signed_and_carries_the_envelope(
    db: None, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(httpx.Response(200))
    _use_transport(monkeypatch, recorder)
    free_form = {"role": "Raiders"}
    reminder = _reminder(webhook_url=None, bot_callback_url=CALLBACK, payload=free_form)
    row = await _fire(reminder)
    assert row["status"] == "fired"

    request = recorder.requests[0]
    timestamp = request.headers["X-Signature-Timestamp"]
    signature = request.headers["X-Signature-SHA256"]
    assert request.headers["Content-Type"] == "application/json"
    assert verify_callback(SECRET, timestamp, signature, request.content)
    assert not verify_callback(SECRET, timestamp, signature, request.content + b" ")
    assert not verify_callback(
        "another-secret-0123", timestamp, signature, request.content
    )
    assert not verify_callback(
        SECRET, str(int(timestamp) - 600), signature, request.content
    )
    assert json.loads(request.content) == {
        "reminder_id": "r1",
        "fired_at": reminder["fire_at"],
        "channel_id": reminder["channel_id"],
        "guild_id": reminder["guild_id"],
        "payload": free_form,
    }


@pytest.fixture
def startup_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Callable[[str], None]]:
    """Points the service at a file database and sets the missed reminder policy."""

    def apply(policy: str) -> None:
        monkeypatch.setenv("SCHEDULER_DB_PATH", str(tmp_path / "scheduler.db"))
        monkeypatch.setenv("MISSED_REMINDER_POLICY", policy)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


async def _seed(overdue: dict[str, Any], future: dict[str, Any]) -> None:
    await database.init(get_settings().scheduler_db_path)
    await database.insert_reminder(overdue)
    await database.insert_reminder(future)
    await database.close()


async def _status(reminder_id: str) -> str:
    row = await database.get_reminder(reminder_id)
    assert row is not None
    return str(row["status"])


async def test_startup_fires_an_overdue_reminder_under_fire(
    startup_env: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    startup_env("fire")
    recorder = Recorder(httpx.Response(200))
    _use_transport(monkeypatch, recorder)
    overdue = _reminder(fire_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    future = _reminder(reminder_id="r2")
    await _seed(overdue, future)

    async with main.lifespan(main.app):
        for _ in range(100):
            if await _status("r1") == "fired":
                break
            await asyncio.sleep(0.02)
        assert await _status("r1") == "fired"
        assert await _status("r2") == "scheduled"
        assert scheduler.pending_count() == 1
    assert len(recorder.requests) == 1


async def test_startup_fails_an_overdue_reminder_under_fail(
    startup_env: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    startup_env("fail")
    recorder = Recorder(httpx.Response(200))
    _use_transport(monkeypatch, recorder)
    overdue = _reminder(fire_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    future = _reminder(reminder_id="r2")
    await _seed(overdue, future)

    async with main.lifespan(main.app):
        assert await _status("r1") == "failed"
        assert await _status("r2") == "scheduled"
        assert scheduler.pending_count() == 1
    assert recorder.requests == []
