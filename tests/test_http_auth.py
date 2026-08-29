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
