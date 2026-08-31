"""The login throttle behind a reverse proxy.

``SECURITY.md`` and the Traefik block in ``docker/compose.example.yaml`` both
recommend running this app behind a reverse proxy. When it is, the immediate
peer is the proxy for every caller, so keying the failed-login throttle on
``request.client.host`` puts every client in ONE failure bucket: five wrong
guesses by anybody lock the login form for everybody, renewable indefinitely.
A self-inflicted denial of service.

Trusting ``X-Forwarded-For`` unconditionally would be strictly worse — an
attacker could then mint a fresh identity per request and defeat the throttle
entirely. So the fix is a configured allowlist of trusted proxies, and the
tests below pin both halves: the header is honoured only when the peer is
allowlisted, and even then only as far as the rightmost UNTRUSTED hop.
"""

import threading
import time

import httpx
import pytest
from pullbackup import http_auth
from pullbackup.api.auth import _forwarded_for_value
from pullbackup.config import ConfigurationError, load_settings, settings

PASSWORD = "0123456789abcdef0123456789abcdef"
USERNAME = "pullbackup-test"
COOKIE = "pullbackup_session"

PROXY = "10.9.0.1"
ATTACKER = "203.0.113.9"
VICTIM = "198.51.100.7"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setitem(settings.__dict__, "http_basic_username", USERNAME)
    monkeypatch.setitem(settings.__dict__, "http_basic_password", PASSWORD)
    monkeypatch.setitem(settings.__dict__, "session_secret", "")
    monkeypatch.setitem(settings.__dict__, "trusted_proxies", "")
    http_auth.reset_auth_state()
    yield
    http_auth.reset_auth_state()


def client(peer: str = "testclient") -> httpx.AsyncClient:
    """An ASGI client whose immediate peer address is ``peer``.

    ``request.client.host`` is what the throttle keys on today, so the peer has
    to be controllable for any of this to be testable end to end.
    """
    from pullbackup import main

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app, client=(peer, 41234)),
        base_url="http://test",
    )


async def _fail_login(c: httpx.AsyncClient, forwarded_for: str | None = None):
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
    return await c.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": "wrong-password-padding-value"},
        headers=headers,
    )


async def _correct_login(c: httpx.AsyncClient, forwarded_for: str | None = None):
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
    return await c.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers=headers,
    )


@pytest.mark.asyncio
async def test_two_clients_behind_a_trusted_proxy_get_independent_buckets(
    configured, monkeypatch
):
    """The defect itself: one client must not be able to lock out another.

    Reproduced live by the PR #16 reviewer against the built image — an
    attacker burned five attempts from one address and the victim, sending the
    CORRECT password from a different address, received 429.
    """
    monkeypatch.setitem(settings.__dict__, "trusted_proxies", PROXY)

    async with client(peer=PROXY) as c:
        attacker = [
            (await _fail_login(c, forwarded_for=ATTACKER)).status_code
            for _ in range(http_auth.LOGIN_MAX_FAILURES)
        ]
        attacker_throttled = await _fail_login(c, forwarded_for=ATTACKER)
        victim = await _correct_login(c, forwarded_for=VICTIM)

    assert attacker == [401] * http_auth.LOGIN_MAX_FAILURES
    assert attacker_throttled.status_code == 429, "the attacker must still be throttled"
    assert victim.status_code == 200, (
        "a different client behind the same proxy shares the attacker's failure "
        "bucket; five wrong guesses lock the login form for everyone"
    )
    assert victim.cookies.get(COOKIE), "the victim must actually get a session"


@pytest.mark.asyncio
async def test_a_spoofed_header_from_an_untrusted_peer_cannot_evade_the_throttle(
    configured, monkeypatch
):
    """The dangerous direction, pinned.

    With no allowlist entry covering the peer, ``X-Forwarded-For`` must be
    ignored outright. If it were honoured, an attacker would mint a fresh
    identity per request and never be throttled at all — strictly worse than
    the shared-bucket defect this change fixes.
    """
    monkeypatch.setitem(settings.__dict__, "trusted_proxies", PROXY)

    async with client(peer=ATTACKER) as c:
        statuses = [
            (await _fail_login(c, forwarded_for=f"10.0.0.{index}")).status_code
            for index in range(http_auth.LOGIN_MAX_FAILURES)
        ]
        after = await _fail_login(c, forwarded_for="10.0.0.99")

    assert statuses == [401] * http_auth.LOGIN_MAX_FAILURES
    assert after.status_code == 429, (
        "a forwarding header from a peer that is not an allowlisted proxy was "
        "honoured; the throttle can be evaded with one header"
    )


