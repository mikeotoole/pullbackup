"""Cookie-session auth (tranche 1).

The credential is unchanged — still the single
``PULLBACKUP_HTTP_BASIC_USERNAME`` / ``PULLBACKUP_HTTP_BASIC_PASSWORD`` pair.
What changes is that a browser can exchange it once for a signed session
cookie instead of re-sending it on every request.

Two properties of the previous middleware are load-bearing and are re-asserted
here rather than assumed:

  * ``/api/system/health`` stays reachable unauthenticated (the docker
    healthcheck depends on it), and
  * an unconfigured or too-short credential returns 503, never 200.
"""

import base64
import json
import time

import httpx
import pytest
from pullbackup import http_auth, main
from pullbackup.config import load_settings, settings

PASSWORD = "0123456789abcdef0123456789abcdef"
USERNAME = "pullbackup-test"

COOKIE = "pullbackup_session"


def basic_auth(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


@pytest.fixture
def configured(monkeypatch):
    """A valid single-user credential, with throttle and revocations reset."""
    monkeypatch.setitem(settings.__dict__, "http_basic_username", USERNAME)
    monkeypatch.setitem(settings.__dict__, "http_basic_password", PASSWORD)
    monkeypatch.setitem(settings.__dict__, "session_secret", "")
    http_auth.reset_auth_state()
    yield
    http_auth.reset_auth_state()


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_login_with_correct_credentials_sets_a_session_cookie(configured):
    async with client() as c:
        response = await c.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )

    assert response.status_code == 200
    cookie = response.cookies.get(COOKIE)
    assert cookie, "login must issue a session cookie"

    # The cookie must not be readable by scripts, must not ride cross-site,
    # and must never contain the password itself.
    set_cookie = response.headers["set-cookie"]
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert PASSWORD not in set_cookie


@pytest.mark.asyncio
async def test_login_with_wrong_password_is_refused_and_issues_no_cookie(configured):
    async with client() as c:
        response = await c.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": "wrong-but-long-enough-padding"},
        )

    assert response.status_code == 401
    # Refused BY THE LOGIN ENDPOINT, not by the middleware blocking the route.
    # Without this the assertion passes against a build that has no login
    # endpoint at all.
    assert response.json()["detail"] == "invalid credentials"
    assert COOKIE not in response.cookies


