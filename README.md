# api-scheduler

This is a REST API for scheduling persistent reminders on behalf of Discord bots. Bots register a timed reminder with a delivery destination and the API fires it at the specified time, even if the bot has restarted in the meantime. Reminders are stored in a SQLite database and restored automatically on service startup. This project is based on the [api-template](https://github.com/Lempki/api-template) repository, which provides the core architecture.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/reminders` | Register a new timed reminder. |
| `GET` | `/reminders` | List reminders, optionally filtered by guild or status. |
| `GET` | `/reminders/{reminder_id}` | Get the status and details of a specific reminder. An unknown ID gets `404`. |
| `DELETE` | `/reminders/{reminder_id}` | Cancel a reminder that is still scheduled. Answers `204 No Content`, or `404` when the reminder does not exist or has already fired, failed, or been cancelled. |
| `GET` | `/health` | Returns the service name, version, scheduler status, and pending job count. The Docker image also uses it as its health check. |

All endpoints except `/health` require a bearer token in the `Authorization` header.
A request without the header or with a wrong token gets `401 Unauthorized` with a `WWW-Authenticate: Bearer` header.
Tokens are compared in constant time.
The version that `/health` and the OpenAPI docs report is read from `pyproject.toml`.

### Breaking changes in this version

These changes break clients written for the previous version.

* `webhook_url` must be a Discord Execute Webhook URL, and `payload` must then be a Discord message body. The old envelope is no longer sent to Discord, because Discord rejected it.
* `bot_callback_url` is refused unless its host is listed in `ALLOWED_CALLBACK_HOSTS`, which is empty by default.
* Every bot callback carries a signature that the receiver should verify.
* `fire_at` must include a timezone offset and lie in the future.
* `channel_id` and `guild_id` must be Discord snowflakes of 17 to 20 digits. The `guild_id` filter of `GET /reminders` is validated the same way, and its `status` filter accepts only the four reminder statuses.
* A reminder that came due while the service was down now fires at startup instead of being dropped. Set `MISSED_REMINDER_POLICY=fail` to mark such reminders failed instead.

### POST /reminders

This body posts "Raid starts in 15 minutes!" to a Discord channel through a webhook.

```json
{
  "fire_at": "2026-12-24T18:00:00+02:00",
  "channel_id": "123456789012345678",
  "guild_id": "987654321098765432",
  "webhook_url": "https://discord.com/api/webhooks/1234567890123456789/your-webhook-token",
  "payload": {
    "content": "Raid starts in 15 minutes!",
    "username": "Raid Reminder",
    "embeds": [{"title": "Molten Core", "description": "Meet at the entrance."}]
  }
}
```

`fire_at` must be an ISO 8601 datetime with a timezone offset, and it must be in the future.
A datetime without an offset gets `422 Unprocessable Entity`.
Times are stored and processed internally as UTC.
`channel_id` and `guild_id` are Discord snowflakes, sent as strings of 17 to 20 digits.
At least one of `webhook_url` or `bot_callback_url` must be provided.

`webhook_url` must have the form `https://discord.com/api/webhooks/{id}/{token}`.
The hosts `discord.com`, `ptb.discord.com`, `canary.discord.com`, and `discordapp.com` are accepted, and so is an API version segment such as `/api/v10/`.
The only query parameter allowed is a numeric `thread_id`, which posts into a thread.

When `webhook_url` is set, `payload` is the JSON body of Discord's [Execute Webhook](https://discord.com/developers/docs/resources/webhook#execute-webhook) endpoint.
The accepted fields are `content`, `username`, `avatar_url`, `tts`, `embeds`, `allowed_mentions`, `components`, `attachments`, `flags`, `thread_name`, `applied_tags`, and `poll`.
Any other field gets `422`.
The body needs at least one of `content`, `embeds`, `components`, or `poll`.
`content` holds at most 2000 characters, `embeds` at most 10 items, and `username` at most 80 characters.
When `allowed_mentions` is missing, it defaults to `{"parse": []}`, so a reminder never pings `@everyone` or a role by accident.
The stored and returned `payload` is the normalized body, including that default.

When only `bot_callback_url` is set, `payload` is a free-form JSON object that is passed to the bot unchanged.
`bot_callback_url` must be an `http` or `https` URL whose hostname is listed in `ALLOWED_CALLBACK_HOSTS`.

Returns `201 Created` with the created reminder, including its assigned `reminder_id` and a `status` of `"scheduled"`.

### Delivery

The webhook receives the stored `payload` as its JSON body.
The service adds `wait=true` to the URL, so Discord answers only after the message exists.
It also adds `with_components=true` when the body has `components`, which Discord requires for webhooks that no application owns.

The bot callback receives this envelope.

```json
{
  "reminder_id": "...",
  "fired_at": "...",
  "channel_id": "...",
  "guild_id": "...",
  "payload": { ... }
}
```

`fired_at` holds the scheduled fire time.

Each attempt sends the reminder to every configured destination that has not yet succeeded.
A reminder is marked `"fired"` when every configured destination has answered with a 2xx status.
A destination that succeeded is not sent the reminder again on a later retry.
The answers are handled as follows.

| Answer | Handling |
|---|---|
| 2xx | The destination succeeded. |
| 429 | The service waits for the `retry_after` value of the JSON body, or else the `Retry-After` header, capped at 60 seconds. It then resends without using up a retry, at most three times in a row. A fourth 429 in a row counts as a transient failure, like a 5xx. |
| Other 4xx | The error is permanent. The reminder is marked `"failed"` at once, and the status and the start of the response body are logged. |
| 5xx or a network error | The attempt is retried up to `DISPATCHER_MAX_RETRIES` times, after 30 seconds, 2 minutes, and 10 minutes. Any later retry also waits 10 minutes. |

After all retries are used, the reminder is marked `"failed"` and will not be retried further.
A reminder cancelled during a wait between sends is not sent again and stays `"cancelled"`.
Delivery is at least once, so a restart during retries can resend a destination that already succeeded.

`webhook_url` is the preferred delivery method because it does not depend on the bot process being reachable.
`bot_callback_url` can be used when the bot needs to perform additional logic at fire time, such as looking up a role name.

### Verifying a bot callback

Every callback request carries two headers.
`X-Signature-Timestamp` holds the Unix time in seconds when the request was signed.
`X-Signature-SHA256` holds the hex HMAC-SHA256 of the timestamp, a dot, and the exact request body bytes, keyed with `API_SECRET`.
The bot already holds this secret, because it sends the same value as its bearer token.

The receiver recomputes the signature over the raw body before parsing it, compares the two in constant time, and rejects a timestamp older than 5 minutes.

```python
import hashlib
import hmac
import time


def verify_callback(secret: str, timestamp: str, signature: str, body: bytes) -> bool:
    """Returns True when a callback request is authentic and recent."""
    if abs(time.time() - int(timestamp)) > 300:
        return False
    message = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

### Missed reminders

A reminder can come due while the service is down.
At startup, `MISSED_REMINDER_POLICY` decides what happens to it.
With `fire`, the default, it is delivered at once.
With `fail`, it is marked `"failed"` and a log line names it.
Reminders that are still in the future are scheduled as usual.

### GET /reminders

Query parameters:

| Parameter | Description |
|---|---|
| `guild_id` | Filter to a specific Discord server. It must be a snowflake of 17 to 20 digits. |
| `status` | Filter by status. Accepted values: `scheduled`, `fired`, `failed`, `cancelled`. Any other value gets `422`. |
| `limit` | Maximum number of results to return. Defaults to `50`, maximum `200`. |
| `offset` | Number of results to skip for pagination. Defaults to `0`. |

## Prerequisites

* [Docker](https://docs.docker.com/get-docker/) and Docker Compose.

Running without Docker requires Python 3.12 and [uv](https://docs.astral.sh/uv/). On Windows, install uv with `winget install --id astral-sh.uv`.

## Setup

The setup script prepares the project in a single run, and it is safe to run again at any time.

On Windows, double-click `setup.bat` or run it from a terminal:

```
setup.bat
```

On macOS or Linux, run the following commands:

```
chmod +x setup.sh
./setup.sh
```

The script asks before it installs anything, and it does the following:

1. It installs [uv](https://docs.astral.sh/uv/) when uv is missing. uv also provides Python 3.12 when the machine lacks it.
2. It offers to install Docker, and the tools that the Docker image includes for running outside Docker. It uses winget on Windows, Homebrew on macOS, and the system package manager on Linux. On Windows it also turns on WSL, which Docker Desktop needs, and says when Windows needs a restart or virtualization is turned off in the firmware.
3. It runs `uv sync`, which installs the package and its locked dependencies into `.venv`.
4. It copies `.env.template` to `.env` on the first run and fills `API_SECRET` with a random value.

A step that fails says what went wrong, why it matters, and what to do next, and the summary at the end lists it again.
The steps live in `scripts/bootstrap.py`, which needs only the Python standard library.

Outside Docker, also set `SCHEDULER_DB_PATH` to a file in a directory that exists, such as `scheduler.db`, because the default `/data` directory exists only in the container.

If you prefer to perform the setup manually, follow these steps:

```bash
uv sync
cp .env.template .env
# Edit .env and set API_SECRET, SCHEDULER_DB_PATH, and other values as needed.
uv run uvicorn scheduler_api.main:app --port 8004
```

### Running

After setup has run once, the run script starts the API.
Double-click `run.bat` on Windows, or run `./run.sh` on macOS and Linux.
It builds and starts the API in Docker in the background, waits until its health check passes, and shows its status.
The container then starts again whenever Docker starts.

The script also takes an action, such as `run.bat stop` on Windows or `./run.sh stop` elsewhere:

| Action | What it does |
|---|---|
| `start` | Builds and starts everything in Docker and waits until it is ready. It is the default. |
| `stop` | Stops the containers. They stay stopped until the next start. |
| `status` | Shows whether each container runs and is healthy. |
| `logs` | Follows the logs. Press Ctrl+C to stop following. |
| `update` | Pulls the latest code, rebuilds on fresh base images, and restarts. |
| `local` | Runs the project in the terminal without Docker. Press Ctrl+C to stop it. |

When a service crashes right after it starts, the script shows the end of its log and stops it, so it does not restart over and over.
The `update` action needs a Git clone. In a downloaded release, it explains how to replace the files by hand instead.

### Docker

Alternatively, you can run the API as a Docker container.

1. Copy `.env.template` to `.env` and set `API_SECRET`.
2. Build and start the container:

   ```
   docker compose up --build
   ```

The container runs on port `8000` internally. Docker Compose maps it to port `8004` on the host. The Docker Compose configuration mounts the named volume `scheduler_data` at `/data`, which holds the SQLite database `/data/scheduler.db`.
Reminders therefore persist across container restarts and rebuilds, until the volume itself is removed, for example with `docker compose down -v`.
The image has a health check that calls `/health`, so Docker marks the container unhealthy when the service stops answering.

## Configuration

All configuration is read from environment variables or from a `.env` file in the project root.

| Variable | Required | Default | Description |
|---|---|---|---|
| `API_SECRET` | Yes | None | Shared bearer token of at least 16 characters. Every client must send this value in the `Authorization` header. The service refuses to start with a placeholder such as `changeme`. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `SCHEDULER_DB_PATH` | No | `/data/scheduler.db` | Path to the SQLite database file. The directory must exist and be writable, and the file is created on first start. |
| `LOG_LEVEL` | No | `INFO` | Log verbosity. Accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `DISPATCHER_MAX_RETRIES` | No | `3` | Number of delivery retry attempts before a reminder is marked as failed. It must be 0 or more. |
| `ALLOWED_CALLBACK_HOSTS` | No | Empty | Hostnames that `bot_callback_url` may point to, as a comma-separated list such as `bot.example.com,localhost` or as a JSON list. While it is empty, every bot callback is refused. |
| `MISSED_REMINDER_POLICY` | No | `fire` | What startup does with a reminder that came due while the service was down. `fire` delivers it at once and `fail` marks it failed. |

The service logs one JSON object per line, including uvicorn's access log.

## Project structure

```
api-scheduler/
├── src/scheduler_api/
│   ├── main.py           # FastAPI application and route definitions.
│   ├── config.py         # This service's settings on top of ServiceSettings.
│   ├── service.py        # Shared settings, secret validation, and the version lookup.
│   ├── logging_config.py # Structured JSON logging, including uvicorn's loggers.
│   ├── auth.py           # Bearer token dependency.
│   ├── models.py         # Pydantic request and response models.
│   ├── database.py       # SQLite schema and async query helpers.
│   ├── reminder_store.py # Business logic for creating, listing, and cancelling reminders.
│   ├── scheduler.py      # APScheduler setup and job lifecycle management.
│   └── dispatcher.py     # Webhook and signed callback delivery with retry logic.
├── tests/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml        # Project metadata and dependencies.
├── uv.lock               # Locked dependency versions.
├── ruff.toml             # Lint and format settings on top of the shared baseline.
├── setup.bat             # Windows setup script.
├── setup.sh              # macOS and Linux setup script.
├── scripts/bootstrap.py  # The steps that both setup scripts run.
├── scripts/run.py        # The actions that both run scripts take.
├── run.bat               # Windows run script.
├── run.sh                # macOS and Linux run script.
└── .env.template         # Template for environment variables.
```

## Running tests

```bash
uv run pytest
```

Run every lint and format check with `uvx pre-commit run --all-files`, or install the hooks once with `uvx pre-commit install` so they run on each commit.
The coding, prose, and commit conventions are documented in [dev-standards](https://github.com/Lempki/dev-standards).

## License

This project is licensed under the [MIT License](LICENSE).
You may use, change, and share it, as long as every copy keeps the copyright notice and the license text.
