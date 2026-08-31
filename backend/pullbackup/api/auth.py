# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""Login, logout, and session probe for the browser.

The credential itself is unchanged. These endpoints only let a browser trade
it once for a signed session cookie so the operator sees a real login form
instead of the browser's Basic prompt. Scripted callers keep using Basic and
never need to touch any of this.
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import http_auth
from ..config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


def _forwarded_for_value(headers) -> str:
    """Every ``X-Forwarded-For`` line, comma-joined.

    Review finding 1 on PR #19 (HIGH). This used ``headers.get(...)``, which
    returns only the FIRST matching header line. A proxy is free to emit its own
    separate line rather than appending to the client's — HAProxy's
    ``option forwardfor`` does exactly that, while nginx's
    ``$proxy_add_x_forwarded_for`` appends in place. Reading only the first line
    therefore walked the CLIENT's list and never saw the proxy's entry, leaving
    the rightmost hop fully attacker-controlled. A client could mint a fresh
    identity per request and the throttle stopped existing: eight consecutive
    failed logins behind a real HAProxy were never throttled.

    RFC 7230 section 3.2.2 makes repeated headers equivalent to one comma-joined
    value, so joining is both correct and what the right-to-left walk assumes.
    """
    return ",".join(headers.getlist(http_auth.FORWARDED_FOR_HEADER))


def _client_key(request: Request) -> str:
    """The identity the failed-login throttle is keyed on.

    Not simply ``request.client.host``: behind the reverse proxy that
    SECURITY.md and docker/compose.example.yaml both recommend, that value is
    the proxy for every caller, so all clients would share one failure bucket
    and five wrong guesses by anybody would lock the login form for everybody.

    ``X-Forwarded-For`` is consulted only when the immediate peer is listed in
    ``PULLBACKUP_TRUSTED_PROXIES``, which is empty by default. See
    ``http_auth.resolve_client_identity`` for why the walk goes right to left.
    """
    peer = request.client.host if request.client else None
    return http_auth.resolve_client_identity(
        peer,
        _forwarded_for_value(request.headers),
    )


def _unconfigured() -> JSONResponse | None:
    """503 when the credential is missing or too weak.

    Same fail-closed rule as the middleware, asserted separately here so the
    login endpoint can never become a way in on an unconfigured instance.
    """
    if not http_auth._valid_configuration(
        settings.http_basic_username,
        settings.http_basic_password,
    ):
        return JSONResponse({"detail": "authentication is not configured"}, status_code=503)
    return None


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        http_auth.SESSION_COOKIE,
        token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        # Only mark Secure when the request actually arrived over https;
        # a Secure cookie on a plain-http LAN deployment is simply dropped
        # by the browser, which would make login silently fail.
        secure=request.url.scheme == "https",
        path="/",
    )


@router.post("/login")
def login(payload: LoginRequest, request: Request) -> Response:
    unconfigured = _unconfigured()
    if unconfigured is not None:
        return unconfigured

    client = _client_key(request)
    retry_after = http_auth.login_throttle_retry_after(client)
    if retry_after:
        return JSONResponse(
            {"detail": "too many failed sign-in attempts"},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    username_ok = http_auth._constant_time_equal(
        payload.username, settings.http_basic_username
    )
    password_ok = http_auth._constant_time_equal(
        payload.password, settings.http_basic_password
    )
    if not (username_ok and password_ok):
        http_auth.record_failed_login(client)
        return JSONResponse({"detail": "invalid credentials"}, status_code=401)

    http_auth.clear_failed_logins(client)
    response = JSONResponse({"authenticated": True})
    _set_session_cookie(response, request, http_auth.issue_session())
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    token = request.cookies.get(http_auth.SESSION_COOKIE, "")
    if token:
        http_auth.revoke_session(token)
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(http_auth.SESSION_COOKIE, path="/")
    return response


@router.get("/session")
def session(request: Request) -> Response:
    unconfigured = _unconfigured()
    if unconfigured is not None:
        return unconfigured
    # Reaching this handler already means the middleware accepted the request,
    # so the answer is yes by construction.
    return JSONResponse({"authenticated": True})
