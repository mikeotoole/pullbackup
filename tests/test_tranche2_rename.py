"""Tranche 2: package rename, env prefix migration, and the database override.

These guard the three changes that CAN break a running deployment, so each one
is written against the failure mode rather than the happy path.

The env prefix is the dangerous one. pydantic-settings falls back to a field's
default for any variable it cannot find, so if the code starts reading
``PULLBACKUP_*`` while the deployment still exports ``PULLBACK_*``, the app does
not crash: it silently reverts ``dest_roots`` to the built-in default and blanks
the HTTP Basic credentials. That is a fail-OPEN config reset on a security
boundary. The loader must refuse to start instead.

Note the prefix trap: ``PULLBACK_`` is a prefix of ``PULLBACKUP_`` only if the
trailing underscore is dropped. Every check below keeps the underscore, and
``test_legacy_prefix_detection_does_not_match_the_new_prefix`` pins that.
"""

import importlib
import json
import os
import re
import tomllib
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPOSITORY_ROOT / "backend"
PACKAGE = BACKEND / "pullbackup"
DOCKERFILE = REPOSITORY_ROOT / "Dockerfile"
PYPROJECT = BACKEND / "pyproject.toml"
COMPOSE = REPOSITORY_ROOT / "docker" / "compose.example.yaml"
ENV_EXAMPLE = REPOSITORY_ROOT / ".env.example"

NEW_PREFIX = "PULLBACKUP_"
OLD_PREFIX = "PULLBACK_"


# --------------------------------------------------------------------------
# 1. package rename
# --------------------------------------------------------------------------

def test_package_directory_is_renamed():
    assert PACKAGE.is_dir(), "backend/pullbackup/ must exist"
    assert not (BACKEND / "pullback").exists(), "old package directory must be gone"


def test_package_imports_under_the_new_name():
    module = importlib.import_module("pullbackup.config")
    assert module is not None


def test_no_python_source_imports_the_old_package():
    """A stale ``from pullbackup.x import y`` would fail only at runtime, on the
    code path that happens to import it."""
    offenders = []
    pattern = re.compile(r"^\s*(?:from|import)\s+pullback(?!up)\b", re.MULTILINE)
    for path in list(PACKAGE.rglob("*.py")) + list((REPOSITORY_ROOT / "tests").rglob("*.py")):
        if pattern.search(path.read_text()):
            offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert not offenders, f"stale old-package imports in: {offenders}"


def test_packaging_and_container_entrypoint_use_the_new_module():
    pyproject = PYPROJECT.read_text()
    assert 'name = "pullbackup"' in pyproject
    assert 'packages = ["pullbackup"]' in pyproject

    dockerfile = DOCKERFILE.read_text()
    assert "COPY backend/pullbackup ./pullbackup" in dockerfile
    assert '"pullbackup.main:app"' in dockerfile, (
        "container CMD must point at the renamed module or the image will not boot"
    )


# --------------------------------------------------------------------------
# 2. env prefix migration, fail-closed
# --------------------------------------------------------------------------

def test_settings_read_the_new_prefix():
    from pullbackup.config import Settings

    assert Settings.model_config["env_prefix"] == NEW_PREFIX


def test_legacy_prefix_detection_does_not_match_the_new_prefix():
    """Guard the guard. ``PULLBACKUP_DATA_DIR`` must not be classified as a
    legacy variable, or the loader would reject a correctly-configured host."""
    from pullbackup.config import orphaned_legacy_variables

    assert orphaned_legacy_variables({"PULLBACK_DATA_DIR": "/data"}) == ["PULLBACK_DATA_DIR"]
    assert orphaned_legacy_variables({"PULLBACKUP_DATA_DIR": "/data"}) == []
    assert orphaned_legacy_variables({"PULLBACKUP_HTTP_BASIC_PASSWORD": "x"}) == []


