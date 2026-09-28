"""This service's settings, read from the environment or from .env."""

import json
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import NoDecode

from .service import ServiceSettings

__all__ = ["MissedReminderPolicy", "Settings", "get_settings"]

MissedReminderPolicy = Literal["fire", "fail"]


class Settings(ServiceSettings):
    """The shared settings plus this service's own.

    Each field reads the environment variable of the same name in upper case.

    Attributes:
        scheduler_db_path: The path to the SQLite database file.
        dispatcher_max_retries: The retries allowed before a reminder is marked as failed.
        allowed_callback_hosts: The hostnames a bot_callback_url may point to.
            The list is empty by default, which refuses every callback.
        missed_reminder_policy: What startup does with a reminder that came due during downtime.
            "fire" delivers it at once and "fail" marks it failed.
    """

    scheduler_db_path: str = "/data/scheduler.db"
    dispatcher_max_retries: int = Field(default=3, ge=0)
    # NoDecode stops pydantic-settings from requiring JSON, so a comma-separated value works too.
    allowed_callback_hosts: Annotated[list[str], NoDecode] = []
    missed_reminder_policy: MissedReminderPolicy = "fire"

    @field_validator("allowed_callback_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: Any) -> Any:
        """Accepts a JSON list or a comma-separated string and lowercases each hostname.

        Args:
            value: The raw value from the environment, from .env, or from a keyword argument.

        Returns:
            A list of lowercase hostnames, or the value unchanged when it is neither form.
        """
        if isinstance(value, str):
            text = value.strip()
            value = json.loads(text) if text.startswith("[") else text.split(",")
        if isinstance(value, list):
            return [str(host).strip().lower() for host in value if str(host).strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Returns the settings, read once and then cached for the process."""
    return Settings()
