import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

SECRET = "test-secret-0123456789"
os.environ["API_SECRET"] = SECRET
os.environ.setdefault("SCHEDULER_DB_PATH", ":memory:")

from scheduler_api.config import Settings, get_settings  # noqa: E402
from scheduler_api.main import VERSION, app  # noqa: E402
from scheduler_api.service import service_version  # noqa: E402

AUTH = {"Authorization": f"Bearer {SECRET}"}
WRONG = {"Authorization": "Bearer wrong"}
CHANNEL = "123456789012345678"
GUILD = "987654321098765432"
WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/abc_DEF-123"

_REMINDER_BODY: dict[str, Any] = {
    "fire_at": "2099-01-01T12:00:00+00:00",
    "channel_id": CHANNEL,
    "guild_id": GUILD,
    "webhook_url": WEBHOOK,
    "payload": {"content": "Hello."},
}


# The lifespan opens the database and starts the scheduler.
# A TestClient context manager gives each test a fresh lifespan and a fresh in-memory database.
def _make_client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def configure(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Sets environment variables and makes get_settings read them again."""

    def apply(**env: str) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


def _post(body: dict[str, Any]) -> httpx2.Response:
    with _make_client() as client:
        return client.post("/reminders", json=body, headers=AUTH)


def test_health() -> None:
    with _make_client() as client:
        r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["scheduler"] == "running"


def test_health_reports_stopped_scheduler_without_lifespan() -> None:
    r = TestClient(app).get("/health")
    assert r.json()["scheduler"] == "stopped"


def test_create_reminder_requires_auth() -> None:
    with _make_client() as client:
        r = client.post("/reminders", json=_REMINDER_BODY)
    assert r.status_code == 401


def test_create_and_get_reminder() -> None:
    with _make_client() as client:
        r = client.post("/reminders", json=_REMINDER_BODY, headers=AUTH)
        assert r.status_code == 201
        reminder_id = r.json()["reminder_id"]

        r2 = client.get(f"/reminders/{reminder_id}", headers=AUTH)
        assert r2.status_code == 200
        assert r2.json()["status"] == "scheduled"


def test_webhook_payload_is_normalized_with_quiet_mentions() -> None:
    body = {**_REMINDER_BODY, "payload": {"content": "Hi @everyone.", "tts": False}}
    with _make_client() as client:
        r = client.post("/reminders", json=body, headers=AUTH)
        assert r.status_code == 201
        stored = client.get(f"/reminders/{r.json()['reminder_id']}", headers=AUTH)
    expected = {
        "content": "Hi @everyone.",
        "tts": False,
        "allowed_mentions": {"parse": []},
    }
    assert r.json()["payload"] == expected
    assert stored.json()["payload"] == expected


def test_explicit_allowed_mentions_are_kept() -> None:
    payload = {"content": "Hi.", "allowed_mentions": {"parse": ["users"]}}
    r = _post({**_REMINDER_BODY, "payload": payload})
    assert r.status_code == 201
    assert r.json()["payload"]["allowed_mentions"] == {"parse": ["users"]}


@pytest.mark.parametrize(
    "url",
    [
        "https://ptb.discord.com/api/webhooks/1/token",
        "https://canary.discord.com/api/v10/webhooks/1/token",
        "https://discordapp.com/api/webhooks/1/tok-en_1",
        "https://discord.com/api/webhooks/1/token?thread_id=123456789012345678",
    ],
)
def test_valid_webhook_urls_are_accepted(url: str) -> None:
    assert _post({**_REMINDER_BODY, "webhook_url": url}).status_code == 201


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/api/webhooks/1/token",
        "http://discord.com/api/webhooks/1/token",
        "https://discord.com/api/webhooks/1/token?wait=true",
        "https://discord.com/api/webhooks/1/token?thread_id=abc",
        "https://discord.com/api/webhooks/abc/token",
        "https://discord.com/api/webhooks/1/token/extra",
        "https://discord.com:8443/api/webhooks/1/token",
        "https://discord.com.evil.example/api/webhooks/1/token",
        "https://discord.com/api/webhooks/test",
    ],
    ids=[
        "wrong-host",
        "http",
        "extra-query",
        "bad-thread-id",
        "bad-id",
        "extra-path",
        "port",
        "lookalike-host",
        "no-token",
    ],
)
def test_invalid_webhook_urls_are_rejected(url: str) -> None:
    r = _post({**_REMINDER_BODY, "webhook_url": url})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "webhook_url"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"content": ""},
        {"username": "Only a name."},
        {"content": "x" * 2001},
        {"embeds": [{"title": str(i)} for i in range(11)]},
        {"content": "Hi.", "username": "x" * 81},
        {"content": "Hi.", "message": "Not a Discord field."},
    ],
    ids=[
        "empty",
        "blank-content",
        "no-message",
        "long-content",
        "too-many-embeds",
        "long-username",
        "unknown-field",
    ],
)
def test_invalid_webhook_payloads_are_rejected(payload: dict[str, Any]) -> None:
    r = _post({**_REMINDER_BODY, "payload": payload})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"][:2] == ["body", "payload"]


def test_content_of_2000_characters_is_accepted() -> None:
    assert (
        _post({**_REMINDER_BODY, "payload": {"content": "x" * 2000}}).status_code == 201
    )


@pytest.mark.parametrize(
    "fire_at",
    [
        "2099-01-01T12:00:00",
        (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    ],
    ids=["naive", "past"],
)
def test_fire_at_must_be_aware_and_in_the_future(fire_at: str) -> None:
    r = _post({**_REMINDER_BODY, "fire_at": fire_at})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "fire_at"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channel_id", "123"),
        ("channel_id", "1234567890123456789012"),
        ("guild_id", "guild-A"),
        ("guild_id", "98765432109876543x"),
    ],
)
def test_bad_snowflakes_are_rejected(field: str, value: str) -> None:
    r = _post({**_REMINDER_BODY, field: value})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", field]


@pytest.mark.parametrize("query", ["guild_id=guild-A", "status=bogus"])
def test_list_filters_are_validated(query: str) -> None:
    with _make_client() as client:
        r = client.get(f"/reminders?{query}", headers=AUTH)
    assert r.status_code == 422


def test_reminder_missing_destination() -> None:
    body = {k: v for k, v in _REMINDER_BODY.items() if k != "webhook_url"}
    assert _post(body).status_code == 422


def test_cancel_reminder() -> None:
    with _make_client() as client:
        r = client.post("/reminders", json=_REMINDER_BODY, headers=AUTH)
        reminder_id = r.json()["reminder_id"]

        r2 = client.delete(f"/reminders/{reminder_id}", headers=AUTH)
        assert r2.status_code == 204


def test_health_includes_version() -> None:
    with _make_client() as client:
        r = client.get("/health")
    assert "version" in r.json()
    assert "pending_jobs" in r.json()


def test_health_reports_the_package_version() -> None:
    with _make_client() as client:
        r = client.get("/health")
    assert r.json()["service"] == "api-scheduler"
    assert r.json()["version"] == VERSION
    assert VERSION == service_version("api-scheduler") != "0.0.0"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Bearer {SECRET}x"},
        {"Authorization": f"Basic {SECRET}"},
    ],
    ids=["missing", "wrong", "longer", "wrong-scheme"],
)
def test_protected_route_rejects_without_valid_token(headers: dict[str, str]) -> None:
    with _make_client() as client:
        r = client.post("/reminders", json=_REMINDER_BODY, headers=headers)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_create_reminder_wrong_auth() -> None:
    with _make_client() as client:
        r = client.post("/reminders", json=_REMINDER_BODY, headers=WRONG)
    assert r.status_code == 401


def test_get_reminder_requires_auth() -> None:
    with _make_client() as client:
        r = client.get("/reminders/nonexistent")
    assert r.status_code == 401


def test_get_reminder_wrong_auth() -> None:
    with _make_client() as client:
        r = client.get("/reminders/nonexistent", headers=WRONG)
    assert r.status_code == 401


def test_get_nonexistent_reminder_returns_404() -> None:
    with _make_client() as client:
        r = client.get("/reminders/does-not-exist", headers=AUTH)
    assert r.status_code == 404


def test_cancel_nonexistent_reminder_returns_404() -> None:
    with _make_client() as client:
        r = client.delete("/reminders/does-not-exist", headers=AUTH)
    assert r.status_code == 404


def test_cancel_reminder_requires_auth() -> None:
    with _make_client() as client:
        r = client.delete("/reminders/any-id")
    assert r.status_code == 401


def test_list_reminders_requires_auth() -> None:
    with _make_client() as client:
        r = client.get("/reminders")
    assert r.status_code == 401


def test_list_reminders_wrong_auth() -> None:
    with _make_client() as client:
        r = client.get("/reminders", headers=WRONG)
    assert r.status_code == 401


def test_list_reminders_empty_initially() -> None:
    with _make_client() as client:
        r = client.get("/reminders", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["reminders"] == []
    assert data["total"] == 0


def test_list_reminders_includes_created_reminder() -> None:
    with _make_client() as client:
        client.post("/reminders", json=_REMINDER_BODY, headers=AUTH)
        r = client.get("/reminders", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_list_reminders_filter_by_guild_id_and_status() -> None:
    guild_a = "111111111111111111"
    guild_b = "222222222222222222"
    with _make_client() as client:
        client.post(
            "/reminders", json={**_REMINDER_BODY, "guild_id": guild_a}, headers=AUTH
        )
        client.post(
            "/reminders", json={**_REMINDER_BODY, "guild_id": guild_b}, headers=AUTH
        )
        r = client.get(f"/reminders?guild_id={guild_a}&status=scheduled", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["reminders"][0]["guild_id"] == guild_a


_CALLBACK_BODY: dict[str, Any] = {
    "fire_at": "2099-01-01T12:00:00+00:00",
    "channel_id": CHANNEL,
    "guild_id": GUILD,
    "bot_callback_url": "https://my-bot.example.com/callback",
    "payload": {"role": "Raiders", "anything": [1, 2, 3]},
}


def test_callback_only_reminder_keeps_a_free_form_payload(
    configure: Callable[..., None],
) -> None:
    configure(ALLOWED_CALLBACK_HOSTS="other.example.com, My-Bot.example.com")
    r = _post(_CALLBACK_BODY)
    assert r.status_code == 201
    assert r.json()["payload"] == _CALLBACK_BODY["payload"]


def test_callbacks_are_refused_by_default(configure: Callable[..., None]) -> None:
    configure(ALLOWED_CALLBACK_HOSTS="")
    r = _post(_CALLBACK_BODY)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "bot_callback_url"]
    assert "ALLOWED_CALLBACK_HOSTS" in r.json()["detail"][0]["msg"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/callback",
        "https://my-bot.example.com.evil.example/callback",
        "ftp://my-bot.example.com/callback",
    ],
)
def test_callback_hosts_outside_the_allowlist_are_rejected(
    configure: Callable[..., None], url: str
) -> None:
    configure(ALLOWED_CALLBACK_HOSTS='["my-bot.example.com"]')
    r = _post({**_CALLBACK_BODY, "bot_callback_url": url})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "bot_callback_url"]


@pytest.mark.parametrize(
    "raw",
    ["a.example.com, B.example.com", '["a.example.com", "B.example.com"]'],
    ids=["comma-separated", "json"],
)
def test_allowed_callback_hosts_parse_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("ALLOWED_CALLBACK_HOSTS", raw)
    settings = Settings(api_secret=SECRET)
    assert settings.allowed_callback_hosts == ["a.example.com", "b.example.com"]


def test_negative_max_retries_are_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(api_secret=SECRET, dispatcher_max_retries=-1)


def test_list_orders_by_instant_across_offsets() -> None:
    # Sorted as raw strings these would come out as 06:00Z, 08:00Z, 07:00Z.
    fire_times = {
        "07:00Z": "2099-01-01T12:00:00+05:00",
        "08:00Z": "2099-01-01T08:00:00+00:00",
        "06:00Z": "2099-01-01T03:00:00-03:00",
    }
    ids: dict[str, str] = {}
    with _make_client() as client:
        for label, fire_at in fire_times.items():
            r = client.post(
                "/reminders", json={**_REMINDER_BODY, "fire_at": fire_at}, headers=AUTH
            )
            ids[r.json()["reminder_id"]] = label
        listed = client.get("/reminders", headers=AUTH).json()["reminders"]
    assert [ids[row["reminder_id"]] for row in listed] == ["06:00Z", "07:00Z", "08:00Z"]
    assert all(row["fire_at"].endswith("Z") for row in listed)