def test_legacy_detection_is_per_variable_not_global():
    """Regression guard for a gap found by running the real container.

    The image sets ``PULLBACKUP_DATA_DIR`` in its own ENV, so at runtime some
    new-prefix variable ALWAYS exists. A global "any new-prefix var present"
    check therefore never fires inside the container, and a stack exporting
    only ``PULLBACK_DEST_ROOTS`` would silently revert the rsync allowlist to
    its default while startup succeeded.
    """
    from pullbackup.config import orphaned_legacy_variables

    environ = {
        "PULLBACKUP_DATA_DIR": "/data",      # set by the Dockerfile
        "PULLBACK_DEST_ROOTS": "/mnt/dest/backups",  # orphaned legacy name
    }
    assert orphaned_legacy_variables(environ) == ["PULLBACK_DEST_ROOTS"]


def test_startup_accepts_the_new_prefix():
    from pullbackup.config import load_settings

    settings = load_settings({
        "PULLBACKUP_DATA_DIR": "/tmp/x",
        "PULLBACKUP_DEST_ROOTS": "/mnt/dest/backups",
        "PULLBACKUP_HTTP_BASIC_USERNAME": "ci",
        "PULLBACKUP_HTTP_BASIC_PASSWORD": "0123456789abcdef0123456789abcdef",
    })
    assert str(settings.data_dir) == "/tmp/x"
    assert settings.dest_roots == "/mnt/dest/backups"


def test_transitional_dual_configuration_is_allowed():
    """During the cutover the deployment exports BOTH prefixes. The new values
    win and startup proceeds — that is what makes a zero-downtime switch
    possible."""
    from pullbackup.config import load_settings

    settings = load_settings({
        "PULLBACK_DEST_ROOTS": "/old/root",
        "PULLBACKUP_DEST_ROOTS": "/mnt/dest/backups",
        "PULLBACKUP_HTTP_BASIC_USERNAME": "ci",
        "PULLBACKUP_HTTP_BASIC_PASSWORD": "0123456789abcdef0123456789abcdef",
    })
    assert settings.dest_roots == "/mnt/dest/backups"


def test_unprefixed_matrix_and_kuma_variables_are_untouched():
    """Those are deliberately unprefixed and must not be swept into the rename."""
    from pullbackup.config import load_settings

    settings = load_settings({
        "PULLBACKUP_HTTP_BASIC_USERNAME": "ci",
        "PULLBACKUP_HTTP_BASIC_PASSWORD": "0123456789abcdef0123456789abcdef",
        "MATRIX_HOMESERVER": "https://matrix.example",
        "MATRIX_TOKEN": "t",
        "MATRIX_ROOM_ID": "!r:example",
    })
    assert settings.matrix_enabled


# --------------------------------------------------------------------------
# 3. database filename override
# --------------------------------------------------------------------------

def test_database_filename_defaults_to_the_existing_file():
    """The deployed volume holds ``pullbackup.db``. Changing this default without
    a migration makes SQLModel create a NEW empty database, and every task,
    source, run, and log silently disappears from the UI."""
    from pullbackup.config import load_settings

    settings = load_settings({
        "PULLBACKUP_DATA_DIR": "/data",
        "PULLBACKUP_HTTP_BASIC_USERNAME": "ci",
        "PULLBACKUP_HTTP_BASIC_PASSWORD": "0123456789abcdef0123456789abcdef",
    })
    assert settings.db_path == Path("/data/pullback.db")


def test_database_filename_can_be_overridden():
    """The override is what makes the eventual controlled rename possible
    without a code change."""
    from pullbackup.config import load_settings

    settings = load_settings({
        "PULLBACKUP_DATA_DIR": "/data",
        "PULLBACKUP_DB_FILENAME": "pullbackup.db",
        "PULLBACKUP_HTTP_BASIC_USERNAME": "ci",
        "PULLBACKUP_HTTP_BASIC_PASSWORD": "0123456789abcdef0123456789abcdef",
    })
    assert settings.db_path == Path("/data/pullbackup.db")


def test_database_filename_rejects_a_path_separator():
    """The override names a file inside the data directory. Accepting a path
    would let it escape the volume and write outside the mount."""
    from pullbackup.config import ConfigurationError, load_settings

    for bad in ("../outside.db", "nested/x.db", "/abs.db"):
        with pytest.raises(ConfigurationError):
            load_settings({
                "PULLBACKUP_DATA_DIR": "/data",
                "PULLBACKUP_DB_FILENAME": bad,
                "PULLBACKUP_HTTP_BASIC_USERNAME": "ci",
                "PULLBACKUP_HTTP_BASIC_PASSWORD": "0123456789abcdef0123456789abcdef",
            })


