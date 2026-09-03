import base64

import httpx
import pytest
from pullbackup import main
from pullbackup.config import settings


def basic_auth(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


@pytest.mark.asyncio
async def test_http_auth_fails_closed_when_credentials_are_unconfigured(monkeypatch):
    monkeypatch.setitem(settings.__dict__, "http_basic_username", "")
    monkeypatch.setitem(settings.__dict__, "http_basic_password", "")
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/api/system/health")
        protected = await client.get("/api/system/info")

    assert health.status_code == 200
    assert protected.status_code == 503


@pytest.mark.asyncio
async def test_http_auth_protects_every_non_health_route(monkeypatch):
    password = bytes(range(16)).hex()
    monkeypatch.setitem(settings.__dict__, "http_basic_username", "pullback-test")
    monkeypatch.setitem(settings.__dict__, "http_basic_password", password)
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/api/system/health")
        missing = await client.get("/api/system/info")
        malformed = await client.get(
            "/api/system/info",
            headers={"Authorization": "Basic not-base64"},
        )
        wrong = await client.get(
            "/api/system/info",
            headers={"Authorization": basic_auth("pullback-test", "wrong")},
        )
        allowed = await client.get(
            "/api/system/info",
            headers={"Authorization": basic_auth("pullback-test", password)},
        )

    assert health.status_code == 200
    for denied in (missing, malformed, wrong):
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == 'Basic realm="Pullback"'
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_a_browser_fetch_is_refused_without_a_basic_challenge(monkeypatch):
    """The native credential dialog must not appear before the login page.

    A browser pops its own Basic dialog for any 401 carrying
    ``WWW-Authenticate: Basic`` — including one answering a same-origin
    ``fetch()``. The SPA loads its shell unauthenticated, fires its first
    ``/api/*`` call with no session cookie, and the operator gets the native
    dialog instead of the login form. Refusing the call is right; advertising
    Basic to a browser subresource request is not.
    """
    password = bytes(range(16)).hex()
    monkeypatch.setitem(settings.__dict__, "http_basic_username", "pullback-test")
    monkeypatch.setitem(settings.__dict__, "http_basic_password", password)
    transport = httpx.ASGITransport(app=main.app)

    # Exactly what Chrome/Firefox/Safari put on a same-origin fetch(), plus the
    # explicit marker the SPA sends so the suppression does not depend on
    # sniffing alone.
    browser_fetch = {
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    explicit = {"X-Pullbackup-Client": "web"}
    # EventSource cannot set a custom header, so the log stream relies on this.
    event_stream = {"Accept": "text/event-stream"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sniffed = await client.get("/api/system/info", headers=browser_fetch)
        marked = await client.get("/api/system/info", headers=explicit)
        streamed = await client.get("/api/runs/1/log/stream", headers=event_stream)

    for denied in (sniffed, marked, streamed):
        # Still refused, and still 401 — never 200, never a redirect.
        assert denied.status_code == 401
        assert "www-authenticate" not in denied.headers
        # The SPA still learns where to send the operator.
        assert denied.headers["x-pullbackup-login"] == "/login"


@pytest.mark.asyncio
async def test_a_scripted_client_still_gets_the_basic_challenge(monkeypatch):
    """Regression guard on the fix: suppression must not reach API clients.

    curl, the compose healthcheck and any scripted caller send no ``Sec-Fetch-*``
    headers, so they must keep receiving the challenge. A browser that *navigates*
    straight to ``/openapi.json`` also keeps it — that is the one browser case
    where the native dialog is the intended way in.
    """
    password = bytes(range(16)).hex()
    monkeypatch.setitem(settings.__dict__, "http_basic_username", "pullback-test")
    monkeypatch.setitem(settings.__dict__, "http_basic_password", password)
    transport = httpx.ASGITransport(app=main.app)

    navigation = {
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        scripted = await client.get("/api/system/info")
        docs = await client.get("/openapi.json")
        navigated = await client.get("/openapi.json", headers=navigation)
        # Suppressing the CHALLENGE must not stop Basic from AUTHENTICATING,
        # including on a browser-shaped request.
        allowed = await client.get(
            "/api/system/info",
            headers={
                **navigation,
                "Sec-Fetch-Mode": "cors",
                "X-Pullbackup-Client": "web",
                "Authorization": basic_auth("pullback-test", password),
            },
        )

    for denied in (scripted, docs, navigated):
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == 'Basic realm="Pullback"'
    assert allowed.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("", ""),
        ("pullback-test", "too-short"),
        ("invalid:user", "0123456789abcdef0123456789abcdef"),
    ],
)
async def test_lifespan_rejects_invalid_http_auth_configuration(
    monkeypatch, username, password
):
    monkeypatch.setattr(settings, "http_basic_username", username)
    monkeypatch.setattr(settings, "http_basic_password", password)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main.runner, "reconcile_stale_runs", lambda: [])
    monkeypatch.setattr(main.ssh, "ensure_default_key", lambda: None)
    monkeypatch.setattr(main.scheduler, "start", lambda: None)
    monkeypatch.setattr(main.scheduler, "shutdown", lambda: None)

    with pytest.raises(RuntimeError, match="HTTP Basic authentication"):
        async with main.lifespan(main.app):
            pass
