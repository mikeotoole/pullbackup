"""Copyright attribution, SPDX headers, and a single authoritative version.

Two conventions are guarded here because both are packaging metadata that decays
silently:

1. **Attribution.** The project is AGPL-3.0-or-later; the licence text says
   nothing about who holds the copyright. The holder is declared once in NOTICE
   and repeated in the README so a reader never has to open a 34 KB legal file.
   The verbatim FSF licence body is *not* touched — see
   ``test_the_licence_body_is_the_verbatim_fsf_text``.

2. **Version.** Three sources used to disagree (``__init__.__version__`` said
   0.10.0, ``pyproject.toml`` said 0.5.1, the deployed image was 0.11.0) and the
   API reported the wrong one, which made a good deploy look like a failed
   rollout. ``pyproject.toml`` is now the only place a version number is written
   and ``__version__`` is derived from installed distribution metadata.
"""

import hashlib
import re
import tomllib
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPOSITORY_ROOT / "backend"
PACKAGE = BACKEND / "pullbackup"
PYPROJECT = BACKEND / "pyproject.toml"
FRONTEND_SRC = REPOSITORY_ROOT / "frontend" / "src"

COPYRIGHT_HOLDER = "Mike O'Toole"
SPDX_IDENTIFIER = "AGPL-3.0-or-later"

# sha256 of the verbatim GNU AGPL-3.0 text as published by the FSF.
LICENCE_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"


# --------------------------------------------------------------------------
# 1. attribution
# --------------------------------------------------------------------------


def test_the_licence_body_is_the_verbatim_fsf_text():
    """Attribution must never be added by editing the licence itself.

    The AGPL body is a legal artifact reproduced verbatim; a copyright line
    spliced into it would make the repository's licence a modified licence.
    This pins the exact bytes so any such edit is a test failure, not a review
    catch.
    """
    licence = REPOSITORY_ROOT / "LICENSE"
    digest = hashlib.sha256(licence.read_bytes()).hexdigest()
    assert digest == LICENCE_SHA256, (
        "LICENSE is no longer the verbatim FSF AGPL-3.0 text. Attribution "
        "belongs in NOTICE and the README, never inside the licence body."
    )


def test_a_notice_file_names_the_copyright_holder():
    """AGPL section 4 talks about "appropriate copyright notice"; the licence
    text itself carries none. NOTICE is the conventional place to say who owns
    the work, and it is short enough that people actually read it.
    """
    notice = REPOSITORY_ROOT / "NOTICE"
    assert notice.is_file(), "NOTICE is missing"

    text = notice.read_text()
    assert COPYRIGHT_HOLDER in text, (
        f"NOTICE does not name the copyright holder {COPYRIGHT_HOLDER!r}"
    )
    assert SPDX_IDENTIFIER in text, "NOTICE does not state the SPDX licence id"


def test_the_readme_names_the_copyright_holder():
    """A reader landing on the repository front page should learn both the
    licence and the holder without opening another file.
    """
    readme = (REPOSITORY_ROOT / "README.md").read_text()
    assert COPYRIGHT_HOLDER in readme, (
        f"README.md does not name the copyright holder {COPYRIGHT_HOLDER!r}"
    )


def test_the_apostrophe_in_the_holder_name_survives_every_declaration():
    """The holder's name contains a typographic hazard: ``Mike O'Toole``.

    The apostrophe is trivially lost to shell quoting, Python literals and JSON
    escaping, and the repository already contains the mangled ``Mike OToole``
    form (the git identity). Every human-facing declaration must carry the real
    name, and none may carry the mangled one.
    """
    declarations = {
        "NOTICE": REPOSITORY_ROOT / "NOTICE",
        "README.md": REPOSITORY_ROOT / "README.md",
    }
    for label, path in declarations.items():
        text = path.read_text()
        assert "O'Toole" in text, f"{label} lost the apostrophe in the holder name"
        assert not re.search(r"\bOToole\b", text), (
            f"{label} contains the apostrophe-stripped form 'OToole'"
        )


def test_the_image_ships_the_copyright_notice_beside_the_licence():
    """AGPL section 4 requires conveyed copies to keep "appropriate copyright
    notices" intact, and the licence text carries none — the holder is only
    named in NOTICE. The image already ships LICENSE (guarded in
    ``test_tranche2_rename``); shipping the licence without the notice would
    hand a recipient a licence with no licensor.

    Read from the Dockerfile's runtime stage so a COPY confined to the frontend
    build stage cannot satisfy it.
    """
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text()
    runtime = dockerfile.split("AS runtime", 1)[-1]
    assert re.search(r"^\s*COPY\s+(\S+\s+)*NOTICE\b", runtime, re.MULTILINE), (
        "the runtime stage never copies NOTICE into the image; the shipped "
        "licence would name no copyright holder"
    )


# --------------------------------------------------------------------------
# 2. SPDX headers on first-party source
# --------------------------------------------------------------------------

PY_SPDX = f"# SPDX-License-Identifier: {SPDX_IDENTIFIER}"
PY_COPYRIGHT = f"# Copyright (C) 2026 {COPYRIGHT_HOLDER}"
TS_SPDX = f"// SPDX-License-Identifier: {SPDX_IDENTIFIER}"
TS_COPYRIGHT = f"// Copyright (C) 2026 {COPYRIGHT_HOLDER}"