# --------------------------------------------------------------------------
# operator-facing documentation of the cutover
# --------------------------------------------------------------------------

def test_env_example_documents_the_new_prefix():
    text = ENV_EXAMPLE.read_text()
    assert "PULLBACKUP_DATA_DIR" in text
    assert "PULLBACKUP_DB_FILENAME" in text


# --------------------------------------------------------------------------
# review 323 regressions
# --------------------------------------------------------------------------

def test_legacy_value_in_dotenv_is_not_missed(tmp_path):
    """Review 323, finding 1 (high/security).

    Settings loads BOTH os.environ and .env, but legacy detection originally
    inspected only os.environ. A host with new-prefix credentials exported and
    PULLBACK_DEST_ROOTS living in .env passed the check, and the operator's
    configured allowlist was silently replaced by the built-in default.
    Reproduced before the fix: dest_roots became /mnt/dest/backups, nothing
    raised.
    """
    from pullbackup.config import orphaned_legacy_variables

    env_file = tmp_path / ".env"
    env_file.write_text("PULLBACK_DEST_ROOTS=/mnt/user/allowlisted\n")

    environ = {
        "PULLBACKUP_HTTP_BASIC_USERNAME": "ci",
        "PULLBACKUP_HTTP_BASIC_PASSWORD": "0123456789abcdef0123456789abcdef",
    }
    assert orphaned_legacy_variables(environ, env_file) == ["PULLBACK_DEST_ROOTS"]


def test_dotenv_counterpart_satisfies_the_check(tmp_path):
    """A .env that has been migrated must NOT be rejected."""
    from pullbackup.config import orphaned_legacy_variables

    env_file = tmp_path / ".env"
    env_file.write_text(
        "PULLBACK_DEST_ROOTS=/mnt/user/allowlisted\n"
        "PULLBACKUP_DEST_ROOTS=/mnt/user/allowlisted\n"
    )
    assert orphaned_legacy_variables({}, env_file) == []


def test_dotenv_parsing_ignores_comments_and_blanks(tmp_path):
    from pullbackup.config import orphaned_legacy_variables

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n# PULLBACK_DEST_ROOTS=/commented/out\n\n"
        "PULLBACKUP_DATA_DIR=/data\n"
    )
    assert orphaned_legacy_variables({}, env_file) == []


def test_ci_workflow_uses_the_new_env_prefix():
    """Regression guard for a real CI failure at head d0ad547a.

    The tests workflow exported PULLBACK_DATA_DIR / PULLBACK_HTTP_BASIC_*, so
    with the fail-closed guard in place every test module aborted at import
    time with ConfigurationError and the job exited 2. The guard was correct;
    the workflow was simply unmigrated.

    It went unnoticed locally because the gate script exported BOTH prefixes,
    which meant the guard could never fire there. Pin the workflow to the new
    prefix so CI and the application agree.
    """
    workflow = (REPOSITORY_ROOT / ".gitea" / "workflows" / "tests.yml").read_text()

    legacy = re.findall(r"^\s*(PULLBACK_[A-Z_]+):", workflow, re.MULTILINE)
    assert not legacy, (
        f"workflow still exports retired legacy variables {legacy}; the "
        "application fail-closes on these at import time"
    )
    assert "PULLBACKUP_DATA_DIR:" in workflow
    assert "PULLBACKUP_HTTP_BASIC_USERNAME:" in workflow
    assert "PULLBACKUP_HTTP_BASIC_PASSWORD:" in workflow


# --------------------------------------------------------------------------
# review 324 regressions
# --------------------------------------------------------------------------

