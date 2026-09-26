"""The FastAPI application, its lifespan, and its routes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status

from . import database, reminder_store, scheduler
from .auth import require_auth
from .config import get_settings
from .logging_config import configure_logging
from .models import (
    CreateReminderRequest,
    HealthResponse,
    ReminderListResponse,
    ReminderResponse,
)
from .service import service_version

# The service name is also the project name in pyproject.toml, which the version is read from.
SERVICE = "discord-api-scheduler"
VERSION = service_version(SERVICE)

# Logging is set up on import, before uvicorn prints its startup lines, so every line is JSON.
configure_logging(get_settings().log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Opens the database and starts the scheduler, then stops both on shutdown."""
    settings = get_settings()
    await database.init(settings.scheduler_db_path)
    await scheduler.start(settings.dispatcher_max_retries)
    yield
    scheduler.stop()
    await database.close()


app = FastAPI(title=SERVICE, version=VERSION, lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Reports that the service is up. It needs no token, so monitors and Docker can call it."""
    sched = scheduler._scheduler
    return HealthResponse(
        status="ok",
        service=SERVICE,
        version=VERSION,
        scheduler="running" if sched and sched.running else "stopped",
        pending_jobs=scheduler.pending_count(),
    )


@app.post(
    "/reminders",
    response_model=ReminderResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_auth)],
)
async def create_reminder(body: CreateReminderRequest) -> ReminderResponse:
    return await reminder_store.create(body)


@app.get(
    "/reminders",
    response_model=ReminderListResponse,
    dependencies=[Depends(require_auth)],
)
async def list_reminders(
    guild_id: str | None = Query(default=None),
    reminder_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ReminderListResponse:
    return await reminder_store.list_reminders(guild_id, reminder_status, limit, offset)


@app.get(
    "/reminders/{reminder_id}",
    response_model=ReminderResponse,
    dependencies=[Depends(require_auth)],
)
async def get_reminder(reminder_id: str) -> ReminderResponse:
    result = await reminder_store.get(reminder_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reminder not found."
        )
    return result


@app.delete(
    "/reminders/{reminder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_auth)],
)
async def cancel_reminder(reminder_id: str) -> None:
    cancelled = await reminder_store.cancel(reminder_id)
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reminder not found or already fired/cancelled.",
        )
