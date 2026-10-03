"""Settings and metadata that every api-* service shares.

A service's config.py subclasses ServiceSettings and adds only its own fields.
"""

from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["MIN_SECRET_LENGTH", "ServiceSettings", "service_version"]

MIN_SECRET_LENGTH = 16

# Placeholders that have appeared in .env templates. A service must never run with one.
_PLACEHOLDER_SECRETS = frozenset({"changeme", "your-secret-here", "secret"})

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ServiceSettings(BaseSettings):
    """The settings every service reads from the environment or from .env.

    Attributes:
        api_secret: The bearer token that callers must send. It is never logged or printed.
        log_level: The minimum level of log records to emit.
    """

    # hide_input_in_errors keeps a rejected secret out of the startup error and the logs.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    api_secret: SecretStr
    log_level: LogLevel = "INFO"

    @field_validator("api_secret")
    @classmethod
    def _reject_weak_secret(cls, secret: SecretStr) -> SecretStr:
        value = secret.get_secret_value()
        if value.strip().lower() in _PLACEHOLDER_SECRETS:
            raise ValueError(
                "API_SECRET is still a placeholder. Generate a real secret."
            )
        if len(value) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"API_SECRET must be at least {MIN_SECRET_LENGTH} characters."
            )
        return secret


def service_version(distribution: str) -> str:
    """Returns the installed version of a service.

    The version lives only in pyproject.toml.
    The health endpoint and the OpenAPI docs read it from the installed package.

    Args:
        distribution: The project name from pyproject.toml, such as "api-media".

    Returns:
        The version, or "0.0.0" when the project is not installed.
    """
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "0.0.0"