@pytest.mark.asyncio
async def test_a_trusted_proxy_client_cannot_impersonate_another_client(
    configured, monkeypatch
):
    """Right-to-left, not leftmost.

    A real proxy APPENDS the address it saw, so the header the app receives is
    ``<whatever the client sent>, <the client's real address>``. Taking the
    leftmost entry would hand the attacker a fresh identity per request; taking
    the rightmost untrusted entry pins it to the address the proxy observed.
    """
    monkeypatch.setitem(settings.__dict__, "trusted_proxies", PROXY)

    async with client(peer=PROXY) as c:
        # Attacker sends "X-Forwarded-For: <victim>"; the proxy appends the
        # attacker's real address.
        statuses = [
            (await _fail_login(c, forwarded_for=f"{VICTIM}, {ATTACKER}")).status_code
            for _ in range(http_auth.LOGIN_MAX_FAILURES)
        ]
        # ...and a crafted chain of invented hops must not buy a fresh bucket.
        crafted = await _fail_login(c, forwarded_for=f"10.1.1.1, 10.2.2.2, {ATTACKER}")
        victim = await _correct_login(c, forwarded_for=VICTIM)

    assert statuses == [401] * http_auth.LOGIN_MAX_FAILURES
    assert crafted.status_code == 429, (
        "prepending hops to X-Forwarded-For bought a fresh failure bucket; the "
        "leftmost value is attacker-controlled and must never be the identity"
    )
    assert victim.status_code == 200, (
        "the impersonated address was charged for the attacker's failures"
    )


@pytest.mark.asyncio
async def test_an_empty_allowlist_behaves_exactly_as_before(configured):
    """The default. An operator who upgrades and changes nothing sees no change.

    With no allowlist the header is ignored, so two callers arriving on the
    same peer still share a bucket — today's behaviour, unchanged.
    """
    async with client(peer=PROXY) as c:
        statuses = [
            (await _fail_login(c, forwarded_for=ATTACKER)).status_code
            for _ in range(http_auth.LOGIN_MAX_FAILURES)
        ]
        other = await _correct_login(c, forwarded_for=VICTIM)

    assert statuses == [401] * http_auth.LOGIN_MAX_FAILURES
    assert other.status_code == 429
    assert int(other.headers["retry-after"]) > 0


@pytest.mark.asyncio
async def test_a_cidr_allowlist_entry_covers_the_proxy(configured, monkeypatch):
    """Operators write ranges, not single addresses, for docker/swarm networks."""
    monkeypatch.setitem(settings.__dict__, "trusted_proxies", "10.9.0.0/16, 172.18.0.0/16")

    async with client(peer=PROXY) as c:
        attacker = [
            (await _fail_login(c, forwarded_for=ATTACKER)).status_code
            for _ in range(http_auth.LOGIN_MAX_FAILURES)
        ]
        victim = await _correct_login(c, forwarded_for=VICTIM)

    assert attacker == [401] * http_auth.LOGIN_MAX_FAILURES
    assert victim.status_code == 200


@pytest.mark.asyncio
async def test_a_malformed_allowlist_trusts_nothing(configured, monkeypatch):
    """Fail closed, never open.

    A typo must not be read as "trust everything" — that would turn a
    misconfiguration into a throttle bypass. Trusting nothing degrades to the
    pre-change shared bucket, which is safe.
    """
    monkeypatch.setitem(settings.__dict__, "trusted_proxies", "not-an-ip-address")

    async with client(peer=PROXY) as c:
        statuses = [
            (await _fail_login(c, forwarded_for=f"10.0.0.{index}")).status_code
            for index in range(http_auth.LOGIN_MAX_FAILURES)
        ]
        after = await _fail_login(c, forwarded_for="10.0.0.99")

    assert statuses == [401] * http_auth.LOGIN_MAX_FAILURES
    assert after.status_code == 429, (
        "a malformed allowlist was treated as trusting the peer"
    )


def test_a_malformed_allowlist_refuses_to_start(monkeypatch):
    """...and it is not silent either.

    Trusting nothing is the safe runtime fallback, but a typo that quietly
    disables the setting an operator believes is on is its own trap, so the
    loader refuses the configuration outright.
    """
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(
            {
                "PULLBACKUP_HTTP_BASIC_USERNAME": USERNAME,
                "PULLBACKUP_HTTP_BASIC_PASSWORD": PASSWORD,
                "PULLBACKUP_TRUSTED_PROXIES": "10.9.0.1, not-an-ip",
            },
            env_file=None,
        )

    assert "TRUSTED_PROXIES" in str(excinfo.value)
    assert "not-an-ip" in str(excinfo.value)


