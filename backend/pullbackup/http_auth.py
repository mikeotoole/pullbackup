# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
import base64
import binascii
import ipaddress
import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from hashlib import sha256
from hmac import compare_digest
from typing import Any

from itsdangerous import BadData, TimestampSigner, URLSafeTimedSerializer
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import parse_trusted_proxies as _parse_trusted_proxies
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

# The SPA stamps this on every API call it makes (frontend/src/lib/api.ts).
# It is the deterministic half of the browser test below.
_WEB_CLIENT_HEADER = b"x-pullbackup-client"
_WEB_CLIENT_VALUE = "web"


def _is_browser_subresource(headers: dict[bytes, bytes]) -> bool:
    """True when a 401 here would pop the browser's native Basic dialog.

    A browser shows its own credential prompt for ANY 401 carrying
    ``WWW-Authenticate: Basic`` — a same-origin ``fetch()`` included. So the
    SPA loads its shell signed-out, fires its first ``/api/*`` call, and the
    operator gets the native dialog instead of our login form; only after
    cancelling does the fetch reject and the router reach ``/login``. (An
    earlier comment on this very code claimed fetch() was exempt. It is not;
    Mike hit it on 0.13.0.)

    Two independent signals, either of which is enough:

      * ``Sec-Fetch-Mode`` other than ``navigate``. It is a forbidden header —
        script cannot set or forge it, only the browser emits it — so its
        presence is proof of a browser and its value distinguishes a
        subresource fetch from a top-level navigation. curl and every scripted
        client send nothing here and are unaffected.
      * ``X-Pullbackup-Client: web``, which the SPA sets explicitly. Fetch
        Metadata is only sent from secure contexts (https, or localhost), so a
        deployment reached over plain http on a LAN address emits no
        ``Sec-Fetch-*`` at all and the first signal goes silent. The explicit
        header covers exactly that case, and ships in the same release.

    A top-level ``navigate`` deliberately keeps the challenge: browsing
    straight at ``/openapi.json`` should offer the dialog, because there the
    native prompt is the intended way in and there is no SPA to redirect.

    ``Accept: text/event-stream`` is a third signal, and it is not redundant.
    ``EventSource`` (the live run-log stream on ``/runs/:id``) cannot carry a
    custom header at all — the API forbids it — so on a plain-http deployment,
    where Fetch Metadata is also absent, the other two signals both go silent
    and a reconnect after the session expires would pop the dialog. A scripted
    caller that sets this Accept loses the challenge, which is a fair trade: it
    has already chosen a browser-shaped streaming API.
    """
    if headers.get(_WEB_CLIENT_HEADER, b"").decode(
        "latin-1", errors="replace"
    ).strip().lower() == _WEB_CLIENT_VALUE:
        return True
    accept = headers.get(b"accept", b"").decode("latin-1", errors="replace").lower()
    if "text/event-stream" in accept:
        return True
    mode = headers.get(b"sec-fetch-mode")
    if mode is None:
        return False
    return mode.decode("latin-1", errors="replace").strip().lower() != "navigate"


logger = logging.getLogger(__name__)

LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60

# Tokens are rejected on AGE by `session_is_valid` before revocation matters,
# and that check accepts `-60 <= age <= max_age`: up to max_age old, and up to
# 60 seconds in the FUTURE, which tolerates a clock jump (the signature is
# verified first, so a future `iat` is not a forgery). A sid is only revoked for
# a token that is valid at that moment, so the worst case is a token dated 60
# seconds ahead of the revoking clock, which stays acceptable through
# `revoked_at + max_age + 60` INCLUSIVE.
#
# The store drops an entry once `expires_at <= now`, so the retention has to
# strictly exceed that closed boundary — hence the extra second. Without it the
# revocation is dropped at exactly the last instant the cookie would still have
# been accepted, and logout stops meaning logout for one tick. Caught by
# test_a_revoked_session_stays_revoked_for_as_long_as_it_could_be_valid.
_REVOCATION_CLOCK_SLACK_SECONDS = 61