def test_dotenv_export_syntax_is_not_a_bypass(tmp_path):
    """Review 324, finding 1 (high/security).

    ``export NAME=value`` is valid dotenv and python-dotenv strips the keyword,
    so pydantic-settings honours the value. The hand-rolled parser recorded the
    name as "export PULLBACK_DEST_ROOTS" and never flagged it, leaving the
    fail-closed guard bypassable by a one-word change in the operator's file.
    Verified against the real library before fixing: pydantic-settings read
    /mnt/user/allowlisted while the detector returned [].
    """
    from pullbackup.config import orphaned_legacy_variables

    env_file = tmp_path / ".env"
    env_file.write_text("export PULLBACK_DEST_ROOTS=/mnt/user/allowlisted\n")
    assert orphaned_legacy_variables({}, env_file) == ["PULLBACK_DEST_ROOTS"]


def test_dotenv_quoted_and_spaced_forms_are_not_a_bypass(tmp_path):
    """The same class of divergence, other supported dotenv spellings."""
    from pullbackup.config import orphaned_legacy_variables

    env_file = tmp_path / ".env"
    env_file.write_text(
        'export PULLBACK_DEST_ROOTS="/mnt/user/a"\n'
        "PULLBACK_DATA_DIR = '/data'\n"
    )
    assert orphaned_legacy_variables({}, env_file) == [
        "PULLBACK_DATA_DIR",
        "PULLBACK_DEST_ROOTS",
    ]


def test_supplied_env_file_actually_supplies_settings_values(tmp_path, monkeypatch):
    """Review 324, finding 2 (medium/correctness).

    load_settings validated legacy names from the supplied file but built
    Settings without it, so model_config's default ".env" was read instead and
    a caller's custom file was silently ignored.

    The environ argument is passed empty AND the ambient process variables are
    cleared, because process values legitimately outrank the file — leaving
    them set would make this pass for the wrong reason.
    """
    from pullbackup.config import load_settings

    for name in ("PULLBACKUP_DEST_ROOTS", "PULLBACKUP_HTTP_BASIC_USERNAME"):
        monkeypatch.delenv(name, raising=False)

    env_file = tmp_path / "custom.env"
    env_file.write_text(
        "PULLBACKUP_DEST_ROOTS=/mnt/from/the/supplied/file\n"
        "PULLBACKUP_HTTP_BASIC_USERNAME=fromfile\n"
        "PULLBACKUP_HTTP_BASIC_PASSWORD=0123456789abcdef0123456789abcdef\n"
    )
    settings = load_settings({}, env_file=env_file)
    assert settings.dest_roots == "/mnt/from/the/supplied/file"
    assert settings.http_basic_username == "fromfile"


def test_process_environment_still_wins_over_the_env_file(tmp_path):
    """Precedence must not invert: explicit process env beats the file."""
    from pullbackup.config import load_settings

    env_file = tmp_path / "custom.env"
    env_file.write_text("PULLBACKUP_DEST_ROOTS=/from/file\n")
    settings = load_settings(
        {
            "PULLBACKUP_DEST_ROOTS": "/from/process",
            "PULLBACKUP_HTTP_BASIC_USERNAME": "ci",
            "PULLBACKUP_HTTP_BASIC_PASSWORD": "0123456789abcdef0123456789abcdef",
        },
        env_file=env_file,
    )
    assert settings.dest_roots == "/from/process"


# --------------------------------------------------------------------------
# legacy adoption (replaces the earlier refuse-to-start behaviour)
# --------------------------------------------------------------------------
#
# Refusing outright deadlocked the rollout. CI runs under
# `pull_request_target`, so Gitea executes the BASE branch's tests.yml, which
# still exports PULLBACK_*; a build that refuses those names can never pass
# its own gate in order to be merged. The live stack has the same ordering
# trap. Adoption keeps the real safety property - no silent fall back to
# built-in defaults - while allowing the transition to proceed.


def test_orphaned_legacy_process_variables_are_adopted():
    """The operator's value must be USED, never silently replaced by a default."""
    from pullbackup.config import load_settings

    settings = load_settings({
        "PULLBACK_DATA_DIR": "/data",
        "PULLBACK_DEST_ROOTS": "/mnt/user/allowlisted",
        "PULLBACK_HTTP_BASIC_USERNAME": "ci",
        "PULLBACK_HTTP_BASIC_PASSWORD": "0123456789abcdef0123456789abcdef",
    }, env_file=None)

    assert settings.dest_roots == "/mnt/user/allowlisted", (
        "the configured allowlist must be honoured, not reverted to the default"
    )
    assert settings.http_basic_username == "ci"
    assert settings.http_basic_password == "0123456789abcdef0123456789abcdef"


