import base64
import binascii
import hmac
import json
import secrets
import time
from hashlib import sha256
from hmac import compare_digest

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import settings

_HEALTH_PATH = "/api/system/health"
_LOGIN_PATH = "/login"

# Paths the middleware lets through.
#
#   * health   — the docker healthcheck and docker/compose.example.yaml depend
#                on it. Unchanged from the Basic-only design.
#   * login    — a login endpoint that required a session would be useless.
#   * logout   — clearing a dead cookie must not itself need a live one.
#
# /api/auth/session is deliberately NOT here: it is meant to be answered by the
# middleware, returning 401 when the caller has no valid credential.
_UNAUTHENTICATED_PATHS = frozenset({
    _HEALTH_PATH,
    "/api/auth/login",
    "/api/auth/logout",
})

# Client-side routes of the single-page app. These serve the same static
# index.html; the router picks the view. They must render to a signed-out
# browser or the login page is unreachable.
#
# Prefixes, not exact paths, because the router has dynamic segments
# (/tasks/:id/edit, /tasks/:id/runs, /runs/:id) — an exact list would break
# deep-linking into those views while signed out.
_SHELL_PATHS = frozenset({"/", "/login", "/favicon.svg"})
_SHELL_PREFIXES = ("/tasks", "/sources", "/runs")

# FastAPI mounts these at the web ROOT, outside /api/. They describe every
# endpoint, parameter and response shape, so they must stay behind the
# credential. Kept as a named set so the intent is greppable, even though the
# allowlist above already excludes them by construction (review finding 1 on
# PR #16, where a negated /api/ prefix exempted them by accident).
_API_DOC_PATHS = frozenset({
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
})


def _is_public_shell(path: str) -> bool:
    """True for the static SPA shell, which must render to a signed-out browser.
    Found by probing the built image: the middleware protected every non-API
    path, so /login -- the page that hosts the sign-in form -- was itself
    behind the session. A signed-out operator received a JSON 401, the client
    redirected to /login, and that 401'd in turn. There was no way to sign in.

    This is an ALLOWLIST, not `not path.startswith("/api/")`. Review finding 1
    on PR #16: FastAPI serves /openapi.json, /docs and /redoc at the web root,
    so a negated /api/ prefix also exempted them and handed an anonymous caller
    a complete map of every endpoint, parameter and response shape. That was a
    regression against base, where all three returned 401.

    Naming what IS public means anything unrecognised -- a future route, a
    metrics endpoint, a docs URL -- falls through to the middleware and stays
    protected. The shell itself is a static bundle containing no instance data;
    everything it renders comes from API calls that are still refused without a
    credential.
    """
    if path in _SHELL_PATHS:
        return True
    # SPA routes with dynamic segments, e.g. /tasks/3/edit.
    if any(path == p or path.startswith(p + "/") for p in _SHELL_PREFIXES):
        return True
    # Hashed bundle assets: /assets/index-<hash>.js and friends.
    if path.startswith("/assets/"):
        return True
    return False

SESSION_COOKIE = "pullbackup_session"

# Session identifiers revoked by an explicit logout. In-memory and
# process-local ON PURPOSE: single-user, single-process app, and a restart
# logging everyone out is the safe direction to fail.
_revoked: set[str] = set()

# Per-client failed-login history, for backoff. HTTP Basic had no login
# endpoint to hammer; a form does, so the endpoint needs its own limit.
_login_attempts: dict[str, list[float]] = {}

LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60


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


def _cookie_value(cookie_header: str, name: str) -> str:
    """Pull one cookie out of a raw Cookie header.

    Hand-rolled rather than using http.cookies.SimpleCookie because that
    parser silently drops the WHOLE header when any single cookie in it is
    malformed — an unrelated bad cookie set by something else on the same host
    would then log the operator out.
    """
    for part in cookie_header.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value.strip().strip('"')
    return ""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signing_key() -> bytes:
    """The HMAC key for session cookies.

    ``PULLBACKUP_SESSION_SECRET`` wins when set. Otherwise the key is derived
    from the configured password, which is what makes an upgrade need no new
    configuration — and, deliberately, makes CHANGING the password invalidate
    every session issued under the old one. The username is mixed in too so a
    username change is equally invalidating.
    """
    explicit = settings.session_secret
    if explicit:
        return sha256(b"pullbackup.session.v1|explicit|" + explicit.encode()).digest()
    return sha256(
        b"pullbackup.session.v1|derived|"
        + settings.http_basic_username.encode()
        + b"|"
        + settings.http_basic_password.encode()
    ).digest()


