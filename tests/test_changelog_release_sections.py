"""The changelog is a release artifact, so it is guarded like one.

A patch release moves bullets rather than writing them. That is a mechanical
edit, and mechanical edits are exactly where wording silently drifts: a bullet
gets "tidied", a technical detail is lost, and the changelog stops describing
what actually shipped. Nothing catches that in review, because a reviewer reads
the new text and it looks fine.

So the guard here is not "a 0.12.1 section exists" -- it is that the 0.12.1
section body is *byte-for-byte* the [Unreleased] body that existed on the exact
release base, ``6f0dbfdde5599b2b1e0359467e20364ff7ee52a3``. The digest below was
taken from that commit before the move:

    git show 6f0dbfdde5599b2b1e0359467e20364ff7ee52a3:CHANGELOG.md

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

RELEASE_VERSION = "0.12.1"

# sha256 of the stripped [Unreleased] body at the release base commit
# 6f0dbfdde5599b2b1e0359467e20364ff7ee52a3 -- the auth-store cap validation
# (Security) plus the dedicated/most-specific destination-root correction
# (Fixed). This is the content 0.12.1 releases.
UNRELEASED_BODY_AT_BASE_SHA256 = (
    "6654e53d91240745e794d322ce99bdeccf0acfb40c822713065e8dcbe9dc0cd3"
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


def test_the_release_section_still_names_both_shipped_fixes():
    """A digest tells you *that* something changed, not *what* is missing.

    These two are the substance of 0.12.1, so they are named explicitly: a
    future rewrite that drops one gets a readable failure rather than a hex
    mismatch.
    """
    body = _body(RELEASE_VERSION)
    assert "PULLBACKUP_AUTH_STORE_MAX_ENTRIES" in body
    assert "is a configured destination root" in body
    assert "### Security" in body
    assert "### Fixed" in body


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def test_a_fresh_empty_unreleased_section_is_left_for_the_next_change():
    body = _body("Unreleased")
    assert body.strip() == "", (
        "[Unreleased] must be left empty after a release so the next change "
        f"has somewhere to go; it still contains: {body.strip()[:200]!r}"
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
