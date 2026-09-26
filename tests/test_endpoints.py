import os

from fastapi.testclient import TestClient

os.environ.setdefault("DISCORD_API_SECRET", "test-secret")
os.environ.setdefault("SCHEDULER_DB_PATH", ":memory:")

from scheduler_api.main import app  # noqa: E402


# Lifespan initialises DB + scheduler; use TestClient as context manager
# so each test gets a fresh lifespan (and a fresh in-memory DB).
def _make_client():
    return TestClient(app)


def test_health():
    with _make_client() as client:
        r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["scheduler"] == "running"


def test_create_reminder_requires_auth():
    with _make_client() as client:
        r = client.post(
            "/reminders",
            json={
                "fire_at": "2099-01-01T12:00:00+00:00",
                "channel_id": "123",
                "guild_id": "456",
                "webhook_url": "https://discord.com/api/webhooks/test",
            },
        )
    assert r.status_code == 403


def test_create_and_get_reminder():
    AUTH = {"Authorization": "Bearer test-secret"}
    with _make_client() as client:
        r = client.post(
            "/reminders",
            json={
                "fire_at": "2099-01-01T12:00:00+00:00",
                "channel_id": "123",
                "guild_id": "456",
                "webhook_url": "https://discord.com/api/webhooks/test",
            },
            headers=AUTH,
        )
        assert r.status_code == 201
        reminder_id = r.json()["reminder_id"]

        r2 = client.get(f"/reminders/{reminder_id}", headers=AUTH)
        assert r2.status_code == 200
        assert r2.json()["status"] == "scheduled"


def test_reminder_missing_destination():
    AUTH = {"Authorization": "Bearer test-secret"}
    with _make_client() as client:
        r = client.post(
            "/reminders",
            json={
                "fire_at": "2099-01-01T12:00:00+00:00",
                "channel_id": "123",
                "guild_id": "456",
            },
            headers=AUTH,
        )
    assert r.status_code == 422


def test_cancel_reminder():
    AUTH = {"Authorization": "Bearer test-secret"}
    with _make_client() as client:
        r = client.post(
            "/reminders",
            json={
                "fire_at": "2099-06-01T12:00:00+00:00",
                "channel_id": "123",
                "guild_id": "456",
                "webhook_url": "https://discord.com/api/webhooks/test",
            },
            headers=AUTH,
        )
        reminder_id = r.json()["reminder_id"]

        r2 = client.delete(f"/reminders/{reminder_id}", headers=AUTH)
        assert r2.status_code == 204


AUTH = {"Authorization": "Bearer test-secret"}
WRONG = {"Authorization": "Bearer wrong"}

_REMINDER_BODY = {
    "fire_at": "2099-01-01T12:00:00+00:00",
    "channel_id": "123",
    "guild_id": "456",
    "webhook_url": "https://discord.com/api/webhooks/test",
}


def test_health_includes_version():
    with _make_client() as client:
        r = client.get("/health")
    assert "version" in r.json()
    assert "pending_jobs" in r.json()


def test_create_reminder_wrong_auth():
    with _make_client() as client:
        r = client.post("/reminders", json=_REMINDER_BODY, headers=WRONG)
    assert r.status_code == 401


def test_get_reminder_requires_auth():
    with _make_client() as client:
        r = client.get("/reminders/nonexistent")
    assert r.status_code == 403


def test_get_reminder_wrong_auth():
    with _make_client() as client:
        r = client.get("/reminders/nonexistent", headers=WRONG)
    assert r.status_code == 401


def test_get_nonexistent_reminder_returns_404():
    with _make_client() as client:
        r = client.get("/reminders/does-not-exist", headers=AUTH)
    assert r.status_code == 404


def test_cancel_nonexistent_reminder_returns_404():
    with _make_client() as client:
        r = client.delete("/reminders/does-not-exist", headers=AUTH)
    assert r.status_code == 404


def test_cancel_reminder_requires_auth():
    with _make_client() as client:
        r = client.delete("/reminders/any-id")
    assert r.status_code == 403


def test_list_reminders_requires_auth():
    with _make_client() as client:
        r = client.get("/reminders")
    assert r.status_code == 403


def test_list_reminders_wrong_auth():
    with _make_client() as client:
        r = client.get("/reminders", headers=WRONG)
    assert r.status_code == 401


def test_list_reminders_empty_initially():
    with _make_client() as client:
        r = client.get("/reminders", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["reminders"] == []
    assert data["total"] == 0


def test_list_reminders_includes_created_reminder():
    with _make_client() as client:
        client.post("/reminders", json=_REMINDER_BODY, headers=AUTH)
        r = client.get("/reminders", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_list_reminders_filter_by_guild_id():
    body_a = {**_REMINDER_BODY, "guild_id": "guild-A"}
    body_b = {**_REMINDER_BODY, "guild_id": "guild-B"}
    with _make_client() as client:
        client.post("/reminders", json=body_a, headers=AUTH)
        client.post("/reminders", json=body_b, headers=AUTH)
        r = client.get("/reminders?guild_id=guild-A", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["reminders"][0]["guild_id"] == "guild-A"


def test_create_reminder_with_bot_callback_url_only():
    body = {
        "fire_at": "2099-01-01T12:00:00+00:00",
        "channel_id": "123",
        "guild_id": "456",
        "bot_callback_url": "https://my-bot.example.com/callback",
    }
    with _make_client() as client:
        r = client.post("/reminders", json=body, headers=AUTH)
    assert r.status_code == 201
