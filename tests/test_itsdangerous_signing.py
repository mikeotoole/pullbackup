# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""The session-cookie signing swap: hand-rolled HMAC -> ``itsdangerous``.

What actually changed is narrow: the code that turns a secret into a token, and
the code that turns a token back into a session id. Everything around it is
deliberately unmoved — the secret's source, how it is configured, the
clock-skew tolerance, the single-clock discipline, signature-before-sid
revocation, and the login throttle.

So these tests are about the SEAM. They assert the two things a library swap
can silently get wrong:

  * that the new verifier refuses everything it should, including — pointedly —
    a cookie minted by the code it replaced, and
  * that the properties earlier review rounds paid for did not evaporate into
    the library's own defaults.

The behavioural surface (login, logout, replay, the auth boundary, the
throttle) is covered by ``test_auth_session.py`` and ``test_bounded_auth_stores.py``
and is intentionally not duplicated here.
"""

import base64
import hmac
import json
import time
from hashlib import sha256

import httpx
import pytest
from pullbackup import http_auth, main
from pullbackup.config import settings

PASSWORD = "0123456789abcdef0123456789abcdef"
USERNAME = "pullbackup-test"
COOKIE = http_auth.SESSION_COOKIE


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setitem(settings.__dict__, "http_basic_username", USERNAME)
    monkeypatch.setitem(settings.__dict__, "http_basic_password", PASSWORD)
    monkeypatch.setitem(settings.__dict__, "session_secret", "")
    monkeypatch.setitem(settings.__dict__, "trusted_proxies", "")
    monkeypatch.setitem(settings.__dict__, "session_max_age_seconds", 3600)
    http_auth.reset_auth_state()
    yield
    http_auth.reset_auth_state()


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://test",
    )


def _legacy_token(sid: str, issued_at: float) -> str:
    """A cookie minted exactly the way the REMOVED signer minted them.

    Reproduced here rather than imported, because the point is that the
    production code no longer contains it. Copied verbatim from
    ``http_auth.issue_session`` at 90f6621: a base64url JSON body of
    ``{"iat": ..., "sid": ...}`` and an HMAC-SHA256 of that body under the same
    derived key, joined with a dot.
    """
    key = sha256(
        b"pullbackup.session.v1|derived|"
        + settings.http_basic_username.encode()
        + b"|"
        + settings.http_basic_password.encode()
    ).digest()

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = json.dumps(
        {"sid": sid, "iat": int(issued_at)}, separators=(",", ":"), sort_keys=True
    ).encode()
    body = b64(payload)
    return f"{body}.{b64(hmac.new(key, body.encode(), sha256).digest())}"


# --------------------------------------------------------------------------
# the upgrade boundary
# --------------------------------------------------------------------------


def test_the_legacy_signer_still_produces_the_token_it_used_to(configured):
    """Anti-vacuity guard for the rejection test below.

    If ``_legacy_token`` drifted into producing junk, the rejection test would
    pass for the wrong reason — junk is refused by anything. So first prove the
    fixture really is the old format: a dotted pair whose body decodes to the
    old ``iat``/``sid`` claims under the old derived key.
    """
    token = _legacy_token("legacy-sid", time.time())
    body, separator, signature = token.partition(".")
    assert separator and signature

    key = sha256(
        b"pullbackup.session.v1|derived|"
        + settings.http_basic_username.encode()
        + b"|"
        + settings.http_basic_password.encode()
    ).digest()
    expected = hmac.new(key, body.encode(), sha256).digest()
    padded = signature + "=" * (-len(signature) % 4)
    assert hmac.compare_digest(expected, base64.urlsafe_b64decode(padded)), (
        "the legacy fixture is not signed the way the old code signed"
    )

    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert set(claims) == {"iat", "sid"}
    assert claims["sid"] == "legacy-sid"


def test_a_cookie_from_the_old_signer_is_rejected_not_silently_accepted(configured):
    """The deliberate upgrade behaviour: everyone is logged out once on deploy.

    The token format changed, so a cookie minted by the previous signer must be
    REFUSED rather than reinterpreted. Chosen, not incidental — the salt is
    bumped to v2 specifically to make it so. The alternative is keeping the
    retired verifier alive forever so two signing implementations must both
    stay correct, which is the maintenance burden this change exists to shed.

    For a single-user app the cost is one re-login, and it fails in the safe
    direction. Note the legacy token is FRESH here (issued now, well inside
    max_age), so the only thing that can refuse it is the signature check.
    """
    fresh_legacy = _legacy_token("legacy-sid", time.time())

    assert http_auth.session_is_valid(fresh_legacy) is False, (
        "a cookie from the retired signer was accepted by the new verifier"
    )


@pytest.mark.asyncio
async def test_the_old_cookie_is_rejected_through_the_real_middleware(configured):
    """The same boundary through HTTP, not just the helper."""
    fresh_legacy = _legacy_token("legacy-sid", time.time())

    async with client() as c:
        refused = await c.get(
            "/api/system/info", headers={"Cookie": f"{COOKIE}={fresh_legacy}"}
        )

    assert refused.status_code == 401


def test_revoking_a_legacy_cookie_does_not_insert_its_sid(configured):
    """PR #16 finding 3 must still hold for the tokens that just stopped working.

    ``/api/auth/logout`` is unauthenticated by design. A token the verifier
    refuses — which now includes every legacy cookie — must not be able to put
    an attacker-chosen sid into the revocation store.
    """
    before = len(http_auth._revoked)
    http_auth.revoke_session(_legacy_token("attacker-chosen", time.time()))

    assert "attacker-chosen" not in http_auth._revoked
    assert len(http_auth._revoked) == before


# --------------------------------------------------------------------------
# properties the swap had to carry across
# --------------------------------------------------------------------------


def test_the_expiry_boundary_is_inclusive_at_exactly_max_age(configured):
    """Exactly on the boundary is VALID; one second past it is not.

    Pinned because expiry moved from a hand-written comparison to a decision
    about how much of ``itsdangerous`` to use. ``loads(max_age=...)`` was
    deliberately NOT used: it measures age against the library's own
    ``time.time()``, which would break the single-clock discipline, and it
    cannot express the future tolerance below.
    """
    minted_at = 1_000_000.0
    token = http_auth.issue_session(now=minted_at)
    max_age = settings.session_max_age_seconds

    assert http_auth.session_is_valid(token, now=minted_at + max_age) is True
    assert http_auth.session_is_valid(token, now=minted_at + max_age + 1) is False


def test_a_future_dated_token_is_tolerated_to_60s_and_refused_past_it(configured):
    """The clock-skew tolerance, both edges.

    A future ``iat`` on a correctly signed token means a clock jump, not a
    forgery — the signature is verified first. So a little is tolerated. An
    unbounded amount is not, because it would extend the session past the
    configured maximum.
    """
    now = 2_000_000.0
    tolerance = http_auth._CLOCK_SKEW_TOLERANCE_SECONDS

    just_inside = http_auth.issue_session(now=now + tolerance)
    too_far = http_auth.issue_session(now=now + tolerance + 1)

    assert http_auth.session_is_valid(just_inside, now=now) is True
    assert http_auth.session_is_valid(too_far, now=now) is False


def test_the_minting_clock_is_the_caller_s_not_the_library_s(configured):
    """``issue_session(now=...)`` must actually stamp that instant.

    ``itsdangerous`` stamps ``int(time.time())`` of its own accord. If the
    override were lost, every token would be minted at the wall clock and the
    age tests above would silently become tests of "now", while the
    revocation-retention proof in ``test_bounded_auth_stores.py`` would stop
    measuring what it claims to.

    Asserted by consequence rather than by reading the timestamp: a token
    minted an hour in the past must be judged an hour old.
    """
    minted_at = time.time() - 3599
    token = http_auth.issue_session(now=minted_at)

    # Still inside the 3600s window, but only just.
    assert http_auth.session_is_valid(token) is True
    assert http_auth.session_is_valid(token, now=minted_at + 3601) is False


def test_revocation_and_age_are_judged_against_one_clock(configured):
    """PR #20's single-clock discipline, end to end through the new signer.

    Both the revocation lookup and the age comparison must use the caller's
    ``now``. If the store were read at the wall clock while age was judged at a
    supplied instant, the two could disagree about whether an entry is live and
    a revoked cookie could come back.
    """
    minted_at = 3_000_000.0  # far from any real wall clock
    token = http_auth.issue_session(now=minted_at)

    assert http_auth.session_is_valid(token, now=minted_at + 10) is True
    http_auth.revoke_session(token, now=minted_at + 10)
    assert http_auth.session_is_valid(token, now=minted_at + 11) is False


def test_a_changed_password_still_invalidates_and_an_explicit_secret_still_does_not(
    configured, monkeypatch
):
    """The secret's source is unchanged by the swap.

    Both branches, because collapsing them would leave the password-derived
    assertion passing on its own.
    """
    derived = http_auth.issue_session()
    assert http_auth.session_is_valid(derived) is True
    monkeypatch.setitem(
        settings.__dict__, "http_basic_password", "fedcba9876543210fedcba9876543210"
    )
    assert http_auth.session_is_valid(derived) is False

    monkeypatch.setitem(settings.__dict__, "session_secret", "s" * 40)
    explicit = http_auth.issue_session()
    monkeypatch.setitem(
        settings.__dict__, "http_basic_password", "00112233445566778899aabbccddeeff"
    )
    assert http_auth.session_is_valid(explicit) is True


def test_the_token_carries_no_credential_material(configured):
    """A signed cookie is not an encrypted one: whatever is in it is readable."""
    token = http_auth.issue_session()

    assert PASSWORD not in token
    assert USERNAME not in token
    # The payload segment decodes to the sid and nothing else.
    payload, _ = http_auth._serializer().loads(token, return_timestamp=True)
    assert set(payload) == {"sid"}


def test_itsdangerous_is_what_is_actually_doing_the_signing(configured):
    """Guard against the swap being reverted in substance while the import stays.

    Asserts the shape the library produces — ``payload.timestamp.signature``,
    three dot-separated segments — and that the library's own verifier accepts
    the token our code minted. A re-hand-rolled two-segment token would fail
    both halves.
    """
    from itsdangerous import URLSafeTimedSerializer

    token = http_auth.issue_session()
    assert len(token.split(".")) == 3, token

    independent = URLSafeTimedSerializer(
        http_auth._signing_key(),
        salt=http_auth._SESSION_SALT,
        signer_kwargs={"digest_method": sha256},
    )
    payload = independent.loads(token)
    assert isinstance(payload["sid"], str)


def test_the_mac_is_still_sha256_not_the_library_default(configured):
    """``itsdangerous`` defaults to HMAC-SHA1; the retired signer used SHA256.

    Found by probing the built image, not by the suite: a swap meant to change
    only the implementation would otherwise have shortened the MAC from 256 to
    160 bits as an invisible side effect of a default. Neither digest is broken
    for signing a session cookie, but a library adoption must not silently
    weaken a property nobody asked it to touch.

    Asserted by signature LENGTH, which is the observable consequence: a
    urlsafe-base64 SHA256 MAC is 43 unpadded characters, SHA1 is 27.
    """
    signature = http_auth.issue_session().rsplit(".", 1)[1]

    assert len(signature) == 43, (
        f"signature is {len(signature)} chars; 27 means the HMAC silently "
        "fell back to itsdangerous' SHA1 default"
    )


def test_a_last_character_signature_alias_is_a_pre_existing_property(configured):
    """Not a defect introduced here — pinned so nobody re-discovers it as one.

    A base64url signature encodes its bytes in 6-bit groups, and neither a
    SHA256 (256 bits, 43 chars = 258 bits) nor a SHA1 MAC divides evenly. The
    final character therefore has spare low bits, so a handful of distinct
    characters decode to the SAME signature bytes. Both this signer and the
    retired one decode-then-compare, so both accept those aliases.

    Measured at base 90f6621 with the OLD hand-rolled signer: 4 of 64 last-char
    variants accepted. Measured here: the same 4. It confirms nothing about the
    strength of the MAC — the aliases are the identical signature, not a second
    valid one — and forging still requires the key. Recorded because a
    tamper probe that flips the last character will occasionally see a 200 and
    look like a signature-verification hole.
    """
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    now = time.time()
    token = http_auth.issue_session(now=now)
    assert http_auth.session_is_valid(token, now=now)

    accepted = [
        ch for ch in alphabet if http_auth.session_is_valid(token[:-1] + ch, now=now)
    ]
    assert len(accepted) == 4, accepted

    # The real assertion: every accepted variant decodes to the SAME bytes, so
    # nothing beyond the one true signature is being admitted.
    def decode(segment: str) -> bytes:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))

    original = decode(token.rsplit(".", 1)[1])
    for ch in accepted:
        variant = (token[:-1] + ch).rsplit(".", 1)[1]
        assert decode(variant) == original

    # And a change ANYWHERE else in the signature is refused outright.
    signature_head_mutated = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    assert http_auth.session_is_valid(signature_head_mutated, now=now) is False
