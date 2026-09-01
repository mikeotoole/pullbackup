# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""One bounded, expiring structure behind both in-memory auth stores.

Four review rounds landed on the same structural idea: in-memory auth state
whose key space an unauthenticated caller can influence.

  PR #16 review  ``_revoked`` grew without bound; anyone could insert sids via
                 the unauthenticated /api/auth/logout. Fixed by verifying the
                 signature first, which bounded WHO may insert, not HOW MANY
                 entries accumulate.
  PR #19 pass 1  ``_login_attempts`` never removed keys. Behind a trusted proxy
                 the key derives from X-Forwarded-For. Fixed with a sweep.
  PR #19 pass 2  The sweep iterated the live dict while anyio threadpool workers
                 mutated it: 183 HTTP 500s per 24,000 unauthenticated requests.
                 Fixed by snapshotting.
  this file      The sweep was O(n) PER REQUEST. Snapshotting a 50k-entry dict
                 and testing every entry cost 7.3 ms of the login handler, so an
                 attacker who inflates the live set inflates the per-request
                 cost for everybody. A quadratic interaction.

Patching the instance a fourth time would leave the fifth round waiting, so the
bound is built once, in ``_ExpiringStore``, and used for both stores.
"""

import threading
import logging
import time

import pytest
from pullbackup import http_auth
from pullbackup.config import settings

PASSWORD = "0123456789abcdef0123456789abcdef"
USERNAME = "pullbackup-test"


@pytest.fixture
def store_cap(monkeypatch):
    """A small cap, so cap behaviour is testable without a huge fixture."""

    def _cap(value: int):
        monkeypatch.setitem(settings.__dict__, "auth_store_max_entries", value)
        return value

    return _cap


# --------------------------------------------------------------------------
# the structure itself
# --------------------------------------------------------------------------


def test_the_store_never_exceeds_its_cap(store_cap):
    """The cap is the memory ceiling and it must hold regardless of traffic.

    Both previous fixes bounded the RATE of growth, not the SIZE. An eviction
    sweep only helps when entries actually age out; a caller sending distinct
    keys faster than the TTL expires them still grows the store without limit.
    """
    store_cap(50)
    store = http_auth._ExpiringStore(ttl=lambda: 3600.0, name="cap-test")

    sizes = []
    for index in range(500):
        store.set(f"key-{index}", index)
        sizes.append(len(store))

    assert max(sizes) <= 50, (
        f"the store grew to {max(sizes)} entries against a cap of 50; the cap "
        "is not a ceiling, it is a suggestion"
    )
    assert len(store) == 50, "the store should sit AT the cap, not below it"


def test_the_cap_evicts_the_entry_closest_to_expiring(store_cap):
    """Eviction order is the whole safety argument, so it is pinned.

    Entries are ordered by expiry, and the cap evicts from the front. That
    means the entry discarded under pressure is always the one with the LEAST
    remaining life — the one that was about to be dropped anyway. Evicting the
    newest, or an arbitrary entry, would discard live state while dead state
    stayed resident.
    """
    store_cap(3)
    store = http_auth._ExpiringStore(ttl=lambda: 3600.0, name="order-test")

    store.set("oldest", 1, now=1000.0)
    store.set("middle", 2, now=2000.0)
    store.set("newest", 3, now=3000.0)
    store.set("newer-still", 4, now=4000.0)

    # Read at the same simulated instant as the last write. Reading at the real
    # wall clock would find every entry expired and the assertions below would
    # pass against an EMPTY store, proving nothing — which is exactly how the
    # first version of this guard passed against a store that evicted the wrong
    # end.
    surviving = store.keys(now=4000.0)

    assert "oldest" not in surviving, "the cap evicted something other than the front"
    assert surviving == ["middle", "newest", "newer-still"]
    assert store.get("newer-still", now=4000.0) == 4, (
        "the newest entry was evicted; eviction is running from the wrong end"
    )


def test_entries_expire_on_their_own_without_any_cap_pressure(store_cap):
    """Per-entry expiry, so stale entries go even when the store is near empty.

    The cap alone would let 9,999 dead entries sit in a 10,000-entry store
    forever on a quiet instance.
    """
    store_cap(10_000)
    store = http_auth._ExpiringStore(ttl=lambda: 60.0, name="expiry-test")

    store.set("stale", "value", now=1000.0)
    assert store.get("stale", now=1030.0) == "value", "expired 30s into a 60s TTL"

    assert store.get("stale", now=1061.0) is None, "an entry outlived its TTL"
    assert "stale" not in store, "an expired entry is still reported as present"
    assert len(store) == 0, "an expired entry still occupies its slot"


def test_cleanup_is_amortised_not_a_scan_per_operation(store_cap):
    """The defect this card exists for.

    The old sweep tested EVERY live entry on EVERY login: 0.139 ms at 1k live
    entries, 7.307 ms at 50k. Cost per request grew with a set an unauthenticated
    caller controls.

    Asserted by COUNTING work rather than by timing it. A wall-clock assertion
    on a laptop under load is a flake generator, and the property that actually
    matters is complexity, not milliseconds. ``store.examined`` counts entries
    the expiry sweep looked at; with an expiry-ordered structure the sweep stops
    at the first live entry, so each entry is examined a small constant number
    of times across its whole lifetime, not once per subsequent operation.

    Numbers: 50,000 live entries then 1,000 reads. Old behaviour would examine
    ~50,000,000 entries. Amortised behaviour examines ~1,000 — one look at the
    unexpired front per call.
    """
    store_cap(100_000)
    store = http_auth._ExpiringStore(ttl=lambda: 3600.0, name="amortised-test")

    for index in range(50_000):
        store.set(f"key-{index}", index, now=1000.0 + index)

    before = store.examined
    for _ in range(1_000):
        store.get("key-25000", now=52_000.0)
    examined = store.examined - before

    assert examined <= 5_000, (
        f"1,000 operations examined {examined} entries against 50,000 live "
        "entries; cleanup is still O(n) per operation, which is the quadratic "
        "interaction this change exists to remove"
    )


def test_expiring_many_entries_at_once_stays_proportional_to_the_dead_ones(store_cap):
    """The other half of amortised: a big sweep is paid for once, not repeatedly.

    50,000 entries all expire. The first operation afterwards must clear them
    (paying 50,000 once, which their insertion already paid for), and the next
    thousand operations must cost nothing.
    """
    store_cap(100_000)
    store = http_auth._ExpiringStore(ttl=lambda: 60.0, name="bulk-expiry-test")

    for index in range(50_000):
        store.set(f"key-{index}", index, now=1000.0)

    store.get("absent", now=2000.0)
    assert len(store) == 0

    before = store.examined
    for _ in range(1_000):
        store.get("absent", now=2000.0)

    assert store.examined - before == 0, (
        "an empty store still examined entries; the sweep is re-scanning "
        "already-dead state"
    )


def test_the_store_is_safe_under_concurrent_readers_and_writers(store_cap):
    """``login`` and ``logout`` are SYNC FastAPI handlers.

    Starlette runs those in the anyio threadpool, so several requests execute in
    real OS threads at once. Iterating the previous dict directly raised
    ``RuntimeError: dictionary changed size during iteration`` out of the
    UNAUTHENTICATED login endpoint — 183 HTTP 500s across 24,000 requests in the
    built image (PR #19 second-pass review). A structure used by both auth
    stores must survive that shape by construction, not by each caller
    remembering to snapshot.
    """
    store_cap(20_000)
    store = http_auth._ExpiringStore(ttl=lambda: 60.0, name="concurrency-test")

    for index in range(20_000):
        store.set(f"seed-{index}", index)

    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                store.get("seed-10")
                len(store)
                "seed-11" in store
        except BaseException as error:  # noqa: BLE001 - the point of the test
            errors.append(error)

    def writer(offset: int) -> None:
        index = 0
        try:
            while not stop.is_set():
                store.set(f"churn-{offset}-{index}", index)
                store.pop(f"churn-{offset}-{index - 50}")
                index += 1
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    threads += [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(2)
    stop.set()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, (
        f"the shared store raised under concurrency: {errors[0]!r} "
        f"({len(errors)} thread(s) affected)"
    )


# --------------------------------------------------------------------------
# what must not change: the throttle must still throttle
# --------------------------------------------------------------------------


def test_a_client_mid_lockout_is_not_evicted_by_pressure_from_other_clients(store_cap):
    """The failure mode that is WORSE than the cost this card is about.

    A locked-out client's entry is, by construction, the one closest to
    expiring: its 60-second window started before every entry created after it.
    A cap that evicts strictly from the front therefore evicts LOCKED-OUT
    CLIENTS FIRST, which silently disables the throttle for exactly the caller
    it exists to stop.

    And it is reachable, not theoretical. Behind a trusted proxy the key derives
    from X-Forwarded-For, so an attacker mints one entry per request: filling a
    100,000-entry store inside the 60-second window needs ~1,667 requests/sec at
    the unauthenticated login endpoint. That is an afternoon's work, not a
    nation state.

    So the cap must reserve room for clients that are actually enforcing a
    lockout, and shed the cheap single-failure entries instead.
    """
    store_cap(50)
    http_auth.reset_auth_state()

    now = 1_000_000.0
    locked = "198.51.100.7"
    for _ in range(http_auth.LOGIN_MAX_FAILURES):
        http_auth.record_failed_login(locked, now=now)

    assert http_auth.login_throttle_retry_after(locked, now=now) > 0, (
        "setup is wrong: the client is not locked out to begin with"
    )

    # A flood of distinct clients, each with a single failure, well inside the
    # locked client's 60-second window.
    for index in range(5_000):
        http_auth.record_failed_login(f"10.{index // 65536}.{(index // 256) % 256}.{index % 256}", now=now + 1)

    retry_after = http_auth.login_throttle_retry_after(locked, now=now + 2)
    assert retry_after > 0, (
        "a locked-out client was evicted by flood pressure from other clients; "
        "the throttle can be cleared by anyone who can reach /login"
    )


def test_lockout_state_survives_pressure_even_when_the_flood_also_locks_out(store_cap):
    """...and the reservation cannot itself be flooded away for free.

    Entries that are enforcing a lockout cost the attacker five real failed
    logins each, so filling the reserved room is five times dearer than filling
    the cheap tier. When it does fill, the eviction still runs oldest-first
    within that room, so the victim evicted is the one closest to being released
    anyway — never a fresher lockout.
    """
    store_cap(20)
    http_auth.reset_auth_state()

    # Anchored to the wall clock, not a synthetic epoch: `record_failed_login`
    # takes `now`, but the STORE sweeps at `time.time()`, so a timestamp two
    # million seconds in the past is swept the instant it lands.
    now = time.time()
    newest = "203.0.113.200"

    # Every client is recorded at ONE frozen instant, deliberately.
    #
    # Review finding 2 on PR #20: this used `now=now + index`, so by the time
    # `newest` was written at now+200 every earlier entry had aged past the
    # 60-second lockout TTL and been swept. The store held ONE entry against a
    # cap of 20 — no cap pressure, no eviction, and both assertions passed
    # trivially. Mutant E proved the gap: inverting protected-tier eviction to
    # sacrifice the NEWEST lockout, exactly the behaviour this docstring names,
    # left the whole suite green.
    #
    # A microsecond apart preserves insertion order while keeping every entry
    # live, so the cap genuinely bites.
    for index in range(100):
        for _ in range(http_auth.LOGIN_MAX_FAILURES):
            http_auth.record_failed_login(f"192.0.2.{index}", now=now + index * 1e-6)

    for _ in range(http_auth.LOGIN_MAX_FAILURES):
        http_auth.record_failed_login(newest, now=now + 0.001)

    # Every client here is locked out, so they all land in the protected tier,
    # which is bounded to half the cap by the finding-3 fix. Ten, not twenty.
    assert len(http_auth._login_attempts) == 10, (
        "fixture failed to exercise the cap: "
        f"{len(http_auth._login_attempts)} entries against a 20-entry cap "
        "whose protected share is 10"
    )
    assert http_auth.login_throttle_retry_after(newest, now=now + 0.002) > 0, (
        "the most recently locked-out client was evicted ahead of older ones"
    )
    assert len(http_auth._login_attempts) <= 20, "the cap did not hold under lockout pressure"
    assert len(http_auth._login_attempts._protected) <= 10, (
        "the protected tier exceeded its bounded share of the cap"
    )


# --------------------------------------------------------------------------
# what must not change: logout must still revoke
# --------------------------------------------------------------------------


def test_a_revoked_session_stays_revoked_for_as_long_as_it_could_be_valid(monkeypatch):
    """The one genuinely dangerous failure mode in this change.

    If a revoked sid is dropped BEFORE its session would have expired, that
    cookie becomes valid again — logout stops meaning logout. So the retention
    interval is derived, not guessed.

    ``session_is_valid`` accepts a token when ``-60 <= age <= max_age``: up to
    max_age old, and up to 60 seconds in the FUTURE, which tolerates a clock
    jump (the signature is verified first, so a future ``iat`` is not a
    forgery). Revocation only happens for a token that is valid AT THAT MOMENT,
    so the worst case is a token minted 60 seconds ahead of the revoking clock,
    which stays acceptable until ``revoked_at + max_age + 60``.

    That is exactly the TTL, so the guard walks the boundary from both sides.
    """
    monkeypatch.setitem(settings.__dict__, "http_basic_username", USERNAME)
    monkeypatch.setitem(settings.__dict__, "http_basic_password", PASSWORD)
    monkeypatch.setitem(settings.__dict__, "session_secret", "")
    monkeypatch.setitem(settings.__dict__, "session_max_age_seconds", 3600)
    http_auth.reset_auth_state()

    minted_at = 1_000_000.0
    # The worst case: a token dated 60s into the future relative to the clock
    # that revokes it.
    token = http_auth.issue_session(now=minted_at + 60)
    http_auth.revoke_session(token, now=minted_at)

    last_moment_it_could_be_accepted = minted_at + 60 + 3600
    assert not http_auth.session_is_valid(
        token, now=last_moment_it_could_be_accepted
    ), (
        "a revoked cookie became usable again at the very last instant the "
        "signature-and-age check would still have accepted it; the revocation "
        "was dropped too early"
    )

    # ...and one second past that the age check refuses it anyway, so retaining
    # the sid beyond this point buys nothing.
    assert not http_auth.session_is_valid(token, now=last_moment_it_could_be_accepted + 1)


def test_dropping_a_revocation_after_the_session_could_expire_changes_nothing(monkeypatch):
    """Why expiring the revocation set is behaviour-preserving at all.

    ``session_is_valid`` refuses a token on AGE regardless of revocation, so
    past ``iat + max_age`` a revoked sid is dead weight: the token is refused
    either way. Pinned here because the whole justification for bounding
    ``_revoked`` rests on it — if an expired-but-unrevoked token were ever
    accepted, dropping the entry WOULD be a behaviour change.
    """
    monkeypatch.setitem(settings.__dict__, "http_basic_username", USERNAME)
    monkeypatch.setitem(settings.__dict__, "http_basic_password", PASSWORD)
    monkeypatch.setitem(settings.__dict__, "session_secret", "")
    monkeypatch.setitem(settings.__dict__, "session_max_age_seconds", 3600)
    http_auth.reset_auth_state()

    minted_at = 2_000_000.0
    never_revoked = http_auth.issue_session(now=minted_at)

    assert http_auth.session_is_valid(never_revoked, now=minted_at + 3600)
    assert not http_auth.session_is_valid(never_revoked, now=minted_at + 3601), (
        "an aged-out token was accepted without ever being revoked; the "
        "revocation set cannot be safely expired on this reasoning"
    )


def test_the_revocation_set_does_not_grow_without_bound(monkeypatch, store_cap):
    """The PR #16 defect, closed at the structure rather than at the caller.

    Verifying the signature before revoking bounded WHO can insert; it did
    nothing about HOW MANY entries accumulate. An operator's own client
    reconnecting on a schedule accumulates one entry per logout, forever, in a
    memory-limited container.
    """
    monkeypatch.setitem(settings.__dict__, "http_basic_username", USERNAME)
    monkeypatch.setitem(settings.__dict__, "http_basic_password", PASSWORD)
    monkeypatch.setitem(settings.__dict__, "session_secret", "")
    monkeypatch.setitem(settings.__dict__, "session_max_age_seconds", 60)
    store_cap(100_000)
    http_auth.reset_auth_state()

    now = 3_000_000.0
    for cycle in range(5_000):
        # One login/logout cycle per second, well past the 60-second lifetime.
        moment = now + cycle
        http_auth.revoke_session(http_auth.issue_session(now=moment), now=moment)

    remaining = http_auth._revoked.size(now=now + 5_000)
    # A 60-second session, so retention is 60 + the clock slack. Steady state is
    # therefore ~121 entries no matter how long the loop runs — bounded by the
    # session lifetime, not by the number of cycles.
    assert remaining <= 130, (
        f"{remaining} revocations retained after 5,000 logout cycles of a "
        "60-second session; the set still grows without bound"
    )


def test_the_cap_holds_on_the_revocation_set_too(store_cap, monkeypatch):
    """The ceiling is a ceiling for both stores, not just the throttle."""
    monkeypatch.setitem(settings.__dict__, "http_basic_username", USERNAME)
    monkeypatch.setitem(settings.__dict__, "http_basic_password", PASSWORD)
    monkeypatch.setitem(settings.__dict__, "session_secret", "")
    monkeypatch.setitem(settings.__dict__, "session_max_age_seconds", 7 * 24 * 3600)
    store_cap(100)
    http_auth.reset_auth_state()

    now = 4_000_000.0
    for cycle in range(1_000):
        http_auth.revoke_session(http_auth.issue_session(now=now), now=now)

    assert http_auth._revoked.size(now=now) <= 100, (
        "the revocation set exceeded its cap; the memory ceiling does not hold"
    )


def test_reset_auth_state_still_fully_clears_both_stores(monkeypatch):
    """Tests across the suite depend on this, so it is pinned explicitly."""
    monkeypatch.setitem(settings.__dict__, "http_basic_username", USERNAME)
    monkeypatch.setitem(settings.__dict__, "http_basic_password", PASSWORD)
    monkeypatch.setitem(settings.__dict__, "session_secret", "")
    http_auth.reset_auth_state()

    token = http_auth.issue_session()
    http_auth.revoke_session(token)
    for _ in range(http_auth.LOGIN_MAX_FAILURES):
        http_auth.record_failed_login("198.51.100.1")

    assert len(http_auth._revoked) == 1
    assert len(http_auth._login_attempts) == 1

    http_auth.reset_auth_state()

    assert len(http_auth._revoked) == 0, "revocations survived reset_auth_state"
    assert len(http_auth._login_attempts) == 0, "throttle state survived reset_auth_state"
    assert http_auth.session_is_valid(token), "the reset did not lift the revocation"
    assert http_auth.login_throttle_retry_after("198.51.100.1") == 0


# --------------------------------------------------------------------------
# end to end: the HTTP behaviour an operator actually sees
# --------------------------------------------------------------------------


def _client(peer: str = "testclient"):
    import httpx
    from pullbackup import main

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app, client=(peer, 41234)),
        base_url="http://test",
    )


@pytest.fixture
def http_configured(monkeypatch):
    monkeypatch.setitem(settings.__dict__, "http_basic_username", USERNAME)
    monkeypatch.setitem(settings.__dict__, "http_basic_password", PASSWORD)
    monkeypatch.setitem(settings.__dict__, "session_secret", "")
    monkeypatch.setitem(settings.__dict__, "trusted_proxies", "10.9.0.1")
    # The authenticated half of the boundary check reaches a real handler, so
    # the schema has to exist or a 500 would be mistaken for a 200 that never
    # happened. The anonymous half is answered by the middleware and never
    # touches the database.
    from sqlmodel import SQLModel

    from pullbackup import db

    SQLModel.metadata.create_all(db.engine)
    http_auth.reset_auth_state()
    yield
    http_auth.reset_auth_state()


@pytest.mark.asyncio
async def test_the_throttle_still_engages_at_the_sixth_failure(http_configured):
    """5 failures then 429 with Retry-After, and the CORRECT password refused."""
    async with _client(peer="203.0.113.5") as c:
        failures = [
            (await c.post(
                "/api/auth/login",
                json={"username": USERNAME, "password": "wrong-password-padding"},
            )).status_code
            for _ in range(http_auth.LOGIN_MAX_FAILURES)
        ]
        throttled = await c.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": "wrong-password-padding"},
        )
        correct_while_locked = await c.post(
            "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )

    assert failures == [401] * http_auth.LOGIN_MAX_FAILURES
    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) > 0
    assert correct_while_locked.status_code == 429, (
        "the correct password was accepted during a lockout"
    )


@pytest.mark.asyncio
async def test_a_successful_login_still_clears_the_counter(http_configured):
    async with _client(peer="203.0.113.6") as c:
        for _ in range(http_auth.LOGIN_MAX_FAILURES - 1):
            await c.post(
                "/api/auth/login",
                json={"username": USERNAME, "password": "wrong-password-padding"},
            )
        good = await c.post(
            "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )
        # Four fresh failures must not trip the throttle if the counter cleared.
        after = [
            (await c.post(
                "/api/auth/login",
                json={"username": USERNAME, "password": "wrong-password-padding"},
            )).status_code
            for _ in range(http_auth.LOGIN_MAX_FAILURES - 1)
        ]

    assert good.status_code == 200
    assert after == [401] * (http_auth.LOGIN_MAX_FAILURES - 1), (
        "the success did not clear the failure counter"
    )


@pytest.mark.asyncio
async def test_logout_still_revokes_and_survives_a_flood_of_other_logins(
    http_configured, monkeypatch
):
    """Login, use the cookie, log out, replay it -> 401, even under pressure.

    The flood is the point: it drives thousands of unrelated entries into the
    auth state between the logout and the replay. If they could evict the
    revocation, that cookie would work again.

    The cap is squeezed to 200 so the flood genuinely overruns it — at the
    100,000 default, 3,000 entries never reach the boundary and the guard would
    pass without testing anything.

    What makes this safe is that the two stores are SEPARATE, each with its own
    cap. Login-throttle pressure cannot evict a revocation at any volume,
    because the two never share a budget. Filling the revocation store instead
    requires an AUTHENTICATED login per entry, which an attacker who can do that
    does not need a resurrected cookie for.
    """
    monkeypatch.setitem(settings.__dict__, "auth_store_max_entries", 200)

    async with _client(peer="10.9.0.1") as c:
        good = await c.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            headers={"X-Forwarded-For": "198.51.100.20"},
        )
        cookie = good.cookies[http_auth.SESSION_COOKIE]

        before = await c.get("/api/tasks", cookies={http_auth.SESSION_COOKIE: cookie})
        await c.post("/api/auth/logout", cookies={http_auth.SESSION_COOKIE: cookie})

        # 3,000 distinct unauthenticated clients against a 200-entry cap.
        for index in range(3_000):
            await c.post(
                "/api/auth/login",
                json={"username": USERNAME, "password": "wrong-password-padding"},
                headers={"X-Forwarded-For": f"172.16.{(index // 256) % 256}.{index % 256}"},
            )

        after = await c.get("/api/tasks", cookies={http_auth.SESSION_COOKIE: cookie})

    assert good.status_code == 200
    assert before.status_code == 200, "the cookie did not work before logout"
    assert len(http_auth._login_attempts) <= 200, (
        "setup is wrong: the flood did not actually overrun the cap"
    )
    assert after.status_code == 401, (
        "a revoked cookie became valid again after a flood of unrelated logins; "
        "the cap evicted the revocation"
    )


@pytest.mark.asyncio
async def test_the_auth_boundary_is_unchanged(http_configured):
    """The allowlist, pinned again here because this change touches the path."""
    import base64 as _b64

    basic = "Basic " + _b64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    async with _client() as c:
        health = await c.get("/api/system/health")
        tasks_anon = await c.get("/api/tasks")
        tasks_basic = await c.get("/api/tasks", headers={"Authorization": basic})
        openapi = await c.get("/openapi.json")

    assert health.status_code == 200, "the healthcheck must stay unauthenticated"
    assert tasks_anon.status_code == 401
    assert tasks_basic.status_code == 200
    assert openapi.status_code == 401, "the API map leaked to an anonymous caller"


@pytest.mark.asyncio
async def test_basic_still_works_while_the_login_form_is_locked(http_configured):
    """A scripted client must not be locked out by the browser form's throttle."""
    import base64 as _b64

    basic = "Basic " + _b64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    async with _client(peer="203.0.113.7") as c:
        for _ in range(http_auth.LOGIN_MAX_FAILURES + 1):
            await c.post(
                "/api/auth/login",
                json={"username": USERNAME, "password": "wrong-password-padding"},
            )
        locked = await c.post(
            "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )
        scripted = await c.get("/api/tasks", headers={"Authorization": basic})

    assert locked.status_code == 429, "setup is wrong: the form is not locked"
    assert scripted.status_code == 200, (
        "HTTP Basic was refused while the login form was throttled"
    )


# --------------------------------------------------------------------------
# the cap is an operator-visible knob, so it must actually be one
# --------------------------------------------------------------------------


def test_the_documented_cap_env_var_is_honoured():
    """Documenting a name the loader ignores is a silent lie.

    Same convention as PULLBACKUP_TRUSTED_PROXIES. A cap an operator cannot see
    or tune is a surprise waiting to happen; a cap they can see, set, and have
    ignored is worse.
    """
    from pullbackup.config import load_settings

    loaded = load_settings(
        {
            "PULLBACKUP_HTTP_BASIC_USERNAME": USERNAME,
            "PULLBACKUP_HTTP_BASIC_PASSWORD": PASSWORD,
            "PULLBACKUP_AUTH_STORE_MAX_ENTRIES": "250",
        },
        env_file=None,
    )

    assert loaded.auth_store_max_entries == 250


def test_the_cap_defaults_to_the_documented_value():
    from pullbackup.config import load_settings

    loaded = load_settings(
        {
            "PULLBACKUP_HTTP_BASIC_USERNAME": USERNAME,
            "PULLBACKUP_HTTP_BASIC_PASSWORD": PASSWORD,
        },
        env_file=None,
    )

    assert loaded.auth_store_max_entries == 100_000, (
        "the default in .env.example and the default in the loader disagree"
    )


def test_the_env_example_documents_the_cap():
    """The knob exists for operators, so it has to be discoverable."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    assert "PULLBACKUP_AUTH_STORE_MAX_ENTRIES" in example.read_text(), (
        "the cap is undocumented; an operator would have to read the source to "
        "learn the number exists"
    )


def test_the_live_eviction_warning_actually_fires(monkeypatch, caplog):
    """Review finding 1 on PR #20 (blocking): the alarm could never ring.

    `_enforce_cap` only reached the warning after popping from `_protected`, but
    `_revoked` is built with warn_on_live_eviction=True and NO protect callable,
    so `_place` always targets `_entries` and `_protected` stays permanently
    empty. The branch was unreachable.

    Proved by execution at the pre-fix head: cap squeezed to 100, 1000
    revocations at one frozen instant so every entry is genuinely live ->
    900 live revocations dropped, ZERO warnings logged. A revoked cookie can
    become valid again in complete silence, while the comment promises the
    operator will hear about it.

    Shipping an alarm that provably cannot ring is worse than shipping none: the
    next reader believes they are covered. The warning must fire whenever an
    UNEXPIRED entry is dropped, from either tier.
    """
    monkeypatch.setitem(settings.__dict__, "auth_store_max_entries", 100)
    http_auth.reset_auth_state()

    store = http_auth._revoked
    now = time.time()
    with caplog.at_level(logging.WARNING, logger=http_auth.__name__):
        for index in range(1000):
            store.set(f"sid-{index}", True, now=now)

    live_lost = 1000 - (len(store._entries) + len(store._protected))
    assert live_lost > 0, "fixture failed to force a live eviction"

    warnings = [r for r in caplog.records if "may become usable again" in r.getMessage()]
    assert warnings, (
        f"{live_lost} unexpired revocations were dropped at the cap and NOTHING "
        "was logged; the operator loses a revocation in silence"
    )


def test_the_warning_stays_quiet_when_only_expired_entries_are_dropped(monkeypatch, caplog):
    """The alarm must not cry wolf.

    Normal operation drops entries that have aged out, which is the whole point
    of the store. Warning on those would train an operator to ignore the one
    message that matters.
    """
    monkeypatch.setitem(settings.__dict__, "auth_store_max_entries", 50)
    http_auth.reset_auth_state()

    # Entries must be genuinely EXPIRED when the store sweeps, so they are
    # dropped as routine housekeeping rather than as live evictions. Writing
    # them a full TTL into the past achieves that: `_sweep` reaps them at the
    # front before `_enforce_cap` ever has to sacrifice a live one.
    store = http_auth._revoked
    now = time.time()
    ttl = store._ttl()
    with caplog.at_level(logging.WARNING, logger=http_auth.__name__):
        for index in range(200):
            store.set(f"sid-{index}", True, now=now - ttl - 1_000 + index)
        # One final write at the real clock: the sweep now reaps all 200 stale
        # entries, so the cap is never under live pressure.
        store.set("sid-current", True, now=now)

    warnings = [r for r in caplog.records if "may become usable again" in r.getMessage()]
    assert not warnings, (
        f"warned {len(warnings)} times while dropping only EXPIRED entries; "
        "that is routine housekeeping, not a lost revocation"
    )


def test_a_saturated_protected_tier_cannot_switch_the_throttle_off(store_cap):
    """Finding 3, found by repairing the vacuous guard above (PR #20 review).

    The protected tier had no ceiling of its own. Fill the whole cap with
    locked-out clients and every NEW client's entry is the only unprotected
    thing in the store, so it is always the one evicted at insert. That client
    never accumulates failures, so it never locks out:

        saturate protected tier:  entries=0  protected=20
        new client, 8 failed logins -> retry_after: [0,0,0,0,0,0,0,0]

    The mechanism added to PRESERVE the throttle could be used to switch it off.
    Verified against the unmodified PR head, so the vacuous fixture was hiding
    it rather than my edits causing it.

    Expensive to reach - the resolver blocks header spoofing, so it needs the
    cap's worth of real source addresses each sustaining 5 failures inside the
    60s window - but a bypass hiding behind a "protection" is exactly what gets
    forgotten. The protected tier now gets a bounded share of the cap so a
    newcomer is always admissible.
    """
    store_cap(20)
    http_auth.reset_auth_state()

    now = time.time()

    # Saturate: far more locked-out clients than the whole cap.
    for index in range(100):
        for _ in range(http_auth.LOGIN_MAX_FAILURES):
            http_auth.record_failed_login(f"192.0.2.{index}", now=now + index * 1e-6)

    protected = len(http_auth._login_attempts._protected)
    assert protected <= 10, (
        f"the protected tier took {protected} of a 20-entry cap; it must not be "
        "able to consume the whole store"
    )

    # A brand-new client must still be able to earn a lockout.
    victim = "203.0.113.200"
    for _ in range(http_auth.LOGIN_MAX_FAILURES):
        http_auth.record_failed_login(victim, now=now + 0.001)

    assert http_auth.login_throttle_retry_after(victim, now=now + 0.002) > 0, (
        "a new client could not be throttled while the protected tier was "
        "saturated: the throttle is off for anyone arriving after the flood"
    )


def test_protected_eviction_sacrifices_the_oldest_lockout_not_the_newest(store_cap):
    """Kill mutant E: `_protected.popitem(last=False)` vs `last=True`.

    The guard above cannot see this, and the reason is structural rather than a
    fixture slip. It asserts on `newest`, which is the LAST write, so `newest` is
    in the tier under either eviction order — under `last=False` because the
    oldest was sacrificed, under `last=True` because it displaced whoever was
    there a microsecond earlier. `retry_after(newest) > 0`, `len(store) <= cap`
    and `len(_protected) <= _protected_cap()` all hold both ways. Confirmed:
    inverting either site, or both, left the full 336-test suite green.

    So assert on the SURVIVING KEY SET instead, which is the thing that actually
    differs. Thirty sequential lockouts against a 20-entry cap (protected share
    10):

        popitem(last=False), correct:  keeps {20..29}  the ten FRESHEST
        popitem(last=True),  mutant E: keeps {0..8, 29}  the oldest eight + one

    Order matters because the oldest lockout is closest to expiring anyway. Under
    `last=True` a client that failed five times an instant ago is discarded while
    one about to be released for free is retained — the attacker's newest victim
    escapes, which is the shape of the bypass this whole tier was added to stop.
    """
    store_cap(20)
    http_auth.reset_auth_state()

    now = time.time()
    protected_cap = http_auth._login_attempts._protected_cap()
    assert protected_cap == 10, f"fixture assumes a protected share of 10, got {protected_cap}"

    # Thirty clients lock out in a known order, all live at one frozen instant.
    for index in range(30):
        for _ in range(http_auth.LOGIN_MAX_FAILURES):
            http_auth.record_failed_login(f"10.0.0.{index}", now=now + index * 1e-6)

    survivors = {
        int(key.rsplit(".", 1)[1])
        for key in http_auth._login_attempts._protected
    }

    assert len(survivors) == protected_cap, (
        f"the protected tier holds {len(survivors)} entries, expected {protected_cap}"
    )
    assert survivors == set(range(20, 30)), (
        "protected eviction did not sacrifice the OLDEST lockout. Surviving "
        f"clients {sorted(survivors)}; expected the ten freshest {sorted(range(20, 30))}. "
        "Retaining older lockouts over newer ones discards the client that just "
        "earned its lockout while keeping one about to be released anyway."
    )


def test_shrinking_the_cap_drops_the_oldest_lockouts_first(store_cap):
    """Kill the surviving mutant-E variant, at the `_enforce_cap` site.

    The guard above kills the `_place` eviction. The `_enforce_cap` one needs a
    different scenario, because at a FIXED cap it is unreachable: it only runs
    when `_entries` is empty and the total still exceeds the cap, and the
    protected share is bounded at `max(1, cap // 2) <= cap`, so protected alone
    can never overflow.

    It becomes reachable when the cap SHRINKS at runtime — `_cap()` reads
    `settings.auth_store_max_entries` on every call, so an operator lowering
    PULLBACKUP_AUTH_STORE_MAX_ENTRIES and reloading hits exactly this path with
    an over-full protected tier and no ordinary entries to sacrifice first.

    Same property as the sibling test: sacrifice the OLDEST lockout, the one
    closest to release, never the freshest.
    """
    store_cap(40)
    http_auth.reset_auth_state()

    now = time.time()
    for index in range(20):
        for _ in range(http_auth.LOGIN_MAX_FAILURES):
            http_auth.record_failed_login(f"10.0.0.{index}", now=now + index * 1e-6)

    before = {int(k.rsplit(".", 1)[1]) for k in http_auth._login_attempts._protected}
    assert before == set(range(20)), f"fixture failed to fill the protected tier: {sorted(before)}"

    # Operator shrinks the cap. Protected (20) now exceeds the whole cap (6),
    # and there are no ordinary entries, so _enforce_cap must eat into it.
    store_cap(6)
    for _ in range(http_auth.LOGIN_MAX_FAILURES):
        http_auth.record_failed_login("10.0.0.99", now=now + 0.001)

    survivors = {int(k.rsplit(".", 1)[1]) for k in http_auth._login_attempts._protected}

    # `_enforce_cap` trims to the TOTAL cap (6), not the protected share — the
    # share is a ceiling on new inserts, not a retroactive trim. What matters is
    # WHICH entries survive.
    assert len(http_auth._login_attempts) <= 6, (
        f"the shrunk cap did not hold: {len(http_auth._login_attempts)} entries against a cap of 6"
    )
    assert survivors == {14, 15, 16, 17, 18, 19}, (
        f"eviction under a shrinking cap kept {sorted(survivors)}; it must drop the "
        "OLDEST lockouts (closest to release) and keep the freshest. Keeping low "
        "client numbers means the newest lockouts were sacrificed instead."
    )


# --------------------------------------------------------------------------
# a cap too small to throttle is refused at startup
# --------------------------------------------------------------------------
#
# ADVISORY 3 from PR #20 review pass 2. PULLBACKUP_AUTH_STORE_MAX_ENTRIES=1
# silently switches the login throttle off for every client after the first
# lockout: `_protected_cap()` is `max(1, cap // 2)`, so at cap=1 the protected
# share IS the whole store, one locked-out client occupies it, and a newcomer's
# entry is evicted at every insert. Measured at the pre-fix head:
#
#   cap=1  protected_cap=1  victim_retry_after=61  fresh x8 -> [0]*8
#   cap=2  protected_cap=1  victim_retry_after=61  fresh x8 -> [0,0,0,0,61,...]
#
# The `max(1, ...)` floors inside the store are correct and load-bearing —
# removing them raises `KeyError: 'dictionary is empty'` from `_place`. The
# defect is that the LOADER accepts the value. It also accepted 0 and -5, which
# reach the same total disablement through the same floors.
#
# Fixed the way this codebase already handles misconfiguration: refuse to start.
# `_validate_db_filename` and `_validate_trusted_proxies` raise
# ConfigurationError, and `require_valid_configuration` refuses a short HTTP
# Basic password rather than accepting a weak one. A throttle store too small to
# hold a lockout AND a newcomer is the same class of mistake, and the failure it
# produces is the worst shape for a security control: absent rather than broken,
# with the app still serving and the login form still working.


def test_the_loader_refuses_a_cap_too_small_to_throttle():
    """cap=1 is a silently disabled throttle, so startup must abort.

    Not clamped. Clamping would honour a number the operator never chose and
    leave them believing the configured value took effect — the same silence in
    a different place. The name and the floor both appear in the message so the
    fix does not require reading the source.
    """
    from pullbackup.config import ConfigurationError, load_settings

    with pytest.raises(ConfigurationError) as raised:
        load_settings(
            {
                "PULLBACKUP_HTTP_BASIC_USERNAME": USERNAME,
                "PULLBACKUP_HTTP_BASIC_PASSWORD": PASSWORD,
                "PULLBACKUP_AUTH_STORE_MAX_ENTRIES": "1",
            },
            env_file=None,
        )

    message = str(raised.value)
    assert "PULLBACKUP_AUTH_STORE_MAX_ENTRIES" in message, (
        f"the error does not name the variable to fix: {message!r}"
    )
    # `"100" in message` would be satisfied by the trailing "The default is
    # 100000.", so it passes on a message naming the WRONG floor or none at all.
    # That is the same substring trap documented in
    # test_the_env_example_documents_the_minimum below, which is why this pins
    # the number with a non-digit boundary instead.
    import re

    from pullbackup.config import MIN_AUTH_STORE_MAX_ENTRIES

    assert re.search(rf"at least {MIN_AUTH_STORE_MAX_ENTRIES}(?!\d)", message), (
        f"the error does not name the minimum the operator must meet: {message!r}"
    )


@pytest.mark.parametrize("value", ["0", "-1", "-5"])
def test_the_loader_refuses_a_nonpositive_cap(value):
    """Zero and negatives were accepted verbatim and reached the same failure.

    Measured at the pre-fix head: the loader returned 0 and -5 unchanged, and
    `_cap()`'s `max(1, ...)` turned both into a one-entry store — cap=1's
    disabled throttle, arrived at without ever writing 1.
    """
    from pullbackup.config import ConfigurationError, load_settings

    with pytest.raises(ConfigurationError):
        load_settings(
            {
                "PULLBACKUP_HTTP_BASIC_USERNAME": USERNAME,
                "PULLBACKUP_HTTP_BASIC_PASSWORD": PASSWORD,
                "PULLBACKUP_AUTH_STORE_MAX_ENTRIES": value,
            },
            env_file=None,
        )


def test_the_floor_itself_is_accepted():
    """The boundary, pinned, so the guard cannot drift one off.

    A validator written as `<= MINIMUM` would reject the very value the error
    message tells the operator to set — the most infuriating possible outcome
    of following the instructions.
    """
    from pullbackup.config import MIN_AUTH_STORE_MAX_ENTRIES, load_settings

    loaded = load_settings(
        {
            "PULLBACKUP_HTTP_BASIC_USERNAME": USERNAME,
            "PULLBACKUP_HTTP_BASIC_PASSWORD": PASSWORD,
            "PULLBACKUP_AUTH_STORE_MAX_ENTRIES": str(MIN_AUTH_STORE_MAX_ENTRIES),
        },
        env_file=None,
    )

    assert loaded.auth_store_max_entries == MIN_AUTH_STORE_MAX_ENTRIES


def test_the_default_clears_the_floor():
    """The shipped default must not be a value the loader would refuse."""
    from pullbackup.config import MIN_AUTH_STORE_MAX_ENTRIES, Settings

    assert Settings.model_fields["auth_store_max_entries"].default >= MIN_AUTH_STORE_MAX_ENTRIES


def test_at_the_floor_a_new_client_can_still_be_locked_out(store_cap):
    """The behaviour the floor exists to protect, asserted directly.

    This is the discriminator. A floor that merely rejects a number proves
    nothing; what matters is that every ACCEPTED cap still throttles. At the
    smallest accepted value, with a lockout already occupying the protected
    tier, a fresh client must still reach its own lockout.

    The rejected value is measured in the same test so the contrast is not
    taken on trust: at cap=1 the same fresh client survives all eight attempts
    with retry_after 0 every time.
    """
    from pullbackup.config import MIN_AUTH_STORE_MAX_ENTRIES

    def fresh_client_retry_afters(cap: int) -> list[int]:
        store_cap(cap)
        http_auth.reset_auth_state()
        now = time.time()
        for _ in range(http_auth.LOGIN_MAX_FAILURES):
            http_auth.record_failed_login("198.51.100.1", now=now)
        assert http_auth.login_throttle_retry_after("198.51.100.1", now=now) > 0, (
            "fixture is wrong: the first client never locked out"
        )
        retry_afters = []
        for _ in range(8):
            http_auth.record_failed_login("203.0.113.9", now=now)
            retry_afters.append(http_auth.login_throttle_retry_after("203.0.113.9", now=now))
        return retry_afters

    at_floor = fresh_client_retry_afters(MIN_AUTH_STORE_MAX_ENTRIES)
    at_one = fresh_client_retry_afters(1)

    assert at_one == [0] * 8, (
        "the reproduction no longer reproduces; cap=1 is supposed to leave the "
        f"throttle off, but produced {at_one}"
    )
    assert any(at_floor), (
        f"at the floor of {MIN_AUTH_STORE_MAX_ENTRIES} a fresh client survived "
        f"eight failed logins with retry_after {at_floor}; every accepted cap "
        "must still be able to throttle a client that is not already locked out"
    )


def test_the_env_example_documents_the_minimum():
    """An operator must not have to trigger the error to learn the floor.

    The assertion is a regex requiring the word "minimum" adjacent to the exact
    number, not a substring search. A plain `"100" in text` check was written
    first and SURVIVED mutation: `100` occurs inside the documented default
    `100000`, so the test passed against an .env.example that documented no
    minimum at all.
    """
    import re
    from pathlib import Path

    from pullbackup.config import MIN_AUTH_STORE_MAX_ENTRIES

    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    pattern = rf"[Mm]inimum {MIN_AUTH_STORE_MAX_ENTRIES}(?!\d)"
    assert re.search(pattern, example), (
        "the minimum accepted cap is undocumented; the loader refuses values "
        f"below {MIN_AUTH_STORE_MAX_ENTRIES} and .env.example does not say so "
        f"(looked for /{pattern}/)"
    )