def test_the_documented_env_var_is_actually_honoured():
    """Documenting a name the loader ignores is a silent lie."""
    loaded = load_settings(
        {
            "PULLBACKUP_HTTP_BASIC_USERNAME": USERNAME,
            "PULLBACKUP_HTTP_BASIC_PASSWORD": PASSWORD,
            "PULLBACKUP_TRUSTED_PROXIES": "10.9.0.0/16, 192.168.1.5",
        },
        env_file=None,
    )

    assert loaded.trusted_proxies == "10.9.0.0/16, 192.168.1.5"
    assert [str(n) for n in loaded.trusted_proxy_networks] == [
        "10.9.0.0/16",
        "192.168.1.5/32",
    ]


def test_the_allowlist_defaults_to_empty():
    loaded = load_settings(
        {
            "PULLBACKUP_HTTP_BASIC_USERNAME": USERNAME,
            "PULLBACKUP_HTTP_BASIC_PASSWORD": PASSWORD,
        },
        env_file=None,
    )

    assert loaded.trusted_proxies == ""
    assert loaded.trusted_proxy_networks == []


# --------------------------------------------------------------------------
# the resolver itself, exercised directly for the cases an HTTP test cannot
# reach cleanly
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("peer", "forwarded_for", "allowlist", "expected"),
    [
        # No allowlist: the header is inert.
        ("10.9.0.1", "1.2.3.4", "", "10.9.0.1"),
        # Untrusted peer: the header is inert.
        ("203.0.113.9", "1.2.3.4", "10.9.0.1", "203.0.113.9"),
        # Trusted peer, single hop.
        ("10.9.0.1", "198.51.100.7", "10.9.0.1", "198.51.100.7"),
        # Trusted peer, attacker prepended a victim address.
        ("10.9.0.1", "198.51.100.7, 203.0.113.9", "10.9.0.1", "203.0.113.9"),
        # Two real trusted hops: walk past both to the first untrusted one.
        ("10.9.0.1", "203.0.113.9, 10.9.0.2", "10.9.0.0/24", "203.0.113.9"),
        # Every entry claims to be a trusted proxy: no untrusted identity
        # exists, so fall back to the peer rather than believing the leftmost.
        ("10.9.0.1", "10.9.0.5, 10.9.0.2", "10.9.0.0/24", "10.9.0.1"),
        # A junk hop stops the walk instead of being skipped over.
        ("10.9.0.1", "198.51.100.7, junk", "10.9.0.1", "10.9.0.1"),
        # Empty header from a trusted peer.
        ("10.9.0.1", "", "10.9.0.1", "10.9.0.1"),
        # IPv6 peer and hop.
        ("2001:db8::1", "2001:db8:1::9", "2001:db8::/64", "2001:db8:1::9"),
        # Malformed allowlist trusts nothing.
        ("10.9.0.1", "1.2.3.4", "nonsense", "10.9.0.1"),
    ],
)
def test_resolve_client_identity(peer, forwarded_for, allowlist, expected):
    assert (
        http_auth.resolve_client_identity(peer, forwarded_for, allowlist) == expected
    )


def test_a_missing_peer_is_still_handled():
    """``request.client`` can be None on some transports."""
    assert http_auth.resolve_client_identity(None, "1.2.3.4", "10.9.0.1") == "unknown"
    assert http_auth.resolve_client_identity(None, "", "") == "unknown"


# --------------------------------------------------------------------------
# review findings on PR #19 (kanban review card t_8e7c292e)
# --------------------------------------------------------------------------


def test_duplicate_forwarded_for_header_lines_are_joined_not_truncated():
    """Finding 1 (HIGH): the resolver read only the FIRST X-Forwarded-For line.

    Starlette's Headers.get() returns the first matching line; getlist() returns
    all of them. RFC 7230 section 3.2.2 says repeated headers are semantically
    identical to one comma-joined value, so the join is the correct read.

    HAProxy's `option forwardfor` emits a SECOND header line rather than
    appending to the client's. Reading only the first meant the code walked the
    CLIENT's list and never saw the proxy's entry, so the rightmost entry was
    fully attacker-controlled — the exact leftmost-first failure the resolver
    docstring says must never happen. The reviewer proved a complete throttle
    bypass behind a real HAProxy: eight consecutive failed logins, never
    throttled, using one header the client sets itself.
    """
    from starlette.datastructures import Headers

    headers = Headers(raw=[
        (b"x-forwarded-for", b"6.6.6.6"),          # attacker-supplied line
        (b"x-forwarded-for", b"172.22.0.7"),       # the proxy's own line
    ])

    forwarded = _forwarded_for_value(headers)

    assert forwarded == "6.6.6.6,172.22.0.7", (
        "duplicate header lines must be joined; reading only the first lets a "
        f"client hide the proxy's entry. got {forwarded!r}"
    )