class _ExpiringStore:
    """A bounded, expiring, thread-safe mapping. One structure, both auth stores.

    Written to close a CATEGORY rather than patch a fourth instance of it. See
    the module docstring of ``tests/test_bounded_auth_stores.py`` for the four
    review rounds that led here.

    Three properties, each of which one of those rounds needed:

    ``cap``
        A hard maximum entry count, so the memory ceiling holds regardless of
        traffic. An eviction sweep alone bounds the RATE of growth, not the
        SIZE: a caller inserting distinct keys faster than the TTL expires them
        still grows the store without limit.

    per-entry expiry
        So dead entries leave a quiet instance too, rather than 9,999 corpses
        sitting in a 10,000-entry store until someone else needs the space.

    amortised cleanup
        The previous sweep tested EVERY live entry on EVERY login: 0.139 ms at
        1k entries, 7.307 ms at 50k. That is a quadratic interaction — an
        attacker who inflates the live set inflates the per-request cost for
        everyone.

    The amortisation comes from the ordering invariant, not from a heuristic:
    **every entry in a given store gets the same TTL, measured from its last
    touch, and a touch moves it to the back.** Insertion order is therefore
    expiry order, so

      * the sweep pops from the FRONT and stops at the first live entry — the
        entries it examines are exactly the ones it removes, plus one. Each
        entry is examined O(1) times across its whole lifetime.
      * the cap evicts from the FRONT, so the entry discarded under pressure is
        always the one with the LEAST remaining life. Evicting the newest, or
        an arbitrary entry, would discard live state while dead state stayed
        resident.

    Do not add a per-entry TTL override. It would break both properties at
    once, silently: an out-of-order expiry stops the sweep early and leaves
    dead entries resident, and the cap starts evicting live entries ahead of
    expired ones.

    ``protect``
        Front-eviction alone is WRONG for the login throttle, and the guard
        ``test_a_client_mid_lockout_is_not_evicted_by_pressure_from_other_clients``
        caught it: a locked-out client's entry is by construction the one
        closest to expiring, so a cap that always evicts the front evicts
        locked-out clients FIRST — silently disabling the throttle for exactly
        the caller it exists to stop. Behind a trusted proxy that is reachable
        at roughly 1,667 unauthenticated requests/sec.

        So entries are held in two tiers sharing one cap. ``protect(value)``
        decides the tier on every write; eviction drains the ordinary tier
        completely before it will touch a protected one. Both tiers keep the
        expiry ordering, so eviction inside either is still oldest-first and
        still O(1).

        Entry to the protected tier costs the attacker five real failed logins
        rather than one request, and the only thing filling it evicts is an
        older lockout — one closer to being released anyway.

    Thread safety is by construction rather than by convention. ``login`` and
    ``logout`` are SYNC FastAPI handlers, so Starlette runs them in the anyio
    threadpool and several requests execute in real OS threads at once. The
    previous code iterated the live dict from the request path and CPython
    raised ``RuntimeError: dictionary changed size during iteration`` out of
    the UNAUTHENTICATED login endpoint — 183 HTTP 500s across 24,000 requests
    in the built image. Snapshotting fixed that instance; holding a lock inside
    the structure means no future caller has to remember to.
    """

    __slots__ = (
        "_entries",
        "_protected",
        "_lock",
        "_ttl",
        "_name",
        "_protect",
        "_warn_on_live_eviction",
        "examined",
    )

    def __init__(
        self,
        ttl: Callable[[], float],
        name: str,
        protect: Callable[[Any], bool] | None = None,
        warn_on_live_eviction: bool = False,
    ) -> None:
        # key -> (expires_at, value), each tier ordered oldest-expiry first.
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._protected: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._ttl = ttl
        self._name = name
        self._protect = protect
        self._warn_on_live_eviction = warn_on_live_eviction
        # Test hook: entries the expiry sweep has looked at. Counting work is a
        # stable way to assert amortised complexity; a wall-clock assertion on a
        # loaded laptop is a flake generator.
        self.examined = 0

    # -- internals, all called with the lock held ---------------------------

    def _cap(self) -> int:
        return max(1, int(settings.auth_store_max_entries))

    def _sweep_tier(self, entries: OrderedDict[str, tuple[float, Any]], now: float) -> None:
        while entries:
            key, (expires_at, _) = next(iter(entries.items()))
            self.examined += 1
            if expires_at > now:
                return
            entries.pop(key, None)

    def _sweep(self, now: float) -> None:
        """Drop expired entries from the front of each tier. O(1) amortised."""
        self._sweep_tier(self._entries, now)
        self._sweep_tier(self._protected, now)

    def _enforce_cap(self, now: float) -> None:
        cap = self._cap()
        while len(self._entries) + len(self._protected) > cap:
            # Ordinary entries go first, in expiry order. Only when there are
            # none left does a protected entry get sacrificed.
            if self._entries:
                _, (expires_at, _) = self._entries.popitem(last=False)
            else:
                _, (expires_at, _) = self._protected.popitem(last=False)

            # Warn on any UNEXPIRED eviction, from EITHER tier.
            #
            # Review finding 1 on PR #20 (blocking): this warned only after
            # popping from `_protected`, but `_revoked` is built with
            # warn_on_live_eviction=True and NO protect callable, so `_place`
            # always targets `_entries` and `_protected` is permanently empty.
            # The branch could never execute. Measured at the pre-fix head with
            # the cap squeezed to 100: 900 live revocations dropped, ZERO
            # warnings logged. An alarm that provably cannot ring is worse than
            # no alarm, because the next reader believes they are covered.
            #
            # Expired evictions stay silent on purpose: that is routine
            # housekeeping, and warning on it would train an operator to ignore
            # the one message that matters.
            #
            # Liveness is judged against the WALL CLOCK, not the caller's `now`.
            # Callers pass their own `now` (tests, and any batched write), so
            # comparing against it reported entries that expired long ago as
            # live and fired the alarm 150 times during pure housekeeping. What
            # matters is whether the entry is still usable at this instant.
            if self._warn_on_live_eviction and expires_at > time.time():
                # The one genuinely dangerous outcome in this change: a revoked
                # sid dropped before its session would have expired makes that
                # cookie valid again. Front-eviction plus authenticated-only
                # insertion makes it unreachable in practice, so if it ever does
                # happen the operator must hear about it rather than lose a
                # revocation in silence.
                logger.warning(
                    "%s: evicted a live entry at the %d-entry cap; a revoked "
                    "session may become usable again. Raise "
                    "PULLBACKUP_AUTH_STORE_MAX_ENTRIES.",
                    self._name,
                    cap,
                )

    def _protected_cap(self) -> int:
        """How much of the cap protected entries may hold.

        Finding 3 on PR #20: the protected tier had no ceiling, so saturating it
        left every NEW client's entry as the only unprotected thing in the
        store — always the one evicted at insert. That client could never
        accumulate failures, so it could never be locked out, and the mechanism
        added to PRESERVE the throttle became a way to switch it off. Measured:
        20 locked-out clients against a 20-entry cap, then a fresh client
        survived 8 failed logins with retry_after 0 every time.

        Half the cap. A COMPLETED lockout cannot be evicted by a flood of cheap
        single-failure clients, because they compete for the other half, while a
        newcomer is always admissible.

        That guarantee covers completed lockouts ONLY, and the distinction
        matters. A client with 1..4 failures is not yet protected, so it sits in
        the ordinary half and can be evicted before it reaches the fifth. An
        attacker sustaining roughly cap/2 distinct single-failure clients
        BETWEEN each of a victim's attempts can therefore keep that victim from
        ever locking out. Measured at cap 20: 9 interleaved flooders per attempt
        and the victim still locks out; 10 or more and it never does.

        Not closed here, deliberately. Protecting partial-failure entries would
        reopen the unbounded-growth problem this store exists to solve, and the
        attack costs roughly cap/2 real source addresses per victim attempt
        sustained inside the 60s window (~50,000 at the default cap), all past
        the trusted-proxy resolver. That is the same order of cost as the flood
        the design already accepts.
        """
        return max(1, self._cap() // 2)

    def _place(self, key: str, value: Any, now: float) -> None:
        """Insert into whichever tier ``value`` belongs to, dropping the other."""
        self._entries.pop(key, None)
        self._protected.pop(key, None)
        wants_protection = self._protect is not None and self._protect(value)
        if wants_protection and len(self._protected) >= self._protected_cap():
            # The protected share is full. Evict the OLDEST protected entry —
            # the lockout closest to expiring anyway — rather than refusing the
            # new one or spilling it into the unprotected tier, where a flood
            # would evict it immediately.
            self._protected.popitem(last=False)
        target = self._protected if wants_protection else self._entries
        target[key] = (now + self._ttl(), value)

    def _find(self, key: str) -> tuple[float, Any] | None:
        found = self._entries.get(key)
        return self._protected.get(key) if found is None else found

    # -- mapping surface ----------------------------------------------------

    def set(self, key: str, value: Any, now: float | None = None) -> None:
        """Insert or refresh ``key``, resetting its TTL and moving it to the back."""
        now = time.time() if now is None else now
        with self._lock:
            self._sweep(now)
            self._place(key, value, now)
            self._enforce_cap(now)

    def get(self, key: str, default: Any = None, now: float | None = None) -> Any:
        now = time.time() if now is None else now
        with self._lock:
            self._sweep(now)
            found = self._find(key)
            if found is None or found[0] <= now:
                return default
            return found[1]

    def pop(self, key: str, default: Any = None, now: float | None = None) -> Any:
        now = time.time() if now is None else now
        with self._lock:
            self._sweep(now)
            found = self._entries.pop(key, None)
            if found is None:
                found = self._protected.pop(key, None)
            return default if found is None else found[1]

    def mutate(self, key: str, change: Callable[[Any], Any], now: float | None = None) -> Any:
        """Read-modify-write ``key`` atomically, refreshing its TTL.

        ``change`` receives the current value (or ``None``) and returns the new
        one. Exists because the failed-login counter is a read-modify-write and
        doing it as ``get`` then ``set`` from the request path drops attempts
        when two threadpool workers interleave — quietly weakening the throttle,
        which is the failure direction that matters.
        """
        now = time.time() if now is None else now
        with self._lock:
            self._sweep(now)
            found = self._find(key)
            updated = change(None if found is None else found[1])
            self._place(key, updated, now)
            self._enforce_cap(now)
            return updated

    def keys(self, now: float | None = None) -> list[str]:
        """A SNAPSHOT of the live keys, never a live view.

        Returning a view would hand a caller the exact iterate-while-mutating
        hazard this class exists to remove.
        """
        now = time.time() if now is None else now
        with self._lock:
            self._sweep(now)
            return list(self._entries) + list(self._protected)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._protected.clear()

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __getitem__(self, key: str) -> Any:
        found = self.get(key, _MISSING)
        if found is _MISSING:
            raise KeyError(key)
        return found

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.get(key, _MISSING) is not _MISSING

    def __len__(self) -> int:
        return self.size()

    def size(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            self._sweep(now)
            return len(self._entries) + len(self._protected)

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())


_MISSING = object()


# Session identifiers revoked by an explicit logout. In-memory and
# process-local ON PURPOSE: single-user, single-process app, and a restart
# logging everyone out is the safe direction to fail.
#
# Bounded and expiring since this card. Dropping an entry once the session
# could no longer be valid anyway is provably behaviour-preserving:
# `session_is_valid` rejects on AGE at line ~232 BEFORE consulting revocation,
# so past that point the sid is dead weight either way.
_revoked = _ExpiringStore(
    ttl=lambda: settings.session_max_age_seconds + _REVOCATION_CLOCK_SLACK_SECONDS,
    name="revoked-sessions",
    warn_on_live_eviction=True,
)

# Per-client failed-login history, for backoff. HTTP Basic had no login
# endpoint to hammer; a form does, so the endpoint needs its own limit.
#
# Entries older than LOGIN_LOCKOUT_SECONDS were ALREADY treated as irrelevant
# by the throttle, so expiry here changes nothing an operator can observe.
#
# A client that is actually enforcing a lockout is PROTECTED from cap eviction.
# Without that, the cap would evict locked-out clients first (their entries are
# the oldest by construction) and a flood of cheap single-failure entries would
# silently clear the throttle for the one caller it exists to stop.
_login_attempts = _ExpiringStore(
    ttl=lambda: float(LOGIN_LOCKOUT_SECONDS),
    name="login-attempts",
    protect=lambda attempts: len(attempts or []) >= LOGIN_MAX_FAILURES,
)



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


def _signing_key() -> bytes:
    """The signing key for session cookies.

    ``PULLBACKUP_SESSION_SECRET`` wins when set. Otherwise the key is derived
    from the configured password, which is what makes an upgrade need no new
    configuration — and, deliberately, makes CHANGING the password invalidate
    every session issued under the old one. The username is mixed in too so a
    username change is equally invalidating.

    Unchanged by the move to ``itsdangerous``: the SOURCE of the secret and the
    way it is configured are exactly as before. Only the code that turns a
    secret into a token changed.
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


# Namespaces the signature. Bumped from the (implicit) v1 hand-rolled format on
# purpose: see `_serializer` for why every pre-existing cookie must stop working
# rather than be carried across.
_SESSION_SALT = "pullbackup.session.v2"

# How far in the FUTURE a token's issue time may sit and still be accepted.
# It can only come from a clock jump or a forgery attempt — and the signature is
# verified before the age is looked at, so a future timestamp on a token that
# got this far is a clock jump, not a forgery. Accepting an unbounded future
# would extend the lifetime past the configured maximum, hence the cap.
_CLOCK_SKEW_TOLERANCE_SECONDS = 60


class _ExplicitClockSigner(TimestampSigner):
    """A ``TimestampSigner`` whose clock is supplied rather than read.

    ``itsdangerous`` stamps ``int(time.time())``. This module resolves ``now``
    ONCE per request and threads it through every check, so that the revocation
    lookup and the age test cannot disagree about what time it is (PR #20). That
    discipline has to reach minting too — ``issue_session(now=...)`` is how the
    revocation-retention proof in ``tests/test_bounded_auth_stores.py`` mints a
    token at a chosen instant.

    Overriding the single clock call is the smallest possible seam: signing,
    encoding, timestamp format and tamper detection all remain the library's.
    """

    def __init__(self, *args: Any, issued_at: float | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._issued_at = issued_at

    def get_timestamp(self) -> int:
        if self._issued_at is None:
            return super().get_timestamp()
        return int(self._issued_at)


def _serializer(issued_at: float | None = None) -> URLSafeTimedSerializer:
    """The session-token serializer.

    ``itsdangerous`` replaces what used to be ~100 lines of HMAC, base64url and
    issued-at handling owned by this file. ``URLSafeTimedSerializer`` produces
    ``payload.timestamp.signature``, verifies the signature in constant time,
    and refuses anything tampered with.

    Two things are deliberately still ours:

    * **Expiry.** ``loads(max_age=...)`` measures age against the library's own
      ``time.time()``, which would break the single-clock discipline above and
      cannot express the ``-60`` future tolerance. So the signature check is the
      library's and the age check stays in ``session_is_valid``, judged against
      the caller's ``now``.
    * **Revocation.** A signed token is valid until it expires by definition, so
      "logout kills this cookie" needs server-side state whatever signs it.
      ``_revoked`` is unchanged.

    Built per call rather than cached because the key is derived from live
    settings: caching it would make a password change stop invalidating
    sessions, which is a security property with a test on it.

    ``salt`` namespaces the signature. It is bumped to v2 because the token
    FORMAT changed, and a cookie minted by the old hand-rolled signer must be
    REJECTED rather than silently reinterpreted. The consequence is stated
    rather than incidental: **on the deploy that ships this change, every
    existing session is logged out once.** For a single-user app that is a
    single re-login, and it is the safe direction — the alternative is keeping
    the old verifier alive so that two signing implementations must both stay
    correct forever, which is exactly the burden this change exists to shed.
    """
    return URLSafeTimedSerializer(
        _signing_key(),
        salt=_SESSION_SALT,
        signer=_ExplicitClockSigner,
        # itsdangerous DEFAULTS to HMAC-SHA1. The retired hand-rolled signer
        # used HMAC-SHA256, so leaving the default in place would quietly
        # shorten the MAC from 256 to 160 bits as a side effect of a change
        # whose entire point was to alter nothing but the implementation.
        # Neither is broken for this use, but a library swap must not weaken a
        # property it was not asked to touch. Pinned by
        # test_the_mac_is_still_sha256_not_the_library_default.
        signer_kwargs={"issued_at": issued_at, "digest_method": sha256},
    )


def issue_session(now: float | None = None) -> str:
    """Mint a signed session token. Callers must check credentials first."""
    return _serializer(issued_at=now if now is not None else time.time()).dumps(
        {"sid": secrets.token_urlsafe(16)}
    )


def _verified_session(token: str) -> tuple[str, float] | None:
    """``(sid, issued_at)`` for a correctly signed token, else ``None``.

    Signature only. Age and revocation are the caller's business, because both
    have to be judged against a single caller-supplied ``now``.
    """
    if not token:
        return None
    try:
        payload, issued_at = _serializer().loads(token, return_timestamp=True)
    except BadData:
        # Covers the whole family: bad signature, wrong salt (so every token
        # from the previous signer), truncated token, malformed base64,
        # undecodable JSON.
        return None
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("sid")
    if not isinstance(session_id, str):
        return None
    return session_id, issued_at.timestamp()


def session_is_valid(token: str, now: float | None = None) -> bool:
    """True only for a well-formed, correctly signed, unexpired, unrevoked token."""
    verified = _verified_session(token)
    if verified is None:
        return False
    session_id, issued_at = verified

    now = time.time() if now is None else now
    # Checked against the SAME clock as the age test below. Reading the
    # revocation store at the wall clock while judging age at a caller-supplied
    # instant would let the two disagree about whether an entry is still live.
    if _revoked.get(session_id, now=now):
        return False

    age = now - issued_at
    # A future-dated token is rejected as well: it can only come from a clock
    # jump or a forgery attempt, and accepting it would extend the lifetime
    # past the configured maximum.
    return (
        -_CLOCK_SKEW_TOLERANCE_SECONDS <= age <= settings.session_max_age_seconds
    )


def revoke_session(token: str, now: float | None = None) -> None:
    """Blacklist a token's session id so the same cookie stops working.

    The signature is verified FIRST. Review finding 3 on PR #16: this parsed the
    body and added claims['sid'] before any signature check, and
    /api/auth/logout is unauthenticated by design — so an anonymous caller could
    insert arbitrary attacker-chosen sids into an unbounded in-memory set inside
    a memory-limited container. Verifying first costs nothing and removes the
    whole class; an unsigned or forged token is simply ignored.

    Verifying first bounded WHO may insert. It did nothing about HOW MANY
    entries accumulate, which is why the entry now expires: see
    ``_REVOCATION_CLOCK_SLACK_SECONDS`` for why the retention interval is
    exactly as long as the token could still be accepted, and not one second
    less.
    """
    now = time.time() if now is None else now
    if not session_is_valid(token, now=now):
        return
    verified = _verified_session(token)
    if verified is None:  # pragma: no cover
        return  # unreachable: session_is_valid already verified this
    _revoked.set(verified[0], True, now=now)


def reset_auth_state() -> None:
    """Clear per-process auth state (revocations, login throttle).

    Exists for tests and for nothing else: the state is intentionally in-memory
    and process-local, so a restart logs everyone out.
    """
    _revoked.clear()
    _login_attempts.clear()


def login_throttle_retry_after(client: str, now: float | None = None) -> int:
    """Seconds the client must wait, or 0 when it may attempt a login.

    No eviction sweep here any more. It used to call ``_evict_expired_logins``,
    which snapshotted the WHOLE dict and tested every entry on every login:
    0.139 ms at 1k live entries, 7.307 ms at 50k, so end-to-end login latency
    ran from 0.43 ms/req at rest to 8.7 ms/req at 60k. Because the key space is
    attacker-influenced behind a trusted proxy, that made an attacker able to
    inflate everyone else's per-request cost. Expiry is now the store's job and
    costs O(1) amortised.

    This is a READ, so it must not extend the lockout: ``get`` deliberately does
    not refresh the TTL. Only ``record_failed_login`` does.
    """
    now = time.time() if now is None else now
    attempts = _login_attempts.get(client, now=now) or []
    recent = [t for t in attempts if now - t < LOGIN_LOCKOUT_SECONDS]
    if len(recent) < LOGIN_MAX_FAILURES:
        return 0
    return max(1, int(LOGIN_LOCKOUT_SECONDS - (now - recent[-LOGIN_MAX_FAILURES]) + 1))


def record_failed_login(client: str, now: float | None = None) -> None:
    """Charge one failure to ``client`` and (re)start its 60-second window.

    ``mutate`` rather than get-then-set: two threadpool workers racing on the
    same client must not lose an attempt, because losing attempts weakens the
    throttle.

    The list is trimmed to the attempts that still matter. Without it a client
    that keeps failing accumulates timestamps without limit inside ONE entry —
    the cap counts entries, so an unbounded value would reintroduce the same
    unbounded-growth defect one level down.
    """
    now = time.time() if now is None else now

    def append(existing: list[float] | None) -> list[float]:
        attempts = [t for t in (existing or []) if now - t < LOGIN_LOCKOUT_SECONDS]
        attempts.append(now)
        return attempts[-LOGIN_MAX_FAILURES:]

    _login_attempts.mutate(client, append, now=now)


def clear_failed_logins(client: str) -> None:
    _login_attempts.pop(client)


# --------------------------------------------------------------------------
# who the throttle is keyed on
# --------------------------------------------------------------------------

FORWARDED_FOR_HEADER = "x-forwarded-for"

# One parser, shared with the startup validator in config, so the set of
# entries the loader accepts and the set the resolver trusts can never drift.
parse_trusted_proxies = _parse_trusted_proxies


def _is_trusted(address: str, networks) -> bool:
    if not networks:
        return False
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def resolve_client_identity(
    peer: str | None,
    forwarded_for: str,
    trusted_proxies: str | None = None,
) -> str:
    """The identity the failed-login throttle is keyed on.

    Behind a reverse proxy ``peer`` is the proxy for every caller, so keying on
    it alone puts every client in one failure bucket and lets five wrong
    guesses lock the login form for everybody. ``X-Forwarded-For`` names the
    real client — but it is a request header, so it is only worth anything when
    the hop that set it is one we configured.

    Hence:

      * peer not in the allowlist (the default, which is empty) -> the header is
        ignored entirely and the answer is ``peer``, exactly as before;
      * peer in the allowlist -> walk the header RIGHT to LEFT, past hops that
        are themselves allowlisted proxies, and take the first one that is not.

    Right-to-left matters. A proxy APPENDS the address it saw, so the rightmost
    entries are the ones our own infrastructure wrote and the leftmost is
    whatever the client sent. Taking the leftmost would let an attacker mint a
    fresh identity per request and defeat the throttle completely — strictly
    worse than the shared bucket. An unparseable hop stops the walk rather than
    being skipped, so a junk entry cannot be used to reach past it.

    When the walk finds no untrusted hop — every entry claims to be one of our
    proxies, or the header is absent — the answer falls back to ``peer``.
    """
    if not peer:
        return "unknown"

    raw = settings.trusted_proxies if trusted_proxies is None else trusted_proxies
    networks = parse_trusted_proxies(raw or "")
    if not _is_trusted(peer, networks):
        return peer

    for hop in reversed([h.strip() for h in forwarded_for.split(",") if h.strip()]):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            # Not an address at all. Stop here rather than continuing left;
            # continuing would let a junk entry act as a fence an attacker can
            # hide behind.
            return peer
        if not _is_trusted(hop, networks):
            return hop
    return peer


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
            # Tells the SPA where to send the operator instead of rendering a
            # raw 401 body. Present either way.
            response_headers = {"X-Pullbackup-Login": _LOGIN_PATH}
            if not _is_browser_subresource(headers):
                # Scripted clients, the compose healthcheck and a browser
                # navigating straight at /openapi.json get the real challenge.
                # A browser subresource fetch does NOT — see
                # _is_browser_subresource for why the previous "fetch() is
                # exempt" claim here was simply wrong.
                response_headers["WWW-Authenticate"] = 'Basic realm="Pullback"'
            response = JSONResponse(
                {"detail": "authentication required"},
                status_code=401,
                headers=response_headers,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