def test_adoption_never_silently_widens_the_allowlist():
    """The precise hazard: a legacy-only allowlist must not become the default."""
    from pullbackup.config import Settings, load_settings

    default_roots = Settings.model_fields["dest_roots"].default
    settings = load_settings({"PULLBACK_DEST_ROOTS": "/mnt/only/this/one"}, env_file=None)

    assert settings.dest_roots == "/mnt/only/this/one"
    assert settings.dest_roots != default_roots


def test_orphaned_legacy_dotenv_values_are_adopted(tmp_path):
    from pullbackup.config import load_settings

    env_file = tmp_path / ".env"
    env_file.write_text("export PULLBACK_DEST_ROOTS=/mnt/user/allowlisted\n")
    settings = load_settings({}, env_file=env_file)
    assert settings.dest_roots == "/mnt/user/allowlisted"


def test_new_prefix_wins_over_an_adopted_legacy_name():
    """A migrated environment must be completely unaffected by adoption."""
    from pullbackup.config import load_settings

    settings = load_settings({
        "PULLBACK_DEST_ROOTS": "/old/value",
        "PULLBACKUP_DEST_ROOTS": "/new/value",
        "PULLBACKUP_HTTP_BASIC_USERNAME": "ci",
        "PULLBACKUP_HTTP_BASIC_PASSWORD": "0123456789abcdef0123456789abcdef",
    }, env_file=None)
    assert settings.dest_roots == "/new/value"


def test_adoption_warns_so_the_shim_is_not_forgotten(caplog):
    """Silent compatibility shims never get removed. This one announces itself."""
    import logging as _logging
    from pullbackup.config import load_settings

    with caplog.at_level(_logging.WARNING):
        load_settings({"PULLBACK_DEST_ROOTS": "/mnt/user/allowlisted"}, env_file=None)

    assert any(
        "PULLBACK_DEST_ROOTS" in r.getMessage() and "PULLBACKUP_" in r.getMessage()
        for r in caplog.records
    ), "adopting a retired name must emit an actionable warning"


def test_ci_base_workflow_environment_is_accepted():
    """Reproduces the exact deadlock: the base branch's tests.yml env block.

    Runs 1853/1855/1857 all died at collection with ConfigurationError under
    precisely these three variables, because pull_request_target executes the
    BASE workflow. This must now load cleanly.
    """
    from pullbackup.config import load_settings

    settings = load_settings({
        "PULLBACK_DATA_DIR": "/tmp/pullback-test-data",
        "PULLBACK_HTTP_BASIC_USERNAME": "ci",
        "PULLBACK_HTTP_BASIC_PASSWORD": "0123456789abcdef0123456789abcdef",
    }, env_file=None)

    assert str(settings.data_dir) == "/tmp/pullback-test-data"
    assert settings.http_basic_username == "ci"


def test_docs_describe_adoption_not_refusal():
    """Review 326 (low/api): the README and .env.example still claimed a
    legacy-only environment refuses to start, after load_settings changed to
    adopt-with-warning. Operators relying on a documented startup failure would
    have seen different behaviour.

    Pinned because this is exactly the drift that produced the finding: the
    behaviour changed and the prose did not.
    """
    readme = (REPOSITORY_ROOT / "README.md").read_text()
    env_example = ENV_EXAMPLE.read_text()

    for name, text in (("README.md", readme), (".env.example", env_example)):
        assert "refuses to start" not in text, (
            f"{name} still documents the superseded refuse-to-start behaviour"
        )
        assert "adopt" in text.lower(), (
            f"{name} must document that orphaned legacy names are adopted"
        )
    assert "warning" in readme.lower()