@pytest.mark.asyncio
async def test_login_with_wrong_username_is_refused(configured):
    async with client() as c:
        response = await c.post(
            "/api/auth/login",
            json={"username": "someone-else", "password": PASSWORD},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"
    assert COOKIE not in response.cookies


@pytest.mark.asyncio
async def test_session_cookie_authenticates_a_request_with_no_basic_header(configured):
    async with client() as c:
        login = await c.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        assert login.status_code == 200
        # httpx keeps the cookie on the client; no Authorization header is sent.
        protected = await c.get("/api/system/info")

    assert protected.status_code == 200


@pytest.mark.asyncio
async def test_basic_credentials_still_authenticate_with_no_cookie(configured):
    """Scripted access and the compose healthcheck must keep working."""
    async with client() as c:
        protected = await c.get(
            "/api/system/info",
            headers={"Authorization": basic_auth(USERNAME, PASSWORD)},
        )

    assert protected.status_code == 200
    assert not protected.cookies


@pytest.mark.asyncio
async def test_health_stays_reachable_and_everything_else_is_refused(configured):
    async with client() as c:
        health = await c.get("/api/system/health")
        info = await c.get("/api/system/info")
        tasks = await c.get("/api/tasks")
        session = await c.get("/api/auth/session")

    assert health.status_code == 200
    for denied in (info, tasks, session):
        assert denied.status_code == 401, denied.request.url
        # The SPA needs somewhere to send the operator instead of a raw 401.
        assert denied.headers["x-pullbackup-login"] == "/login"


@pytest.mark.asyncio
async def test_session_endpoint_confirms_a_logged_in_caller(configured):
    async with client() as c:
        await c.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        session = await c.get("/api/auth/session")

    assert session.status_code == 200
    assert session.json() == {"authenticated": True}


@pytest.mark.asyncio
async def test_an_unrelated_malformed_cookie_does_not_break_the_session(configured):
    """A junk cookie on the same host must not log the operator out."""
    async with client() as c:
        await c.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        token = c.cookies[COOKIE]
        protected = await c.get(
            "/api/system/info",
            headers={"Cookie": f'junk="unclosed; {COOKIE}={token}'},
        )

    assert protected.status_code == 200


@pytest.mark.asyncio
async def test_logout_makes_the_same_cookie_stop_working(configured):
    """Server-side invalidation, not just a browser-side cookie clear.

    Clearing the cookie only asks the browser to forget it. Anyone who copied
    the value must be locked out too, so the check replays the exact token.
    """
    async with client() as c:
        await c.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        token = c.cookies[COOKIE]
        before = await c.get("/api/system/info")
        await c.post("/api/auth/logout")
        replayed = await c.get(
            "/api/system/info",
            headers={"Cookie": f"{COOKIE}={token}"},
        )

    assert before.status_code == 200
    assert replayed.status_code == 401


@pytest.mark.asyncio
async def test_a_tampered_cookie_is_refused(configured):
    async with client() as c:
        await c.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        body, _, signature = c.cookies[COOKIE].partition(".")
        forged = http_auth._b64url_encode(b'{"iat":9999999999,"sid":"forged"}')

        responses = [
            # signature swapped for another valid-looking one
            await c.get("/api/system/info", headers={"Cookie": f"{COOKIE}={body}.{signature[::-1]}"}),
            # payload rewritten, original signature kept
            await c.get("/api/system/info", headers={"Cookie": f"{COOKIE}={forged}.{signature}"}),
            # unsigned payload
            await c.get("/api/system/info", headers={"Cookie": f"{COOKIE}={forged}"}),
            # structurally valid base64, semantically junk
            await c.get("/api/system/info", headers={"Cookie": f"{COOKIE}=not-a-token"}),
        ]

    for refused in responses:
        assert refused.status_code == 401


@pytest.mark.asyncio
async def test_an_expired_cookie_is_refused(configured, monkeypatch):
    monkeypatch.setitem(settings.__dict__, "session_max_age_seconds", 3600)
    expired = http_auth.issue_session(now=time.time() - 7200)
    still_fresh = http_auth.issue_session(now=time.time() - 60)

    async with client() as c:
        stale = await c.get("/api/system/info", headers={"Cookie": f"{COOKIE}={expired}"})
        fresh = await c.get("/api/system/info", headers={"Cookie": f"{COOKIE}={still_fresh}"})

    assert stale.status_code == 401
    assert fresh.status_code == 200


@pytest.mark.asyncio
async def test_changing_the_password_invalidates_existing_sessions(configured, monkeypatch):
    """The derived signing key must be bound to the credential."""
    async with client() as c:
        await c.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        token = c.cookies[COOKIE]
        before = await c.get("/api/system/info")

        monkeypatch.setitem(
            settings.__dict__, "http_basic_password", "fedcba9876543210fedcba9876543210"
        )
        after = await c.get("/api/system/info", headers={"Cookie": f"{COOKIE}={token}"})

    assert before.status_code == 200
    assert after.status_code == 401


@pytest.mark.asyncio
async def test_an_explicit_session_secret_survives_a_password_change(configured, monkeypatch):
    """The opt-in secret decouples session lifetime from the credential.

    Without this the two branches of _signing_key could collapse into one and
    the password-derived test above would still pass.
    """
    monkeypatch.setitem(settings.__dict__, "session_secret", "s" * 40)

    async with client() as c:
        await c.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        token = c.cookies[COOKIE]
        monkeypatch.setitem(
            settings.__dict__, "http_basic_password", "fedcba9876543210fedcba9876543210"
        )
        after = await c.get("/api/system/info", headers={"Cookie": f"{COOKIE}={token}"})

    assert after.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("", ""),
        (USERNAME, ""),
        (USERNAME, "too-short"),
        ("", PASSWORD),
    ],
)
async def test_unconfigured_credentials_fail_closed_everywhere(
    monkeypatch, username, password
):
    """A blanked or weak credential must never open the app.

    Checked through BOTH doors: the middleware, and the login endpoint, which
    the middleware lets through unauthenticated and so has to enforce the rule
    itself.
    """
    monkeypatch.setitem(settings.__dict__, "http_basic_username", username)
    monkeypatch.setitem(settings.__dict__, "http_basic_password", password)
    monkeypatch.setitem(settings.__dict__, "session_secret", "")
    http_auth.reset_auth_state()

    async with client() as c:
        health = await c.get("/api/system/health")
        via_middleware = await c.get("/api/system/info")
        via_login = await c.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        via_session = await c.get("/api/auth/session")

    assert health.status_code == 200
    assert via_middleware.status_code == 503
    # The important half: supplying the very credentials the instance is
    # (mis)configured with must NOT produce a session.
    assert via_login.status_code == 503
    assert COOKIE not in via_login.cookies
    assert via_session.status_code == 503

    http_auth.reset_auth_state()


