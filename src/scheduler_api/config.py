"""This service's settings, read from the environment or from .env."""

from functools import lru_cache

from .service import ServiceSettings

__all__ = ["Settings", "get_settings"]


class Settings(ServiceSettings):
    """The shared settings plus this service's own.

    Each field reads the environment variable of the same name in upper case.

    Attributes:
        scheduler_db_path: The path to the SQLite database file.
        dispatcher_max_retries: The retries allowed before a reminder is marked as failed.
    """

    scheduler_db_path: str = "/data/scheduler.db"
    dispatcher_max_retries: int = 3


@lru_cache
def get_settings() -> Settings:
    """Returns the settings, read once and then cached for the process."""
    return Settings()
