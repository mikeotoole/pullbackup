"""The changelog is a release artifact, so it is guarded like one.

A patch release moves bullets rather than writing them. That is a mechanical
edit, and mechanical edits are exactly where wording silently drifts: a bullet
gets "tidied", a technical detail is lost, and the changelog stops describing
what actually shipped. Nothing catches that in review, because a reviewer reads
the new text and it looks fine.

So the guard here is not "a 0.13.0 section exists" -- it is that the 0.13.0
section body is *byte-for-byte* the [Unreleased] body that existed on the exact
release base, ``3b39e4ddd59ca3748de030462025da1fbef6f758``. The digest below was
taken from that commit before the move:

    git show 3b39e4ddd59ca3748de030462025da1fbef6f758:CHANGELOG.md

The digest is over the stripped section body, so re-spacing around the heading
is allowed and re-wording is not.

The structural half exists because Keep a Changelog is a format, not a vibe: a
released section needs an ISO date, an empty [Unreleased] must be left behind
for the next change, and versions must read newest-first.
"""

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Mike O'Toole

import hashlib
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPOSITORY_ROOT / "CHANGELOG.md"

RELEASE_VERSION = "0.13.0"

# sha256 of the stripped [Unreleased] body at the release base commit
# 3b39e4ddd59ca3748de030462025da1fbef6f758 -- the itsdangerous session-signing
# swap (Changed), whose user-visible consequence is that upgrading signs every
# existing session out once. This is the content 0.13.0 releases.
UNRELEASED_BODY_AT_BASE_SHA256 = (
    "bc08fcdc9e95f76f57a9bf40d98c5be1bd851d630d22852c6c78f219ccca5f16"
)

HEADING = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?\s*$", re.M)


def _sections() -> list[tuple[str, str | None, str]]:
    """[(version, iso_date_or_None, body), ...] in document order."""
    text = CHANGELOG.read_text()
    matches = list(HEADING.finditer(text))
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1), match.group(2), text[match.end() : end]))
    return sections


def _body(version: str) -> str:
    for name, _date, body in _sections():
        if name == version:
            return body
    raise AssertionError(
        f"CHANGELOG.md has no '## [{version}]' section; "
        f"found {[name for name, _, _ in _sections()]}"
    )


# --------------------------------------------------------------------------
# content preservation
# --------------------------------------------------------------------------


def test_the_release_section_preserves_the_unreleased_body_verbatim():
    digest = hashlib.sha256(_body(RELEASE_VERSION).strip().encode()).hexdigest()
    assert digest == UNRELEASED_BODY_AT_BASE_SHA256, (
        f"the [{RELEASE_VERSION}] body is not the [Unreleased] body from the "
        "release base. A release moves bullets; it does not reword them. If a "
        "bullet genuinely needs to change, change it in a separate commit that "
        "says so and update this digest deliberately."
    )


def test_the_release_section_still_names_what_shipped():
    """A digest tells you *that* something changed, not *what* is missing.

    These are the substance of 0.13.0, so they are named explicitly: a future
    rewrite that drops one gets a readable failure rather than a hex mismatch.

    Each release retargets this test to its own contents. 0.12.1 named the
    auth-store cap and the destination-root correction; 0.13.0 ships the
    session-signing swap, so it names that instead. The digest above still
    pins the body byte-for-byte -- this is the readable half of the same
    guard, not a second, weaker one.
    """
    body = _body(RELEASE_VERSION)
    assert "itsdangerous" in body
    # The user-visible consequence, which is the reason this is a MINOR bump
    # and not a patch: existing sessions do not survive the upgrade. If a
    # future edit tidies this sentence away, the release stops warning about
    # the one thing an operator needs to know before deploying it.
    assert "signs every existing session out once" in body
    assert "### Changed" in body


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def test_a_fresh_unreleased_section_exists_for_the_next_change():
    """The release must leave an [Unreleased] heading behind, and must not
    leave the shipped bullets sitting in it.

    This deliberately does NOT assert the section is byte-empty. It did, and
    that made the test unsatisfiable for every subsequent change: the moment
    any branch added a bullet under [Unreleased] - which is exactly where the
    heading's own failure message says the next change should go - this test
    failed. Verified against clean main by inserting an unrelated hypothetical
    bullet: 1 failed, no PR of any kind could have been green.

    The property worth pinning is that a release MOVES its bullets rather than
    copying them, so nothing shipped in [RELEASE_VERSION] is left duplicated
    under [Unreleased]. A new entry added after the release is correct and must
    not fail.
    """
    unreleased = _body("Unreleased")
    released = _body(RELEASE_VERSION)

    # The heading must still exist; _body raises if it does not.
    def _bullets(section: str) -> set[str]:
        # Collapse internal whitespace before comparing. A mechanical
        # copy-instead-of-move is verbatim by construction, but a copy that
        # picks up a re-wrap or a double space would otherwise slip through the
        # set comparison while still being the duplication this guards against.
        return {
            " ".join(line.split())
            for line in section.splitlines()
            if line.strip().startswith("- ")
        }

    released_bullets = _bullets(released)
    unreleased_bullets = _bullets(unreleased)
    leaked = released_bullets & unreleased_bullets
    assert not leaked, (
        f"the release copied bullets instead of moving them; these appear under "
        f"both [Unreleased] and [{RELEASE_VERSION}]: {sorted(leaked)[:3]}"
    )


def test_unreleased_comes_first_and_released_versions_are_newest_first():
    names = [name for name, _, _ in _sections()]
    assert names[0] == "Unreleased", f"first section is {names[0]!r}"

    released = [
        (name, date) for name, date, _ in _sections() if name != "Unreleased"
    ]
    assert released[0][0] == RELEASE_VERSION, (
        f"the newest released section is {released[0][0]!r}, not {RELEASE_VERSION!r}"
    )

    dates = [date for _name, date in released]
    assert all(d is not None for d in dates), (
        f"every released section needs an ISO date: {released}"
    )
    present = [d for d in dates if d is not None]
    assert present == sorted(present, reverse=True), (
        f"released sections must read newest-first: {released}"
    )


def test_every_released_section_has_a_semver_version_and_an_iso_date():
    for name, date, _body_text in _sections():
        if name == "Unreleased":
            continue
        assert re.fullmatch(r"\d+\.\d+\.\d+", name), (
            f"{name!r} is not a semantic version"
        )
        assert date is not None, f"[{name}] has no release date"


def test_the_changelog_version_matches_the_declared_package_version():
    """The one thing that makes this file a release artifact rather than prose.

    ``backend/pyproject.toml`` is the single authoritative version (see
    tests/test_licence_headers_and_version.py). If the changelog's newest
    released section disagrees with it, one of the two is lying about what
    shipped.
    """
    import tomllib

    declared = tomllib.loads(
        (REPOSITORY_ROOT / "backend" / "pyproject.toml").read_text()
    )["project"]["version"]
    newest = next(name for name, _, _ in _sections() if name != "Unreleased")
    assert newest == declared, (
        f"CHANGELOG.md's newest release is {newest!r} but "
        f"backend/pyproject.toml declares {declared!r}"
    )
