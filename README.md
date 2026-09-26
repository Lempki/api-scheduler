# discord-api-scheduler

This is a REST API for scheduling persistent reminders on behalf of Discord bots. Bots register a timed reminder with a delivery destination and the API fires it at the specified time, even if the bot has restarted in the meantime. Reminders are stored in a SQLite database and restored automatically on service startup. This project is based on the [discord-api-template](https://github.com/Lempki/discord-api-template) repository, which provides the core architecture.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/reminders` | Register a new timed reminder. |
| `GET` | `/reminders` | List reminders, optionally filtered by guild or status. |
| `GET` | `/reminders/{reminder_id}` | Get the status and details of a specific reminder. |
| `DELETE` | `/reminders/{reminder_id}` | Cancel a pending reminder. |
| `GET` | `/health` | Returns the service name, version, scheduler status, and pending job count. The Docker image also uses it as its health check. |

All endpoints except `/health` require a bearer token in the `Authorization` header.
A request without the header or with a wrong token gets `401 Unauthorized` with a `WWW-Authenticate: Bearer` header.
Tokens are compared in constant time.
The version that `/health` and the OpenAPI docs report is read from `pyproject.toml`.

### POST /reminders

```json
{
  "fire_at": "2026-12-24T18:00:00+02:00",
  "channel_id": "123456789012345678",
  "guild_id": "987654321098765432",
  "payload": {
    "message": "Merry Christmas!"
  },
  "webhook_url": "https://discord.com/api/webhooks/..."
}
```

`fire_at` must be an ISO 8601 datetime with a timezone offset. Times are stored and processed internally as UTC. At least one of `webhook_url` or `bot_callback_url` must be provided.

Returns the created reminder including its assigned `reminder_id` and a `status` of `"scheduled"`.

### Delivery

When a reminder fires, the API sends a `POST` request to the configured `webhook_url` or `bot_callback_url` with the following body:

```json
{
  "reminder_id": "...",
  "fired_at": "...",
  "channel_id": "...",
  "guild_id": "...",
  "payload": { ... }
}
```

If delivery fails, the API retries up to three times with exponential backoff: 30 seconds, 2 minutes, and 10 minutes. After all attempts are exhausted, the reminder is marked as `"failed"` and will not be retried further.

`webhook_url` is the preferred delivery method because it does not depend on the bot process being reachable. `bot_callback_url` can be used when the bot needs to perform additional logic at fire time, such as looking up a role name.

### GET /reminders

Query parameters:

| Parameter | Description |
|---|---|
| `guild_id` | Filter to a specific Discord server. |
| `status` | Filter by status. Accepted values: `scheduled`, `fired`, `failed`, `cancelled`. |
| `limit` | Maximum number of results to return. Defaults to `50`, maximum `200`. |
| `offset` | Number of results to skip for pagination. Defaults to `0`. |

## Prerequisites

* [Docker](https://docs.docker.com/get-docker/) and Docker Compose.

Running without Docker requires Python 3.12 and [uv](https://docs.astral.sh/uv/). On Windows, install uv with `winget install --id astral-sh.uv`.

## Setup

You can use the included setup script to prepare the project in a single step.

On Windows, run the following command:

```
setup.bat
```

On macOS or Linux, run the following commands:

```
chmod +x setup.sh
./setup.sh
```

The script runs `uv sync`, which creates the `.venv` virtual environment if needed and installs the package with its locked dependencies. It copies `.env.template` to `.env` on the first run. You must edit `.env` and set `DISCORD_API_SECRET` before starting the API.

If you prefer to perform the setup manually, follow these steps:

```bash
uv sync
cp .env.template .env
# Edit .env and set DISCORD_API_SECRET and other values as needed.
uv run uvicorn scheduler_api.main:app --port 8004
```

### Docker

Alternatively, you can run the API as a Docker container.

1. Copy `.env.template` to `.env` and set `DISCORD_API_SECRET`.
2. Build and start the container:

   ```
   docker-compose up --build
   ```

The container runs on port `8000` internally. Docker Compose maps it to port `8004` on the host. The Docker Compose configuration also creates a named volume for the SQLite database so that reminders persist across container restarts.
The image has a health check that calls `/health`, so Docker marks the container unhealthy when the service stops answering.

## Configuration

All configuration is read from environment variables or from a `.env` file in the project root.

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_API_SECRET` | Yes | None | Shared bearer token of at least 16 characters. All Discord bots must send this value in the `Authorization` header. The service refuses to start with a placeholder such as `changeme`. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `SCHEDULER_DB_PATH` | No | `/data/scheduler.db` | Absolute path to the SQLite database file. The directory must be writable. |
| `LOG_LEVEL` | No | `INFO` | Log verbosity. Accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `DISPATCHER_MAX_RETRIES` | No | `3` | Number of delivery retry attempts before a reminder is marked as failed. |

The service logs one JSON object per line, including uvicorn's access log.

## Project structure

```
discord-api-scheduler/
├── src/scheduler_api/
│   ├── main.py             # FastAPI application and route definitions.
│   ├── config.py           # This service's settings on top of ServiceSettings.
│   ├── service.py          # Shared settings, secret validation, and the version lookup.
│   ├── logging_config.py   # Structured JSON logging, including uvicorn's loggers.
│   ├── auth.py             # Bearer token dependency.
│   ├── models.py           # Pydantic request and response models.
│   ├── database.py         # SQLite schema and async query helpers.
│   ├── reminder_store.py   # Business logic for creating, listing, and cancelling reminders.
│   ├── scheduler.py        # APScheduler setup and job lifecycle management.
│   └── dispatcher.py       # Webhook and callback delivery with retry logic.
├── tests/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml      # Project metadata and dependencies.
├── uv.lock             # Locked dependency versions.
├── ruff.toml           # Lint and format settings on top of the shared baseline.
├── setup.bat           # Windows setup script.
├── setup.sh            # macOS and Linux setup script.
└── .env.template       # Template for environment variables.
```

## Running tests

```bash
uv run pytest
```

Run every lint and format check with `uvx pre-commit run --all-files`, or install the hooks once with `uvx pre-commit install` so they run on each commit.
The coding, prose, and commit conventions are documented in [discord-dev-standards](https://github.com/Lempki/discord-dev-standards).
