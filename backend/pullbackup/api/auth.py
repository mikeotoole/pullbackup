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


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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