def test_a_client_cannot_bypass_the_throttle_with_a_second_header_line(monkeypatch):
    """The end-to-end consequence of finding 1, at the resolver.

    A trusted proxy appends its own line. The identity must come from the
    proxy's view, not the client's, so a client cannot mint a fresh bucket per
    request by setting its own X-Forwarded-For.
    """
    from starlette.datastructures import Headers

    monkeypatch.setitem(settings.__dict__, "trusted_proxies", "172.22.0.5")

    seen = set()
    for attempt in range(8):
        headers = Headers(raw=[
            (b"x-forwarded-for", f"7.7.7.{attempt}".encode()),  # attacker varies this
            (b"x-forwarded-for", b"172.22.0.7"),                # proxy's real view
        ])
        seen.add(http_auth.resolve_client_identity("172.22.0.5", _forwarded_for_value(headers)))

    assert seen == {"172.22.0.7"}, (
        "every request must land in ONE bucket keyed on the proxy's view; "
        f"a varying client header produced {len(seen)} buckets: {sorted(seen)}"
    )


def test_the_throttle_store_does_not_grow_without_bound(monkeypatch):
    """Finding 2 (MEDIUM): with a trusted proxy the throttle key is
    attacker-influenced, and aged-out entries were pruned to an empty list but
    the key itself was never removed. 5000 requests produced 5000 dict entries
    (~970 MiB extrapolated to 1M) in a memory-limited container, unauthenticated.

    Structurally the same defect as the unbounded revocation set caught by the
    PR #16 review.
    """
    http_auth.reset_auth_state()

    stale = time.time() - 9999
    for index in range(200):
        http_auth.record_failed_login(f"10.0.0.{index}", now=stale)

    # Touching the store must evict keys whose attempts have all aged out.
    http_auth.login_throttle_retry_after("10.0.0.0")

    remaining = len(http_auth._login_attempts)
    assert remaining == 0, (
        f"{remaining} aged-out keys retained; an unauthenticated caller behind a "
        "trusted proxy can grow this dict without bound"
    )


def test_the_eviction_sweep_is_safe_under_concurrent_logins():
    """Review finding 1 on PR #19 second pass (HIGH).

    ``login`` is a SYNC FastAPI handler, so Starlette runs it in the anyio
    threadpool and several requests execute in real OS threads at once. The
    eviction sweep iterated ``_login_attempts.items()`` directly, and the
    per-entry ``any(...)`` call gives another worker room to insert or remove a
    key mid-iteration. CPython then raises ``RuntimeError: dictionary changed
    size during iteration`` out of an UNAUTHENTICATED endpoint — an HTTP 500 in
    security-relevant code reachable by anyone who can reach /login.

    The reviewer measured 183 HTTP 500s across 24,000 requests in the built
    image behind a real HAProxy; an otherwise identical image with only the
    sweep removed served 24,000 clean 401s. Reproduced here in-process before
    the fix: 6 exceptions in 4 seconds at 8 sweepers vs 4 writers.

    The pre-existing single-key write was atomic. Whole-dict iteration is not,
    so the sweep must work from a snapshot.
    """
    http_auth.reset_auth_state()

    stale = time.time() - 9999
    for index in range(200000):
        http_auth._login_attempts[f"10.{index // 65536}.{(index // 256) % 256}.{index % 256}"] = [stale]

    errors: list[BaseException] = []
    stop = threading.Event()

    def sweep() -> None:
        while not stop.is_set():
            try:
                http_auth.login_throttle_retry_after("1.1.1.1")
            except BaseException as error:  # noqa: BLE001 - the point of the test
                errors.append(error)
                return

    def churn() -> None:
        index = 0
        while not stop.is_set():
            http_auth.record_failed_login(f"172.16.{(index // 256) % 256}.{index % 256}")
            index += 1

    threads = [threading.Thread(target=sweep) for _ in range(8)]
    threads += [threading.Thread(target=churn) for _ in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(2)
    stop.set()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, (
        f"the throttle path raised under concurrency: {errors[0]!r} "
        f"({len(errors)} thread(s) affected). A 500 from the unauthenticated "
        "login endpoint is a crash in security-relevant code."
    )