def _first_party_python() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _first_party_typescript() -> list[Path]:
    paths = [
        p
        for p in sorted(FRONTEND_SRC.rglob("*"))
        if p.suffix in {".ts", ".tsx"}
        and not p.name.endswith((".test.ts", ".test.tsx", ".d.ts"))
    ]
    return paths


def test_the_header_scan_actually_finds_source_files():
    """Guard against the scan silently matching nothing.

    A header test whose glob is wrong passes vacuously forever. These lower
    bounds are well below the current file counts, so they will not churn, but
    they fail loudly if the scope collapses to zero.
    """
    assert len(_first_party_python()) >= 15
    assert len(_first_party_typescript()) >= 10


@pytest.mark.parametrize(
    "path", _first_party_python(), ids=lambda p: str(p.relative_to(REPOSITORY_ROOT))
)
def test_every_first_party_python_file_carries_the_spdx_header(path: Path):
    """Scope, stated deliberately so nobody has to guess it later:

    ``backend/pullbackup/**/*.py`` — every module shipped in the wheel.

    Not in scope: ``tests/`` (not distributed), ``scripts/`` (vendor-patching
    helpers), ``frontend/dist`` and ``node_modules`` (build output and
    third-party code), and anything vendored.
    """
    head = path.read_text().splitlines()[:4]
    assert PY_SPDX in head, f"{path.relative_to(REPOSITORY_ROOT)} lacks {PY_SPDX!r}"
    assert PY_COPYRIGHT in head, (
        f"{path.relative_to(REPOSITORY_ROOT)} lacks {PY_COPYRIGHT!r}"
    )


@pytest.mark.parametrize(
    "path", _first_party_typescript(), ids=lambda p: str(p.relative_to(REPOSITORY_ROOT))
)
def test_every_first_party_typescript_file_carries_the_spdx_header(path: Path):
    """Scope, stated deliberately so nobody has to guess it later:

    ``frontend/src/**/*.ts`` and ``**/*.tsx``, excluding ``*.test.ts(x)`` and
    ``*.d.ts``.

    Not in scope: ``frontend/dist`` (build output), ``node_modules``
    (third-party), ``*.js`` test helpers, and anything vendored.
    """
    head = path.read_text().splitlines()[:4]
    assert TS_SPDX in head, f"{path.relative_to(REPOSITORY_ROOT)} lacks {TS_SPDX!r}"
    assert TS_COPYRIGHT in head, (
        f"{path.relative_to(REPOSITORY_ROOT)} lacks {TS_COPYRIGHT!r}"
    )


# --------------------------------------------------------------------------
# 3. one authoritative version
# --------------------------------------------------------------------------


def _declared_version() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def test_the_declared_version_is_the_deployed_one():
    """The version being released, and the tag of the image built from it.

    Bumping this constant is the deliberate release step: it is the one place a
    release is declared, and it fails loudly if someone builds an image tagged
    differently from what the source will report at /api/system/health. That
    disagreement is exactly the drift this guard exists to end -- three sources
    once said 0.10.0, 0.5.1 and 0.11.0 at the same time.
    """
    assert _declared_version() == "0.14.0"


def test_the_package_does_not_hardcode_a_version_literal():
    """The literal is the drift mechanism. If it comes back, so does the bug.

    ``__version__`` must be *derived*, so there is exactly one place — the
    packaging metadata — where a version number is written down.
    """
    source = (PACKAGE / "__init__.py").read_text()
    assert not re.search(r'__version__\s*=\s*["\']\d', source), (
        "backend/pullbackup/__init__.py assigns a literal version string; it "
        "must be derived from installed distribution metadata instead"
    )
    assert "importlib.metadata" in source or "importlib import metadata" in source


def test_the_imported_version_matches_the_installed_distribution_metadata():
    """Prove the derivation through the real metadata API, not by reading the
    literal we just wrote.
    """
    from importlib.metadata import version as distribution_version

    import pullbackup

    assert pullbackup.__version__ == distribution_version("pullbackup")


def test_the_installed_distribution_metadata_matches_pyproject():
    from importlib.metadata import version as distribution_version

    assert distribution_version("pullbackup") == _declared_version(), (
        "the installed pullbackup distribution is stale relative to "
        "backend/pyproject.toml; reinstall the project"
    )


def test_the_health_endpoint_reports_the_declared_version():
    """The whole point of the card: the *running app* under-reported itself, so
    a correct source value was not enough. Probe the real endpoint.
    """
    from fastapi.testclient import TestClient

    from pullbackup.main import app

    response = TestClient(app).get("/api/system/health")
    assert response.status_code == 200
    assert response.json()["version"] == _declared_version()


def test_the_info_endpoint_reports_the_declared_version():
    from fastapi.testclient import TestClient

    from pullbackup.main import app

    client = TestClient(app)
    response = client.get(
        "/api/system/info", auth=("ci", "0123456789abcdef0123456789abcdef")
    )
    assert response.status_code == 200
    assert response.json()["version"] == _declared_version()