def test_ssh_key_comment_does_not_leak_a_private_hostname():
    """The key comment is baked into every generated public key, which operators
    then paste into authorized_keys on their own machines. Hardcoding a private
    deployment hostname would publish it to strangers.
    """
    source = (PACKAGE / "services" / "ssh.py").read_text()
    assert "pullback@seal" not in source, "private hostname must not ship in key comments"
    assert "socket.gethostname" in source, "key comment should use the running host"


def test_ssh_key_comment_is_generated_from_the_local_host():
    from pullbackup.services.ssh import key_comment

    comment = key_comment()
    assert comment.startswith("pullbackup@"), comment
    assert "seal" not in comment or comment != "pullbackup@seal"
    # Must never be empty after the @, which ssh-keygen would accept silently.
    assert len(comment.split("@", 1)[1]) > 0


# --------------------------------------------------------------------------
# open-source readiness
# --------------------------------------------------------------------------

PRIVATE_MARKERS = (
    "lagoon.cloud",
    "otoole.io",
    "icloud.com",
    "pez.bz",
    "registry-write",
    "6a1640720ef6",   # Komodo stack id
    "pullback@seal",
)

# Files that legitimately reference private infrastructure: the CI workflows
# target Mike's Gitea, and the naming guards assert on these very strings.
PRIVATE_ALLOWED = {
    ".gitea/workflows/ai-review.yml",
    ".gitea/workflows/tests.yml",
    "tests/test_naming_tranche1.py",
    "tests/test_tranche2_rename.py",
    "docs/research/product-name.md",
}


def _tracked_files():
    import subprocess
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPOSITORY_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.split()
    return [f for f in out if f not in PRIVATE_ALLOWED]


def test_shipped_files_do_not_reference_private_infrastructure():
    """Anything a stranger clones must not carry Mike's hostnames, registry,
    Komodo stack id, or personal email addresses."""
    offenders = []
    for rel in _tracked_files():
        path = REPOSITORY_ROOT / rel
        if not path.is_file() or path.suffix in {".png", ".jpg", ".ico", ".svg"}:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for marker in PRIVATE_MARKERS:
            if marker in text:
                offenders.append(f"{rel}: {marker}")
    assert not offenders, "private references in shipped files: " + ", ".join(offenders)


def test_repository_root_has_no_deployment_compose():
    """Mike's live stack must not ship. A neutral example lives in docker/."""
    assert not (REPOSITORY_ROOT / "compose.yaml").exists()
    assert (REPOSITORY_ROOT / "docker" / "compose.example.yaml").is_file()


def test_open_source_project_files_exist():
    for name in ("CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"):
        assert (REPOSITORY_ROOT / name).is_file(), f"{name} is missing"


def test_repository_is_licensed():
    """Mike chose AGPL-3.0 (2026-08-29).

    Existence alone is too weak a guard: a truncated or wrong-licence file would
    pass. This checks the artifact is really AGPL-3.0 and that every place the
    project declares a licence agrees with it, because a repo whose LICENSE and
    package metadata disagree is worse than one with neither.
    """
    licence = REPOSITORY_ROOT / "LICENSE"
    assert licence.is_file(), "LICENSE is missing"

    text = licence.read_text()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text
    # The clause that distinguishes AGPL from GPL: network use counts.
    assert "Remote Network Interaction" in text, (
        "this does not look like the AGPL: the network-use clause is the whole "
        "reason it was chosen over GPL-3.0 for server software"
    )
    # A truncated copy would still contain the header.
    assert len(text) > 30_000, f"LICENSE looks truncated ({len(text)} chars)"

    pyproject = tomllib.loads(PYPROJECT.read_text())
    declared = pyproject["project"].get("license")
    if isinstance(declared, dict):
        declared = declared.get("text")
    assert declared == "AGPL-3.0-or-later", (
        f"backend/pyproject.toml declares license={declared!r}; it must match the "
        "LICENSE file as an SPDX identifier"
    )


def test_the_readme_states_the_licence():
    """A reader must be able to learn the licence without opening LICENSE."""
    readme = (REPOSITORY_ROOT / "README.md").read_text()
    assert "AGPL" in readme, "README does not mention the licence"