def issue_session(now: float | None = None) -> str:
    """Mint a signed session token. Callers must check credentials first."""
    payload = json.dumps(
        {"sid": secrets.token_urlsafe(16), "iat": int(now if now is not None else time.time())},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    body = _b64url_encode(payload)
    signature = hmac.new(_signing_key(), body.encode(), sha256).digest()
    return f"{body}.{_b64url_encode(signature)}"


def session_is_valid(token: str, now: float | None = None) -> bool:
    """True only for a well-formed, correctly signed, unexpired, unrevoked token."""
    if not token:
        return False
    body, separator, supplied_signature = token.partition(".")
    if not separator:
        return False
    try:
        expected = hmac.new(_signing_key(), body.encode(), sha256).digest()
        if not compare_digest(expected, _b64url_decode(supplied_signature)):
            return False
        claims = json.loads(_b64url_decode(body))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return False

    if not isinstance(claims, dict):
        return False
    session_id = claims.get("sid")
    issued_at = claims.get("iat")
    if not isinstance(session_id, str) or not isinstance(issued_at, int):
        return False
    if session_id in _revoked:
        return False

    age = (now if now is not None else time.time()) - issued_at
    # A future-dated token is rejected as well: it can only come from a clock
    # jump or a forgery attempt, and accepting it would extend the lifetime
    # past the configured maximum.
    return -60 <= age <= settings.session_max_age_seconds


def revoke_session(token: str) -> None:
    """Blacklist a token's session id so the same cookie stops working.

    The signature is verified FIRST. Review finding 3 on PR #16: this parsed the
    body and added claims['sid'] before any signature check, and
    /api/auth/logout is unauthenticated by design — so an anonymous caller could
    insert arbitrary attacker-chosen sids into an unbounded in-memory set inside
    a memory-limited container. Verifying first costs nothing and removes the
    whole class; an unsigned or forged token is simply ignored.
    """
    if not session_is_valid(token):
        return
    body, _, _ = token.partition(".")
    try:
        claims = json.loads(_b64url_decode(body))
    except (ValueError, binascii.Error, UnicodeDecodeError):  # pragma: no cover
        return  # unreachable: session_is_valid already parsed this
    sid = claims.get("sid")
    if isinstance(sid, str):
        _revoked.add(sid)


def reset_auth_state() -> None:
    """Clear per-process auth state (revocations, login throttle).

    Exists for tests and for nothing else: the state is intentionally in-memory
    and process-local, so a restart logs everyone out.
    """
    _revoked.clear()
    _login_attempts.clear()


def login_throttle_retry_after(client: str, now: float | None = None) -> int:
    """Seconds the client must wait, or 0 when it may attempt a login."""
    now = time.time() if now is None else now
    recent = [t for t in _login_attempts.get(client, []) if now - t < LOGIN_LOCKOUT_SECONDS]
    _login_attempts[client] = recent
    if len(recent) < LOGIN_MAX_FAILURES:
        return 0
    return max(1, int(LOGIN_LOCKOUT_SECONDS - (now - recent[-LOGIN_MAX_FAILURES]) + 1))


def record_failed_login(client: str, now: float | None = None) -> None:
    _login_attempts.setdefault(client, []).append(time.time() if now is None else now)


def clear_failed_logins(client: str) -> None:
    _login_attempts.pop(client, None)


class HttpBasicAuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or path in _UNAUTHENTICATED_PATHS
            or _is_public_shell(path)
        ):
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
        cookie_header = headers.get(b"cookie", b"").decode("latin-1", errors="replace")
        session_token = _cookie_value(cookie_header, SESSION_COOKIE)

        # Either credential is sufficient: the cookie for the browser, Basic
        # for scripted access and the compose healthcheck.
        if not (
            session_is_valid(session_token)
            or _authorized(authorization, username, password)
        ):
            response = JSONResponse(
                {"detail": "authentication required"},
                status_code=401,
                headers={
                    # Kept: scripted clients rely on the Basic challenge, and
                    # the SPA reaches the API through fetch(), which does not
                    # surface a native Basic dialog for a 401 response.
                    "WWW-Authenticate": 'Basic realm="Pullback"',
                    # Tells the SPA where to send the operator instead of
                    # rendering a raw 401 body.
                    "X-Pullbackup-Login": _LOGIN_PATH,
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
