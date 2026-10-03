"""Request and response models, including the Discord Execute Webhook body."""

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

SNOWFLAKE_PATTERN = r"^[0-9]{17,20}$"

Snowflake = Annotated[str, StringConstraints(pattern=SNOWFLAKE_PATTERN)]
ReminderStatus = Literal["scheduled", "fired", "failed", "cancelled"]

_WEBHOOK_HOSTS = frozenset(
    {"discord.com", "ptb.discord.com", "canary.discord.com", "discordapp.com"}
)
_WEBHOOK_PATH = re.compile(r"/api(?:/v[0-9]+)?/webhooks/[0-9]+/[A-Za-z0-9_-]+")
_WEBHOOK_SHAPE = "https://discord.com/api/webhooks/{id}/{token}"


def validate_webhook_url(url: str) -> str:
    """Checks that a URL is a Discord Execute Webhook URL.

    Args:
        url: The URL to check.

    Returns:
        The URL unchanged.

    Raises:
        ValueError: When the scheme, host, path, or query does not match a Discord webhook.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"webhook_url must use https and look like {_WEBHOOK_SHAPE}.")
    if parts.netloc.lower() not in _WEBHOOK_HOSTS:
        hosts = ", ".join(sorted(_WEBHOOK_HOSTS))
        raise ValueError(f"webhook_url must point to one of these hosts: {hosts}.")
    if not _WEBHOOK_PATH.fullmatch(parts.path):
        raise ValueError(f"webhook_url must look like {_WEBHOOK_SHAPE}.")
    if parts.fragment:
        raise ValueError("webhook_url must not have a fragment.")
    query = parse_qsl(parts.query, keep_blank_values=True)
    names = [name for name, _ in query]
    if any(name != "thread_id" for name in names) or len(names) > 1:
        raise ValueError("webhook_url allows only one query parameter, thread_id.")
    if query and not re.fullmatch(r"[0-9]+", query[0][1]):
        raise ValueError("The thread_id in webhook_url must be numeric.")
    return url


def validate_callback_url(url: str) -> str:
    """Checks that a bot callback URL uses http or https and names a host.

    The host allowlist is checked separately, because it depends on the settings.

    Args:
        url: The URL to check.

    Returns:
        The URL unchanged.

    Raises:
        ValueError: When the scheme is not http or https or the host is missing.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("bot_callback_url must be an http or https URL with a host.")
    return url


class DiscordWebhookBody(BaseModel):
    """The JSON parameters of Discord's Execute Webhook endpoint.

    Unknown fields are refused, so a typo fails at creation time instead of at fire time.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, max_length=2000)
    username: str | None = Field(default=None, max_length=80)
    avatar_url: str | None = None
    tts: bool | None = None
    embeds: list[dict[str, Any]] | None = Field(default=None, max_length=10)
    allowed_mentions: dict[str, Any] | None = None
    components: list[dict[str, Any]] | None = None
    attachments: list[dict[str, Any]] | None = None
    flags: int | None = None
    thread_name: str | None = None
    applied_tags: list[str] | None = None
    poll: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _require_message_and_quiet_mentions(self) -> "DiscordWebhookBody":
        """Requires a visible message part and turns off mentions unless they were set."""
        if not (self.content or self.embeds or self.components or self.poll):
            raise ValueError(
                "A Discord webhook payload needs content, embeds, components, or poll."
            )
        if self.allowed_mentions is None:
            # Without this default, an @everyone in the content would ping the whole server.
            self.allowed_mentions = {"parse": []}
        return self


class CreateReminderRequest(BaseModel):
    """The body of POST /reminders.

    When webhook_url is set, payload is the Discord Execute Webhook body.
    It is validated and stored in normalized form.
    When only bot_callback_url is set, payload is a free-form JSON object for the bot.
    """

    fire_at: AwareDatetime
    channel_id: Snowflake
    guild_id: Snowflake
    # The URLs come before payload, so the payload validator can see which destination is set.
    webhook_url: str | None = None
    bot_callback_url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict, validate_default=True)

    @field_validator("fire_at")
    @classmethod
    def _fire_at_in_future(cls, value: datetime) -> datetime:
        """Rejects a fire time that is not in the future."""
        if value <= datetime.now(UTC):
            raise ValueError("fire_at must be in the future.")
        return value

    @field_validator("webhook_url")
    @classmethod
    def _check_webhook_url(cls, value: str | None) -> str | None:
        """Rejects anything that is not a Discord Execute Webhook URL."""
        return None if value is None else validate_webhook_url(value)

    @field_validator("bot_callback_url")
    @classmethod
    def _check_callback_url(cls, value: str | None) -> str | None:
        """Rejects a callback URL that is not http or https."""
        return None if value is None else validate_callback_url(value)

    @field_validator("payload")
    @classmethod
    def _check_payload(
        cls, value: dict[str, Any], info: ValidationInfo
    ) -> dict[str, Any]:
        """Validates and normalizes the payload as a Discord body when a webhook is set."""
        if info.data.get("webhook_url"):
            body = DiscordWebhookBody.model_validate(value)
            return body.model_dump(exclude_none=True)
        return value

    @model_validator(mode="after")
    def at_least_one_destination(self) -> "CreateReminderRequest":
        """Requires at least one delivery destination."""
        if not self.webhook_url and not self.bot_callback_url:
            raise ValueError("Provide webhook_url, bot_callback_url, or both.")
        return self


class ReminderResponse(BaseModel):
    """A stored reminder as the API returns it.

    For a webhook reminder, the payload is the normalized Discord body.
    For a callback-only reminder, it is the free-form object that was sent.
    """

    reminder_id: str
    fire_at: datetime
    channel_id: str
    guild_id: str
    payload: dict[str, Any]
    webhook_url: str | None
    bot_callback_url: str | None
    status: ReminderStatus
    retry_count: int
    created_at: datetime


class ReminderListResponse(BaseModel):
    """One page of reminders, with the total number of matches and the paging values used."""

    reminders: list[ReminderResponse]
    total: int
    limit: int
    offset: int


class HealthResponse(BaseModel):
    """The body of GET /health, with the scheduler state and the number of pending jobs."""

    status: str
    service: str
    version: str
    scheduler: str
    pending_jobs: int