@pytest.mark.asyncio
async def test_repeated_failed_logins_are_throttled(configured):
    wrong = {"username": USERNAME, "password": "wrong-password-padding-value"}

    async with client() as c:
        statuses = [(await c.post("/api/auth/login", json=wrong)).status_code
                    for _ in range(http_auth.LOGIN_MAX_FAILURES)]
        throttled = await c.post("/api/auth/login", json=wrong)
        # The lockout must also apply to the CORRECT password, otherwise it
        # does nothing to slow an attacker who eventually guesses right.
        correct = await c.post(
            "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )

    assert statuses == [401] * http_auth.LOGIN_MAX_FAILURES
    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) > 0
    assert correct.status_code == 429
    assert COOKIE not in correct.cookies


@pytest.mark.asyncio
async def test_the_throttle_expires_so_an_operator_is_not_locked_out_forever(configured):
    client_key = "testclient"
    now = time.time()
    for _ in range(http_auth.LOGIN_MAX_FAILURES):
        http_auth.record_failed_login(client_key, now=now)

    assert http_auth.login_throttle_retry_after(client_key, now=now) > 0
    assert http_auth.login_throttle_retry_after(
        client_key, now=now + http_auth.LOGIN_LOCKOUT_SECONDS + 1
    ) == 0


@pytest.mark.asyncio
async def test_a_successful_login_clears_the_failure_count(configured):
    wrong = {"username": USERNAME, "password": "wrong-password-padding-value"}

    async with client() as c:
        for _ in range(http_auth.LOGIN_MAX_FAILURES - 1):
            await c.post("/api/auth/login", json=wrong)
        good = await c.post(
            "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )
        # With the counter cleared there is a full budget again.
        after = [(await c.post("/api/auth/login", json=wrong)).status_code
                 for _ in range(http_auth.LOGIN_MAX_FAILURES)]

    assert good.status_code == 200
    assert after == [401] * http_auth.LOGIN_MAX_FAILURES


def test_documented_session_env_vars_are_actually_honoured():
    """README and .env.example promise these names work; prove they do.

    A config field is only reachable if load_settings maps its env name onto
    it. Documenting a variable the loader ignores is a silent lie that shows up
    as "my sessions still expire in 7 days".
    """
    loaded = load_settings(
        {
            "PULLBACKUP_HTTP_BASIC_USERNAME": USERNAME,
            "PULLBACKUP_HTTP_BASIC_PASSWORD": PASSWORD,
            "PULLBACKUP_SESSION_SECRET": "an-explicit-signing-secret",
            "PULLBACKUP_SESSION_MAX_AGE_SECONDS": "3600",
        },
        env_file=None,
    )

    assert loaded.session_secret == "an-explicit-signing-secret"
    assert loaded.session_max_age_seconds == 3600


def test_session_defaults_to_seven_days_when_unset():
    loaded = load_settings(
        {
            "PULLBACKUP_HTTP_BASIC_USERNAME": USERNAME,
            "PULLBACKUP_HTTP_BASIC_PASSWORD": PASSWORD,
        },
        env_file=None,
    )

    assert loaded.session_secret == ""
    assert loaded.session_max_age_seconds == 7 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_the_login_page_itself_loads_without_a_session(configured):
    """The one route that MUST render to a signed-out browser.

    Found by probing the built image, not by the suite: the middleware
    protected every non-API path including /login, so the SPA shell that hosts
    the sign-in form was itself behind the session. An unauthenticated operator
    got a JSON 401 body, the client redirected to /login, and that 401'd too --
    an unbreakable loop with no way to sign in at all.

    The API routes behind it stay protected; only the static shell is public,
    and it contains no data.
    """
    async with client() as c:
        login_page = await c.get("/login")
        # The assets the shell needs must load too, or it renders blank.
        root = await c.get("/")
        # ...while the API underneath stays shut.
        tasks = await c.get("/api/tasks")

    assert login_page.status_code != 401, "signed-out operator cannot reach the login page"
    assert root.status_code != 401
    assert tasks.status_code == 401


@pytest.mark.asyncio
async def test_the_public_shell_carve_out_never_exposes_an_api_route(configured):
    """The counterweight to the exemption above.

    Making the SPA shell public is the single most dangerous edit in this
    change: widened by one character it becomes "everything is public". This
    walks every route the app actually registers and asserts that each /api
    route is still refused without a credential.
    """
    from pullbackup import main

    # Read the registered paths from the OpenAPI schema rather than walking
    # app.routes: this FastAPI version wraps included routers in an opaque
    # _IncludedRouter whose .path is None, so a naive walk silently found
    # nothing and the test passed against zero routes. The anti-vacuity
    # assertion below is what caught that.
    api_paths = sorted(
        p for p in main.app.openapi()["paths"] if p.startswith("/api/") and "{" not in p
    )
    # If this list ever empties, the test passes vacuously and guards nothing.
    assert len(api_paths) >= 5, api_paths

    exempt = {"/api/system/health", "/api/auth/login", "/api/auth/logout"}
    async with client() as c:
        for path in api_paths:
            if path in exempt:
                continue
            response = await c.get(path)
            assert response.status_code == 401, f"{path} is reachable unauthenticated"


def test_the_shell_carve_out_is_scoped_to_non_api_paths():
    assert http_auth._is_public_shell("/login") is True
    assert http_auth._is_public_shell("/") is True
    assert http_auth._is_public_shell("/assets/index-abc123.js") is True
    assert http_auth._is_public_shell("/favicon.svg") is True

    assert http_auth._is_public_shell("/api/tasks") is False
    assert http_auth._is_public_shell("/api/sources") is False
    assert http_auth._is_public_shell("/api/runs/1/log") is False
    assert http_auth._is_public_shell("/api/system/ssh-pubkey") is False


# --------------------------------------------------------------------------
# review findings on PR #16 (kanban review card t_b1f20cff)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_openapi_schema_is_not_reachable_without_a_credential(configured):
    """Finding 1 (medium): the shell carve-out was `not path.startswith("/api/")`,
    but FastAPI serves /openapi.json, /docs and /redoc at the web ROOT. The
    exemption meant for the login page also handed an anonymous caller a complete
    map of every endpoint, parameter and response shape.

    Confirmed a regression: base 567d70f returned 401 for all three, this branch
    returned 200. The existing carve-out guard could not catch it because it only
    enumerates /api/-prefixed paths.
    """
    async with client() as c:
        for path in ("/openapi.json", "/docs", "/redoc"):
            response = await c.get(path)
            assert response.status_code == 401, (
                f"{path} must require a credential; got {response.status_code}. "
                "The shell carve-out is for the SPA, not for API documentation."
            )


@pytest.mark.asyncio
async def test_unrecognised_root_paths_are_protected_not_exempt(configured):
    """The carve-out must name what IS public rather than negate one prefix.

    A negated prefix silently exempts every future route mounted outside /api/,
    which is exactly how the schema leaked.
    """
    async with client() as c:
        for path in ("/metrics", "/admin", "/config.json"):
            response = await c.get(path)
            assert response.status_code == 401, (
                f"unrecognised path {path} must fall through to the middleware; "
                f"got {response.status_code}"
            )


def test_the_signed_out_browser_can_still_load_the_login_page(configured):
    """Narrowing the carve-out must not re-break the defect it was added for.

    Asserted against the predicate rather than by fetching "/", because the
    built frontend is not present in a source checkout — an HTTP assertion here
    would fail with 404 for environmental reasons and pass for the wrong ones.
    """
    for path in ("/", "/login", "/assets/index-abc123.js", "/favicon.svg"):
        assert http_auth._is_public_shell(path), (
            f"{path} must stay reachable to a signed-out browser, otherwise the "
            "login page is once again trapped behind the login"
        )


def test_revoking_a_session_requires_a_valid_signature(configured):
    """Finding 3 (low): revoke_session() added claims['sid'] to the revocation set
    BEFORE verifying the signature, and /api/auth/logout is unauthenticated by
    design. An anonymous caller could insert arbitrary sids into an unbounded
    in-memory set inside a 512 MB container.

    Proven against the built image: three POSTs with a forged body and a garbage
    signature each returned 200 and each added an entry.
    """
    payload = json.dumps({"sid": "attacker-controlled", "iat": 1, "exp": 9999999999})
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    forged = f"{body}.AAAA"

    http_auth.revoke_session(forged)

    assert "attacker-controlled" not in http_auth._revoked, (
        "revoke_session accepted an unsigned token; an anonymous caller can grow "
        "the revocation set without ever holding a valid session"
    )


@pytest.mark.asyncio
async def test_logout_with_a_forged_cookie_does_not_grow_the_revocation_set(configured):
    """The same defect through the real unauthenticated endpoint."""
    before = len(http_auth._revoked)

    async with client() as c:
        for index in range(3):
            payload = json.dumps({"sid": f"attacker-{index}", "iat": 1, "exp": 9999999999})
            body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
            response = await c.post(
                "/api/auth/logout",
                cookies={COOKIE: f"{body}.AAAA"},
            )
            assert response.status_code == 200, "logout stays reachable for a dead cookie"

    assert len(http_auth._revoked) == before, (
        f"forged logout tokens grew the revocation set: {before} -> {len(http_auth._revoked)}"
    )