def test_the_frontend_package_declares_the_same_licence():
    """Review finding 2 on PR #17 (low): frontend/package.json had no `license`
    field, so npm tooling reported it as UNLICENSED — a fifth declaration site
    contradicting the other four. It is `private: true` and never published, so
    this was never a legal exposure, but metadata that disagrees with LICENSE is
    the exact confusion this suite exists to prevent.
    """
    package = json.loads((REPOSITORY_ROOT / "frontend" / "package.json").read_text())
    assert package.get("license") == "AGPL-3.0-or-later", (
        f"frontend/package.json declares license={package.get('license')!r}; "
        "it must agree with LICENSE and backend/pyproject.toml"
    )


def test_security_policy_gives_a_private_reporting_route():
    """This is backup software holding SSH keys; vulnerabilities must not be
    reported in a public issue."""
    text = (REPOSITORY_ROOT / "SECURITY.md").read_text().lower()
    assert "do not" in text and "issue" in text
    assert "@" in text or "advisory" in text


def test_dockerfile_installs_every_declared_dependency():
    """Review 344 (high/correctness): config.py imports python-dotenv, but the
    Dockerfile installed a hand-maintained dependency list that omitted it, so
    the image would raise ModuleNotFoundError at import and uvicorn could not
    boot.

    The real defect is the duplication: pyproject.toml and the Dockerfile each
    carried their own copy of the dependency list, so they could drift silently.
    This guard fails on ANY declared dependency the image would not install.
    """
    import re
    import tomllib

    pyproject = tomllib.loads(PYPROJECT.read_text())
    declared = pyproject["project"]["dependencies"]
    dockerfile = DOCKERFILE.read_text()

    # Installing straight from the packaging metadata satisfies this by
    # construction. Match only a real "pip install ." / "pip install -e ." —
    # an earlier version of this check also accepted the mere presence of the
    # word pyproject.toml (it is COPYed in), which made the guard unfailable.
    installs_from_metadata = bool(
        re.search(r"pip install\s+(?:[^\n&|]*\s)?(?:-e\s+)?\.(?:\s|$|\\)", dockerfile)
    )
    if installs_from_metadata:
        return

    missing = []
    for spec in declared:
        name = re.split(r"[><=\[]", spec, maxsplit=1)[0].strip()
        if name not in dockerfile:
            missing.append(name)
    assert not missing, (
        "Dockerfile does not install declared dependencies: "
        + ", ".join(missing)
        + ". The image will fail at import."
    )


def test_the_dockerfile_ships_the_licence_into_the_image():
    """Review finding 1 on PR #17 (medium, blocking).

    The image and wheel declared `License-Expression: AGPL-3.0-or-later` while
    carrying no copy of the licence text. AGPL-3.0 sections 4 and 6 require that
    whoever conveys the work — including object code — give recipients a copy of
    the licence along with the program, and `docker/compose.example.yaml` points
    at a published image, so the container is an intended distribution channel.

    The existing licence guard cannot catch this by construction: it reads the
    repository tree, not the artifact. This one reads the Dockerfile, so the next
    edit that drops the COPY fails here instead of shipping a compliance gap.
    """
    dockerfile = DOCKERFILE.read_text()

    copies_licence = re.search(
        r"^\s*COPY\s+(--from=\S+\s+)?LICENSE\b", dockerfile, re.MULTILINE
    )
    assert copies_licence, (
        "the Dockerfile never copies LICENSE into the image, but the package "
        "metadata declares AGPL-3.0-or-later. Distributing the binary without "
        "the licence text is exactly what AGPL sections 4 and 6 forbid."
    )


def test_the_licence_copy_lands_in_the_application_directory():
    """A COPY that lands somewhere unreachable is not a fix.

    Asserted separately so the previous test cannot be satisfied by a stray
    COPY into an unrelated stage or path.
    """
    dockerfile = DOCKERFILE.read_text()
    runtime = dockerfile.split("AS runtime", 1)[-1]
    assert re.search(r"^\s*COPY\s+LICENSE\b", runtime, re.MULTILINE), (
        "LICENSE must be copied in the runtime stage; a copy confined to the "
        "frontend build stage never reaches the shipped image"
    )
