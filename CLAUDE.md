# api-scheduler

A FastAPI service that schedules persistent reminders on behalf of Discord bots.
Reminders are stored in SQLite on the `/data` volume and scheduled with APScheduler 3, so they survive restarts.
At fire time the service delivers each reminder to a webhook or bot callback URL, with retries on failure.
A webhook reminder's `payload` is a validated Discord Execute Webhook body, which is posted to Discord as is.
A bot callback receives an envelope signed with HMAC-SHA256 under `API_SECRET`, and only hosts in `ALLOWED_CALLBACK_HOSTS` are accepted.
This project is based on [api-template](https://github.com/Lempki/api-template).
The shared conventions live in [discord-dev-standards](https://github.com/Lempki/discord-dev-standards), and its README is the rulebook for code, prose, and commits.

## Commands

* `uv sync` installs the package and its locked dependencies into `.venv`.
* `uv run uvicorn scheduler_api.main:app --reload` starts the API on port 8000. It reads its settings from `.env`.
* `uv run pytest` runs the tests.
* `uvx pre-commit run --all-files` runs every lint and format hook.
* `docker compose up --build` runs the service in a container, exposed on host port 8004.

## Layout

* `src/scheduler_api/main.py` defines the app, the lifespan, and the routes.
* `src/scheduler_api/config.py` adds this service's settings to `ServiceSettings`.
* `src/scheduler_api/service.py` holds `ServiceSettings`, which validates the shared secret, and `service_version()`, which reads the version from pyproject.toml.
* `src/scheduler_api/logging_config.py` turns every log record, including uvicorn's, into one JSON line.
* `src/scheduler_api/auth.py` holds the bearer token dependency that protects every route except `/health`.
* `src/scheduler_api/models.py` holds the request and response models, the Discord webhook body model, and the URL checks.
* `src/scheduler_api/database.py` holds the SQLite schema and async query helpers.
* `src/scheduler_api/reminder_store.py` holds the business logic for creating, listing, and cancelling reminders.
* `src/scheduler_api/scheduler.py` sets up APScheduler, manages the job lifecycle, and applies `MISSED_REMINDER_POLICY` at startup.
* `src/scheduler_api/dispatcher.py` delivers reminders to a webhook and a signed bot callback with one shared `httpx.AsyncClient`.
  Its `fire()` docstring holds the delivery rule for 2xx, 429, other 4xx, and 5xx answers.

## Template rules

* `.template-manifest.toml` in the template lists the core files this service keeps identical to it.
* Pick up a template change with `dev-standards template-check --apply`, run against `api-template`.
* Service-specific behavior lives outside the manifest, such as `main.py`, `config.py`, `models.py`, `database.py`, `reminder_store.py`, `scheduler.py`, and `dispatcher.py`.
* Keep the version only in pyproject.toml, and keep `SERVICE` in main.py equal to the project name there.
* `uv run mypy src` must pass in strict mode, because CI runs it.
* APScheduler stays on 3.x, because 4.x is an incompatible pre-release line.
