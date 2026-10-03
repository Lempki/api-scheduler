"""Bearer token authentication shared by every protected route."""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings

__all__ = ["require_auth"]

# auto_error is off, so a missing header reaches require_auth.
# There it gets the same 401 answer as a wrong token.
_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    """Builds the 401 response that RFC 9110 prescribes for missing or invalid credentials."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Rejects the request unless it carries the service's bearer token.

    Raises:
        HTTPException: 401 when the token is missing or wrong.
    """
    if credentials is None:
        raise _unauthorized("Missing bearer token.")
    expected = settings.api_secret.get_secret_value().encode()
    # A constant-time comparison does not reveal how much of a guessed token was correct.
    if not secrets.compare_digest(credentials.credentials.encode(), expected):
        raise _unauthorized("Invalid bearer token.")
