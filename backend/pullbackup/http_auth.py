import base64
import binascii
from hashlib import sha256
from hmac import compare_digest

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import settings

_HEALTH_PATH = "/api/system/health"


def _valid_configuration(username: str, password: str) -> bool:
    return (
        bool(username)
        and ":" not in username
        and not any(ord(char) < 32 or ord(char) == 127 for char in username)
        and len(password) >= 32
        and not any(ord(char) < 32 or ord(char) == 127 for char in password)
    )


def require_valid_configuration() -> None:
    if not _valid_configuration(
        settings.http_basic_username,
        settings.http_basic_password,
    ):
        raise RuntimeError(
            "HTTP Basic authentication requires a valid username and "
            "a password of at least 32 characters"
        )


def _constant_time_equal(left: str, right: str) -> bool:
    return compare_digest(
        sha256(left.encode()).digest(),
        sha256(right.encode()).digest(),
    )


def _authorized(header: str, username: str, password: str) -> bool:
    try:
        scheme, encoded = header.split(" ", 1)
        if scheme.lower() != "basic":
            return False
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        supplied_username, separator, supplied_password = decoded.partition(":")
        if not separator:
            return False
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False

    username_matches = _constant_time_equal(supplied_username, username)
    password_matches = _constant_time_equal(supplied_password, password)
    return username_matches and password_matches


class HttpBasicAuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == _HEALTH_PATH:
            await self.app(scope, receive, send)
            return

        username = settings.http_basic_username
        password = settings.http_basic_password
        if not _valid_configuration(username, password):
            response = JSONResponse(
                {"detail": "authentication is not configured"},
                status_code=503,
            )
            await response(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode(
            "latin-1",
            errors="replace",
        )
        if not _authorized(authorization, username, password):
            response = JSONResponse(
                {"detail": "authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Pullback"'},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
